"""Integration tests for v0.2 raw_cdp primitives against FakeCDP.

Drives run_driver_attempt() with wait_for_event / expect_unsupported /
save_session_as / session-addressed steps and asserts the status truth table
plus session routing. FakeCDP now records inbound sessionIds and can emit
events via a reply's "__events__" list.
"""
from __future__ import annotations

import pytest

from runner import run as runner_run
from _fakes import make_resolved, make_task_dict, stub_browser

GATE_OFF = {"required": False, "status": "off", "chrome_attempt_ref": None}


def run_attempt(tmp_path, fake, task, engine="moli"):
    run_dir = tmp_path / "run"
    return runner_run.run_driver_attempt(
        run_dir,
        run_dir / "results.jsonl",
        "fake_run_v2",
        task,
        engine,
        1,
        "seed123",
        stub_browser(fake.port, engine=engine),
        GATE_OFF,
        score_eligible=True,
        fixture_base_url=None,
    )


def eval_result(value, value_type="string"):
    return {"result": {"result": {"type": value_type, "value": value}}}


# --- wait_for_event happy path ----------------------------------------------


def test_wait_for_event_observes_and_saves_event_params(tmp_path, fake_cdp):
    binding_event = {"method": "Runtime.bindingCalled", "params": {"name": "__x", "payload": "p-42"}}
    fake = fake_cdp(
        {
            "Runtime.addBinding": {"result": {}},
            # The evaluate response carries the binding event right behind it.
            "Runtime.evaluate": {"result": {}, "__events__": [binding_event]},
        }
    )
    task = make_resolved(
        task=make_task_dict(
            task_id="v2_wait_ok_001",
            driver={
                "kind": "raw_cdp",
                "steps": [
                    {"method": "Runtime.addBinding", "params": {"name": "__x"}},
                    {"method": "Runtime.evaluate", "params": {"expression": "window.__x('p-42')"}},
                    {"wait_for_event": "Runtime.bindingCalled", "match": {"name": "__x"}, "timeout_ms": 2000, "save_result_as": "binding"},
                ],
            },
            grader={
                "kind": "inline_assertions",
                "checks": [
                    {"kind": "event_observed", "name": "binding", "method": "Runtime.bindingCalled"},
                    {"kind": "saved_path_equals", "name": "binding", "path": "payload", "expected": "p-42"},
                ],
            },
        ),
        subset_gate="off",
    )
    result = run_attempt(tmp_path, fake, task)
    assert result["status"] == "pass", result.get("failure")


def test_wait_for_event_timeout_is_semantic_fail(tmp_path, fake_cdp):
    # No event ever arrives -> wait_for_event raises -> cdp_semantic fail
    # (kernel_workitem true for moli), NOT crash/infra.
    fake = fake_cdp({"Runtime.enable": {"result": {}}})
    task = make_resolved(
        task=make_task_dict(
            task_id="v2_wait_timeout_001",
            driver={
                "kind": "raw_cdp",
                "steps": [
                    {"method": "Runtime.enable"},
                    {"wait_for_event": "Runtime.bindingCalled", "timeout_ms": 300},
                ],
            },
            grader={"kind": "inline_assertions", "checks": [{"kind": "no_error"}]},
        ),
        subset_gate="off",
    )
    result = run_attempt(tmp_path, fake, task, engine="moli")
    assert result["status"] == "fail"
    assert result["failure"]["class"] == "cdp_semantic"
    assert result["failure"]["kernel_workitem"] is True


def test_optional_wait_for_event_timeout_is_swallowed(tmp_path, fake_cdp):
    fake = fake_cdp({"Runtime.enable": {"result": {}}})
    task = make_resolved(
        task=make_task_dict(
            task_id="v2_wait_optional_001",
            driver={
                "kind": "raw_cdp",
                "steps": [
                    {"method": "Runtime.enable"},
                    {"wait_for_event": "Runtime.bindingCalled", "timeout_ms": 200, "optional": True},
                ],
            },
            grader={"kind": "inline_assertions", "checks": [{"kind": "no_error"}]},
        ),
        subset_gate="off",
    )
    result = run_attempt(tmp_path, fake, task)
    assert result["status"] == "pass", result.get("failure")


def test_download_wait_ignores_in_progress_until_matching_completed(tmp_path, fake_cdp):
    guid = "DL-COMPLETE-1"
    behaviors = []

    def set_download_behavior(params):
        behaviors.append(params["behavior"])
        return {"result": {}}

    fake = fake_cdp(
        {
            "Browser.setDownloadBehavior": set_download_behavior,
            "Runtime.evaluate": {
                "result": {},
                "__events__": [
                    {
                        "method": "Browser.downloadWillBegin",
                        "params": {"guid": guid},
                    },
                    {
                        "method": "Browser.downloadProgress",
                        "params": {"guid": guid, "state": "inProgress"},
                    },
                    {
                        "method": "Browser.downloadProgress",
                        "params": {"guid": guid, "state": "completed"},
                    },
                ],
            }
        }
    )
    task = make_resolved(
        task=make_task_dict(
            task_id="v2_download_terminal_001",
            driver={
                "kind": "raw_cdp",
                "steps": [
                    {
                        "method": "Browser.setDownloadBehavior",
                        "session": "browser",
                        "params": {
                            "behavior": "allowAndName",
                            "downloadPath": "{artifact_dir}",
                            "eventsEnabled": True,
                        },
                    },
                    {"method": "Runtime.evaluate", "params": {"expression": "startDownload()"}},
                    {
                        "wait_for_event": "Browser.downloadWillBegin",
                        "save_result_as": "begin",
                    },
                    {
                        "wait_for_event": "Browser.downloadProgress",
                        "match": {
                            "guid": "{saved:begin.guid}",
                            "state": "completed",
                        },
                        "save_result_as": "terminal",
                    },
                ],
            },
            grader={
                "kind": "inline_assertions",
                "checks": [
                    {
                        "kind": "saved_path_equals",
                        "name": "terminal",
                        "path": "state",
                        "expected": "completed",
                    }
                ],
            },
        ),
        subset_gate="off",
    )

    result = run_attempt(tmp_path, fake, task)

    assert result["status"] == "pass", result.get("failure")
    assert behaviors == ["allowAndName", "default"]
    assert not any(
        request["method"] == "Browser.cancelDownload"
        for request in fake.requests
    )


@pytest.mark.parametrize("terminal_state", ["canceled", "completed"])
def test_cancel_wait_consumes_terminal_event_buffered_before_command_response(
    tmp_path, fake_cdp, terminal_state
):
    guid = f"DL-{terminal_state.upper()}-1"
    fake = fake_cdp(
        {
            "Runtime.evaluate": {
                "result": {},
                "__events__": [
                    {
                        "method": "Browser.downloadWillBegin",
                        "params": {"guid": guid},
                    }
                ],
            },
            "Browser.cancelDownload": {
                "result": {},
                "__events_before__": [
                    {
                        "method": "Browser.downloadProgress",
                        "params": {"guid": guid, "state": "inProgress"},
                    },
                    {
                        "method": "Browser.downloadProgress",
                        "params": {"guid": guid, "state": terminal_state},
                    },
                ],
            },
        }
    )
    task = make_resolved(
        task=make_task_dict(
            task_id=f"v2_cancel_terminal_{terminal_state}",
            driver={
                "kind": "raw_cdp",
                "steps": [
                    {"method": "Runtime.evaluate", "params": {"expression": "startDownload()"}},
                    {
                        "wait_for_event": "Browser.downloadWillBegin",
                        "save_result_as": "begin",
                    },
                    {
                        "method": "Browser.cancelDownload",
                        "session": "browser",
                        "params": {"guid": "{saved:begin.guid}"},
                    },
                    {
                        "wait_for_event": "Browser.downloadProgress",
                        "session": "browser",
                        "match": {
                            "guid": "{saved:begin.guid}",
                            "state": {
                                runner_run.EVENT_MATCH_ONE_OF: ["canceled", "completed"],
                            },
                        },
                        "save_result_as": "terminal",
                    },
                ],
            },
            grader={
                "kind": "inline_assertions",
                "checks": [
                    {
                        "kind": "saved_path_one_of",
                        "name": "terminal",
                        "path": "state",
                        "expected": ["canceled", "completed"],
                    }
                ],
            },
        ),
        subset_gate="off",
    )

    result = run_attempt(tmp_path, fake, task)

    assert result["status"] == "pass", result.get("failure")


def test_download_cleanup_cancels_and_drains_after_task_failure(
    tmp_path, fake_cdp
):
    guid = "DL-ACTIVE-FAILURE"
    behaviors = []

    def set_download_behavior(params):
        behaviors.append(params["behavior"])
        return {"result": {}}

    def evaluate(params):
        if params.get("expression") == "startDownload()":
            return {
                "result": {},
                "__events__": [
                    {
                        "method": "Browser.downloadWillBegin",
                        "params": {"guid": guid},
                    }
                ],
            }
        return {"error": {"message": "forced semantic failure"}}

    fake = fake_cdp(
        {
            "Browser.setDownloadBehavior": set_download_behavior,
            "Runtime.evaluate": evaluate,
            "Browser.cancelDownload": {
                "result": {},
                "__events_before__": [
                    {
                        "method": "Browser.downloadProgress",
                        "params": {"guid": guid, "state": "canceled"},
                    }
                ],
            },
        }
    )
    task = make_resolved(
        task=make_task_dict(
            task_id="v2_download_cleanup_failure",
            driver={
                "kind": "raw_cdp",
                "steps": [
                    {
                        "method": "Browser.setDownloadBehavior",
                        "session": "browser",
                        "params": {
                            "behavior": "allowAndName",
                            "downloadPath": "{artifact_dir}",
                            "eventsEnabled": True,
                        },
                    },
                    {
                        "method": "Runtime.evaluate",
                        "params": {"expression": "startDownload()"},
                    },
                    {
                        "method": "Runtime.evaluate",
                        "params": {"expression": "forceFailure()"},
                    },
                ],
            },
            grader={
                "kind": "inline_assertions",
                "checks": [{"kind": "no_error"}],
            },
        ),
        subset_gate="off",
    )

    result = run_attempt(tmp_path, fake, task)

    assert result["status"] == "fail"
    assert result["failure"]["class"] == "cdp_semantic"
    assert "forced semantic failure" in result["failure"]["detail"]
    assert behaviors == ["allowAndName", "default"]
    cancel_requests = [
        request
        for request in fake.requests
        if request["method"] == "Browser.cancelDownload"
    ]
    assert [request["params"]["guid"] for request in cancel_requests] == [guid]


def test_download_cleanup_pumps_begin_event_pending_after_command_response(
    tmp_path, fake_cdp
):
    guid = "DL-PENDING-AFTER-RESPONSE"
    fake = fake_cdp(
        {
            "Runtime.evaluate": {
                "result": {},
                # This frame follows the response. The next step fails while
                # resolving a local session, so no later command can buffer it.
                "__events__": [
                    {
                        "method": "Browser.downloadWillBegin",
                        "params": {"guid": guid},
                    }
                ],
            },
            "Browser.cancelDownload": {
                "result": {},
                "__events_before__": [
                    {
                        "method": "Browser.downloadProgress",
                        "params": {"guid": guid, "state": "canceled"},
                    }
                ],
            },
        }
    )
    task = make_resolved(
        task=make_task_dict(
            task_id="v2_download_cleanup_pending_begin",
            driver={
                "kind": "raw_cdp",
                "steps": [
                    {
                        "method": "Browser.setDownloadBehavior",
                        "session": "browser",
                        "params": {
                            "behavior": "allowAndName",
                            "downloadPath": "{artifact_dir}",
                            "eventsEnabled": True,
                        },
                    },
                    {
                        "method": "Runtime.evaluate",
                        "params": {"expression": "startDownload()"},
                    },
                    {
                        "method": "Runtime.evaluate",
                        "session": "missing-local-session",
                        "params": {"expression": "neverSent()"},
                    },
                ],
            },
            grader={
                "kind": "inline_assertions",
                "checks": [{"kind": "no_error"}],
            },
        ),
        subset_gate="off",
    )

    result = run_attempt(tmp_path, fake, task)

    assert result["status"] == "infra"
    assert "unknown session `missing-local-session`" in result["failure"]["detail"]
    cancel_requests = [
        request
        for request in fake.requests
        if request["method"] == "Browser.cancelDownload"
    ]
    assert [request["params"]["guid"] for request in cancel_requests] == [guid]
    assert [
        request["params"]["behavior"]
        for request in fake.requests
        if request["method"] == "Browser.setDownloadBehavior"
    ] == ["allowAndName", "default"]


def test_download_cleanup_reset_error_does_not_change_task_result(
    tmp_path, fake_cdp
):
    def set_download_behavior(params):
        if params.get("behavior") == "default":
            return {"error": {"message": "reset unavailable"}}
        return {"result": {}}

    fake = fake_cdp(
        {
            "Browser.setDownloadBehavior": set_download_behavior,
        }
    )
    task = make_resolved(
        task=make_task_dict(
            task_id="v2_download_cleanup_nonfatal",
            driver={
                "kind": "raw_cdp",
                "steps": [
                    {
                        "method": "Browser.setDownloadBehavior",
                        "session": "browser",
                        "params": {
                            "behavior": "allowAndName",
                            "downloadPath": "{artifact_dir}",
                            "eventsEnabled": True,
                        },
                    }
                ],
            },
            grader={
                "kind": "inline_assertions",
                "checks": [{"kind": "no_error"}],
            },
        ),
        subset_gate="off",
    )

    result = run_attempt(tmp_path, fake, task)

    assert result["status"] == "pass", result.get("failure")
    assert result["cdp_error_count"] == 0
    assert [
        request["params"]["behavior"]
        for request in fake.requests
        if request["method"] == "Browser.setDownloadBehavior"
    ] == ["allowAndName", "default"]


def test_download_behavior_is_reset_when_configuration_response_is_an_error(
    tmp_path, fake_cdp
):
    calls = []

    def set_download_behavior(params):
        calls.append(params["behavior"])
        if len(calls) == 1:
            return {"error": {"message": "configuration response failed"}}
        return {"result": {}}

    fake = fake_cdp(
        {
            "Browser.setDownloadBehavior": set_download_behavior,
        }
    )
    task = make_resolved(
        task=make_task_dict(
            task_id="v2_download_cleanup_config_response_failure",
            driver={
                "kind": "raw_cdp",
                "steps": [
                    {
                        "method": "Browser.setDownloadBehavior",
                        "session": "browser",
                        "params": {
                            "behavior": "allowAndName",
                            "downloadPath": "{artifact_dir}",
                            "eventsEnabled": True,
                        },
                    }
                ],
            },
            grader={
                "kind": "inline_assertions",
                "checks": [{"kind": "no_error"}],
            },
        ),
        subset_gate="off",
    )

    result = run_attempt(tmp_path, fake, task)

    assert result["status"] == "fail"
    assert "configuration response failed" in result["failure"]["detail"]
    assert calls == ["allowAndName", "default"]


def test_browser_download_behavior_reset_preserves_browser_context_id(
    tmp_path, fake_cdp
):
    fake = fake_cdp()
    task = make_resolved(
        task=make_task_dict(
            task_id="v2_browser_download_cleanup_context",
            driver={
                "kind": "raw_cdp",
                "steps": [
                    {
                        "method": "Browser.setDownloadBehavior",
                        "session": "browser",
                        "params": {
                            "behavior": "allowAndName",
                            "browserContextId": "CONTEXT-7",
                            "downloadPath": "{artifact_dir}",
                            "eventsEnabled": True,
                        },
                    }
                ],
            },
            grader={
                "kind": "inline_assertions",
                "checks": [{"kind": "no_error"}],
            },
        ),
        subset_gate="off",
    )

    result = run_attempt(tmp_path, fake, task)

    assert result["status"] == "pass", result.get("failure")
    requests = [
        request
        for request in fake.requests
        if request["method"] == "Browser.setDownloadBehavior"
    ]
    assert [request["params"]["behavior"] for request in requests] == [
        "allowAndName",
        "default",
    ]
    assert [request["params"]["browserContextId"] for request in requests] == [
        "CONTEXT-7",
        "CONTEXT-7",
    ]


def test_page_download_behavior_resets_on_the_configured_session(
    tmp_path, fake_cdp
):
    fake = fake_cdp()
    task = make_resolved(
        task=make_task_dict(
            task_id="v2_page_download_cleanup_session",
            driver={
                "kind": "raw_cdp",
                "steps": [
                    {
                        "method": "Page.setDownloadBehavior",
                        "session": "page",
                        "params": {
                            "behavior": "allow",
                            "downloadPath": "{artifact_dir}",
                        },
                    }
                ],
            },
            grader={
                "kind": "inline_assertions",
                "checks": [{"kind": "no_error"}],
            },
        ),
        subset_gate="off",
    )

    result = run_attempt(tmp_path, fake, task)

    assert result["status"] == "pass", result.get("failure")
    requests = [
        request
        for request in fake.requests
        if request["method"] == "Page.setDownloadBehavior"
    ]
    assert [request["params"]["behavior"] for request in requests] == [
        "allow",
        "default",
    ]
    assert requests[1]["sessionId"] == requests[0]["sessionId"]


# --- expect_unsupported negative probe --------------------------------------


def test_expect_unsupported_passes_on_clean_rejection(tmp_path, fake_cdp):
    fake = fake_cdp({"Page.getCookies": {"error": {"message": "'Page.getCookies' wasn't found"}}})
    task = make_resolved(
        task=make_task_dict(
            task_id="v2_negative_ok_001",
            driver={"kind": "raw_cdp", "steps": [{"method": "Page.getCookies", "expect_unsupported": True}]},
            grader={"kind": "inline_assertions", "checks": [{"kind": "unsupported_observed", "method": "Page.getCookies"}]},
        ),
        subset_gate="off",
    )
    result = run_attempt(tmp_path, fake, task)
    assert result["status"] == "pass", result.get("failure")


def test_expect_unsupported_fails_when_command_unexpectedly_succeeds(tmp_path, fake_cdp):
    fake = fake_cdp({"Page.getCookies": {"result": {"cookies": []}}})
    task = make_resolved(
        task=make_task_dict(
            task_id="v2_negative_bad_001",
            driver={"kind": "raw_cdp", "steps": [{"method": "Page.getCookies", "expect_unsupported": True}]},
            grader={"kind": "inline_assertions", "checks": [{"kind": "unsupported_observed", "method": "Page.getCookies"}]},
        ),
        subset_gate="off",
    )
    result = run_attempt(tmp_path, fake, task)
    assert result["status"] == "fail"
    assert result["failure"]["class"] == "cdp_semantic"


# --- session addressing + save_session_as -----------------------------------


def test_save_session_as_routes_later_step_to_captured_session(tmp_path, fake_cdp):
    fake = fake_cdp(
        {
            "Target.attachToTarget": {"result": {"sessionId": "CHILD-1"}},
            "Runtime.evaluate": eval_result("ok"),
        }
    )
    task = make_resolved(
        task=make_task_dict(
            task_id="v2_session_route_001",
            driver={
                "kind": "raw_cdp",
                "steps": [
                    {"method": "Target.attachToTarget", "session": "browser", "params": {"targetId": "T1", "flatten": True}, "save_session_as": "child"},
                    {"method": "Runtime.evaluate", "session": "child", "params": {"expression": "1"}, "save_as": "v"},
                ],
            },
            grader={"kind": "inline_assertions", "checks": [{"kind": "value_equals", "name": "v", "expected": "ok"}]},
        ),
        subset_gate="off",
    )
    result = run_attempt(tmp_path, fake, task)
    assert result["status"] == "pass", result.get("failure")
    # The Runtime.evaluate must have been addressed to the captured child session.
    evals = [r for r in fake.requests if r["method"] == "Runtime.evaluate" and r["params"].get("expression") == "1"]
    assert evals and evals[-1]["sessionId"] == "CHILD-1"


def test_save_result_as_exposes_full_result_for_saved_placeholders(tmp_path, fake_cdp):
    seen = {}

    def capture(params):
        seen["targetId"] = params.get("targetId")
        return {"result": {}}

    fake = fake_cdp(
        {
            "Target.createTarget": {"result": {"targetId": "TARGET-9"}},
            "Target.closeTarget": lambda params: capture(params),
        }
    )
    task = make_resolved(
        task=make_task_dict(
            task_id="v2_save_result_001",
            driver={
                "kind": "raw_cdp",
                "steps": [
                    {"method": "Target.createTarget", "session": "browser", "params": {"url": "about:blank"}, "save_result_as": "created"},
                    {"method": "Target.closeTarget", "session": "browser", "params": {"targetId": "{saved:created.targetId}"}},
                ],
            },
            grader={"kind": "inline_assertions", "checks": [{"kind": "no_error"}]},
        ),
        subset_gate="off",
    )
    result = run_attempt(tmp_path, fake, task)
    assert result["status"] == "pass", result.get("failure")
    assert seen["targetId"] == "TARGET-9"
