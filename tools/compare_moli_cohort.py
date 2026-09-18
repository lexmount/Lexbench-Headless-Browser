#!/usr/bin/env python3
"""Check that two complete cohort runs differ only in the Moli build and outcomes."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import sys

from run_moli_cohort import PROFILE, ROOT, file_sha256, frozen_tasks


def normalized_manifest(manifest: dict) -> dict:
    """Remove run-local fields and the sole intended treatment variable."""
    result = json.loads(json.dumps(manifest))
    for key in ("argv", "run_id", "started_at", "completed_at", "site", "layout_retry"):
        result.pop(key, None)
    result.get("engine_set", {}).pop("name", None)
    moli = result["engines"]["moli"]
    for key in ("version", "sha256", "sha256_12", "expected_sha256", "expected_sha256_12"):
        moli.pop(key, None)
    result.get("host_telemetry", {}).pop("summary", None)
    return result


def load_run(path: Path) -> tuple[dict, list[dict]]:
    manifest = json.loads((path / "run_manifest.json").read_text(encoding="utf-8"))
    rows = [json.loads(line) for line in (path / "results.jsonl").read_text(encoding="utf-8").splitlines()]
    profile, task_ids = frozen_tasks()
    count = len(task_ids) * profile["attempts_per_task"]
    if manifest.get("completion_status") != "completed" or manifest.get("completed_result_rows") != count or len(rows) != count:
        raise ValueError(f"{path}: incomplete run; expected {count} rows")
    if manifest.get("bench_manifest", {}).get("sha256") != profile["bench_manifest_sha256"]:
        raise ValueError(f"{path}: benchmark manifest changed")
    if manifest.get("selected_engines") != ["moli"] or manifest.get("k_runs") != profile["attempts_per_task"]:
        raise ValueError(f"{path}: wrong engine set or repetition count")
    return manifest, rows


def row_keys(rows: list[dict], task_ids: list[str], attempts: int) -> set[tuple[str, int]]:
    keys = [(r["task_id"], r["attempt"]) for r in rows]
    expected = {(task, attempt) for task in task_ids for attempt in range(1, attempts + 1)}
    if len(keys) != len(expected) or set(keys) != expected:
        raise ValueError("missing, duplicate or unexpected task attempts")
    return set(keys)


def first_difference(left: object, right: object, path: str = "manifest") -> str | None:
    if isinstance(left, dict) and isinstance(right, dict):
        for key in sorted(left.keys() | right.keys()):
            difference = first_difference(left.get(key), right.get(key), f"{path}.{key}")
            if difference:
                return difference
        return None
    if isinstance(left, list) and isinstance(right, list):
        if len(left) != len(right):
            return f"{path}.length"
        for index, (a, b) in enumerate(zip(left, right)):
            difference = first_difference(a, b, f"{path}[{index}]")
            if difference:
                return difference
        return None
    return path if left != right else None


def compare(reference: Path, candidate: Path) -> tuple[dict, dict]:
    base, base_rows = load_run(reference)
    other, other_rows = load_run(candidate)
    profile, tasks = frozen_tasks()
    row_keys(base_rows, tasks, profile["attempts_per_task"])
    row_keys(other_rows, tasks, profile["attempts_per_task"])
    stable_fields = ("task_id", "attempt", "seed", "task_version", "subset_id", "layer", "driver", "evaluation_axis", "launch_profile")
    def stable_rows(rows: list[dict]) -> dict[tuple[str, int], dict]:
        return {(row["task_id"], row["attempt"]): {field: row.get(field) for field in stable_fields} for row in rows}
    base_inputs, other_inputs = stable_rows(base_rows), stable_rows(other_rows)
    for key in sorted(base_inputs):
        difference = first_difference(base_inputs[key], other_inputs[key], f"result[{key[0]}:{key[1]}]")
        if difference:
            raise ValueError(f"non-Moli task input differs: {difference}")
    difference = first_difference(normalized_manifest(base), normalized_manifest(other))
    if difference:
        raise ValueError(f"non-Moli condition differs: {difference}")
    def conditions(path: Path, manifest: dict) -> dict:
        receipt = json.loads(path.with_suffix(".conditions.json").read_text(encoding="utf-8"))
        if receipt.get("run_id") != manifest["run_id"] or receipt.get("moli_sha256") != manifest["engines"]["moli"]["sha256"]:
            raise ValueError(f"{path}: conditions receipt does not match run")
        if receipt.get("profile_sha256") != file_sha256(PROFILE):
            raise ValueError(f"{path}: frozen run profile differs from receipt")
        return {key: value for key, value in receipt.items() if key not in ("run_id", "moli_sha256", "moli_version")}
    difference = first_difference(conditions(reference, base), conditions(candidate, other), "conditions")
    if difference:
        raise ValueError(f"non-Moli binary or profile differs: {difference}")
    for label, manifest in (("reference", base), ("candidate", other)):
        if not manifest["engines"]["moli"].get("sha256"):
            raise ValueError(f"{label}: missing measured Moli binary hash")
    return base, other


def outcomes(rows: list[dict]) -> tuple[Counter, Counter]:
    statuses = Counter(row["status"] for row in rows)
    by_task = defaultdict(list)
    for row in rows:
        by_task[row["task_id"]].append(row["status"])
    tasks = Counter({
        "three_pass": sum(all(status == "pass" for status in attempts) for attempts in by_task.values()),
        "any_pass": sum(any(status == "pass" for status in attempts) for attempts in by_task.values()),
        "with_infra": sum(any(status == "infra" for status in attempts) for attempts in by_task.values()),
    })
    return statuses, tasks


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reference_run_id")
    parser.add_argument("candidate_run_id")
    args = parser.parse_args()
    try:
        base, other = compare(ROOT / "runs" / args.reference_run_id, ROOT / "runs" / args.candidate_run_id)
    except (OSError, KeyError, ValueError) as exc:
        parser.exit(1, f"Comparison rejected: {exc}\n")
    print("All recorded non-Moli run conditions and task attempts match.")
    print(f"Reference Moli: {base['engines']['moli']['version']} {base['engines']['moli']['sha256']}")
    print(f"Candidate Moli: {other['engines']['moli']['version']} {other['engines']['moli']['sha256']}")
    _, base_rows = load_run(ROOT / "runs" / args.reference_run_id)
    _, other_rows = load_run(ROOT / "runs" / args.candidate_run_id)
    profile, _ = frozen_tasks()
    for label, rows in (("Reference", base_rows), ("Candidate", other_rows)):
        statuses, tasks = outcomes(rows)
        print(f"{label}: three-pass tasks {tasks['three_pass']}/{profile['task_count']}; "
              f"any-pass tasks {tasks['any_pass']}/{profile['task_count']}; "
              f"attempts {dict(statuses)}; tasks with infra {tasks['with_infra']}")


if __name__ == "__main__":
    main()
