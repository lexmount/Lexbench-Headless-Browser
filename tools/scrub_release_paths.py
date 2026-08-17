#!/usr/bin/env python3
"""Copy a run tree into a release-ready tree, dropping machine-local details.

Run products record the paths and origins they actually used, which is right
for a local audit trail and wrong for a published artifact. Three kinds of
detail are machine-local rather than evidence:

  repo-root prefix   the absolute path of this checkout    -> <repo>
  fixture origins    the host a fixture round was served
                     from, whether a self-deployed tunnel
                     or a personal static host             -> <dynamic-fixture-origin>
                                                              <static-fixture-origin>
  local timestamps   ISO-8601 with a non-UTC offset        -> the same instant at +00:00

Fixture origins are discovered from the products themselves rather than
listed here: every round records the origin it used in `base_url` (fixture
verification reports) and `scope.fixture_base_url` (run summaries), and the
reports describe that origin as a run parameter. Discovery keeps this script
publishable, since it names no host, and it keeps the rule honest for whatever
origin the next operator deploys to. The static and dynamic roles are told
apart by their contract file: `ks_static_verification.json` names the static
origin, dynamic verification and per-run reports name the dynamic one.

The endpoint under test is never redacted, because which endpoint answered is
the whole claim. Neither are digests: engine and adapter sha256 values are the
fingerprint chain, and only the path that led to a binary is a local detail.

A file whose bytes are not valid UTF-8 is copied verbatim and reported, since
a length-changing edit can corrupt a binary product; the operator decides what
to do with it rather than this script guessing.

Usage:
    python3 tools/scrub_release_paths.py runs/ --dest build/release-runs/
    python3 tools/scrub_release_paths.py runs/ --check
    python3 tools/scrub_release_paths.py build/release-runs/ --check --origins-from runs/

Checking a tree that has already been scrubbed needs `--origins-from` pointing
at the original, since the origins are discovered from the products and a
scrubbed product no longer names one. Without it the check refuses to run
rather than reporting a clean tree it never tested.
"""

from __future__ import annotations

import argparse
import datetime
import json
import pathlib
import re
import shutil
import sys
import urllib.parse

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

STATIC_PLACEHOLDER = "<static-fixture-origin>"
DYNAMIC_PLACEHOLDER = "<dynamic-fixture-origin>"

# Quick-tunnel hostnames are ephemeral by construction, so they are treated as
# a dynamic origin even in a tree where no verification report survives.
TUNNEL_RE = re.compile(r"\b[a-z0-9][a-z0-9-]*\.trycloudflare\.com\b")

LOCAL_TS_RE = re.compile(
    r"\b(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?)([+-]\d{2}:?\d{2})\b"
)

RULES = ("repo_root_prefix", "fixture_origin", "local_timestamp")


def _to_utc(match: re.Match[str]) -> str:
    stamp, offset = match.group(1), match.group(2)
    if offset in ("+00:00", "+0000", "-00:00", "-0000"):
        return match.group(0)
    if ":" not in offset:
        offset = f"{offset[:3]}:{offset[3:]}"
    try:
        moment = datetime.datetime.fromisoformat(f"{stamp}{offset}")
    except ValueError:
        return match.group(0)
    return moment.astimezone(datetime.timezone.utc).isoformat()


def _netloc(url: str) -> str | None:
    if not isinstance(url, str) or "//" not in url:
        return None
    parsed = urllib.parse.urlsplit(url)
    return parsed.netloc or None


def discover_origins(root: pathlib.Path) -> dict[str, str]:
    """Map fixture-origin host -> placeholder, read from the products."""
    origins: dict[str, str] = {}

    def note(url: str | None, placeholder: str) -> None:
        host = _netloc(url or "")
        if not host:
            return
        # An already-scrubbed product names a placeholder where the host used
        # to be, and a placeholder is not an origin to go looking for.
        if STATIC_PLACEHOLDER in host or DYNAMIC_PLACEHOLDER in host:
            return
        # A host already claimed as static stays static.
        origins.setdefault(host, placeholder)

    for path in sorted(root.rglob("*.json")):
        if path.name not in (
            "ks_static_verification.json",
            "ks_dynamic_verification.json",
            "fixture_verification.json",
            "summary.json",
        ):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        role = STATIC_PLACEHOLDER if path.name == "ks_static_verification.json" else DYNAMIC_PLACEHOLDER
        note(data.get("base_url"), role)
        note(data.get("expected_deployment_base_url"), STATIC_PLACEHOLDER)
        scope = data.get("scope")
        if isinstance(scope, dict):
            note(scope.get("fixture_base_url"), DYNAMIC_PLACEHOLDER)
            note(scope.get("static_fixture_base_url"), STATIC_PLACEHOLDER)

    # The public repository and its Pages site are the intended published
    # locations, so they are evidence rather than a local detail.
    for public in ("github.com", "lexmount.github.io", "raw.githubusercontent.com"):
        origins.pop(public, None)
    return origins


def scrub_text(text: str, prefix: str, origins: dict[str, str]) -> tuple[str, dict[str, int]]:
    counts = dict.fromkeys(RULES, 0)

    counts["repo_root_prefix"] = text.count(prefix)
    if counts["repo_root_prefix"]:
        text = text.replace(prefix, "<repo>")

    for host, placeholder in sorted(origins.items(), key=lambda kv: -len(kv[0])):
        hits = text.count(host)
        if hits:
            counts["fixture_origin"] += hits
            text = text.replace(host, placeholder)

    def tunnel_sub(match: re.Match[str]) -> str:
        counts["fixture_origin"] += 1
        return DYNAMIC_PLACEHOLDER

    text = TUNNEL_RE.sub(tunnel_sub, text)

    def ts_sub(match: re.Match[str]) -> str:
        replaced = _to_utc(match)
        if replaced != match.group(0):
            counts["local_timestamp"] += 1
        return replaced

    text = LOCAL_TS_RE.sub(ts_sub, text)
    return text, counts


def iter_files(root: pathlib.Path):
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            yield path


def read_text(path: pathlib.Path) -> str | None:
    try:
        return path.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        return None


def binary_carries(raw: bytes, prefix: str, origins: dict[str, str]) -> bool:
    if prefix.encode() in raw or b"trycloudflare.com" in raw:
        return True
    return any(host.encode() in raw for host in origins)


def do_scrub(source: pathlib.Path, dest: pathlib.Path, prefix: str) -> dict:
    if dest.exists():
        raise FileExistsError(f"destination already exists: {dest}")
    origins = discover_origins(source)
    totals = dict.fromkeys(RULES, 0)
    rewritten: list[str] = []
    binary_with_match: list[str] = []
    scanned = 0

    for path in iter_files(source):
        scanned += 1
        target = dest / path.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        text = read_text(path)
        if text is None:
            shutil.copy2(path, target)
            if binary_carries(path.read_bytes(), prefix, origins):
                binary_with_match.append(str(path.relative_to(source)))
            continue
        scrubbed, counts = scrub_text(text, prefix, origins)
        if scrubbed != text:
            rewritten.append(str(path.relative_to(source)))
            for rule, n in counts.items():
                totals[rule] += n
        target.write_text(scrubbed, encoding="utf-8")
        shutil.copystat(path, target)

    return {
        "source": str(source),
        "dest": str(dest),
        "stripped_prefix": prefix,
        "redacted_origins": {host: role for host, role in sorted(origins.items())},
        "files_scanned": scanned,
        "files_rewritten": len(rewritten),
        "replacements": totals,
        "binary_files_with_match": binary_with_match,
    }


def tree_is_scrubbed(root: pathlib.Path) -> bool:
    """Whether the tree already carries origin placeholders."""
    for path in iter_files(root):
        text = read_text(path)
        if text and (STATIC_PLACEHOLDER in text or DYNAMIC_PLACEHOLDER in text):
            return True
    return False


def do_check(root: pathlib.Path, prefix: str, origins: dict[str, str] | None = None) -> int:
    # Discovery reads the origin out of the products, so a tree that has
    # already been scrubbed yields nothing to look for and the origin half of
    # this check would pass without testing anything. That reads as a clean
    # bill of health, which is worse than no check at all, so it is refused
    # rather than reported. The refusal does not care where the empty result
    # came from: a missing --origins-from and one aimed at the wrong tree
    # leave this check equally blind.
    if origins is None:
        origins = discover_origins(root)
    if not origins and tree_is_scrubbed(root):
        print(
            json.dumps(
                {
                    "root": str(root),
                    "verified": False,
                    "reason": (
                        "tree already carries origin placeholders, so no origin "
                        "could be discovered from it; rerun with --origins-from "
                        "pointing at the unscrubbed tree"
                    ),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 2
    offenders: dict[str, dict[str, int]] = {}
    for path in iter_files(root):
        text = read_text(path)
        if text is None:
            if binary_carries(path.read_bytes(), prefix, origins):
                offenders[str(path.relative_to(root))] = {"binary_match": 1}
            continue
        _, counts = scrub_text(text, prefix, origins)
        hits = {rule: n for rule, n in counts.items() if n}
        if hits:
            offenders[str(path.relative_to(root))] = hits
    report = {
        "root": str(root),
        "checked_origins": {host: role for host, role in sorted(origins.items())},
        "verified": True,
        "clean": not offenders,
        "offenders": offenders,
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if not offenders else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("tree", type=pathlib.Path, help="run tree to scrub or check")
    parser.add_argument("--dest", type=pathlib.Path, help="write the scrubbed copy here")
    parser.add_argument(
        "--check",
        action="store_true",
        help="report any remaining machine-local detail and exit non-zero if found",
    )
    parser.add_argument(
        "--strip-prefix",
        default=str(REPO_ROOT),
        help="absolute checkout path to replace with <repo> (default: this checkout)",
    )
    parser.add_argument(
        "--origins-from",
        type=pathlib.Path,
        help="discover fixture origins from this tree instead of the one being checked",
    )
    args = parser.parse_args(argv)

    tree = args.tree.resolve()
    if not tree.is_dir():
        sys.exit(f"not a directory: {tree}")
    prefix = args.strip_prefix.rstrip("/")

    if args.check:
        origins = None
        if args.origins_from:
            source = args.origins_from.resolve()
            if not source.is_dir():
                sys.exit(f"--origins-from is not a directory: {source}")
            origins = discover_origins(source)
            # Naming a source tree is a claim that the origins are in it.
            # Coming back empty means the claim is wrong, and continuing
            # would check the origin rule against nothing at all.
            if not origins:
                sys.exit(
                    f"no fixture origin found in {source}; --origins-from "
                    "must point at the unscrubbed tree that produced the run"
                )
        return do_check(tree, prefix, origins)
    if not args.dest:
        sys.exit("either --dest or --check is required")
    try:
        report = do_scrub(tree, args.dest.resolve(), prefix)
    except FileExistsError as exc:
        sys.exit(str(exc))
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
