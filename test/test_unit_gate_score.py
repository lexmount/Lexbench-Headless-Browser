"""TESTING.md §5 (真值表 A) — gate policy resolution and score eligibility.

Covers:
  - resolve_gate_policy precedence: CLI > task > subset > suite > "off"
  - score_eligible_for_run: each false-reason path (§5.3)
  - should_include_score truth table (§5.4)
"""
from __future__ import annotations

import pytest

from runner import run as runner_run
from _fakes import make_resolved, make_task_dict, make_l2_task_dict

ALL_ENGINES = ["chrome", "moli", "lightpanda", "obscura"]


# --- resolve_gate_policy ----------------------------------------------------


def test_default_gate_is_off():
    task = make_resolved(subset_gate=None)
    assert runner_run.resolve_gate_policy(task, None, {}) == "off"


def test_suite_gate_beats_default():
    task = make_resolved(subset_gate=None)
    assert runner_run.resolve_gate_policy(task, None, {"chrome_gate": "off"}) == "off"


def test_subset_gate_beats_suite():
    task = make_resolved(subset_gate="best_effort")
    assert runner_run.resolve_gate_policy(task, None, {"chrome_gate": "off"}) == "best_effort"


def test_task_gate_beats_subset():
    task = make_resolved(task=make_task_dict(chrome_gate="off"), subset_gate="best_effort")
    assert runner_run.resolve_gate_policy(task, None, {"chrome_gate": "required"}) == "off"


def test_cli_gate_beats_task():
    task = make_resolved(task=make_task_dict(chrome_gate="off"), subset_gate="best_effort")
    assert runner_run.resolve_gate_policy(task, "required", {"chrome_gate": "off"}) == "required"


def test_invalid_gate_policy_raises():
    task = make_resolved()
    with pytest.raises(runner_run.BenchError, match="invalid Chrome baseline policy"):
        runner_run.resolve_gate_policy(task, "banana", {})


def test_invalid_task_gate_raises():
    task = make_resolved(task=make_task_dict(chrome_gate="sometimes"))
    with pytest.raises(runner_run.BenchError):
        runner_run.resolve_gate_policy(task, None, {})


# --- score_eligible_for_run -------------------------------------------------

SUITE_OK = {"fallback_allowed": False}


def test_eligible_baseline():
    tasks = [make_resolved(subset_gate="best_effort")]  # L1 main, weak gate is fine for L1
    eligible, reasons = runner_run.score_eligible_for_run(SUITE_OK, tasks, ALL_ENGINES, None, debug=False)
    assert eligible is True
    assert reasons == []


def test_reason_suite_fallback():
    tasks = [make_resolved()]
    eligible, reasons = runner_run.score_eligible_for_run({"fallback_allowed": True}, tasks, ALL_ENGINES, None, False)
    assert eligible is False
    assert any("fallback_allowed" in reason for reason in reasons)


def test_reason_debug_run():
    tasks = [make_resolved()]
    eligible, reasons = runner_run.score_eligible_for_run(SUITE_OK, tasks, ALL_ENGINES, None, debug=True)
    assert eligible is False
    assert "--debug run" in reasons


def test_reason_partial_engine_set():
    tasks = [make_resolved()]
    eligible, reasons = runner_run.score_eligible_for_run(SUITE_OK, tasks, ["chrome", "moli"], None, False)
    assert eligible is False
    assert "partial engine set" in reasons


def test_partial_engines_always_ineligible_now():
    # Lanes are gone: every task in a subset is scored, so a partial engine set is
    # ineligible regardless of the task (formerly-"diagnostic" tasks count too).
    tasks = [make_resolved(task=make_l2_task_dict(tags=["diagnostic"]))]
    eligible, reasons = runner_run.score_eligible_for_run(SUITE_OK, tasks, ["moli"], None, False)
    assert eligible is False
    assert "partial engine set" in reasons


def test_l2_main_with_off_gate_is_eligible():
    tasks = [make_resolved(task=make_l2_task_dict(), subset_gate="required")]
    eligible, reasons = runner_run.score_eligible_for_run(SUITE_OK, tasks, ALL_ENGINES, "off", False)
    assert eligible is True
    assert reasons == []


def test_independent_mode_with_off_gate_is_eligible():
    # Independent mode also accepts the default off gate; all four engines run
    # and are scored on their own.
    tasks = [make_resolved(task=make_l2_task_dict(), subset_gate="required")]
    eligible, reasons = runner_run.score_eligible_for_run(
        SUITE_OK, tasks, ALL_ENGINES, "off", False, score_mode="independent"
    )
    assert eligible is True
    assert reasons == []


def test_l1_main_with_best_effort_gate_is_eligible():
    tasks = [make_resolved(subset_gate="best_effort")]
    eligible, _ = runner_run.score_eligible_for_run(SUITE_OK, tasks, ALL_ENGINES, None, False)
    assert eligible is True


def test_candidate_only_mode_is_eligible_when_chrome_gate_is_off():
    tasks = [make_resolved(task=make_l2_task_dict(), subset_gate="required")]
    eligible, reasons = runner_run.score_eligible_for_run(
        SUITE_OK,
        tasks,
        ["moli", "lightpanda", "obscura"],
        "off",
        False,
    )
    assert eligible is True
    assert reasons == []


def test_candidate_only_mode_requires_chrome_gate_off():
    tasks = [make_resolved(task=make_l2_task_dict(), subset_gate="required")]
    eligible, reasons = runner_run.score_eligible_for_run(
        SUITE_OK,
        tasks,
        ["moli", "lightpanda", "obscura"],
        None,
        False,
    )
    assert eligible is False
    assert "partial engine set" in reasons


# --- should_include_score ---------------------------------------------------


def _result(status="pass", fallback_used=False):
    return {"status": status, "fallback_used": fallback_used}


def test_include_score_happy_path():
    task = make_resolved()
    assert runner_run.should_include_score(_result("pass"), task, "moli", True) is True
    assert runner_run.should_include_score(_result("pass"), task, "lightpanda", True) is True


def test_real_failures_stay_in_denominator():
    # fail/unsupported/timeout/crash all count toward the score denominator.
    task = make_resolved()
    for status in ("fail", "unsupported", "timeout", "crash"):
        assert runner_run.should_include_score(_result(status), task, "moli", True) is True


def test_exclude_when_run_not_score_eligible():
    task = make_resolved()
    assert runner_run.should_include_score(_result("pass"), task, "moli", False) is False


def test_exclude_chrome_in_default_mode():
    # Default baseline-checked mode: Chrome may run, but it is not a scored engine.
    task = make_resolved()
    assert runner_run.should_include_score(_result("pass"), task, "chrome", True) is False


def test_free_form_tags_do_not_silently_change_score_policy():
    # L2 exclusions come from the validated semantic capability role, not an
    # unvalidated free-form tag in an ad-hoc/legacy task.
    task = make_resolved(task=make_l2_task_dict(tags=["diagnostic"]))
    assert runner_run.should_include_score(_result("pass"), task, "moli", True) is True


def test_independent_mode_scores_all_four_engines():
    task = make_resolved()
    for engine in ("chrome", "moli", "lightpanda", "obscura"):
        assert runner_run.should_include_score(_result("pass"), task, engine, True, "independent") is True


def test_exclude_chrome_gate_fail_status():
    task = make_resolved()
    assert runner_run.should_include_score(_result("chrome_gate_fail"), task, "moli", True) is False


def test_exclude_infra_status():
    task = make_resolved()
    assert runner_run.should_include_score(_result("infra"), task, "moli", True) is False


def test_exclude_fallback_used():
    task = make_resolved()
    assert runner_run.should_include_score(_result("pass", fallback_used=True), task, "moli", True) is False


def test_purpose_diagnostic_tag_excludes_score_for_every_engine():
    # A task the oracle cannot validate is evidence for all engines and a score
    # unit for none.
    task = make_resolved(task=make_task_dict(tags=["purpose.diagnostic", "version.v0_1"]))
    for engine in ALL_ENGINES:
        assert runner_run.should_include_score(_result("pass"), task, engine, True) is False
