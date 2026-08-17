from __future__ import annotations

import json

import pytest

from runner import run as bench
from tools import kitesurf_dynamic_fixture as fixture


def test_committed_contract_matches_source_and_verifies_local_server(
    tmp_path,
) -> None:
    committed = json.loads(fixture.DEFAULT_MANIFEST.read_text(encoding="utf-8"))

    assert fixture.build_manifest() == committed

    server = bench.FixtureServer()
    base_url = server.start()
    report_path = tmp_path / "fixture_verification.json"
    try:
        report = fixture.verify_dynamic_fixture(
            base_url,
            report_path=report_path,
        )
    finally:
        server.stop()

    assert report["verified"] is True
    assert report["local_contract_verified"] is True
    assert report["verified_static_route_count"] == 127
    assert report["verified_dynamic_probe_count"] == 28
    assert all(row["verified"] for row in report["static_routes"])
    assert all(row["verified"] for row in report["dynamic_probes"])
    compact = fixture.compact_verification(report, report_path)
    assert compact["static_routes"] == {"verified": 127, "required": 127}
    assert compact["local_contract_verified"] is True
    assert compact["dynamic_probes"] == {"verified": 28, "required": 28}
    assert len(compact["report_sha256"]) == 64


def test_verifier_rejects_manifest_that_no_longer_matches_local_fixture(
    tmp_path,
) -> None:
    manifest = json.loads(fixture.DEFAULT_MANIFEST.read_text(encoding="utf-8"))
    manifest["static_routes"][0]["sha256"] = "0" * 64
    contract = {
        "static_routes": manifest["static_routes"],
        "dynamic_probes": manifest["dynamic_probes"],
    }
    manifest["contract_sha256"] = fixture._canonical_sha256(contract)
    manifest_path = tmp_path / "drifted.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    report_path = tmp_path / "report.json"

    with pytest.raises(
        fixture.DynamicFixtureError,
        match="does not match the checked-out FixtureServer",
    ):
        fixture.verify_dynamic_fixture(
            "https://must-not-connect.invalid",
            manifest_path,
            report_path=report_path,
        )

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["verified"] is False
    assert report["local_static_contract_verified"] is False
    assert report["static_routes"] == []


def test_verifier_rejects_rewritten_dynamic_contract_before_network(
    tmp_path,
) -> None:
    manifest = json.loads(fixture.DEFAULT_MANIFEST.read_text(encoding="utf-8"))
    manifest["dynamic_probes"][0]["expect"]["sha256"] = "0" * 64
    contract = {
        "static_routes": manifest["static_routes"],
        "dynamic_probes": manifest["dynamic_probes"],
    }
    manifest["contract_sha256"] = fixture._canonical_sha256(contract)
    manifest_path = tmp_path / "rewritten.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    report_path = tmp_path / "report.json"

    with pytest.raises(
        fixture.DynamicFixtureError,
        match="does not match the checked-out FixtureServer",
    ):
        fixture.verify_dynamic_fixture(
            "https://must-not-connect.invalid",
            manifest_path,
            report_path=report_path,
        )

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["verified"] is False
    assert report["local_static_contract_verified"] is True
    assert report["local_dynamic_contract_verified"] is False
    assert report["static_routes"] == []


def test_verifier_records_remote_deployment_mismatch(
    monkeypatch,
    tmp_path,
) -> None:
    server = bench.FixtureServer()
    base_url = server.start()
    report_path = tmp_path / "remote-mismatch.json"
    original_fetch = fixture._fetch_http

    def tampered_fetch(request_base, request_spec, timeout_s, **kwargs):
        actual, patterns = original_fetch(
            request_base,
            request_spec,
            timeout_s,
            **kwargs,
        )
        if (
            request_base == base_url
            and request_spec["path"] == "/__fixture__/deployment-contract"
        ):
            actual["sha256"] = "0" * 64
        return actual, patterns

    monkeypatch.setattr(fixture, "_fetch_http", tampered_fetch)
    try:
        with pytest.raises(
            fixture.DynamicFixtureError,
            match="deployment_contract",
        ):
            fixture.verify_dynamic_fixture(
                base_url,
                report_path=report_path,
            )
    finally:
        server.stop()

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["verified"] is False
    deployment = next(
        row
        for row in report["dynamic_probes"]
        if row["id"] == "deployment_contract"
    )
    assert deployment["verified"] is False
    assert deployment["actual"]["sha256"] == "0" * 64
    assert deployment["expected"]["sha256"] != "0" * 64


def test_dynamic_contract_covers_stateful_and_streaming_surfaces() -> None:
    manifest = json.loads(fixture.DEFAULT_MANIFEST.read_text(encoding="utf-8"))
    probe_ids = {probe["id"] for probe in manifest["dynamic_probes"]}

    assert {
        "websocket_echo",
        "sse_messages",
        "deployment_contract",
        "auth_denied",
        "auth_granted",
        "upload_receipt",
        "app_login",
        "app_cart_add",
        "app_checkout",
        "app_logout",
        "inventory_document",
        "inventory_event",
        "inventory_grader_no_events",
        "grader_expected_pass",
        "grader_expected_fail",
    } <= probe_ids
    assert all(
        len(probe["expect"]["sha256"]) == 64
        for probe in manifest["dynamic_probes"]
    )
