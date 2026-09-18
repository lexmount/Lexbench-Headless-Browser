"""Whole-case recovery, evidence integrity, and opt-in execution behavior."""

import copy
import json
from pathlib import Path
import sys

import pytest

from runner import layout_retry, run

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))
from compare_moli_cohort import normalized_manifest  # noqa: E402


def batch(tmp_path, task, statuses, layout=False):
    rows = []
    for attempt, status in enumerate(statuses, 1):
        artifact = f"{task}/{attempt}/{'on' if layout else 'off'}"
        row = {
            "task_id": task, "engine": "moli", "attempt": attempt,
            "status": status, "seed": f"seed-{attempt}", "duration_ms": 10,
            "artifact_dir": artifact, "run_id": "physical",
            "engine_provenance": {"layout_enabled": layout},
        }
        path = tmp_path / artifact / "run.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(row))
        rows.append(row)
    return rows


def test_successful_whole_batch_only_replaces_failed_case(tmp_path):
    original = batch(tmp_path, "pass", ["pass"] * 3)
    original += batch(tmp_path, "recover", ["fail", "pass", "fail"])
    original += batch(tmp_path, "still-fail", ["fail"] * 3)
    retry = batch(tmp_path, "recover", ["pass"] * 3, True)
    retry += batch(tmp_path, "still-fail", ["pass", "fail", "pass"], True)
    final = layout_retry.replace_cases(original, retry, 3, tmp_path, "logical")
    assert len(final) == 9
    assert final[:3] == original[:3]
    assert final[6:] == original[6:]
    assert all(row["status"] == "pass" and row["run_id"] == "logical" for row in final[3:6])
    assert layout_retry.pass_count(original) == 1
    assert layout_retry.pass_count(final) == 2
    assert all(row["layout_retry"]["total_execution_duration_ms"] == 20 for row in final[3:6])


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "wrong-seed", "layout-off"])
def test_incomplete_or_wrong_rerun_cannot_replace_results(tmp_path, mutation):
    original = batch(tmp_path, "task", ["fail"] * 3)
    retry = batch(tmp_path, "task", ["pass"] * 3, True)
    if mutation == "missing":
        retry.pop()
    elif mutation == "duplicate":
        retry.append(copy.deepcopy(retry[0]))
    elif mutation == "wrong-seed":
        retry[0]["seed"] = "different"
    else:
        retry[0]["engine_provenance"]["layout_enabled"] = False
    with pytest.raises(ValueError):
        layout_retry.replace_cases(original, retry, 3, tmp_path, "logical")


def test_gate_skip_and_pass_are_not_retried(tmp_path):
    rows = batch(tmp_path, "pass", ["pass"] * 3)
    rows += batch(tmp_path, "gate", ["chrome_gate_fail"] * 3)
    assert layout_retry.failed_cases(rows, 3) == set()


def test_cli_defaults_and_opt_in():
    parser = run.build_parser()
    args = parser.parse_args(["run"])
    assert args.moli_layout == "off" and args.try_layout is False
    assert parser.parse_args(["run", "--try-layout"]).try_layout is True
    assert layout_retry.policy("on", True)["retry_layout"] is None
    with pytest.raises(SystemExit):
        parser.parse_args(["run", "--moli-layout", "auto"])


def test_recovery_metadata_is_not_a_version_comparison_control():
    base = {
        "engines": {"moli": {"layout_mode": "off"}},
        "moli_layout_policy": layout_retry.policy("off", True),
    }
    candidate = copy.deepcopy(base)
    candidate["layout_retry"] = {"retried_cases": ["one"]}
    candidate["moli_failure_tasks"] = {"count": 1, "sha256": "outcome"}
    assert normalized_manifest(base) == normalized_manifest(candidate)
    candidate["moli_layout_policy"] = layout_retry.policy("off", False)
    assert normalized_manifest(base) != normalized_manifest(candidate)


def test_interrupted_recovery_cannot_generate_final_report(tmp_path):
    manifest = {
        "moli_layout_policy": layout_retry.policy("off", True),
        "completion_status": "interrupted",
    }
    with pytest.raises(ValueError, match="incomplete"):
        layout_retry.verify(tmp_path, manifest, [])


@pytest.mark.parametrize("field", ["seed", "driver", "binary_sha256"])
def test_changed_inputs_rejected_even_when_retry_fails(tmp_path, field):
    original = batch(tmp_path, "task", ["fail"] * 3)
    retry = batch(tmp_path, "task", ["fail"] * 3, True)
    target = retry[0]["engine_provenance"] if field == "binary_sha256" else retry[0]
    target[field] = "changed"
    with pytest.raises(ValueError, match="mismatch"):
        layout_retry.replace_cases(original, retry, 3, tmp_path, "logical")


def test_rerun_orchestration_and_receipt(tmp_path):
    original = batch(tmp_path, "pass", ["pass"] * 3)
    original += batch(tmp_path, "recover", ["fail"] * 3)
    original += batch(tmp_path, "fail", ["fail"] * 3)
    results = tmp_path / "results.jsonl"
    results.write_text("".join(json.dumps(row) + "\n" for row in original))
    retry = batch(tmp_path, "recover", ["pass"] * 3, True)
    retry += batch(tmp_path, "fail", ["fail"] * 3, True)
    lookup = {(row["task_id"], row["attempt"]): row for row in retry}
    called = []

    def execute(row):
        key = (row["task_id"], row["attempt"])
        called.append(key)
        result = lookup[key]
        with (tmp_path / "layout_retry_results.jsonl").open("a") as handle:
            handle.write(json.dumps(result) + "\n")
        return result

    receipt = layout_retry.rerun_failed_cases(tmp_path, "logical", 3, execute, lambda _: None)
    final = [json.loads(line) for line in results.read_text().splitlines()]
    assert len(called) == 6
    assert len(final) == 9
    assert final[:3] == original[:3]
    assert final[6:] == original[6:]
    assert receipt["recovered_cases"] == ["recover"]
    manifest = {
        "run_id": "logical", "k_runs": 3, "completion_status": "completed",
        "moli_layout_policy": layout_retry.policy("off", True), "layout_retry": receipt,
    }
    layout_retry.verify(tmp_path, manifest, final)
    del manifest["layout_retry"]
    with pytest.raises(ValueError, match="receipt is missing"):
        layout_retry.verify(tmp_path, manifest, final)


def test_interrupted_batch_preserves_original_matrix(tmp_path):
    original = batch(tmp_path, "fail", ["fail"] * 3)
    results = tmp_path / "results.jsonl"
    raw = "".join(json.dumps(row) + "\n" for row in original)
    results.write_text(raw)

    def interrupted(row):
        raise RuntimeError("interrupted")

    with pytest.raises(RuntimeError, match="interrupted"):
        layout_retry.rerun_failed_cases(tmp_path, "logical", 3, interrupted, lambda _: None)
    assert results.read_text() == raw
    assert (tmp_path / "initial_results.jsonl").read_text() == raw


def test_all_pass_batch_does_not_execute_or_create_recovery_files(tmp_path):
    original = batch(tmp_path, "pass", ["pass"] * 3)
    (tmp_path / "results.jsonl").write_text("".join(json.dumps(row) + "\n" for row in original))

    def unexpected(row):
        pytest.fail("passing case was rerun")

    assert layout_retry.rerun_failed_cases(tmp_path, "logical", 3, unexpected, lambda _: None) is None
    assert not (tmp_path / "initial_results.jsonl").exists()


def test_remaining_failures_use_final_results_once_per_case(tmp_path):
    rows = batch(tmp_path, "recovered", ["pass"] * 3)
    rows += batch(tmp_path, "failed", ["fail", "pass", "fail"])
    receipt = run.write_moli_failure_tasks(tmp_path, rows)
    assert receipt["count"] == 1
    assert (tmp_path / receipt["path"]).read_text() == "failed\n"
    receipt = run.write_moli_failure_tasks(tmp_path, rows[:3])
    assert receipt["count"] == 0
    assert (tmp_path / receipt["path"]).read_text() == ""
