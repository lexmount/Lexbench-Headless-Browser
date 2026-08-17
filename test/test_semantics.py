"""L1/L2 boundary and L2 capability-level score contracts."""

from __future__ import annotations

import copy
import json
from collections import Counter
from pathlib import Path

import pytest

from runner import run as runner_run
from runner import semantics


REPO_ROOT = Path(__file__).resolve().parents[1]


def _task(task_id: str, *, tags: list[str] | None = None, grader: str = "server_side") -> dict:
    return {
        "task_id": task_id,
        "tags": tags or ["version.v0_1"],
        "grader": {"kind": grader},
    }


def _map(probes: list[dict]) -> dict:
    return {
        "schema_version": 1,
        "layer": "L2",
        "evaluation_axis": "web_platform_workflow_semantic_correctness",
        "score_unit": "semantic_capability_attempt",
        "aggregation": "all semantic probes pass",
        "probe_roles": {
            "semantic_probe": "scored",
            "driver_cross_check": "cross-check",
            "diagnostic": "diagnostic",
        },
        "capabilities": [
            {
                "capability_id": "web.storage.example",
                "title": "Example storage capability",
                "category": "storage",
                "description": "Correlated probes produce one verdict.",
                "observable": "storage_state",
                "probes": probes,
            }
        ],
    }


def _probe(task_id: str, role: str) -> dict:
    return {"task_id": task_id, "role": role, "claim": f"claim for {task_id}"}


def _row(task_id: str, engine: str, status: str, *, score_included: bool = True) -> dict:
    return {
        "layer": "L2",
        "subset_id": "l2.web_platform",
        "task_id": task_id,
        "engine": engine,
        "attempt": 1,
        "status": status,
        "score_included": score_included,
        "chrome_gate": {"required": False},
        "failure": None if status == "pass" else {"class": "cdp_semantic"},
    }


def test_checked_in_map_covers_every_active_l2_task() -> None:
    suite = runner_run.load_suite(runner_run.DEFAULT_MANIFEST)
    map_path, payload = runner_run.load_l2_semantic_capability_map(
        runner_run.DEFAULT_MANIFEST, suite
    )
    assert map_path is not None
    assert payload is not None
    l2_tasks, load_errors = runner_run.all_layer_task_objects(
        runner_run.DEFAULT_MANIFEST, suite, "L2"
    )
    assert load_errors == []
    assert semantics.validate_capability_map(
        payload, l2_tasks, runner_run.rel_to_bench(map_path)
    ) == []

    index = semantics.capability_task_index(payload)
    assert set(index) == set(l2_tasks)
    roles = Counter(item["role"] for item in index.values())
    assert roles == {
        "semantic_probe": 147,
        "driver_cross_check": 3,
        "diagnostic": 38,
    }
    assert len(payload["capabilities"]) == 72
    assert (
        sum(
            any(probe["role"] == "semantic_probe" for probe in capability["probes"])
            for capability in payload["capabilities"]
        )
        == 64
    )
    assert index["v3_pw_flow_shop_cart"]["role"] == "semantic_probe"
    assert index["v3_pp_flow_shop_cart"]["role"] == "driver_cross_check"
    assert index["r3_probe_b1_localstorage"]["role"] == "diagnostic"
    assert (
        index["r3_probe_b1_localstorage"]["capability_id"]
        == index["v2_leg_probe_b1_localstorage"]["capability_id"]
    )
    for task_id in sorted(l2_tasks):
        if not task_id.startswith("v2_leg_"):
            continue
        diagnostic_id = "r3_" + task_id.removeprefix("v2_leg_")
        if diagnostic_id not in l2_tasks:
            continue
        assert index[task_id]["role"] == "semantic_probe"
        assert index[diagnostic_id]["role"] == "diagnostic"
        assert index[task_id]["capability_id"] == index[diagnostic_id]["capability_id"]


def test_resolved_tasks_and_attempt_rows_carry_semantic_role() -> None:
    _suite, tasks = runner_run.expand_tasks(
        runner_run.DEFAULT_MANIFEST,
        requested_tasks=[
            "v3_pw_flow_shop_cart",
            "v3_pp_flow_shop_cart",
            "r3_probe_b1_localstorage",
        ],
    )
    by_id = {task.task_id: task for task in tasks}
    semantic = by_id["v3_pw_flow_shop_cart"]
    cross_check = by_id["v3_pp_flow_shop_cart"]
    diagnostic = by_id["r3_probe_b1_localstorage"]
    assert semantic.semantic_capability["role"] == "semantic_probe"
    assert cross_check.semantic_capability["role"] == "driver_cross_check"
    assert diagnostic.semantic_capability["role"] == "diagnostic"

    pass_result = {"status": "pass", "fallback_used": False}
    assert runner_run.should_include_score(pass_result, semantic, "moli", True)
    assert not runner_run.should_include_score(pass_result, cross_check, "moli", True)
    assert not runner_run.should_include_score(pass_result, diagnostic, "moli", True)

    row = runner_run.attempt_base_result(
        "run", semantic, "moli", 1, "seed", {"required": False}, "artifact"
    )
    assert row["evaluation_axis"] == "web_platform_workflow_semantic_correctness"
    assert row["semantic_capability"]["capability_id"] == "workflow.paginated_cart"


def test_map_validation_rejects_unmapped_duplicate_and_direct_return_grading() -> None:
    tasks = {
        "semantic-a": _task("semantic-a", grader="inline_assertions"),
        "unmapped": _task("unmapped"),
    }
    payload = _map(
        [
            _probe("semantic-a", "semantic_probe"),
            _probe("semantic-a", "diagnostic"),
        ]
    )
    errors = semantics.validate_capability_map(payload, tasks, "map.json")
    assert any("assigned to both" in error for error in errors)
    assert any("server_side grader" in error for error in errors)
    assert any("active L2 task `unmapped` has no capability" in error for error in errors)


def test_map_validation_keeps_diagnostics_and_driver_checks_out_of_semantic_role() -> None:
    tasks = {
        "semantic": _task("semantic"),
        "diagnostic": _task("diagnostic", tags=["purpose.diagnostic", "version.v0_1"]),
        "cross": _task("cross"),
    }
    payload = _map(
        [
            _probe("semantic", "semantic_probe"),
            _probe("diagnostic", "diagnostic"),
            _probe("cross", "driver_cross_check"),
        ]
    )
    assert semantics.validate_capability_map(payload, tasks, "map.json") == []

    wrong = copy.deepcopy(payload)
    wrong["capabilities"][0]["probes"][1]["role"] = "semantic_probe"
    errors = semantics.validate_capability_map(wrong, tasks, "map.json")
    assert any("purpose.diagnostic task must use role `diagnostic`" in error for error in errors)


def test_semantic_summary_counts_correlated_probes_once() -> None:
    payload = _map(
        [
            _probe("semantic-a", "semantic_probe"),
            _probe("semantic-b", "semantic_probe"),
            _probe("cross", "driver_cross_check"),
            _probe("diagnostic", "diagnostic"),
        ]
    )
    snapshot = semantics.capability_map_snapshot(
        payload,
        ["semantic-a", "semantic-b", "cross", "diagnostic"],
        path="config/map.json",
        sha256="a" * 64,
    )
    manifest = {
        "selected_engines": ["moli", "lightpanda"],
        "l2_semantic_capability_map": snapshot,
        "resolved_tasks": [
            {"task_id": "semantic-a", "driver": "node_cdp_probe"},
            {"task_id": "semantic-b", "driver": "node_cdp_probe"},
            {"task_id": "cross", "driver": "framework_puppeteer"},
            {"task_id": "diagnostic", "driver": "node_cdp_probe"},
        ],
    }
    rows = [
        _row("semantic-a", "moli", "pass"),
        _row("semantic-b", "moli", "fail"),
        _row("cross", "moli", "pass"),
        _row("diagnostic", "moli", "pass"),
        _row("semantic-a", "lightpanda", "pass"),
        _row("semantic-b", "lightpanda", "pass"),
        _row("cross", "lightpanda", "fail"),
        _row("diagnostic", "lightpanda", "fail"),
    ]
    summary = semantics.summarize_semantic_results(manifest, rows)

    assert summary["by_engine"]["moli"]["total"] == 1
    assert summary["by_engine"]["moli"]["pass"] == 0
    assert summary["by_engine"]["lightpanda"]["total"] == 1
    assert summary["by_engine"]["lightpanda"]["pass"] == 1
    assert summary["driver_cross_checks"]["task_count"] == 1
    assert summary["diagnostics"]["task_count"] == 1
    assert summary["pairwise"]["moli__lightpanda"]["right_only"] == 1


def test_partial_capability_selection_is_missing_not_a_partial_score() -> None:
    payload = _map(
        [
            _probe("semantic-a", "semantic_probe"),
            _probe("semantic-b", "semantic_probe"),
        ]
    )
    snapshot = semantics.capability_map_snapshot(
        payload,
        ["semantic-a"],
        path="config/map.json",
        sha256="b" * 64,
    )
    manifest = {
        "selected_engines": ["moli"],
        "l2_semantic_capability_map": snapshot,
        "resolved_tasks": [{"task_id": "semantic-a", "driver": "node_cdp_probe"}],
    }
    summary = semantics.summarize_semantic_results(
        manifest, [_row("semantic-a", "moli", "pass")]
    )
    assert summary["complete_selection_capabilities"] == 0
    assert summary["by_engine"]["moli"]["total"] == 0
    assert summary["by_engine"]["moli"]["missing"] == 1


def test_scores_and_scorecard_lead_with_distinct_evaluation_axes(tmp_path: Path) -> None:
    payload = _map(
        [
            _probe("semantic", "semantic_probe"),
            _probe("cross", "driver_cross_check"),
        ]
    )
    snapshot = semantics.capability_map_snapshot(
        payload,
        ["semantic", "cross"],
        path="config/map.json",
        sha256="c" * 64,
    )
    manifest = {
        "run_id": "semantic-scorecard",
        "bench_id": "unit",
        "bench_version": "test",
        "score_eligible": True,
        "selected_engines": ["moli", "lightpanda"],
        "enabled_subsets": ["l2.web_platform"],
        "engines": {},
        "l2_semantic_capability_map": snapshot,
        "resolved_tasks": [
            {
                "task_id": "semantic",
                "driver": "node_cdp_probe",
                "semantic_capability": {"role": "semantic_probe"},
            },
            {
                "task_id": "cross",
                "driver": "framework_puppeteer",
                "semantic_capability": {"role": "driver_cross_check"},
            },
        ],
    }
    rows = [
        _row("semantic", "moli", "pass"),
        _row("semantic", "lightpanda", "fail"),
        _row("cross", "moli", "pass"),
        _row("cross", "lightpanda", "pass"),
    ]
    scores = runner_run.summarize_results(manifest, rows)
    assert "protocol_driver_compatibility" in scores["evaluation_axes"]
    assert (
        scores["evaluation_axes"]["web_platform_workflow_semantic_correctness"]
        ["by_engine"]["moli"]["total"]
        == 1
    )

    runner_run.write_scorecard(tmp_path, manifest, rows, scores)
    scorecard = (tmp_path / "scorecard.md").read_text(encoding="utf-8")
    assert "## L1 protocol / driver compatibility evidence" in scorecard
    assert "## L2 semantic correctness (capability-level)" in scorecard
    assert "`web.storage.example`" in scorecard
    assert "## L2 representative driver cross-checks" in scorecard
    assert "## L2 task-level evidence (non-headline)" in scorecard


def test_l2_flow_semantics_use_server_side_expected_answer_grading() -> None:
    expected = json.loads(
        (REPO_ROOT / "fixtures/v0_3/flow/expected_answers.fragment.json").read_text(
            encoding="utf-8"
        )
    )
    for path in sorted((REPO_ROOT / "tasks/L2").glob("*/v3_*_flow_*.json")):
        task = json.loads(path.read_text(encoding="utf-8"))
        assert task["grader"] == {
            "endpoint": "/__grade__/expected_answer",
            "kind": "server_side",
        }
        assert task["task_id"] in expected
        assert all("expected" not in check for check in task["driver"]["checks"])


def test_framework_calibration_tasks_are_l1_compatibility_evidence() -> None:
    paths = sorted((REPO_ROOT / "tasks/L1").glob("*/v3_*_cal_*.json"))
    assert len(paths) == 10
    for path in paths:
        task = json.loads(path.read_text(encoding="utf-8"))
        assert task["layer"] == "L1"
        assert task["subset_id"] in {"l1.playwright", "l1.puppeteer"}
        assert task["artifact_profile"] == "l1_standard"


def test_legacy_run_without_snapshot_reports_unavailable_axis(tmp_path: Path) -> None:
    manifest = {
        "run_id": "legacy",
        "bench_id": "unit",
        "bench_version": "test",
        "score_eligible": True,
        "selected_engines": ["moli"],
        "engines": {},
        "resolved_tasks": [{"task_id": "semantic", "driver": "node_cdp_probe"}],
    }
    rows = [_row("semantic", "moli", "pass")]
    scores = runner_run.summarize_results(manifest, rows)
    axis = scores["evaluation_axes"]["web_platform_workflow_semantic_correctness"]
    assert axis["available"] is False

    runner_run.write_scorecard(tmp_path, manifest, rows, scores)
    scorecard = (tmp_path / "scorecard.md").read_text(encoding="utf-8")
    assert "## L2 semantic correctness (capability-level)" not in scorecard
    assert "## L2 task-level evidence (non-headline)" in scorecard


def test_invalid_capability_map_value_raises_a_clear_error(tmp_path: Path) -> None:
    # PR #132 review: a non-string/empty `semantic_capability_map` must not
    # surface as a misleading file-not-found sentinel path.
    suite = {"layers": [{"layer_id": "L2", "semantic_capability_map": "   "}]}
    with pytest.raises(ValueError, match="non-empty string path"):
        semantics.capability_map_reference(tmp_path / "manifest.json", suite)
