#!/usr/bin/env python3
"""Certify one baseline/profiled Moli try-layout resource pair on macOS."""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import statistics
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from runner import layout_retry  # noqa: E402
from runner.resources import duration_calibration  # noqa: E402


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain an object")
    return value


def _rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def _stats(values: list[float]) -> dict[str, float | int]:
    if not values:
        raise ValueError("resource population is empty")
    if any(not math.isfinite(value) for value in values):
        raise ValueError("resource population contains a non-finite value")
    return {
        "n": len(values),
        "sum": sum(values),
        "mean": statistics.mean(values),
        "p50": _percentile(values, 0.50),
        "p95": _percentile(values, 0.95),
        "min": min(values),
        "max": max(values),
    }


def _matrix(rows: list[dict[str, Any]], task_ids: list[str], attempts: int, label: str) -> None:
    keys = [(row.get("task_id"), row.get("attempt")) for row in rows if row.get("engine") == "moli"]
    expected = {(task_id, attempt) for task_id in task_ids for attempt in range(1, attempts + 1)}
    if len(keys) != len(expected) or set(keys) != expected:
        raise ValueError(f"{label} has missing, duplicate or unexpected task attempts")


def _physical(run_dir: Path, manifest: dict[str, Any], final: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    layout_retry.verify(run_dir, manifest, final)
    receipt = manifest.get("layout_retry")
    if not receipt:
        if layout_retry.failed_cases(final, int(manifest["k_runs"])):
            raise ValueError("layout retry receipt is missing for failed initial cases")
        return final, []
    initial = _rows(run_dir / receipt["initial_results"])
    retry = _rows(run_dir / receipt["retry_results"])
    if _sha(run_dir / receipt["initial_results"]) != receipt["initial_results_sha256"]:
        raise ValueError("initial result hash mismatch")
    if _sha(run_dir / receipt["retry_results"]) != receipt["retry_results_sha256"]:
        raise ValueError("retry result hash mismatch")
    return initial, retry


def _resource_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    rss: list[float] = []
    cpu: list[float] = []
    for row in rows:
        resource = row.get("resource") or {}
        backend = resource.get("measurement_backend") or {}
        if backend.get("memory_rss") != "darwin_ps_process_tree_rss":
            raise ValueError("resource row does not use the macOS process-tree RSS backend")
        if backend.get("memory_pss") != "unavailable" or resource.get("pss_peak_bytes") is not None:
            raise ValueError("macOS resource row must not report PSS")
        rss_value = resource.get("rss_peak_bytes")
        cpu_value = resource.get("cpu_total_ms")
        if not isinstance(rss_value, (int, float)) or isinstance(rss_value, bool):
            raise ValueError("resource row is missing peak RSS")
        if not isinstance(cpu_value, (int, float)) or isinstance(cpu_value, bool):
            raise ValueError("resource row is missing CPU time")
        rss.append(float(rss_value) / 1048576)
        cpu.append(float(cpu_value))
    return {"rss_peak_mib": _stats(rss), "cpu_time_ms": _stats(cpu)}


def summarize_pair(
    baseline_dir: Path,
    engine_dir: Path,
    binary_receipt_path: Path,
    *,
    expected_tasks: int = 557,
) -> dict[str, Any]:
    binary_receipt = _json(binary_receipt_path)
    if binary_receipt.get("schema") != "moli-binary-receipt/v1":
        raise ValueError("unsupported Moli binary receipt")
    loaded: dict[str, tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]] = {}
    for label, run_dir, mode in (("baseline", baseline_dir, "baseline"), ("engine", engine_dir, "engine")):
        manifest_path = run_dir / "run_manifest.json"
        results_path = run_dir / "results.jsonl"
        manifest = _json(manifest_path)
        final = _rows(results_path)
        if manifest.get("completion_status") != "completed":
            raise ValueError(f"{label} run is not complete")
        if manifest.get("selected_engines") != ["moli"] or manifest.get("k_runs") != 5:
            raise ValueError(f"{label} run must contain Moli with k=5")
        if (manifest.get("runner") or {}).get("jobs") != 1:
            raise ValueError(f"{label} run must use one worker")
        if (manifest.get("resource_profile") or {}).get("mode") != mode:
            raise ValueError(f"{label} resource profile mode mismatch")
        if (manifest.get("moli_layout_policy") or {}) != layout_retry.policy("off", True):
            raise ValueError(f"{label} run must use the try-layout policy")
        moli = (manifest.get("engines") or {}).get("moli") or {}
        if moli.get("sha256") != binary_receipt.get("sha256") or moli.get("version") != binary_receipt.get("version"):
            raise ValueError(f"{label} Moli binary identity mismatch")
        task_ids = [str(item["task_id"]) for item in manifest.get("resolved_tasks") or []]
        if len(task_ids) != expected_tasks or len(set(task_ids)) != expected_tasks:
            raise ValueError(f"{label} run must contain {expected_tasks} unique tasks")
        _matrix(final, task_ids, 5, f"{label} final matrix")
        expected_calls = expected_tasks * 5
        if len(final) != expected_calls or manifest.get("completed_result_rows") != expected_calls:
            raise ValueError(f"{label} final matrix must contain {expected_calls} rows")
        initial, retry = _physical(run_dir, manifest, final)
        _matrix(initial, task_ids, 5, f"{label} initial matrix")
        retried = sorted(layout_retry.failed_cases(initial, 5))
        _matrix(retry, retried, 5, f"{label} retry matrix")
        loaded[label] = (manifest, final, initial, retry)

    baseline_manifest, baseline_final, baseline_initial, baseline_retry = loaded["baseline"]
    engine_manifest, engine_final, engine_initial, engine_retry = loaded["engine"]
    baseline_tasks = [(item["task_id"], item["sha256"]) for item in baseline_manifest["resolved_tasks"]]
    engine_tasks = [(item["task_id"], item["sha256"]) for item in engine_manifest["resolved_tasks"]]
    if baseline_tasks != engine_tasks:
        raise ValueError("baseline and profiled task manifests differ")
    for key in ("seed", "k_runs", "score_mode"):
        if baseline_manifest.get(key) != engine_manifest.get(key):
            raise ValueError(f"baseline and profiled runs differ in {key}")
    baseline_runner = baseline_manifest.get("runner") or {}
    engine_runner = engine_manifest.get("runner") or {}
    for key in ("source", "fixtures", "harness_pins"):
        if baseline_runner.get(key) != engine_runner.get(key):
            raise ValueError(f"baseline and profiled runner {key} differ")
    calibration = duration_calibration(
        engine_final,
        baseline_final,
        float((engine_manifest.get("resource_profile") or {}).get("max_observer_effect_pct") or 20),
        profiled_manifest=engine_manifest,
        baseline_manifest=baseline_manifest,
    )
    host_summary = _json(engine_dir / "host_summary.json")
    final_metrics = _resource_metrics(engine_final)
    initial_metrics = _resource_metrics(engine_initial)
    retry_metrics = _resource_metrics(engine_retry)
    all_physical = engine_initial + engine_retry
    all_metrics = _resource_metrics(all_physical)
    baseline_retried = set(layout_retry.failed_cases(baseline_initial, 5))
    engine_retried = set(layout_retry.failed_cases(engine_initial, 5))
    quality_reasons: list[str] = []
    if host_summary.get("polluted"):
        quality_reasons.append("host telemetry pollution gate failed")
    if not calibration.get("acceptable"):
        quality_reasons.append("profiler observer-effect gate failed")
    logical_calls = expected_tasks * 5
    if calibration.get("matched_attempts") != logical_calls or not calibration.get("complete_pairing"):
        quality_reasons.append("baseline/profiled logical pairing is incomplete")
    if calibration.get("provenance_mismatches"):
        quality_reasons.append("baseline/profiled provenance differs")
    task_ids_bytes = "".join(task_id + "\n" for task_id, _ in sorted(engine_tasks)).encode()
    return {
        "schema": "lexbench_moli_macos_resource_summary/1",
        "candidate": {
            "version": binary_receipt["version"],
            "source_commit": binary_receipt["source_commit"],
            "binary_sha256": binary_receipt["sha256"],
            "target": binary_receipt["target"],
            "profile": binary_receipt["profile"],
        },
        "method": {
            "policy_id": layout_retry.POLICY_ID,
            "initial_layout": "off",
            "retry_layout": "on",
            "attempts_per_case": 5,
            "replacement_rule": "replace_only_when_all_retry_attempts_pass",
        },
        "population": {
            "tasks": expected_tasks,
            "attempts_per_task": 5,
            "logical_calls": logical_calls,
            "initial_physical_calls": len(engine_initial),
            "retry_physical_calls": len(engine_retry),
            "total_physical_calls": len(all_physical),
        },
        "outcomes": {
            "final_status_counts": dict(sorted(Counter(row["status"] for row in engine_final).items())),
            "baseline_profile_status_mismatches": sum(a["status"] != b["status"] for a, b in zip(baseline_final, engine_final)),
            "baseline_retried_cases": len(baseline_retried),
            "profiled_retried_cases": len(engine_retried),
            "layout_decision_mismatch_cases": len(baseline_retried ^ engine_retried),
        },
        "metrics": {
            "final_logical_calls": final_metrics,
            "initial_physical_calls": initial_metrics,
            "retry_physical_calls": retry_metrics,
            "total_physical_calls": all_metrics,
        },
        "quality": {
            "publishable": not quality_reasons,
            "reasons": quality_reasons,
            "memory_metric": "process_tree_rss",
            "pss_available": False,
            "host": host_summary,
            "observer_effect": calibration,
        },
        "provenance": {
            "task_ids_sha256": hashlib.sha256(task_ids_bytes).hexdigest(),
            "runner_source": engine_runner.get("source"),
            "fixtures": engine_runner.get("fixtures"),
            "baseline": {
                "run_id": baseline_manifest["run_id"],
                "manifest_sha256": _sha(baseline_dir / "run_manifest.json"),
                "results_sha256": _sha(baseline_dir / "results.jsonl"),
            },
            "profiled": {
                "run_id": engine_manifest["run_id"],
                "manifest_sha256": _sha(engine_dir / "run_manifest.json"),
                "results_sha256": _sha(engine_dir / "results.jsonl"),
                "host_summary_sha256": _sha(engine_dir / "host_summary.json"),
            },
            "binary_receipt_sha256": _sha(binary_receipt_path),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-run", required=True, type=Path)
    parser.add_argument("--profiled-run", required=True, type=Path)
    parser.add_argument("--binary-receipt", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        summary = summarize_pair(args.baseline_run, args.profiled_run, args.binary_receipt)
    except (OSError, KeyError, TypeError, ValueError) as exc:
        parser.exit(1, f"Summary rejected: {exc}\n")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
