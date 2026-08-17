#!/usr/bin/env python3
"""Build a sanitized, hash-linked audit of Kitesurf CDP trace artifacts."""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import pathlib
import sys
from typing import Any


class AuditError(ValueError):
    pass


def parse_run(value: str) -> tuple[str, pathlib.Path]:
    label, separator, raw_path = value.partition("=")
    if not separator or not label or not raw_path:
        raise argparse.ArgumentTypeError("--run must use LABEL=RUN_DIRECTORY")
    return label, pathlib.Path(raw_path).expanduser().resolve()


def sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_trace_path(run_dir: pathlib.Path, relative: str) -> pathlib.Path:
    path = (run_dir / relative).resolve()
    try:
        path.relative_to(run_dir)
    except ValueError as exc:
        raise AuditError(f"trace escapes run directory: {relative}") from exc
    if not path.is_file():
        raise AuditError(f"trace is missing: {path}")
    return path


def audit_trace(path: pathlib.Path) -> dict[str, Any]:
    directions: collections.Counter[str] = collections.Counter()
    commands: collections.Counter[str] = collections.Counter()
    events: collections.Counter[str] = collections.Counter()
    errors: list[dict[str, Any]] = []
    identity: dict[str, Any] | None = None
    line_count = 0
    with path.open(encoding="utf-8") as handle:
        for line_count, line in enumerate(handle, 1):
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise AuditError(f"invalid JSONL at {path}:{line_count}: {exc}") from exc
            direction = str(row.get("direction") or "unknown")
            method = str(row.get("method") or "")
            directions[direction] += 1
            if direction == "send":
                commands[method] += 1
            elif direction == "event":
                events[method] += 1
            elif direction == "recv":
                if method == "Browser.getVersion" and isinstance(row.get("result"), dict):
                    result = row["result"]
                    identity = {
                        key: result.get(key)
                        for key in (
                            "product",
                            "protocolVersion",
                            "revision",
                            "jsVersion",
                            "userAgent",
                        )
                    }
                if isinstance(row.get("error"), dict):
                    error = row["error"]
                    errors.append(
                        {
                            "method": method,
                            "code": error.get("code"),
                            "message": error.get("message"),
                        }
                    )
    return {
        "bytes": path.stat().st_size,
        "sha256": sha256(path),
        "line_count": line_count,
        "directions": dict(sorted(directions.items())),
        "command_methods": dict(sorted(commands.items())),
        "event_methods": dict(sorted(events.items())),
        "cdp_errors": errors,
        "identity": identity,
    }


def load_results(path: pathlib.Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise AuditError(f"invalid JSONL at {path}:{line_number}: {exc}") from exc
            if not isinstance(row, dict) or not isinstance(row.get("trace"), str):
                raise AuditError(f"result has no trace at {path}:{line_number}")
            rows.append(row)
    return rows


def audit_failure(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    detail = value.get("detail")
    return {
        "layer": value.get("layer"),
        "code": value.get("code"),
        "detail_sha256": (
            hashlib.sha256(str(detail).encode("utf-8")).hexdigest()
            if detail is not None
            else None
        ),
    }


def build_audit(runs: list[tuple[str, pathlib.Path]]) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    run_sources: list[dict[str, Any]] = []
    seen_labels: set[str] = set()
    for label, run_dir in runs:
        if label in seen_labels:
            raise AuditError(f"duplicate run label: {label}")
        seen_labels.add(label)
        results_path = run_dir / "results.jsonl"
        summary_path = run_dir / "summary.json"
        provenance_path = run_dir / "provenance.json"
        for required in (results_path, summary_path, provenance_path):
            if not required.is_file():
                raise AuditError(f"required run artifact is missing: {required}")
        run_sources.append(
            {
                "label": label,
                "results_sha256": sha256(results_path),
                "summary_sha256": sha256(summary_path),
                "provenance_sha256": sha256(provenance_path),
            }
        )
        for row in load_results(results_path):
            trace_relative = str(row["trace"])
            trace = audit_trace(safe_trace_path(run_dir, trace_relative))
            metrics = row.get("metrics") or {}
            if trace["directions"].get("send", 0) != metrics.get("cdp_call_count"):
                raise AuditError(
                    f"command-count mismatch for {label}/{trace_relative}"
                )
            if len(trace["cdp_errors"]) != metrics.get("cdp_error_count"):
                raise AuditError(f"error-count mismatch for {label}/{trace_relative}")
            entries.append(
                {
                    "run": label,
                    "sequence": row.get("sequence"),
                    "engine": row.get("engine"),
                    "case_id": row.get("case_id"),
                    "attempt": row.get("attempt"),
                    "status": row.get("status"),
                    "failure": audit_failure(row.get("failure")),
                    "result_metrics": metrics,
                    "trace": trace_relative,
                    **trace,
                }
            )

    command_totals: collections.Counter[str] = collections.Counter()
    event_totals: collections.Counter[str] = collections.Counter()
    for entry in entries:
        command_totals.update(entry["command_methods"])
        event_totals.update(entry["event_methods"])
    return {
        "schema": "experimental.issue136.trace_audit.v1",
        "sanitization": {
            "included": [
                "trace hashes and sizes",
                "direction/method counts",
                "Browser.getVersion identity",
                "CDP error method/code/message",
                "linked result status, failure, and metrics",
            ],
            "excluded": [
                "command parameters",
                "event parameters and network headers",
                "target/session/object identifiers",
            ],
        },
        "source_runs": run_sources,
        "aggregate": {
            "trace_count": len(entries),
            "bytes": sum(int(entry["bytes"]) for entry in entries),
            "lines": sum(int(entry["line_count"]) for entry in entries),
            "engines": sorted({str(entry["engine"]) for entry in entries}),
            "cases": sorted({str(entry["case_id"]) for entry in entries}),
            "command_methods": dict(sorted(command_totals.items())),
            "event_methods": dict(sorted(event_totals.items())),
        },
        "traces": entries,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="append", required=True, type=parse_run)
    return parser.parse_args()


def main() -> int:
    audit = build_audit(parse_args().run)
    json.dump(audit, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AuditError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2)
