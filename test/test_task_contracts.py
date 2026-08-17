"""Regression tests for task contracts that are easy to weaken accidentally."""

from __future__ import annotations

import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_debugger_stepout_requires_caller_pause_and_completion() -> None:
    task_path = REPO_ROOT / "tasks/L1/raw_cdp/v4_cdp_debugger_stepout.json"
    task = json.loads(task_path.read_text(encoding="utf-8"))
    steps = task["driver"]["steps"]

    setup_expression = steps[2]["params"]["expression"]
    assert "function outerTimer()" in setup_expression
    assert "function inner()" in setup_expression
    assert "return 40" in setup_expression
    assert "window.__v4done = inner() + 2" in setup_expression

    step_out_index = next(index for index, step in enumerate(steps) if step.get("method") == "Debugger.stepOut")
    assert steps[step_out_index + 1] == {
        "timeout_ms": 3000,
        "wait_for_event": "Debugger.resumed",
    }

    post_step_pause = steps[step_out_index + 2]
    assert post_step_pause == {
        "save_result_as": "step_pause",
        "timeout_ms": 3000,
        "wait_for_event": "Debugger.paused",
    }
    assert steps[step_out_index + 3] == {"method": "Debugger.resume"}

    completion_probe = steps[step_out_index + 4]
    assert completion_probe == {
        "method": "Runtime.evaluate",
        "params": {
            "expression": "window.__v4done",
            "returnByValue": True,
        },
        "save_as": "done",
    }

    checks = {check["label"]: check for check in task["grader"]["checks"]}
    assert checks["initial_pause_is_nested"] == {
        "expected": "inner",
        "kind": "saved_path_equals",
        "label": "initial_pause_is_nested",
        "name": "initial_pause",
        "path": "callFrames.0.functionName",
    }
    assert checks["step_pause_reason"] == {
        "expected": "step",
        "kind": "saved_path_equals",
        "label": "step_pause_reason",
        "name": "step_pause",
        "path": "reason",
    }
    assert checks["stepped_into_caller"] == {
        "expected": "outerTimer",
        "kind": "saved_path_equals",
        "label": "stepped_into_caller",
        "name": "step_pause",
        "path": "callFrames.0.functionName",
    }
    assert checks["caller_completed_after_resume"] == {
        "expected": 42,
        "kind": "value_equals",
        "label": "caller_completed_after_resume",
        "name": "done",
    }
