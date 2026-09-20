#!/usr/bin/env python3
"""Validate and summarize one complete Moli ``--try-layout`` cohort run."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import sys
from typing import Any

from run_moli_cohort import PROFILE, file_sha256, frozen_tasks

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from runner import layout_retry  # noqa: E402


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain an object")
    return value


def _read_rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _matrix(rows: list[dict[str, Any]], task_ids: list[str], attempts: int, label: str) -> None:
    keys = [(row.get("task_id"), row.get("attempt")) for row in rows if row.get("engine") == "moli"]
    expected = {(task_id, attempt) for task_id in task_ids for attempt in range(1, attempts + 1)}
    if len(keys) != len(expected) or set(keys) != expected:
        raise ValueError(f"{label} has missing, duplicate or unexpected task attempts")


def _passed(rows: list[dict[str, Any]]) -> set[str]:
    grouped: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        if row.get("engine") == "moli":
            grouped[row["task_id"]].append(row["status"])
    return {task_id for task_id, statuses in grouped.items() if statuses and all(status == "pass" for status in statuses)}


def _status_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    return dict(sorted(Counter(row["status"] for row in rows if row.get("engine") == "moli").items()))


def summarize_run(run_dir: Path, contract: dict[str, Any]) -> dict[str, Any]:
    manifest_path = run_dir / "run_manifest.json"
    results_path = run_dir / "results.jsonl"
    manifest = _read_json(manifest_path)
    final_rows = _read_rows(results_path)
    task_ids = list(contract["task_ids"])
    attempts = int(contract["attempts_per_task"])
    expected_rows = len(task_ids) * attempts

    if manifest.get("completion_status") != "completed":
        raise ValueError("run is not complete")
    if manifest.get("selected_engines") != ["moli"] or int(manifest.get("k_runs") or 0) != attempts:
        raise ValueError("run engine set or repetition count differs from the cohort contract")
    if manifest.get("completed_result_rows") != expected_rows or len(final_rows) != expected_rows:
        raise ValueError(f"final matrix must contain {expected_rows} rows")
    if (manifest.get("bench_manifest") or {}).get("sha256") != contract["bench_manifest_sha256"]:
        raise ValueError("benchmark manifest differs from the cohort contract")
    policy = manifest.get("moli_layout_policy") or {}
    expected_policy = layout_retry.policy("off", True)
    if policy != expected_policy:
        raise ValueError("run must use layout off followed by opt-in failed-case layout retries")
    _matrix(final_rows, task_ids, attempts, "final matrix")
    layout_retry.verify(run_dir, manifest, final_rows)
    receipt = manifest.get("layout_retry")
    if receipt is None:
        initial_rows = final_rows
        retry_rows: list[dict[str, Any]] = []
        if layout_retry.failed_cases(initial_rows, attempts):
            raise ValueError("layout retry receipt is missing for failed initial cases")
    elif isinstance(receipt, dict):
        initial_rows = _read_rows(run_dir / receipt["initial_results"])
        retry_rows = _read_rows(run_dir / receipt["retry_results"])
    else:
        raise ValueError("layout retry receipt must be an object")
    _matrix(initial_rows, task_ids, attempts, "initial matrix")

    retried = layout_retry.failed_cases(initial_rows, attempts)
    recorded_retried = receipt.get("retried_cases") if receipt else []
    if sorted(retried) != recorded_retried:
        raise ValueError("retry receipt does not match failed cases in the initial matrix")
    retry_task_ids = sorted(retried)
    _matrix(retry_rows, retry_task_ids, attempts, "retry matrix")
    if any((row.get("engine_provenance") or {}).get("layout_enabled") is not False for row in initial_rows):
        raise ValueError("initial matrix contains a layout-enabled execution")
    if any((row.get("engine_provenance") or {}).get("layout_enabled") is not True for row in retry_rows):
        raise ValueError("retry matrix contains a layout-disabled execution")

    recovered = _passed(retry_rows)
    recorded_recovered = receipt.get("recovered_cases") if receipt else []
    if sorted(recovered) != recorded_recovered:
        raise ValueError("retry receipt recovered cases do not match the retry matrix")
    initial_passed = _passed(initial_rows)
    final_passed = _passed(final_rows)
    if final_passed != initial_passed | recovered:
        raise ValueError("final passed cases are not the union of initial and recovered passes")

    conditions_path = run_dir.with_suffix(".conditions.json")
    conditions = _read_json(conditions_path)
    moli = (manifest.get("engines") or {}).get("moli") or {}
    expected_conditions = {
        "schema": "lexbench_moli_cohort_conditions/1",
        "run_id": manifest.get("run_id"),
        "profile_sha256": contract["profile_sha256"],
        "task_ids_sha256": contract["task_ids_sha256"],
        "bench_manifest_sha256": contract["bench_manifest_sha256"],
        "moli_sha256": moli.get("sha256"),
        "moli_version": moli.get("version"),
        "moli_layout": "off",
        "try_layout": True,
    }
    for key, expected in expected_conditions.items():
        if conditions.get(key) != expected:
            raise ValueError(f"conditions receipt mismatch: {key}")
    for key in ("chromedriver_sha256", "chromedriver_version"):
        if not conditions.get(key):
            raise ValueError(f"conditions receipt is missing: {key}")
    moli_commit = conditions.get("moli_commit")
    if not isinstance(moli_commit, str) or len(moli_commit) != 40 or any(c not in "0123456789abcdef" for c in moli_commit):
        raise ValueError("conditions receipt is missing a valid Moli source commit")

    unrecovered = retried - recovered
    return {
        "schema": "lexbench_moli_try_layout_summary/1",
        "run_id": manifest["run_id"],
        "method": {
            "policy_id": layout_retry.POLICY_ID,
            "initial_layout": "off",
            "retry_layout": "on",
            "attempts_per_case": attempts,
            "replacement_rule": "replace_only_when_all_retry_attempts_pass",
        },
        "population": {"cases": len(task_ids), "task_ids_sha256": contract["task_ids_sha256"]},
        "outcomes": {
            "initial_passed_cases": len(initial_passed),
            "final_passed_cases": len(final_passed),
            "final_success_rate_pct": 100.0 * len(final_passed) / len(task_ids),
            "retried_cases": len(retried),
            "recovered_cases": len(recovered),
            "unrecovered_cases": len(unrecovered),
            "extra_physical_calls": len(retry_rows),
            "initial_status_counts": _status_counts(initial_rows),
            "retry_status_counts": _status_counts(retry_rows),
            "final_status_counts": _status_counts(final_rows),
        },
        "case_ids": {
            "initial_passed": sorted(initial_passed),
            "recovered": sorted(recovered),
            "unrecovered": sorted(unrecovered),
        },
        "provenance": {
            "moli_version": moli.get("version"),
            "moli_sha256": moli.get("sha256"),
            "moli_commit": moli_commit,
            "bench_manifest_sha256": contract["bench_manifest_sha256"],
            "profile_sha256": contract["profile_sha256"],
            "run_manifest_sha256": _sha256(manifest_path),
            "conditions_sha256": _sha256(conditions_path),
            "chromedriver_version": conditions["chromedriver_version"],
            "chromedriver_sha256": conditions["chromedriver_sha256"],
            "initial_results_sha256": receipt["initial_results_sha256"] if receipt else _sha256(results_path),
            "retry_results_sha256": receipt["retry_results_sha256"] if receipt else None,
            "final_results_sha256": receipt["final_results_sha256"] if receipt else _sha256(results_path),
            "runner_source": (manifest.get("runner") or {}).get("source"),
            "fixtures": (manifest.get("runner") or {}).get("fixtures"),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_id")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        profile, task_ids = frozen_tasks()
        contract = {
            **profile,
            "task_ids": task_ids,
            "profile_sha256": file_sha256(PROFILE),
        }
        summary = summarize_run(ROOT / "runs" / args.run_id, contract)
    except (OSError, KeyError, TypeError, ValueError) as exc:
        parser.exit(1, f"Summary rejected: {exc}\n")
    text = json.dumps(summary, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(text, encoding="utf-8")
    else:
        print(text, end="")


if __name__ == "__main__":
    main()
