"""Contract tests for fixed layout-off/on macOS Moli resource summaries."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

import pytest

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))
from summarize_moli_macos_resources import summarize_fixed_pairs, summarize_fixed_runs  # noqa: E402
from runner import layout as layout_policy  # noqa: E402

MOLI_SHA = "9" * 64


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8")


def _resource(rss_mib: int, cpu_ms: float) -> dict:
    return {
        "measurement_backend": {
            "cpu": "proc_tree",
            "memory_pss": "unavailable",
            "memory_rss": "darwin_ps_process_tree_rss",
        },
        "cpu_total_ms": cpu_ms,
        "rss_peak_bytes": rss_mib * 1048576,
        "pss_peak_bytes": None,
        "collection_wall_ms": 100.0,
    }


def _run(tmp_path: Path, layout: str, profiled: bool) -> Path:
    phase = "profiled" if profiled else "baseline"
    run_dir = tmp_path / f"{layout}-{phase}"
    run_dir.mkdir()
    rows = []
    for task in ("one", "two"):
        for attempt in range(1, 6):
            resource = _resource(100 + (10 if layout == "on" else 0), 12 if layout == "on" else 10) if profiled else None
            rows.append({
                "run_id": run_dir.name,
                "engine": "moli",
                "task_id": task,
                "attempt": attempt,
                "status": "pass" if task == "one" else "fail",
                "seed": f"seed-{task}-{attempt}",
                "task_version": 1,
                "duration_ms": 100.0,
                "engine_provenance": {"layout_enabled": layout == "on", "binary_sha256": MOLI_SHA},
                "resource": resource,
            })
    _write_jsonl(run_dir / "results.jsonl", rows)
    manifest = {
        "run_id": run_dir.name,
        "completion_status": "completed",
        "completed_result_rows": 10,
        "selected_engines": ["moli"],
        "k_runs": 5,
        "seed": "fixed",
        "score_mode": "independent",
        "resolved_tasks": [
            {"task_id": task, "sha256": hashlib.sha256(task.encode()).hexdigest()}
            for task in ("one", "two")
        ],
        "engines": {"moli": {"version": "moli 1.1.9", "sha256": MOLI_SHA}},
        "host": {"platform": "macOS", "machine": "arm64"},
        "runner": {
            "jobs": 1,
            "browser_reuse": "per_worker_process_per_engine",
            "source": {"tree_sha256": "runner-tree"},
            "fixtures": {"tree_sha256": "fixture-tree"},
            "harness_pins": {"drivers": {}},
        },
        "resource_profile": {
            "mode": "engine" if profiled else "baseline",
            "engine_order": "balanced_rotation",
            "engine_order_algorithm": "sha256",
            "max_observer_effect_pct": 20,
            "sample_interval_ms": 250,
        },
        "moli_layout_policy": layout_policy.policy(layout),
    }
    (run_dir / "run_manifest.json").write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    (run_dir / "host_summary.json").write_text(json.dumps({"polluted": False, "samples": 20}), encoding="utf-8")
    return run_dir


def _receipt(tmp_path: Path) -> Path:
    path = tmp_path / "binary-receipt.json"
    path.write_text(json.dumps({
        "schema": "moli-binary-receipt/v1",
        "version": "moli 1.1.9",
        "sha256": MOLI_SHA,
        "source_commit": "8" * 40,
        "target": "aarch64-apple-darwin",
        "profile": "debug",
    }), encoding="utf-8")
    return path


def _protocol(tmp_path: Path) -> Path:
    path = tmp_path / "protocol.json"
    path.write_text(json.dumps({
        "historical_common_pass_keys": [
            {"task_id": "one", "attempt": attempt} for attempt in range(1, 6)
        ]
    }), encoding="utf-8")
    return path


def _summary(tmp_path: Path) -> dict:
    return summarize_fixed_pairs(
        _run(tmp_path, "off", True),
        _run(tmp_path, "on", True),
        _receipt(tmp_path),
        _protocol(tmp_path),
        expected_tasks=2,
        expected_frozen_calls=5,
    )


def test_summary_reports_two_fixed_configurations_and_signed_changes(tmp_path):
    summary = _summary(tmp_path)
    assert summary["schema"] == "lexbench_moli_macos_fixed_resource_summary/1"
    assert summary["population"] == {
        "tasks": 2,
        "attempts_per_task": 5,
        "calls_per_configuration": 10,
        "frozen_comparison_calls": 5,
        "frozen_comparison_tasks": 1,
    }
    assert summary["method"]["resource_runs"] == 2
    assert summary["method"]["observer_calibration"] == "not_requested"
    assert all(set(c["provenance"]) == {"profiled"} for c in summary["configurations"].values())
    assert summary["configurations"]["off"]["metrics"]["all_predeclared_calls"]["rss_peak_mib"]["p50"] == 100
    assert summary["configurations"]["on"]["metrics"]["all_predeclared_calls"]["rss_peak_mib"]["p50"] == 110
    comparison = summary["comparisons"]["all_predeclared_calls"]
    assert comparison["layout_on_vs_off_rss_p50_change_pct"] == pytest.approx(10)
    assert comparison["layout_on_vs_off_cpu_mean_change_pct"] == pytest.approx(20)
    assert summary["quality"]["publishable"] is True
    assert summary["quality"]["pss_available"] is False


def test_summary_rejects_layout_retry_or_missing_macos_rss(tmp_path):
    off_profiled = _run(tmp_path, "off", True)
    on_profiled = _run(tmp_path, "on", True)
    rows = [json.loads(line) for line in off_profiled.joinpath("results.jsonl").read_text().splitlines()]
    rows[0]["resource"]["rss_peak_bytes"] = None
    _write_jsonl(off_profiled / "results.jsonl", rows)
    with pytest.raises(ValueError, match="missing peak RSS"):
        summarize_fixed_pairs(
            off_profiled, on_profiled,
            _receipt(tmp_path), _protocol(tmp_path), expected_tasks=2, expected_frozen_calls=5,
        )

    rows[0]["resource"]["rss_peak_bytes"] = 100 * 1048576
    _write_jsonl(off_profiled / "results.jsonl", rows)
    manifest_path = off_profiled / "run_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["layout_retry"] = {"unexpected": True}
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="must not contain layout retries"):
        summarize_fixed_pairs(
            off_profiled, on_profiled,
            _receipt(tmp_path), _protocol(tmp_path), expected_tasks=2, expected_frozen_calls=5,
        )


def test_official_on_only_does_not_require_an_unrequested_off_run(tmp_path):
    summary = summarize_fixed_runs(
        {"on": _run(tmp_path, "on", True)}, _receipt(tmp_path), _protocol(tmp_path),
        expected_tasks=2, expected_frozen_calls=5,
    )
    assert summary["method"]["resource_runs"] == 1
    assert summary["method"]["configurations"] == ["layout_on"]
    assert set(summary["configurations"]) == {"on"}
    assert summary["comparisons"] == {}
    assert summary["configurations"]["on"]["metrics"]["all_predeclared_calls"]["cpu_time_ms"]["n"] == 10
