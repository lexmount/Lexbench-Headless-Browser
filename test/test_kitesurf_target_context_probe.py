from __future__ import annotations

import json
import types

from tools import kitesurf_target_context_probe as probe


def test_cleanup_retries_until_target_close_is_confirmed() -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.calls = 0

        def command(self, method: str, params: dict) -> dict:
            assert method == "Target.closeTarget"
            assert params == {"targetId": "target-1"}
            self.calls += 1
            return {"success": self.calls == 2}

    client = FakeClient()
    report = probe.cleanup_created_target(
        client,
        {"ok": True, "result": {"targetId": "target-1"}},
    )

    assert report["confirmed"] is True
    assert client.calls == 2
    assert [attempt["confirmed"] for attempt in report["attempts"]] == [
        False,
        True,
    ]


def test_cleanup_reports_unconfirmed_after_both_attempts_fail() -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.calls = 0

        def command(self, _method: str, _params: dict) -> dict:
            self.calls += 1
            return {"success": False}

    client = FakeClient()
    report = probe.cleanup_created_target(
        client,
        {"ok": True, "result": {"targetId": "target-1"}},
    )

    assert report["confirmed"] is False
    assert client.calls == 2
    assert report["target_id"] == "target-1"
    assert "two attempts" in report["error"]


def test_ambiguous_create_failure_is_not_treated_as_clean() -> None:
    report = probe.cleanup_created_target(
        object(),
        {
            "ok": False,
            "creation_state": "ambiguous",
            "error": "CDPTransportTimeout: response was not received",
        },
    )

    assert report["required"] is True
    assert report["confirmed"] is False
    assert "ambiguous" in report["reason"]


def test_main_aborts_later_rounds_after_unconfirmed_cleanup(
    monkeypatch,
    tmp_path,
) -> None:
    output = tmp_path / "target-context"
    args = types.SimpleNamespace(
        endpoint="wss://example.test/devtools/browser",
        expect_product="Chrome/test",
        expect_protocol_version="1.3",
        expect_revision="@test",
        rounds=3,
        delay_ms=0,
        output=output,
    )
    calls = []

    def fake_round(_args, _output, round_number):
        calls.append(round_number)
        return {
            "round": round_number,
            "status": "infra",
            "failure": {
                "layer": "harness",
                "code": "target_cleanup_unconfirmed",
            },
            "duration_ms": 1,
            "identity": {
                "product": "Chrome/test",
                "protocolVersion": "1.3",
                "revision": "@test",
            },
            "expected_identity": {
                "product": "Chrome/test",
                "protocolVersion": "1.3",
                "revision": "@test",
            },
            "advertised_context_ids": [],
            "selected_context_id": None,
            "create_with_advertised_context": {"ok": False},
            "cleanup_with_advertised_context": {"confirmed": False},
            "create_without_context": {"ok": False},
            "cleanup_without_context": {"confirmed": False},
            "isolation_restored": False,
            "cdp_call_count": 2,
            "cdp_error_count": 0,
            "ws_disconnect_count": 0,
        }

    monkeypatch.setattr(probe, "parse_args", lambda: args)
    monkeypatch.setattr(probe, "source_commit", lambda: "source")
    monkeypatch.setattr(
        probe, "capture_source_provenance", lambda *_args, **_kwargs: {}
    )
    monkeypatch.setattr(probe, "run_round", fake_round)

    assert probe.main() == 2
    assert calls == [1]
    summary = json.loads(
        (output / "summary.json").read_text(encoding="utf-8")
    )
    assert summary["aborted"] is True
    assert summary["requested_rounds"] == 3
    assert summary["completed_rounds"] == 1
    assert summary["status_counts"] == {"infra": 1}
