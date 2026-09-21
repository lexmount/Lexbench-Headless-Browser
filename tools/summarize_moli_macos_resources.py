#!/usr/bin/env python3
"""Certify fixed layout-off/on Moli resource cohorts measured on macOS."""

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
from runner import layout as layout_policy  # noqa: E402


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain an object")
    return value


def _rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _stats(values: list[float]) -> dict[str, float | int]:
    if not values or any(not math.isfinite(value) for value in values):
        raise ValueError("resource population is empty or contains a non-finite value")
    ordered = sorted(values)

    def percentile(q: float) -> float:
        position = (len(ordered) - 1) * q
        low = int(position)
        high = min(low + 1, len(ordered) - 1)
        return ordered[low] + (ordered[high] - ordered[low]) * (position - low)

    return {
        "n": len(values),
        "sum": sum(values),
        "mean": statistics.mean(values),
        "p50": percentile(0.50),
        "p95": percentile(0.95),
        "min": ordered[0],
        "max": ordered[-1],
    }


def _matrix(rows: list[dict[str, Any]], task_ids: list[str], attempts: int, label: str) -> None:
    keys = [(row.get("task_id"), row.get("attempt")) for row in rows if row.get("engine") == "moli"]
    expected = {(task_id, attempt) for task_id in task_ids for attempt in range(1, attempts + 1)}
    if len(keys) != len(expected) or set(keys) != expected:
        raise ValueError(f"{label} has missing, duplicate or unexpected task attempts")


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


def _change_pct(candidate: float, baseline: float) -> float:
    if baseline == 0:
        raise ValueError("cannot compute a percentage change from zero")
    return (candidate / baseline - 1) * 100


def summarize_fixed_runs(
    profiled_dirs: dict[str, Path],
    binary_receipt_path: Path,
    comparison_protocol_path: Path,
    *,
    expected_tasks: int = 557,
    expected_frozen_calls: int = 1045,
) -> dict[str, Any]:
    receipt = _json(binary_receipt_path)
    if receipt.get("schema") != "moli-binary-receipt/v1":
        raise ValueError("unsupported Moli binary receipt")
    protocol = _json(comparison_protocol_path)
    frozen_items = protocol.get("historical_common_pass_keys")
    if not isinstance(frozen_items, list) or len(frozen_items) != expected_frozen_calls:
        raise ValueError(f"historical protocol must contain {expected_frozen_calls} frozen comparison keys")
    frozen_keys = {(str(item.get("task_id")), int(item.get("attempt", 0))) for item in frozen_items}
    if len(frozen_keys) != expected_frozen_calls:
        raise ValueError("historical comparison keys are invalid or duplicated")

    if not profiled_dirs or set(profiled_dirs) - {"off", "on"}:
        raise ValueError("provide fixed off and/or on resource runs")
    run_specs = {(layout, "profiled"): path for layout, path in profiled_dirs.items()}
    loaded: dict[tuple[str, str], tuple[Path, dict[str, Any], list[dict[str, Any]]]] = {}
    common_tasks: list[tuple[str, str]] | None = None
    common_controls: dict[str, Any] | None = None
    for (layout, phase), run_dir in run_specs.items():
        manifest = _json(run_dir / "run_manifest.json")
        rows = _rows(run_dir / "results.jsonl")
        label = f"layout-{layout} {phase}"
        if manifest.get("completion_status") != "completed":
            raise ValueError(f"{label} run is not complete")
        if manifest.get("selected_engines") != ["moli"] or manifest.get("k_runs") != 5:
            raise ValueError(f"{label} run must contain Moli with k=5")
        if (manifest.get("runner") or {}).get("jobs") != 1:
            raise ValueError(f"{label} run must use one worker")
        if (manifest.get("resource_profile") or {}).get("mode") != "engine":
            raise ValueError(f"{label} resource profile mode mismatch")
        if (manifest.get("moli_layout_policy") or {}) != layout_policy.policy(layout):
            raise ValueError(f"{label} run must use fixed layout {layout}")
        if manifest.get("layout_retry") or (run_dir / "layout_retry_results.jsonl").exists():
            raise ValueError(f"{label} must not contain layout retries")
        moli = (manifest.get("engines") or {}).get("moli") or {}
        if moli.get("sha256") != receipt.get("sha256") or moli.get("version") != receipt.get("version"):
            raise ValueError(f"{label} Moli binary identity mismatch")
        tasks = [(str(item["task_id"]), str(item["sha256"])) for item in manifest.get("resolved_tasks") or []]
        if len(tasks) != expected_tasks or len({task_id for task_id, _ in tasks}) != expected_tasks:
            raise ValueError(f"{label} run must contain {expected_tasks} unique tasks")
        task_ids = [task_id for task_id, _ in tasks]
        _matrix(rows, task_ids, 5, label)
        expected_calls = expected_tasks * 5
        if len(rows) != expected_calls or manifest.get("completed_result_rows") != expected_calls:
            raise ValueError(f"{label} must contain {expected_calls} rows")
        if any((row.get("engine_provenance") or {}).get("layout_enabled") != (layout == "on") for row in rows):
            raise ValueError(f"{label} row layout provenance mismatch")
        if any((row.get("engine_provenance") or {}).get("binary_sha256") != receipt["sha256"] for row in rows):
            raise ValueError(f"{label} row binary identity mismatch")
        controls = {
            "seed": manifest.get("seed"),
            "score_mode": manifest.get("score_mode"),
            "source": (manifest.get("runner") or {}).get("source"),
            "fixtures": (manifest.get("runner") or {}).get("fixtures"),
            "harness_pins": (manifest.get("runner") or {}).get("harness_pins"),
        }
        if common_tasks is None:
            common_tasks, common_controls = tasks, controls
        elif tasks != common_tasks or controls != common_controls:
            raise ValueError("fixed-layout resource runs do not share frozen tasks and controls")
        loaded[(layout, phase)] = (run_dir, manifest, rows)

    all_keys = {(task_id, attempt) for task_id, _ in common_tasks or [] for attempt in range(1, 6)}
    if not frozen_keys.issubset(all_keys):
        raise ValueError("historical comparison keys are not covered by the candidate runs")

    configurations: dict[str, Any] = {}
    quality_reasons: list[str] = []
    for layout in profiled_dirs:
        profiled_dir, profiled_manifest, profiled_rows = loaded[(layout, "profiled")]
        profiled_by_key = {(row["task_id"], row["attempt"]): row for row in profiled_rows}
        profiled_host = _json(profiled_dir / "host_summary.json")
        reasons: list[str] = []
        if profiled_host.get("polluted"):
            reasons.append("host telemetry pollution gate failed")
        all_metrics = _resource_metrics([profiled_by_key[key] for key in sorted(all_keys)])
        frozen_metrics = _resource_metrics([profiled_by_key[key] for key in sorted(frozen_keys)])
        configurations[layout] = {
            "layout": layout,
            "metrics": {
                "all_predeclared_calls": all_metrics,
                "frozen_historical_common_pass_calls": frozen_metrics,
            },
            "outcomes": {
                "profiled_status_counts": dict(sorted(Counter(row["status"] for row in profiled_rows).items())),
            },
            "quality": {
                "publishable": not reasons,
                "reasons": reasons,
                "profiled_host": profiled_host,
                "observer_effect": {"measured": False},
            },
            "provenance": {
                "profiled": {
                    "run_id": profiled_manifest["run_id"],
                    "manifest_sha256": _sha(profiled_dir / "run_manifest.json"),
                    "results_sha256": _sha(profiled_dir / "results.jsonl"),
                    "host_summary_sha256": _sha(profiled_dir / "host_summary.json"),
                },
            },
        }
        quality_reasons.extend(f"layout {layout}: {reason}" for reason in reasons)

    comparisons: dict[str, Any] = {}
    for population in (("all_predeclared_calls", "frozen_historical_common_pass_calls") if set(configurations) == {"off", "on"} else ()):
        off = configurations["off"]["metrics"][population]
        on = configurations["on"]["metrics"][population]
        comparisons[population] = {
            "layout_on_vs_off_rss_p50_change_pct": _change_pct(
                on["rss_peak_mib"]["p50"], off["rss_peak_mib"]["p50"]
            ),
            "layout_on_vs_off_cpu_mean_change_pct": _change_pct(
                on["cpu_time_ms"]["mean"], off["cpu_time_ms"]["mean"]
            ),
        }

    task_ids_bytes = "".join(task_id + "\n" for task_id, _ in sorted(common_tasks or [])).encode()
    profiled_manifest = next(iter(loaded.values()))[1]
    return {
        "schema": "lexbench_moli_macos_fixed_resource_summary/1",
        "candidate": {
            "version": receipt["version"],
            "source_commit": receipt["source_commit"],
            "binary_sha256": receipt["sha256"],
            "target": receipt["target"],
            "profile": receipt["profile"],
        },
        "method": {
            "configurations": [f"layout_{layout}" for layout in profiled_dirs],
            "attempts_per_case": 5,
            "resource_sample_interval_ms": (profiled_manifest.get("resource_profile") or {}).get("sample_interval_ms"),
            "resource_runs": len(profiled_dirs),
            "observer_calibration": "not_requested",
        },
        "population": {
            "tasks": expected_tasks,
            "attempts_per_task": 5,
            "calls_per_configuration": len(all_keys),
            "frozen_comparison_calls": len(frozen_keys),
            "frozen_comparison_tasks": len({task_id for task_id, _ in frozen_keys}),
        },
        "configurations": configurations,
        "comparisons": comparisons,
        "quality": {
            "publishable": not quality_reasons,
            "reasons": quality_reasons,
            "memory_metric": "process_tree_rss",
            "pss_available": False,
        },
        "provenance": {
            "task_ids_sha256": hashlib.sha256(task_ids_bytes).hexdigest(),
            "runner_source": (common_controls or {}).get("source"),
            "fixtures": (common_controls or {}).get("fixtures"),
            "host": profiled_manifest.get("host"),
            "binary_receipt_sha256": _sha(binary_receipt_path),
            "comparison_protocol_sha256": _sha(comparison_protocol_path),
        },
    }


def summarize_fixed_pairs(off_profiled_dir: Path, on_profiled_dir: Path,
                          binary_receipt_path: Path, comparison_protocol_path: Path,
                          *, expected_tasks: int = 557, expected_frozen_calls: int = 1045) -> dict[str, Any]:
    return summarize_fixed_runs(
        {"off": off_profiled_dir, "on": on_profiled_dir}, binary_receipt_path,
        comparison_protocol_path, expected_tasks=expected_tasks,
        expected_frozen_calls=expected_frozen_calls,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--off-profiled-run", type=Path)
    parser.add_argument("--on-profiled-run", required=True, type=Path)
    parser.add_argument("--binary-receipt", required=True, type=Path)
    parser.add_argument("--comparison-protocol", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        summary = summarize_fixed_runs(
            {layout: path for layout, path in (("off", args.off_profiled_run), ("on", args.on_profiled_run)) if path is not None},
            args.binary_receipt,
            args.comparison_protocol,
        )
    except (OSError, KeyError, TypeError, ValueError) as exc:
        parser.exit(1, f"Summary rejected: {exc}\n")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
