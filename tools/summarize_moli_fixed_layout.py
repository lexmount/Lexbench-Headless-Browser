#!/usr/bin/env python3
"""Summarize complete fixed-layout matrices from retained physical executions."""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path

from run_moli_cohort import PROFILE, file_sha256, frozen_tasks
from typing import Any
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runner.layout import require_fixed

def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain an object")
    return value


def _read_rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _matrix(rows: list[dict[str, Any]], task_ids: list[str], attempts: int, label: str) -> None:
    keys = [(row.get("task_id"), row.get("attempt")) for row in rows if row.get("engine") == "moli"]
    expected = {(task_id, attempt) for task_id in task_ids for attempt in range(1, attempts + 1)}
    if len(keys) != len(expected) or set(keys) != expected:
        raise ValueError(f"{label} has missing, duplicate or unexpected task attempts")





def summarize_fixed_sources(sources: list[Path], contract: dict, layout: str) -> dict:
    """Require every task/attempt exactly once; never substitute opposite-layout passes."""
    if layout not in {"off", "on"}:
        raise ValueError("layout must be off or on")
    rows, bindings = [], []
    controls = None
    task_hashes = {}
    for run_dir in sources:
        manifest_path = run_dir / "run_manifest.json"
        if manifest_path.is_symlink():
            raise ValueError("symlinked manifest is not allowed")
        manifest = _read_json(manifest_path)
        if manifest.get("completion_status") != "completed":
            raise ValueError("source run is incomplete")
        if manifest.get("selected_engines") != ["moli"] or manifest.get("k_runs") != contract["attempts_per_task"]:
            raise ValueError("source engine or repetition count mismatch")
        if (manifest.get("bench_manifest") or {}).get("sha256") != contract["bench_manifest_sha256"]:
            raise ValueError("source benchmark manifest mismatch")
        if manifest.get("seed") != contract["seed"] or manifest.get("score_mode") != contract["score_mode"]:
            raise ValueError("source seed or scoring mismatch")
        binary = manifest["engines"]["moli"]
        current_controls = {
            "moli_sha256": binary["sha256"], "moli_version": binary["version"],
            "fixtures": manifest["runner"].get("fixtures"),
            "harness_pins": manifest["runner"].get("harness_pins"),
        }
        if controls is not None and current_controls != controls:
            raise ValueError("source binaries, fixtures or harness pins differ")
        controls = current_controls
        require_fixed(manifest)
        relative = "results.jsonl"
        path = run_dir / relative
        if path.is_symlink() or path.resolve().parent != run_dir.resolve():
            raise ValueError("source results must be an ordinary file inside the run")
        part = _read_rows(path)
        resolved = {t["task_id"]: t["sha256"] for t in manifest["resolved_tasks"]}
        _matrix(part, list(resolved), contract["attempts_per_task"], "source matrix")
        if manifest.get("completed_result_rows") != len(part):
            raise ValueError("source completion count mismatch")
        for row in part:
            provenance = row.get("engine_provenance") or {}
            if provenance.get("layout_enabled") is not (layout == "on"):
                raise ValueError("physical execution layout mismatch")
            if provenance.get("binary_sha256") != binary["sha256"]:
                raise ValueError("physical execution binary mismatch")
            task_id = row["task_id"]
            if task_id not in resolved:
                raise ValueError("physical execution lacks a frozen task")
            if task_id in task_hashes and task_hashes[task_id] != resolved[task_id]:
                raise ValueError("task content differs between sources")
            task_hashes[task_id] = resolved[task_id]
            rows.append({**row, "source_run_id": manifest["run_id"]})
        bindings.append({"run_id": manifest["run_id"], "results_file": relative,
                         "manifest_sha256": file_sha256(manifest_path),
                         "results_sha256": file_sha256(path), "calls": len(part)})
    _matrix(rows, contract["task_ids"], contract["attempts_per_task"], "fixed-layout matrix")
    if len(rows) != len(contract["task_ids"]) * contract["attempts_per_task"]:
        raise ValueError("unexpected non-Moli rows")
    cases = []
    for task_id in sorted(contract["task_ids"]):
        attempts = sorted((r for r in rows if r["task_id"] == task_id), key=lambda r: r["attempt"])
        cases.append({"task_id": task_id, "task_sha256": task_hashes[task_id],
                      "passes": sum(r["status"] == "pass" for r in attempts),
                      "attempts": [{key: r.get(key) for key in
                                    ("attempt", "status", "failure", "answer", "artifact_dir", "run_id", "source_run_id", "seed")}
                                   for r in attempts]})
    passed = sum(c["passes"] == contract["attempts_per_task"] for c in cases)
    return {"schema": "lexbench_moli_fixed_layout_summary/1", "layout": layout,
            "population": {"cases": len(cases), "attempts_per_case": contract["attempts_per_task"],
                           "calls": len(rows), "task_ids_sha256": contract["task_ids_sha256"]},
            "outcomes": {"passed_cases": passed, "success_rate_pct": passed / len(cases) * 100,
                         "status_counts": dict(sorted(Counter(r["status"] for r in rows).items()))},
            "binary": {k: controls[k] for k in ("moli_sha256", "moli_version")},
            "sources": bindings, "cases": cases}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--layout", choices=("off", "on"), required=True)
    parser.add_argument("--source", action="append", type=Path, metavar="RUN_DIR", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    profile, task_ids = frozen_tasks()
    contract = {**profile, "task_ids": task_ids, "profile_sha256": file_sha256(PROFILE)}
    result = summarize_fixed_sources(args.source, contract, args.layout)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
