"""Contract-only Moli layout choices must not depend on measured outcomes."""

from pathlib import Path
import json

from runner.moli_layout_policy import choose_layout


ROOT = Path(__file__).resolve().parents[1]


def task(task_id: str) -> dict:
    paths = list((ROOT / "tasks").rglob(f"{task_id}.json"))
    assert len(paths) == 1
    return json.loads(paths[0].read_text(encoding="utf-8"))


def test_coordinate_input_and_geometry_use_layout():
    for task_id in (
        "v2_diag_dispatchmouse_geometry",
        "pw_raw_input_dispatchmouseevent",
        "v4_cdp_input_dispatchtouchevent",
        "r3_fv_geom_gbcr",
        "ab_main_click_viacss",
    ):
        assert choose_layout(task(task_id)) == "on"


def test_read_only_protocol_probe_uses_mock():
    assert choose_layout(task("pw_raw_schema_getdomains")) == "off"


def test_unknown_driver_and_missing_features_fail_closed():
    assert choose_layout({"driver": {"kind": "new_tool"}, "features": ["web.url.parse"]}) == "on"
    assert choose_layout({"driver": {"kind": "raw_cdp"}, "features": []}) == "on"


def test_embedded_geometry_operation_uses_layout():
    assert choose_layout({
        "driver": {"kind": "raw_cdp", "steps": [{"method": "Runtime.evaluate", "params": {"expression": "node.getBoundingClientRect().width"}}]},
        "features": ["cdp.runtime.evaluate"],
    }) == "on"


def test_reported_visual_contracts_require_layout():
    for task_id in (
        "v2_t2_dom_getnodeforlocation",
        "v2_diag_domsnapshot_computed_styles",
        "v2_leg_probe_a3_checked_reveal",
        "pw_raw_emulation_devicemetrics",
    ):
        assert choose_layout(task(task_id)) == "on", task_id


def test_full_frozen_corpus_only_disables_layout_for_metadata_reads():
    from runner.run import validate_manifest

    _, resolved, errors = validate_manifest(ROOT / "manifest.json")
    assert not errors
    assert len(resolved) == 1928
    assignments = {item.task_id: choose_layout(item.task) for item in resolved}
    assert set(assignments.values()) == {"on", "off"}
    assert {task_id for task_id, mode in assignments.items() if mode == "off"} == {
        "pw_raw_browser_getversion", "pw_raw_schema_getdomains",
    }


def test_unclassified_contract_components_fail_closed():
    import copy

    safe = task("pw_raw_browser_getversion")
    assert choose_layout(safe) == "off"
    mutations = (
        lambda t: t["features"].append("web.css.rendered_visibility"),
        lambda t: t["features"].append("future.unclassified_feature"),
        lambda t: t["driver"]["steps"].append({"method": "DOM.getNodeForLocation", "params": {"x": 1, "y": 1}}),
        lambda t: t["driver"]["steps"].append({"method": "Future.command", "params": {}}),
        lambda t: t["driver"]["steps"].append({"method": "Runtime.evaluate", "params": {"expression": "document.body.innerText"}}),
        lambda t: t["driver"]["steps"][0].update(params={"future": True}),
        lambda t: t["driver"]["steps"][0].update(before="render()"),
        lambda t: t["driver"].update(script="probe.js"),
        lambda t: t.update(scene={"kind": "url", "url": "http://fixture/visual"}),
        lambda t: t["grader"]["checks"].append({"kind": "visible"}),
        lambda t: t["driver"].update(steps=[]),
    )
    for mutate in mutations:
        candidate = copy.deepcopy(safe)
        mutate(candidate)
        assert choose_layout(candidate) == "on", candidate


def test_arbitrary_node_scripts_are_not_classified_from_feature_names():
    candidate = task("pw_raw_browser_getversion")
    candidate["driver"] = {"kind": "node_cdp_probe", "script": "unknown.js"}
    assert choose_layout(candidate) == "on"
