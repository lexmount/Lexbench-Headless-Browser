#!/usr/bin/env python3
"""Verify the immutable content contract for public Kitesurf fixtures."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import pathlib
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = REPO_ROOT / "config/kitesurf_static_fixture.json"
MANIFEST_SCHEMA = "experimental.kitesurf_static_fixture.v1"
REPORT_SCHEMA = "experimental.kitesurf_static_fixture_verification.v1"
SHA256 = re.compile(r"[0-9a-f]{64}")
GIT_COMMIT = re.compile(r"[0-9a-f]{40}")
MAX_FILE_BYTES = 5 * 1024 * 1024


class FixtureVerificationError(RuntimeError):
    """Raised after a verification report records at least one mismatch."""

    def __init__(self, message: str, report: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.report = report


def deployment_base_url(manifest_path: pathlib.Path | None = None) -> str:
    """Where the pinned fixtures are published, per the manifest.

    The static origin is a run parameter, so callers that need a default read
    it from the manifest instead of carrying a host of their own.
    """
    path = manifest_path or DEFAULT_MANIFEST
    manifest = json.loads(path.read_text(encoding="utf-8"))
    value = manifest.get("deployment_base_url")
    if not isinstance(value, str):
        raise FixtureVerificationError(
            "fixture manifest deployment_base_url must be a string"
        )
    return value


def _validated_base_url(value: str) -> str:
    base_url = value.rstrip("/")
    parsed = urllib.parse.urlparse(base_url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise FixtureVerificationError(
            "fixture base URL must be credential-free HTTP(S) without query or fragment"
        )
    return base_url


def _validated_manifest(path: pathlib.Path) -> tuple[dict[str, Any], str]:
    try:
        raw = path.read_bytes()
        manifest = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise FixtureVerificationError(
            f"fixture manifest is not readable JSON: {path}: {exc}"
        ) from exc
    if not isinstance(manifest, dict) or manifest.get("schema") != MANIFEST_SCHEMA:
        raise FixtureVerificationError(
            f"unsupported fixture manifest schema in {path}"
        )
    source = manifest.get("source")
    repository = (
        str(source.get("repository") or "")
        if isinstance(source, dict)
        else ""
    )
    parsed_repository = urllib.parse.urlparse(repository)
    # Two source forms: an external repository pinned by commit (historical),
    # or a tree versioned inside this repository ("path", e.g. pages/) whose
    # content identity is carried by the per-file sha256 pins below plus the
    # in-repo lockstep test.
    in_repo_path = str(source.get("path") or "") if isinstance(source, dict) else ""
    has_commit_pin = bool(GIT_COMMIT.fullmatch(str(source.get("commit") or ""))) if isinstance(source, dict) else False
    has_path_pin = bool(in_repo_path) and not pathlib.PurePosixPath(in_repo_path).is_absolute() and ".." not in pathlib.PurePosixPath(in_repo_path).parts
    if (
        not isinstance(source, dict)
        or parsed_repository.scheme != "https"
        or not parsed_repository.hostname
        or parsed_repository.username
        or parsed_repository.password
        or parsed_repository.query
        or parsed_repository.fragment
        or not (has_commit_pin or has_path_pin)
    ):
        raise FixtureVerificationError(
            "fixture manifest source must include a credential-free HTTPS "
            "repository and either a 40-hex commit or an in-repo path"
        )
    deployment_base_url = manifest.get("deployment_base_url")
    if not isinstance(deployment_base_url, str):
        raise FixtureVerificationError(
            "fixture manifest deployment_base_url must be a string"
        )
    _validated_base_url(deployment_base_url)
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise FixtureVerificationError("fixture manifest files must be non-empty")
    seen: set[str] = set()
    for index, item in enumerate(files):
        if not isinstance(item, dict):
            raise FixtureVerificationError(
                f"fixture manifest files[{index}] must be an object"
            )
        relative = str(item.get("path") or "")
        pure = pathlib.PurePosixPath(relative)
        if (
            not relative
            or relative.startswith("/")
            or "\\" in relative
            or "?" in relative
            or "#" in relative
            or any(part in {"", ".", ".."} for part in pure.parts)
            or pure.as_posix() != relative
        ):
            raise FixtureVerificationError(
                f"fixture manifest files[{index}] has an unsafe path: {relative!r}"
            )
        if relative in seen:
            raise FixtureVerificationError(
                f"fixture manifest contains duplicate path: {relative}"
            )
        seen.add(relative)
        digest = str(item.get("sha256") or "")
        size = item.get("size")
        if not SHA256.fullmatch(digest):
            raise FixtureVerificationError(
                f"fixture manifest {relative} has an invalid SHA-256"
            )
        if (
            not isinstance(size, int)
            or isinstance(size, bool)
            or not 0 <= size <= MAX_FILE_BYTES
        ):
            raise FixtureVerificationError(
                f"fixture manifest {relative} has an invalid size"
            )
    return manifest, hashlib.sha256(raw).hexdigest()


def _origin(url: str) -> tuple[str, str, int | None]:
    parsed = urllib.parse.urlparse(url)
    port = parsed.port
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    return parsed.scheme, str(parsed.hostname), port


def _fetch(url: str, expected_size: int, timeout_s: float) -> tuple[bytes, str]:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "*/*",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "User-Agent": "Agent-Browser-Bench-fixture-verifier/1",
        },
    )
    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            with urllib.request.urlopen(request, timeout=timeout_s) as response:
                final_url = response.geturl()
                if response.getcode() != 200:
                    raise FixtureVerificationError(
                        f"fixture returned HTTP {response.getcode()}: {final_url}"
                    )
                if _origin(final_url) != _origin(url):
                    raise FixtureVerificationError(
                        f"fixture redirect changed origin: {url} -> {final_url}"
                    )
                body = response.read(expected_size + 1)
            return body, final_url
        except FixtureVerificationError:
            raise
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt < 3:
                time.sleep(0.25 * attempt)
    assert last_error is not None
    raise last_error


def verify_fixture(
    base_url: str,
    manifest_path: pathlib.Path = DEFAULT_MANIFEST,
    *,
    report_path: pathlib.Path | None = None,
    timeout_s: float = 10.0,
    fetch: Callable[[str, int, float], tuple[bytes, str]] = _fetch,
) -> dict[str, Any]:
    """Fetch every pinned file and fail unless size and SHA-256 both match."""

    if timeout_s <= 0 or timeout_s > 30:
        raise FixtureVerificationError("fixture verification timeout must be in (0, 30]")
    resolved_manifest = manifest_path.resolve()
    manifest, manifest_sha256 = _validated_manifest(resolved_manifest)
    base_url = _validated_base_url(base_url)
    rows: list[dict[str, Any]] = []
    for expected in manifest["files"]:
        relative = str(expected["path"])
        url = base_url + "/" + urllib.parse.quote(relative, safe="/")
        row: dict[str, Any] = {
            "path": relative,
            "url": url,
            "expected_size": expected["size"],
            "expected_sha256": expected["sha256"],
            "verified": False,
        }
        try:
            body, final_url = fetch(url, int(expected["size"]), timeout_s)
            row.update(
                {
                    "final_url": final_url,
                    "actual_size": len(body),
                    "actual_sha256": hashlib.sha256(body).hexdigest(),
                }
            )
            row["verified"] = bool(
                row["actual_size"] == row["expected_size"]
                and row["actual_sha256"] == row["expected_sha256"]
            )
            if not row["verified"]:
                row["error"] = "fixture content does not match the pinned manifest"
        except Exception as exc:
            row["error"] = f"{type(exc).__name__}: {exc}"
        rows.append(row)
    verified = all(row["verified"] for row in rows)
    report = {
        "schema": REPORT_SCHEMA,
        "created_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "verified": verified,
        "base_url": base_url,
        "manifest_path": str(resolved_manifest),
        "manifest_sha256": manifest_sha256,
        "source": manifest["source"],
        "expected_deployment_base_url": manifest.get("deployment_base_url"),
        "file_count": len(rows),
        "verified_file_count": sum(row["verified"] for row in rows),
        "files": rows,
    }
    if report_path is not None:
        report_path.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    if not verified:
        failures = [row["path"] for row in rows if not row["verified"]]
        raise FixtureVerificationError(
            "fixture verification failed for: " + ", ".join(failures),
            report,
        )
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--manifest", type=pathlib.Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    parser.add_argument("--timeout-s", type=float, default=10.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = verify_fixture(
        args.base_url,
        args.manifest,
        report_path=args.output,
        timeout_s=args.timeout_s,
    )
    print(
        f"verified {report['verified_file_count']}/{report['file_count']} "
        f"fixture files at {report['base_url']}"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except FixtureVerificationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2)
