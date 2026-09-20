"""Contract tests for the macOS Moli resource pair summary."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

import pytest

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))
from summarize_moli_macos_resources import summarize_pair  # noqa: E402
from runner import layout_retry  # noqa: E402


MOLI_SHA = "9" * 64


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8")


def _resource(rss: int) -> dict:
    return {
        "measurement_backend": {
            "cpu": "proc_tree",
            "memory_pss": "unavailable",
            "memory_rss": "darwin_ps_process_tree_rss",
        },
        "cpu_total_ms": 10.0,
        "rss_peak_bytes": rss,
        "pss_peak_bytes": None,
        "collection_wall_ms": 100.0,
    }


def _row(run_dir: Path, task: str, attempt: int, status: str, layout: bool, profiled: bool) -> dict:
    artifact = f"artifacts/{task}/{attempt}/{'on' if layout else 'off'}"
    row = {
        "run_id": run_dir.name,
        "engine": "moli",
        "task_id": task,
        "attempt": attempt,
        "status": status,
        "seed": f"seed-{task}-{attempt}",
        "task_version": 1,
        "subset_id": "raw",
        "layer": "l1",
        "driver": "raw_cdp",
        "launch_profile": "default",
        "duration_ms": 100.0,
        "artifact_dir": artifact,
        "engine_provenance": {"layout_enabled": layout, "binary_sha256": MOLI_SHA},
        "resource": _resource(100 * 1048576 + attempt) if profiled else None,
    }
    target = run_dir / artifact / "run.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(row, sort_keys=True), encoding="utf-8")
    return row


def _run(tmp_path: Path, name: str, profiled: bool) -> Path:
    run_dir = tmp_path / name
    run_dir.mkdir()
    initial = []
    for task, statuses in (("pass", ["pass"] * 5), ("recover", ["fail"] * 5)):
        initial.extend(_row(run_dir, task, attempt, status, False, profiled) for attempt, status in enumerate(statuses, 1))
    _write_jsonl(run_dir / "results.jsonl", initial)
    retries = {
        ("recover", attempt): _row(run_dir, "recover", attempt, "pass", True, profiled)
        for attempt in range(1, 6)
    }

    def execute(original):
        result = retries[(original["task_id"], original["attempt"])]
        with (run_dir / "layout_retry_results.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(result, sort_keys=True) + "\n")
        return result

    receipt = layout_retry.rerun_failed_cases(run_dir, name, 5, execute, lambda _: None)
    engine = {"moli": {"version": "moli 1.1.9", "sha256": MOLI_SHA}}
    source = {"tree_sha256": "runner-tree"}
    fixtures = {"tree_sha256": "fixture-tree"}
    manifest = {
        "run_id": name,
        "completion_status": "completed",
        "completed_result_rows": 10,
        "selected_engines": ["moli"],
        "k_runs": 5,
        "seed": "fixed",
        "score_mode": "independent",
        "resolved_tasks": [
            {"task_id": "pass", "sha256": hashlib.sha256(b"pass").hexdigest()},
            {"task_id": "recover", "sha256": hashlib.sha256(b"recover").hexdigest()},
        ],
        "engines": engine,
        "host": {"platform": "macOS", "machine": "arm64"},
        "runner": {
            "jobs": 1,
            "browser_reuse": "per_worker_process_per_engine",
            "source": source,
            "fixtures": fixtures,
            "harness_pins": {"drivers": {}},
        },
        "resource_profile": {
            "mode": "engine" if profiled else "baseline",
            "engine_order": "balanced_rotation",
            "engine_order_algorithm": "sha256",
            "max_observer_effect_pct": 20,
        },
        "moli_layout_policy": layout_retry.policy("off", True),
        "layout_retry": receipt,
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


def test_summary_separates_final_logical_and_retry_physical_cost(tmp_path):
    baseline = _run(tmp_path, "baseline", False)
    profiled = _run(tmp_path, "profiled", True)
    summary = summarize_pair(baseline, profiled, _receipt(tmp_path), expected_tasks=2)
    assert summary["population"] == {
        "tasks": 2,
        "attempts_per_task": 5,
        "logical_calls": 10,
        "initial_physical_calls": 10,
        "retry_physical_calls": 5,
        "total_physical_calls": 15,
    }
    assert summary["metrics"]["final_logical_calls"]["rss_peak_mib"]["n"] == 10
    assert summary["metrics"]["retry_physical_calls"]["cpu_time_ms"]["sum"] == 50
    assert summary["quality"]["publishable"] is True
    assert summary["quality"]["pss_available"] is False


def test_summary_rejects_pss_or_missing_macos_rss(tmp_path):
    baseline = _run(tmp_path, "baseline", False)
    profiled = _run(tmp_path, "profiled", True)
    final_path = profiled / "results.jsonl"
    rows = [json.loads(line) for line in final_path.read_text().splitlines()]
    rows[0]["resource"]["rss_peak_bytes"] = None
    _write_jsonl(final_path, rows)
    initial_path = profiled / "initial_results.jsonl"
    initial = [json.loads(line) for line in initial_path.read_text().splitlines()]
    initial[0]["resource"]["rss_peak_bytes"] = None
    _write_jsonl(initial_path, initial)
    manifest_path = profiled / "run_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["layout_retry"]["initial_results_sha256"] = hashlib.sha256(initial_path.read_bytes()).hexdigest()
    manifest["layout_retry"]["final_results_sha256"] = hashlib.sha256(final_path.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    with pytest.raises(ValueError, match="missing peak RSS"):
        summarize_pair(baseline, profiled, _receipt(tmp_path), expected_tasks=2)
