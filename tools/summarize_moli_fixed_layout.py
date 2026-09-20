#!/usr/bin/env python3
"""Summarize complete fixed-layout matrices from retained physical executions."""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path

from run_moli_cohort import PROFILE, file_sha256, frozen_tasks
from summarize_moli_try_layout import _matrix, _read_json, _read_rows, summarize_run


def summarize_fixed_sources(sources: list[tuple[Path, str]], contract: dict, layout: str) -> dict:
    """Require every task/attempt exactly once; never substitute opposite-layout passes."""
    if layout not in {"off", "on"}:
        raise ValueError("layout must be off or on")
    rows, bindings = [], []
    controls = None
    task_hashes = {}
    for run_dir, source in sources:
        if source not in {"initial", "retry", "results"}:
            raise ValueError("unknown physical execution source")
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
        if source in {"initial", "retry"}:
            summarize_run(run_dir, contract)
            receipt = manifest["layout_retry"]
            relative = receipt["initial_results" if source == "initial" else "retry_results"]
        else:
            if manifest.get("layout_retry") or (manifest.get("moli_layout_policy") or {}).get("try_layout"):
                raise ValueError("fixed source must not select replacement results")
            relative = "results.jsonl"
        path = run_dir / relative
        if path.is_symlink() or path.resolve().parent != run_dir.resolve():
            raise ValueError("source results must be an ordinary file inside the run")
        part = _read_rows(path)
        resolved = {t["task_id"]: t["sha256"] for t in manifest["resolved_tasks"]}
        if source == "results":
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
    parser.add_argument("--source", action="append", nargs=2, metavar=("RUN_DIR", "initial|retry|results"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    profile, task_ids = frozen_tasks()
    contract = {**profile, "task_ids": task_ids, "profile_sha256": file_sha256(PROFILE)}
    result = summarize_fixed_sources([(Path(p), kind) for p, kind in args.source], contract, args.layout)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
