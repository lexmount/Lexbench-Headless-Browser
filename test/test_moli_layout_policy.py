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
