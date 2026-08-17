"""Shared, deliberately small helpers for the experimental Kitesurf probes.

The Kitesurf lane is not part of the formal engine roster.  These helpers keep
the experimental entrypoints consistent without coupling them to the formal
benchmark configuration.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import subprocess
from typing import Any, Iterable


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def add_expected_identity_arguments(parser: Any) -> None:
    """Add the three required Browser.getVersion attribution inputs."""

    parser.add_argument("--expect-product", required=True)
    parser.add_argument("--expect-protocol-version", required=True)
    parser.add_argument("--expect-revision", required=True)


def expected_identity_from_args(args: Any) -> dict[str, str]:
    return {
        "product": str(args.expect_product),
        "protocolVersion": str(args.expect_protocol_version),
        "revision": str(args.expect_revision),
    }


def _git_text(*args: str) -> str | None:
    completed = subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    value = completed.stdout.strip()
    return value if completed.returncode == 0 and value else None


def _git_bytes(*args: str) -> bytes:
    completed = subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
    )
    return completed.stdout if completed.returncode == 0 else b""


def source_commit() -> str | None:
    """Return the checked-out commit for compatibility with older summaries."""

    return _git_text("rev-parse", "HEAD")


def _relative_source_path(path: pathlib.Path) -> tuple[str, pathlib.Path]:
    resolved = path.resolve()
    try:
        relative = resolved.relative_to(REPO_ROOT.resolve())
    except ValueError as exc:
        raise ValueError(f"source path is outside the repository: {path}") from exc
    return relative.as_posix(), resolved


def _untracked_state() -> tuple[list[str], bytes]:
    raw_names = _git_bytes("ls-files", "--others", "--exclude-standard", "-z")
    names = sorted(
        value.decode("utf-8", errors="surrogateescape")
        for value in raw_names.split(b"\0")
        if value
    )
    digest_input = bytearray()
    for name in names:
        path = REPO_ROOT / name
        digest_input.extend(name.encode("utf-8", errors="surrogateescape"))
        digest_input.extend(b"\0")
        if path.is_symlink():
            digest_input.extend(b"symlink\0")
            digest_input.extend(path.readlink().as_posix().encode("utf-8"))
        elif path.is_file():
            digest_input.extend(b"file\0")
            digest_input.extend(path.read_bytes())
        else:
            digest_input.extend(b"other\0")
        digest_input.extend(b"\0")
    return names, bytes(digest_input)


def capture_source_provenance(
    script_path: pathlib.Path,
    *,
    extra_paths: Iterable[pathlib.Path] = (),
    runtime_executables: Iterable[pathlib.Path] = (),
) -> dict[str, Any]:
    """Capture enough source state to distinguish clean and modified reruns.

    A commit SHA alone is ambiguous when a probe is run with local changes.
    The returned record includes the tracked diff, untracked content, hashes of
    the entrypoint and selected dependencies, the exact runtime executables
    requested by the caller, and the relationship to the fetched
    ``origin/main`` tip.
    """

    head = source_commit()
    status = _git_bytes(
        "status", "--porcelain=v1", "--untracked-files=all"
    ).decode("utf-8", errors="surrogateescape").rstrip("\n")
    tracked_diff = _git_bytes("diff", "--binary", "HEAD")
    untracked_paths, untracked_state = _untracked_state()
    source_files: dict[str, str] = {}
    for path in (script_path, *extra_paths):
        relative, resolved = _relative_source_path(path)
        source_files[relative] = hashlib.sha256(resolved.read_bytes()).hexdigest()
    executable_rows: list[dict[str, Any]] = []
    for invoked in runtime_executables:
        invoked_path = invoked.absolute()
        resolved = invoked_path.resolve(strict=True)
        if not resolved.is_file():
            raise ValueError(f"runtime executable is not a file: {invoked}")
        stat = resolved.stat()
        executable_rows.append(
            {
                "invoked_path": str(invoked_path),
                "resolved_path": str(resolved),
                "size": stat.st_size,
                "sha256": hashlib.sha256(resolved.read_bytes()).hexdigest(),
            }
        )

    origin_main = _git_text("rev-parse", "--verify", "origin/main")
    merge_base = (
        _git_text("merge-base", "HEAD", "origin/main") if origin_main else None
    )
    combined = hashlib.sha256()
    combined.update(tracked_diff)
    combined.update(b"\0untracked\0")
    combined.update(untracked_state)
    return {
        "schema": "experimental.kitesurf_source.v2",
        "head": head,
        "head_tree": _git_text("rev-parse", "HEAD^{tree}"),
        "branch": _git_text("branch", "--show-current"),
        "origin_main": origin_main,
        "merge_base_origin_main": merge_base,
        "contains_origin_main": bool(origin_main and merge_base == origin_main),
        "dirty": bool(status),
        "status_porcelain": status.splitlines(),
        "tracked_diff_sha256": hashlib.sha256(tracked_diff).hexdigest(),
        "untracked_paths": untracked_paths,
        "untracked_content_sha256": hashlib.sha256(untracked_state).hexdigest(),
        "worktree_state_sha256": combined.hexdigest(),
        "source_files_sha256": dict(sorted(source_files.items())),
        "runtime_executables": sorted(
            executable_rows,
            key=lambda row: (row["invoked_path"], row["resolved_path"]),
        ),
    }


def write_json(path: pathlib.Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
