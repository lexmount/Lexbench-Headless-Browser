"""Unit tests for the v0.2 raw_cdp primitives (pure functions, no server).

Covers:
  - event_params_match (recursive subset matcher used by wait_for_event + array_contains)
  - grade_inline_check new kinds: event_observed / unsupported_observed /
    saved_path_equals / saved_path_truthy / saved_path_contains /
    array_length / array_contains
  - validate_raw_cdp_steps schema rules for the new step vocabulary
"""
from __future__ import annotations

import json

from runner import run as runner_run
from _fakes import make_task_dict


# --- event_params_match ------------------------------------------------------


def test_event_params_match_none_matches_anything():
    assert runner_run.event_params_match({"a": 1}, None) is True
    assert runner_run.event_params_match([], None) is True


def test_event_params_match_scalar_and_nested_subset():
    params = {"name": "b", "targetInfo": {"type": "page", "targetId": "T1", "url": "x"}}
    assert runner_run.event_params_match(params, {"name": "b"}) is True
    assert runner_run.event_params_match(params, {"targetInfo": {"type": "page"}}) is True
    assert runner_run.event_params_match(params, {"targetInfo": {"targetId": "T1"}}) is True


def test_event_params_match_rejects_missing_or_wrong():
    params = {"name": "b", "targetInfo": {"type": "page"}}
    assert runner_run.event_params_match(params, {"name": "c"}) is False
    assert runner_run.event_params_match(params, {"missing": 1}) is False
    assert runner_run.event_params_match(params, {"targetInfo": {"type": "iframe"}}) is False


def test_event_params_match_list_index_wise():
    assert runner_run.event_params_match([{"a": 1}, {"b": 2}], [{"a": 1}, {}]) is True
    assert runner_run.event_params_match([{"a": 1}], [{"a": 1}, {"b": 2}]) is False  # length differs


def test_event_params_match_one_of_operator():
    match = {
        "guid": "DL-1",
        "state": {runner_run.EVENT_MATCH_ONE_OF: ["canceled", "completed"]},
    }
    assert runner_run.event_params_match({"guid": "DL-1", "state": "canceled"}, match) is True
    assert runner_run.event_params_match({"guid": "DL-1", "state": "completed"}, match) is True
    assert runner_run.event_params_match({"guid": "DL-1", "state": "inProgress"}, match) is False
    assert runner_run.event_params_match({"guid": "DL-2", "state": "completed"}, match) is False


# --- grade_inline_check: event_observed / unsupported_observed ---------------


def test_event_observed_check():
    saved = {"__events_observed__": ["Fetch.requestPaused"], "paused": {"requestId": "R1"}}
    ok, _ = runner_run.grade_inline_check(
        {"kind": "event_observed", "name": "paused", "method": "Fetch.requestPaused"}, saved
    )
    assert ok is True
    ok2, _ = runner_run.grade_inline_check(
        {"kind": "event_observed", "name": "paused", "method": "Never.happened"}, saved
    )
    assert ok2 is False


def test_unsupported_observed_check():
    saved = {"__unsupported__": {"Page.getCookies": {"message": "'Page.getCookies' wasn't found", "class": "unsupported"}}}
    ok, _ = runner_run.grade_inline_check({"kind": "unsupported_observed", "method": "Page.getCookies"}, saved)
    assert ok is True
    miss, _ = runner_run.grade_inline_check({"kind": "unsupported_observed", "method": "Page.enable"}, saved)
    assert miss is False


# --- grade_inline_check: saved_path_* ---------------------------------------


def test_saved_path_equals_and_truthy_and_contains():
    saved = {"att": {"targetInfo": {"type": "page", "url": "http://x/l1/core"}}}
    saved["att__raw"] = saved["att"]
    assert runner_run.grade_inline_check(
        {"kind": "saved_path_equals", "name": "att", "path": "targetInfo.type", "expected": "page"}, saved
    )[0] is True
    assert runner_run.grade_inline_check(
        {"kind": "saved_path_equals", "name": "att", "path": "targetInfo.type", "expected": "iframe"}, saved
    )[0] is False
    assert runner_run.grade_inline_check(
        {"kind": "saved_path_truthy", "name": "att", "path": "targetInfo.url"}, saved
    )[0] is True
    assert runner_run.grade_inline_check(
        {"kind": "saved_path_contains", "name": "att", "path": "targetInfo.url", "expected": "/l1/core"}, saved
    )[0] is True


def test_saved_path_one_of():
    saved = {"terminal": {"state": "canceled"}, "terminal__raw": {"state": "canceled"}}
    assert runner_run.grade_inline_check(
        {
            "kind": "saved_path_one_of",
            "name": "terminal",
            "path": "state",
            "expected": ["canceled", "completed"],
        },
        saved,
    )[0] is True
    assert runner_run.grade_inline_check(
        {
            "kind": "saved_path_one_of",
            "name": "terminal",
            "path": "state",
            "expected": ["completed"],
        },
        saved,
    )[0] is False


# --- grade_inline_check: array_length / array_contains ----------------------


def test_array_length_expected_min_max():
    nodes = [{"role": {"value": "button"}}, {"role": {"value": "link"}}, {"role": {"value": "heading"}}]
    saved = {"ax": {"nodes": nodes}, "ax__raw": {"nodes": nodes}}
    assert runner_run.grade_inline_check({"kind": "array_length", "name": "ax", "path": "nodes", "expected": 3}, saved)[0] is True
    assert runner_run.grade_inline_check({"kind": "array_length", "name": "ax", "path": "nodes", "min": 2}, saved)[0] is True
    assert runner_run.grade_inline_check({"kind": "array_length", "name": "ax", "path": "nodes", "max": 2}, saved)[0] is False
    # non-array path -> fail, not crash
    assert runner_run.grade_inline_check({"kind": "array_length", "name": "ax", "path": "missing"}, saved)[0] is False


def test_array_contains_scalar_and_subset():
    nodes = [{"role": {"value": "button"}}, {"role": {"value": "link"}}]
    saved = {"ax": {"nodes": nodes}, "ax__raw": {"nodes": nodes}}
    assert runner_run.grade_inline_check(
        {"kind": "array_contains", "name": "ax", "path": "nodes", "expected": {"role": {"value": "button"}}}, saved
    )[0] is True
    assert runner_run.grade_inline_check(
        {"kind": "array_contains", "name": "ax", "path": "nodes", "expected": {"role": {"value": "textbox"}}}, saved
    )[0] is False
    scalars = {"xs": [1, 2, 3], "xs__raw": [1, 2, 3]}
    assert runner_run.grade_inline_check({"kind": "array_contains", "name": "xs", "expected": 2}, scalars)[0] is True
    assert runner_run.grade_inline_check({"kind": "array_contains", "name": "xs", "expected": 9}, scalars)[0] is False


# --- validate_raw_cdp_steps --------------------------------------------------


def _steps_errors(steps):
    return runner_run.validate_raw_cdp_steps("tasks/unit/x.json", steps)


def test_validate_steps_accepts_v2_vocabulary():
    steps = [
        {"method": "Target.createTarget", "session": "browser", "params": {}, "save_result_as": "c"},
        {"wait_for_event": "Target.attachedToTarget", "match": {"targetInfo": {"type": "page"}}, "timeout_ms": 2000, "save_session_as": "child"},
        {
            "wait_for_event": "Browser.downloadProgress",
            "match": {"state": {runner_run.EVENT_MATCH_ONE_OF: ["canceled", "completed"]}},
        },
        {"method": "Runtime.evaluate", "session": "child", "params": {"expression": "1"}, "save_as": "v"},
        {"method": "Page.getCookies", "expect_unsupported": True},
        {"sleep_ms": 100},
    ]
    assert _steps_errors(steps) == []


def test_validate_steps_rejects_unknown_session():
    steps = [{"method": "Runtime.evaluate", "session": "ghost", "params": {}}]
    errors = _steps_errors(steps)
    assert any("unknown session `ghost`" in e for e in errors)


def test_validate_steps_rejects_empty_and_conflicting_step():
    assert any("must declare one of" in e for e in _steps_errors([{"params": {}}]))
    assert any("cannot be both" in e for e in _steps_errors([{"method": "X", "wait_for_event": "Y"}]))


def test_validate_steps_type_and_expect_unsupported_rules():
    assert any("wait_for_event must be a string" in e for e in _steps_errors([{"wait_for_event": 3}]))
    assert any("timeout_ms must be a number" in e for e in _steps_errors([{"wait_for_event": "E", "timeout_ms": "soon"}]))
    assert any("expect_unsupported is only valid on a command step" in e for e in _steps_errors([{"wait_for_event": "E", "expect_unsupported": True}]))


def test_validate_steps_rejects_malformed_one_of_matcher():
    empty = _steps_errors(
        [{"wait_for_event": "E", "match": {"state": {runner_run.EVENT_MATCH_ONE_OF: []}}}]
    )
    assert any("$one_of must be a non-empty list" in error for error in empty)
    siblings = _steps_errors(
        [
            {
                "wait_for_event": "E",
                "match": {
                    "state": {
                        runner_run.EVENT_MATCH_ONE_OF: ["completed"],
                        "other": True,
                    }
                },
            }
        ]
    )
    assert any("cannot be combined with sibling keys" in error for error in siblings)


def test_validate_task_surfaces_step_errors_through_validate_manifest(bench_factory):
    bad = make_task_dict(
        task_id="v2_bad_step_001",
        driver={"kind": "raw_cdp", "steps": [{"method": "Runtime.evaluate", "session": "nope"}]},
    )
    manifest_path = bench_factory(l1_tasks=[bad])
    _suite, _tasks, errors = runner_run.validate_manifest(manifest_path)
    assert any("unknown session `nope`" in e for e in errors)


def test_download_tasks_require_matching_terminal_progress_events():
    tasks_dir = runner_run.BENCH_ROOT / "tasks" / "L1" / "raw_cdp"
    specs = {
        "v2_t2_page_download_events.json": (
            "Page.downloadProgress",
            "dlb",
            "dlp",
            "completed",
        ),
        "v2_t2_browser_download.json": (
            "Browser.downloadProgress",
            "dwb",
            "dprog",
            "completed",
        ),
    }
    for filename, (method, begin_name, terminal_name, state) in specs.items():
        task = json.loads((tasks_dir / filename).read_text(encoding="utf-8"))
        terminal = next(step for step in task["driver"]["steps"] if step.get("save_result_as") == terminal_name)
        assert terminal["wait_for_event"] == method
        assert terminal.get("optional") is not True
        assert terminal["match"] == {
            "guid": f"{{saved:{begin_name}.guid}}",
            "state": state,
        }
        assert {
            "kind": "saved_path_equals",
            "name": terminal_name,
            "path": "state",
            "expected": state,
        } in task["grader"]["checks"]
        expected_feature = (
            "cdp.page.download_progress"
            if method.startswith("Page.")
            else "cdp.browser.download_progress"
        )
        assert expected_feature in task["features"]

    cancel = json.loads(
        (tasks_dir / "v2_t2_browser_cancel_download.json").read_text(encoding="utf-8")
    )
    terminal = next(
        step for step in cancel["driver"]["steps"] if step.get("save_result_as") == "dterminal"
    )
    assert terminal.get("optional") is not True
    assert terminal["match"] == {
        "guid": "{saved:dwb.guid}",
        "state": {runner_run.EVENT_MATCH_ONE_OF: ["canceled", "completed"]},
    }
    assert {
        "kind": "saved_path_one_of",
        "name": "dterminal",
        "path": "state",
        "expected": ["canceled", "completed"],
    } in cancel["grader"]["checks"]
    assert "cdp.browser.download_progress" in cancel["features"]
