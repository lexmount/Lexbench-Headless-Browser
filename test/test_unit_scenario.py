"""Unit tests for scenario/adapter expansion.

Covers spec validation, per-driver expansion (the kind swap that turns one
driver-agnostic scenario into N bindings), and the sync check that keeps the
committed generated task files in lockstep with their specs.
"""
from __future__ import annotations

import json

import pytest

from runner import scenario as sc


def _spec(**overrides):
    base = {
        "scenario_id": "demo_read",
        "family": "navigation_lifecycle",
        "title": "Demo read",
        "description": "Navigate and read a heading.",
        "layer": "L1",
        "scene": {"kind": "self_hosted_fixture", "url": "/l1/core"},
        "steps": [
            {"op": "new_page"},
            {"op": "goto", "url": "{fixture_url}"},
            {"op": "text_content", "selector": "#title", "save_as": "answer"},
        ],
        "checks": [{"kind": "saved_truthy", "name": "answer"}],
        "cdp_anchors": ["cdp.page.navigate"],
        "expected_answer": {"mode": "equals", "expected": "CDP Core Fixture"},
        "drivers": ["playwright", "puppeteer"],
        "driver_skips": {
            key: "unit-test skip" for key in sc.DRIVER_REGISTRY if key not in ("playwright", "puppeteer")
        },
    }
    base.update(overrides)
    return base


# --- validation --------------------------------------------------------------


def test_valid_spec_has_no_errors():
    assert sc.validate_scenario(_spec()) == []


def test_missing_scenario_id_flagged():
    spec = _spec()
    del spec["scenario_id"]
    assert any("scenario_id" in e for e in sc.validate_scenario(spec))


def test_unknown_driver_flagged():
    assert any("not in DRIVER_REGISTRY" in e for e in sc.validate_scenario(_spec(drivers=["not_a_real_driver"])))


def test_empty_steps_flagged():
    assert any("steps must be a non-empty list" in e for e in sc.validate_scenario(_spec(steps=[])))


def test_step_without_op_flagged():
    assert any("must be an object with a string `op`" in e for e in sc.validate_scenario(_spec(steps=[{"selector": "#x"}])))


def test_unknown_launch_profile_flagged():
    errors = sc.validate_scenario(_spec(launch_profile="unknown"))
    assert any("launch_profile `unknown`" in error for error in errors)


@pytest.mark.parametrize(
    "field",
    ["source_reference", "migration_notes", "expected_status_claim"],
)
def test_historical_scenario_metadata_is_rejected(field):
    errors = sc.validate_scenario(_spec(**{field: "old"}))
    assert any(f"obsolete field `{field}` is not allowed" in error for error in errors)


# --- expansion ---------------------------------------------------------------


def test_expand_swaps_driver_kind():
    pw = sc.expand_scenario(_spec(), "playwright")
    pp = sc.expand_scenario(_spec(), "puppeteer")
    assert pw["driver"]["kind"] == "framework_playwright"
    assert pp["driver"]["kind"] == "framework_puppeteer"
    # The intent (steps/checks) is identical across bindings — only the kind and
    # the driver-namespaced metadata differ.
    assert pw["driver"]["steps"] == pp["driver"]["steps"]


def test_expand_task_id_and_subset():
    pw = sc.expand_scenario(_spec(), "playwright")
    assert pw["task_id"] == "sc_demo_read__pw"
    assert pw["subset_id"] == "l1.playwright"
    assert pw["layer"] == "L1"
    assert pw[sc.GENERATED_MARKER] == "tasks/scenarios/demo_read.scenario.json"
    assert pw["title"] == "Demo read (playwright)"
    assert pw["description"] == "Navigate and read a heading."
    assert "source_reference" not in pw


def test_expand_features_are_driver_namespaced_plus_anchors():
    pw = sc.expand_scenario(_spec(), "playwright")
    assert "fw.playwright.op.goto" in pw["features"]
    assert "fw.playwright.op.text_content" in pw["features"]
    assert "cdp.page.navigate" in pw["features"]  # anchor carried through
    pp = sc.expand_scenario(_spec(), "puppeteer")
    assert "fw.puppeteer.op.goto" in pp["features"]


def test_expand_carries_version_and_family_tags():
    pw = sc.expand_scenario(_spec(), "playwright")
    assert "version.v0_4" in pw["tags"]
    assert "family.navigation_lifecycle" in pw["tags"]


def test_expand_carries_explicit_launch_profile():
    pw = sc.expand_scenario(_spec(launch_profile="all_resources"), "playwright")
    assert pw["launch_profile"] == "all_resources"


def test_render_is_deterministic():
    task = sc.expand_scenario(_spec(), "playwright")
    a = sc.render_task_json(task)
    b = sc.render_task_json(json.loads(a))
    assert a == b
    assert a.endswith("\n")


# --- sync check --------------------------------------------------------------


def test_repo_scenarios_are_in_sync():
    # The committed generated task files must match their specs exactly.
    in_sync, diffs = sc.check_sync()
    assert in_sync, f"generated scenario tasks are out of sync: {diffs}"


def test_stale_file_detected(tmp_path, monkeypatch):
    # Point the generator at a temp scenario + driver dir, generate, then mutate
    # a generated file and confirm check_sync flags it stale.
    spec = _spec(scenario_id="tmp_demo")
    scen_dir = tmp_path / "scenarios"
    scen_dir.mkdir()
    (scen_dir / "tmp_demo.scenario.json").write_text(json.dumps(spec))
    drv_dir = tmp_path / "pw"
    drv_dir.mkdir()
    monkeypatch.setattr(sc, "BENCH_ROOT", tmp_path)
    monkeypatch.setattr(sc, "SCENARIOS_DIR", scen_dir)
    monkeypatch.setitem(sc.DRIVER_REGISTRY["playwright"], "dir", "pw")
    monkeypatch.setitem(sc.DRIVER_REGISTRY["puppeteer"], "dir", "pw")
    specs = sc.load_scenarios(scen_dir)
    sc.generate(specs)
    in_sync, _ = sc.check_sync(specs)
    assert in_sync
    gen = next(drv_dir.glob("sc_tmp_demo__pw.json"))
    gen.write_text(gen.read_text().replace("Demo read", "Tampered"))
    in_sync, diffs = sc.check_sync(specs)
    assert not in_sync
    assert any("stale" in d for d in diffs)


# --- thin-client driver expansion ---------------------------------------------


def test_expand_chrome_remote_interface_column():
    spec = _spec(drivers=["playwright", "puppeteer", "chrome_remote_interface"])
    cri = sc.expand_scenario(spec, "chrome_remote_interface")
    assert cri["driver"]["kind"] == "thin_chrome_remote_interface"
    assert cri["task_id"] == "sc_demo_read__cri"
    assert cri["subset_id"] == "l1.chrome_remote_interface"
    assert "fw.chrome_remote_interface.op.goto" in cri["features"]
    assert "cdp.page.navigate" in cri["features"]
    # The scenario intent is byte-identical across the framework and thin
    # client bindings — only kind and driver-namespaced metadata differ.
    pw = sc.expand_scenario(spec, "playwright")
    assert cri["driver"]["steps"] == pw["driver"]["steps"]
    assert cri["driver"]["checks"] == pw["driver"]["checks"]


def test_expand_selenium_marks_engine_native_transport():
    selenium = sc.expand_scenario(_spec(), "selenium")
    assert selenium["driver"]["kind"] == "webdriver_selenium"
    assert selenium["driver"]["transport_policy"] == "engine_native"
    assert selenium["task_version"] == 2


def test_every_registry_column_is_runnable():
    # Every DRIVER_REGISTRY kind must be dispatchable by the runner: either a
    # framework kind (framework_probe.js) or a registered scenario adapter.
    from runner import run as runner_run

    for key, reg in sc.DRIVER_REGISTRY.items():
        kind = reg["kind"]
        known = kind in runner_run.FRAMEWORK_DRIVER_KINDS or kind in runner_run.SCENARIO_ADAPTER_KINDS
        assert known, f"driver `{key}` kind `{kind}` has no runner dispatch"
        if kind in runner_run.SCENARIO_ADAPTER_KINDS:
            spec = runner_run.SCENARIO_ADAPTER_KINDS[kind]
            assert (runner_run.BENCH_ROOT / spec["script"]).exists(), f"driver `{key}`: adapter script missing"


# --- server-side grading and skip completeness -------------------------------


def test_expected_answer_required():
    spec = _spec()
    spec.pop("expected_answer")
    assert any("expected_answer is required" in e for e in sc.validate_scenario(spec))


def test_expected_answer_needs_answer_save():
    spec = _spec()
    spec["steps"][-1]["save_as"] = "not_answer"
    spec["checks"] = []
    assert any("save_as `answer`" in e for e in sc.validate_scenario(spec))


def test_missing_driver_without_skip_flagged():
    spec = _spec()
    spec["driver_skips"] = {}
    assert any("binding or an explicit skip-with-reason" in e for e in sc.validate_scenario(spec))


def test_driver_both_bound_and_skipped_flagged():
    spec = _spec()
    spec["driver_skips"]["playwright"] = "conflict"
    assert any("both bound and skipped" in e for e in sc.validate_scenario(spec))


def test_expand_emits_server_side_grader():
    task = sc.expand_scenario(_spec(), "playwright")
    assert task["grader"] == {"checks": ["answer_matches_expected"], "endpoint": sc.GRADE_ENDPOINT, "kind": "server_side"}


def test_expected_fragment_covers_all_bindings():
    import json as _json

    spec = _spec()
    content = _json.loads(sc.expected_fragment_content([spec]))
    assert content["sc_demo_read__pw"] == {"mode": "equals", "expected": "CDP Core Fixture"}
    assert content["sc_demo_read__pp"] == {"mode": "equals", "expected": "CDP Core Fixture"}
    # skipped drivers get no expectation entry
    assert not any(k.endswith("__rod") for k in content)
