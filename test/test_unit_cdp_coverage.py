"""Unit tests for the CDP stable-surface coverage matrix.

Covers the squeeze-key matching that absorbs the dataset's snake-case
irregularities, the tri-state accounting (tested/waived/gap) plus the policy
`out_of_scope` state, waiver-ledger validation, and the collision guard that
protects feature matching from ambiguity.
"""
from __future__ import annotations

import json
import pathlib

import pytest

from runner import coverage as cov


# --- fixtures: a tiny synthetic protocol + task tree ------------------------


def _write_protocol(tmp_path: pathlib.Path) -> pathlib.Path:
    proto_dir = tmp_path / "proto"
    proto_dir.mkdir()
    browser = {
        "version": {"major": "1", "minor": "3"},
        "domains": [
            {
                "domain": "Page",
                "commands": [
                    {"name": "navigate"},
                    {"name": "captureScreenshot"},
                    {"name": "handleJavaScriptDialog"},
                    {"name": "getResourceTree", "experimental": True},
                ],
                "events": [{"name": "loadEventFired"}],
            },
            {
                "domain": "DOMSnapshot",
                "commands": [{"name": "captureSnapshot"}],
            },
            {
                "domain": "Debugger",
                "commands": [{"name": "enable"}, {"name": "pause"}],
            },
            {
                "domain": "HeadlessExperimental",
                "experimental": True,
                "commands": [{"name": "beginFrame"}],
            },
        ],
    }
    js = {"version": {"major": "1", "minor": "3"}, "domains": []}
    (proto_dir / "browser_protocol.json").write_text(json.dumps(browser))
    (proto_dir / "js_protocol.json").write_text(json.dumps(js))
    return proto_dir


def _write_task(tasks_dir: pathlib.Path, task_id: str, features: list[str]) -> None:
    (tasks_dir / f"{task_id}.json").write_text(
        json.dumps({"task_id": task_id, "features": features})
    )


def _build(tmp_path, task_features_map, waivers=None):
    proto_dir = _write_protocol(tmp_path)
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    for tid, feats in task_features_map.items():
        _write_task(tasks_dir, tid, feats)
    waivers_path = tmp_path / "cdp_waivers.json"
    waivers_path.write_text(json.dumps({"waivers": waivers or {}}))
    return cov.build_coverage(protocol_dir=proto_dir, tasks_dir=tasks_dir, waivers_path=waivers_path)


# --- squeeze key normalization ----------------------------------------------


def test_squeeze_absorbs_snakecase_irregularities():
    assert cov.squeeze("DOMSnapshot") == cov.squeeze("dom_snapshot") == "domsnapshot"
    assert cov.squeeze("handleJavaScriptDialog") == cov.squeeze("handle_javascript_dialog")


def test_feature_key_matches_irregular_domain(tmp_path):
    # cdp.domsnapshot.capture_snapshot must map to DOMSnapshot.captureSnapshot
    r = _build(tmp_path, {"t1": ["cdp.domsnapshot.capture_snapshot"]})
    tested_ids = {m.member.id for m in r.tested}
    assert "DOMSnapshot.captureSnapshot" in tested_ids


def test_feature_key_matches_javascript_wordbreak(tmp_path):
    r = _build(tmp_path, {"t1": ["cdp.page.handle_javascript_dialog"]})
    assert "Page.handleJavaScriptDialog" in {m.member.id for m in r.tested}


# --- tri-state accounting ----------------------------------------------------


def test_stable_universe_excludes_experimental(tmp_path):
    r = _build(tmp_path, {})
    ids = {m.member.id for m in r.members}
    assert "Page.getResourceTree" not in ids  # experimental command
    assert "HeadlessExperimental.beginFrame" not in ids  # experimental domain
    assert "Page.navigate" in ids


def test_tested_waived_gap_partition(tmp_path):
    r = _build(
        tmp_path,
        {"t1": ["cdp.page.navigate", "cdp.page.load_event_fired"]},
        waivers={"Debugger.pause": {"category": "not_applicable", "reason": "no JS debugger surface in bench"}},
    )
    states = {m.member.id: m.state for m in r.members}
    assert states["Page.navigate"] == "tested"
    assert states["Page.loadEventFired"] == "tested"
    assert states["Debugger.pause"] == "waived"
    assert states["Debugger.enable"] == "gap"
    # captureScreenshot is a policy out_of_scope member, never gap
    assert states["Page.captureScreenshot"] == "out_of_scope"


def test_out_of_scope_not_counted_in_gap_or_waiver(tmp_path):
    r = _build(tmp_path, {})
    assert "Page.captureScreenshot" in {m.member.id for m in r.out_of_scope}
    assert "Page.captureScreenshot" not in {m.member.id for m in r.gap}
    assert r.waiver_fraction == 0.0


# --- waiver ledger validation -----------------------------------------------


def test_waiver_bad_category_flagged(tmp_path):
    r = _build(
        tmp_path,
        {},
        waivers={"Debugger.pause": {"category": "bogus", "reason": "x"}},
    )
    assert any("category `bogus`" in e for e in r.waiver_errors)


def test_waiver_missing_reason_flagged(tmp_path):
    r = _build(
        tmp_path,
        {},
        waivers={"Debugger.pause": {"category": "not_applicable", "reason": ""}},
    )
    assert any("missing a non-empty reason" in e for e in r.waiver_errors)


def test_waiver_for_unknown_member_flagged(tmp_path):
    r = _build(
        tmp_path,
        {},
        waivers={"Nonsense.method": {"category": "not_applicable", "reason": "x"}},
    )
    assert any("does not name a stable protocol member" in e for e in r.waiver_errors)


def test_waiver_redundant_when_tested(tmp_path):
    r = _build(
        tmp_path,
        {"t1": ["cdp.page.navigate"]},
        waivers={"Page.navigate": {"category": "not_applicable", "reason": "x"}},
    )
    assert any("redundant" in e for e in r.waiver_errors)


# --- synthetic / unmapped features ------------------------------------------


def test_synthetic_feature_not_flagged_as_unmapped(tmp_path):
    r = _build(tmp_path, {"t1": ["cdp.protocol.unknown_method"]})
    assert "cdp.protocol.unknown_method" not in r.unmapped_features


def test_typo_feature_flagged_as_unmapped(tmp_path):
    r = _build(tmp_path, {"t1": ["cdp.page.navigatex"]})
    assert "cdp.page.navigatex" in r.unmapped_features


# --- frozen waiver whitelist (Eric's #47 ruling, 2026-07-19) -----------------


def test_waiver_not_in_frozen_set_is_flagged_as_added(tmp_path):
    # Debugger.pause is a legal stable waiver here but not in the frozen set,
    # so it must surface as a new addition.
    r = _build(
        tmp_path,
        {},
        waivers={"Debugger.pause": {"category": "not_applicable", "reason": "x"}},
    )
    assert "Debugger.pause" in r.waivers_added
    assert not r.waiver_errors  # structurally valid, just not frozen


def test_frozen_set_absent_reports_missing(tmp_path):
    # The synthetic protocol has none of the real frozen members, so every frozen
    # waiver is reported missing from this ledger.
    r = _build(tmp_path, {})
    assert set(r.waivers_missing) == set(cov.FROZEN_WAIVERS)
    assert r.waivers_added == []


def test_real_ledger_matches_frozen_set_exactly():
    # The committed repository must satisfy the frozen acceptance policy.
    r = cov.build_coverage()
    assert r.waived_ids == set(cov.FROZEN_WAIVERS)
    assert r.waivers_added == []
    assert r.waivers_missing == []


# --- real protocol: collision-free guard ------------------------------------


def test_real_protocol_has_no_squeeze_collisions():
    # Uses the actually pinned devtools-protocol; load must not raise.
    members, version = cov.load_protocol()
    assert version.startswith("1.")
    assert any(m.domain == "Page" and m.name == "navigate" and m.stable for m in members)
