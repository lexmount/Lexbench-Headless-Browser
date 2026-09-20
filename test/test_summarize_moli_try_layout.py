"""Public-contract tests for Moli try-layout result summarization."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import sys

import pytest

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))
from summarize_moli_try_layout import summarize_run  # noqa: E402
from runner import layout_retry  # noqa: E402


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def rows(run_dir: Path, task: str, statuses: list[str], layout: bool) -> list[dict]:
    result = []
    for attempt, status in enumerate(statuses, 1):
        artifact = f"artifacts/{task}/{attempt}/{'on' if layout else 'off'}"
        row = {
            "run_id": "physical", "engine": "moli", "task_id": task,
            "attempt": attempt, "status": status, "seed": f"seed-{attempt}",
            "task_version": 1, "subset_id": "raw", "layer": "l1",
            "driver": "webdriver", "launch_profile": "default",
            "duration_ms": 10, "artifact_dir": artifact,
            "engine_provenance": {"layout_enabled": layout, "binary_sha256": "moli-sha"},
        }
        target = run_dir / artifact / "run.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(row), encoding="utf-8")
        result.append(row)
    return result


def write_jsonl(path: Path, values: list[dict]) -> None:
    path.write_text("".join(json.dumps(value, sort_keys=True) + "\n" for value in values), encoding="utf-8")


def fixture(tmp_path: Path) -> tuple[Path, dict]:
    run_dir = tmp_path / "candidate"
    run_dir.mkdir()
    initial = rows(run_dir, "already-pass", ["pass"] * 3, False)
    initial += rows(run_dir, "recover", ["fail"] * 3, False)
    initial += rows(run_dir, "remain-fail", ["fail"] * 3, False)
    retries = rows(run_dir, "recover", ["pass"] * 3, True)
    retries += rows(run_dir, "remain-fail", ["pass", "fail", "pass"], True)
    write_jsonl(run_dir / "results.jsonl", initial)
    retry_lookup = {(row["task_id"], row["attempt"]): row for row in retries}

    def execute(original):
        result = retry_lookup[(original["task_id"], original["attempt"])]
        with (run_dir / "layout_retry_results.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(result) + "\n")
        return result

    receipt = layout_retry.rerun_failed_cases(run_dir, "candidate", 3, execute, lambda _: None)
    manifest = {
        "run_id": "candidate", "completion_status": "completed",
        "selected_engines": ["moli"], "k_runs": 3, "completed_result_rows": 9,
        "bench_manifest": {"sha256": "bench-sha"},
        "moli_layout_policy": layout_retry.policy("off", True), "layout_retry": receipt,
        "engines": {"moli": {"version": "moli 1.2.0", "sha256": "moli-sha"}},
        "runner": {"source": {"tree_sha256": "runner-sha"}, "fixtures": {"tree_sha256": "fixtures-sha"}},
    }
    (run_dir / "run_manifest.json").write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    conditions = {
        "schema": "lexbench_moli_cohort_conditions/1", "run_id": "candidate",
        "profile_sha256": "profile-sha", "task_ids_sha256": "tasks-sha",
        "bench_manifest_sha256": "bench-sha", "moli_sha256": "moli-sha",
        "moli_version": "moli 1.2.0", "moli_layout": "off", "try_layout": True,
        "chromedriver_sha256": "driver-sha", "chromedriver_version": "140.0",
    }
    run_dir.with_suffix(".conditions.json").write_text(json.dumps(conditions), encoding="utf-8")
    contract = {
        "task_ids": ["already-pass", "recover", "remain-fail"],
        "attempts_per_task": 3, "profile_sha256": "profile-sha",
        "task_ids_sha256": "tasks-sha", "bench_manifest_sha256": "bench-sha",
    }
    return run_dir, contract


def test_summary_keeps_case_denominator_and_separates_retry_cost(tmp_path):
    run_dir, contract = fixture(tmp_path)
    summary = summarize_run(run_dir, contract)
    assert summary["population"]["cases"] == 3
    assert summary["outcomes"] == {
        "initial_passed_cases": 1, "final_passed_cases": 2,
        "final_success_rate_pct": pytest.approx(200 / 3),
        "retried_cases": 2, "recovered_cases": 1, "unrecovered_cases": 1,
        "extra_physical_calls": 6,
        "initial_status_counts": {"fail": 6, "pass": 3},
        "retry_status_counts": {"fail": 1, "pass": 5},
        "final_status_counts": {"fail": 3, "pass": 6},
    }
    assert summary["case_ids"] == {
        "initial_passed": ["already-pass"], "recovered": ["recover"],
        "unrecovered": ["remain-fail"],
    }
    assert summary["provenance"]["chromedriver_sha256"] == "driver-sha"


def test_all_pass_initial_matrix_is_valid_without_retry_files(tmp_path):
    run_dir = tmp_path / "candidate"
    run_dir.mkdir()
    final = rows(run_dir, "pass-one", ["pass"] * 3, False)
    write_jsonl(run_dir / "results.jsonl", final)
    manifest = {
        "run_id": "candidate", "completion_status": "completed",
        "selected_engines": ["moli"], "k_runs": 3, "completed_result_rows": 3,
        "bench_manifest": {"sha256": "bench-sha"},
        "moli_layout_policy": layout_retry.policy("off", True),
        "engines": {"moli": {"version": "moli 1.2.0", "sha256": "moli-sha"}},
    }
    (run_dir / "run_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    conditions = {
        "schema": "lexbench_moli_cohort_conditions/1", "run_id": "candidate",
        "profile_sha256": "profile-sha", "task_ids_sha256": "tasks-sha",
        "bench_manifest_sha256": "bench-sha", "moli_sha256": "moli-sha",
        "moli_version": "moli 1.2.0", "moli_layout": "off", "try_layout": True,
        "chromedriver_sha256": "driver-sha", "chromedriver_version": "140.0",
    }
    run_dir.with_suffix(".conditions.json").write_text(json.dumps(conditions), encoding="utf-8")
    contract = {
        "task_ids": ["pass-one"], "attempts_per_task": 3,
        "profile_sha256": "profile-sha", "task_ids_sha256": "tasks-sha",
        "bench_manifest_sha256": "bench-sha",
    }
    summary = summarize_run(run_dir, contract)
    assert summary["outcomes"]["final_passed_cases"] == 1
    assert summary["outcomes"]["retried_cases"] == 0
    assert summary["provenance"]["retry_results_sha256"] is None


@pytest.mark.parametrize("mutation, message", [
    ("missing-final-row", "final matrix must contain"),
    ("retry-layout-off", "layout rerun input or launch mismatch"),
    ("try-layout-false", "must use layout off"),
    ("wrong-binary", "conditions receipt mismatch: moli_sha256"),
])
def test_incomplete_or_mismatched_evidence_is_rejected(tmp_path, mutation, message):
    run_dir, contract = fixture(tmp_path)
    manifest_path = run_dir / "run_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    if mutation == "missing-final-row":
        values = (run_dir / "results.jsonl").read_text().splitlines()
        (run_dir / "results.jsonl").write_text("\n".join(values[:-1]) + "\n")
        manifest["completed_result_rows"] = 8
    elif mutation == "retry-layout-off":
        retry_path = run_dir / "layout_retry_results.jsonl"
        values = [json.loads(line) for line in retry_path.read_text().splitlines()]
        values[0]["engine_provenance"]["layout_enabled"] = False
        write_jsonl(retry_path, values)
        manifest["layout_retry"]["retry_results_sha256"] = sha(retry_path)
    elif mutation == "try-layout-false":
        manifest["moli_layout_policy"] = layout_retry.policy("off", False)
    else:
        conditions_path = run_dir.with_suffix(".conditions.json")
        conditions = json.loads(conditions_path.read_text())
        conditions["moli_sha256"] = "other"
        conditions_path.write_text(json.dumps(conditions))
    manifest_path.write_text(json.dumps(manifest, sort_keys=True))
    with pytest.raises(ValueError, match=message):
        summarize_run(run_dir, contract)
