"""Contract tests for driver-axis primitives."""
from __future__ import annotations

import json
from pathlib import Path

from runner import scenario as sc


ROOT = Path(__file__).resolve().parents[1]
SCENARIO_IDS = {
    "agentloop_ax_role_name_ids",
    "agentloop_ax_id_stable_across_mutation",
    "computed_style_breadth",
    "multi_tab_collect",
    "form_focus_active_element",
}


def load_spec(scenario_id: str) -> dict:
    path = ROOT / "tasks" / "scenarios" / f"{scenario_id}.scenario.json"
    return json.loads(path.read_text())


def test_issue_106_scenarios_use_the_existing_driver_axis() -> None:
    for scenario_id in SCENARIO_IDS:
        spec = load_spec(scenario_id)
        assert sc.validate_scenario(spec) == []
        assert set(spec["drivers"]) | set(spec.get("driver_skips", {})) == set(
            sc.DRIVER_REGISTRY
        )


def test_observation_family_stays_a_diagnostic_lane() -> None:
    """#106 adds probes to the driver axis without changing the scoring rubric.

    The observation family reports what an engine exposes, not whether it can
    perform a workflow, so every scenario in it is diagnostic evidence and is
    excluded from scoring for all engines. Scoring these would silently make the
    family a scored lane: `computed_style_breadth` asks for 100 computed
    properties, which Chrome clears and the three candidate engines do not, so
    it would subtract from every candidate on evidence that says only "not
    Blink". The action-family additions stay scored like their siblings.
    """
    for path in (ROOT / "tasks" / "scenarios").glob("*.scenario.json"):
        spec = json.loads(path.read_text())
        if spec.get("family") == "observation":
            assert spec.get("diagnostic") is True, spec["scenario_id"]
        else:
            assert not spec.get("diagnostic"), spec["scenario_id"]


def test_ax_snapshot_is_bound_everywhere_a_snapshot_is_available() -> None:
    expected = set(sc.DRIVER_REGISTRY) - {"selenium"}
    for scenario_id in ("obs_ax_snapshot_heading", "obs_ax_snapshot_label"):
        spec = load_spec(scenario_id)
        assert set(spec["drivers"]) == expected
        assert set(spec["driver_skips"]) == {"selenium"}
        assert "adapter op vocabulary has no accessibility-tree op" not in json.dumps(
            spec
        )


def test_identity_and_computed_style_have_api_specific_skips() -> None:
    identity = load_spec("agentloop_ax_role_name_ids")
    assert set(identity["driver_skips"]) == {
        "chrome_devtools_mcp",
        "agent_browser",
        "selenium",
    }
    assert "backendDOMNodeId" in identity["driver_skips"]["chrome_devtools_mcp"]
    assert "backendDOMNodeId" in identity["driver_skips"]["agent_browser"]

    computed = load_spec("computed_style_breadth")
    assert set(computed["driver_skips"]) == {
        "chrome_devtools_mcp",
        "agent_browser",
        "selenium",
    }
    reasons = list(computed["driver_skips"].values())
    assert len(set(reasons)) == len(reasons)
    assert all("CSS" in reason or "computed" in reason for reason in reasons)


def test_multi_tab_and_focus_cover_every_driver() -> None:
    expected = set(sc.DRIVER_REGISTRY)
    assert set(load_spec("multi_tab_collect")["drivers"]) == expected
    assert set(load_spec("form_focus_active_element")["drivers"]) == expected


def test_adapter_sources_expose_issue_106_ops() -> None:
    snapshot_sources = [
        "runner/scripts/framework_probe.js",
        "runner/scripts/adapters/cri_adapter.js",
        "runner/scripts/adapters/cdp_use_adapter.py",
        "runner/scripts/adapters/pydoll_adapter.py",
        "runner/scripts/adapters/stagehand_adapter.js",
        "runner/scripts/adapters/cdt_mcp_adapter.js",
        "runner/scripts/adapters/chromedp_adapter/main.go",
        "runner/scripts/adapters/rod_adapter/main.go",
        "runner/scripts/adapters/chromiumoxide_adapter/src/main.rs",
        "runner/scripts/adapters/ferrum_adapter.rb",
        "runner/scripts/adapters/ab_scenario_adapter.js",
    ]
    for relative in snapshot_sources:
        assert "ax_snapshot" in (ROOT / relative).read_text(), relative

    raw_sources = [
        source
        for source in snapshot_sources
        if source
        not in {
            "runner/scripts/adapters/cdt_mcp_adapter.js",
            "runner/scripts/adapters/ab_scenario_adapter.js",
        }
    ]
    for relative in raw_sources:
        text = (ROOT / relative).read_text()
        assert "ax_node_identity" in text, relative
        assert "computed_style_breadth" in text, relative

    assert "ax_node_identity" in (
        ROOT / "runner/scripts/adapters/cdt_mcp_adapter.js"
    ).read_text()


def test_fixture_has_stable_ax_and_computed_style_targets() -> None:
    fixture = (ROOT / "fixtures/v0_4/obs/obs.html").read_text()
    assert 'id="ax-stable"' in fixture
    assert 'aria-label="ax-stable-63"' in fixture
    assert 'id="computed-style-target"' in fixture
