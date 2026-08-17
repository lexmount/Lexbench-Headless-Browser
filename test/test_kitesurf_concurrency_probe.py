from __future__ import annotations

import json
import threading
import types

from tools import kitesurf_concurrency_probe as probe


IDENTITY = {
    "product": "Chrome/test",
    "protocolVersion": "1.3",
    "revision": "@test",
}


def test_worker_rejects_success_false_and_retries_cleanup(
    monkeypatch, tmp_path
) -> None:
    instances = []

    class FakeClient:
        def __init__(self, *_args, **_kwargs) -> None:
            self.call_count = 0
            self.error_count = 0
            self.disconnect_count = 0
            self.close_target_calls = 0
            self.closed = False
            instances.append(self)

        def connect(self) -> None:
            pass

        def command(self, method: str, _params=None, **_kwargs) -> dict:
            self.call_count += 1
            if method == "Browser.getVersion":
                return dict(IDENTITY)
            if method == "Target.createTarget":
                return {"targetId": "target-1"}
            if method == "Target.attachToTarget":
                return {"sessionId": "session-1"}
            if method == "Runtime.evaluate":
                return {"result": {"value": 42}}
            if method == "Target.closeTarget":
                self.close_target_calls += 1
                return {"success": False}
            raise AssertionError(method)

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(probe.bench, "CDPClient", FakeClient)
    row = probe.run_worker(
        "wss://example.test/devtools/browser",
        IDENTITY,
        tmp_path,
        1,
        1,
        1,
        threading.Barrier(1),
    )

    assert row["status"] == "fail"
    assert row["failure_class"] == "cdp_semantic"
    assert row["binding"]["verified"] is True
    assert "did not confirm closure" in row["error"]
    assert row["cleanup_attempted"] is True
    assert row["isolation_restored"] is False
    assert "cleanup did not confirm closure" in row["cleanup_error"]
    assert instances[0].close_target_calls == 2
    assert instances[0].closed is True


def test_main_stops_before_later_levels_after_cleanup_failure(
    monkeypatch, tmp_path
) -> None:
    output = tmp_path / "concurrency"
    args = types.SimpleNamespace(
        output=output,
        endpoint="wss://example.test/devtools/browser",
        expect_product="Chrome/test",
        expect_protocol_version="1.3",
        expect_revision="@test",
        expected_identity=IDENTITY,
        level=[1, 3],
        rounds=1,
        cooldown_seconds=0.0,
    )
    calls = []

    def fake_worker(
        _endpoint,
        _expected_product,
        _output_dir,
        level,
        round_number,
        worker,
        _barrier,
    ):
        calls.append((level, round_number, worker))
        return {
            "level": level,
            "round": round_number,
            "worker": worker,
            "status": "fail",
            "duration_ms": 1,
            "product": "Chrome/test",
            "value": 42,
            "cleanup_attempted": True,
            "isolation_restored": False,
            "cleanup_error": "success=false",
            "cdp_call_count": 6,
            "cdp_error_count": 0,
            "ws_disconnect_count": 0,
        }

    monkeypatch.setattr(probe, "parse_args", lambda: args)
    monkeypatch.setattr(probe, "source_commit", lambda: "source")
    monkeypatch.setattr(
        probe,
        "capture_source_provenance",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(probe, "run_worker", fake_worker)

    assert probe.main() == 2
    assert calls == [(1, 1, 1)]
    summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    assert summary["aborted"] is True
    assert summary["abort_kind"] == "isolation"
    assert summary["evidence_complete"] is False
    assert summary["completed_sessions"] == 1
    assert summary["requested_sessions"] == 4
    assert summary["by_level"]["3"]["attempts"] == 0


def test_worker_marks_create_transport_timeout_as_unclean(
    monkeypatch,
    tmp_path,
) -> None:
    class FakeClient:
        def __init__(self, *_args, **_kwargs) -> None:
            self.call_count = 0
            self.error_count = 0
            self.disconnect_count = 0

        def connect(self) -> None:
            pass

        def command(self, method: str, *_args, **_kwargs) -> dict:
            self.call_count += 1
            if method == "Browser.getVersion":
                return dict(IDENTITY)
            if method == "Target.createTarget":
                raise probe.bench.CDPTransportTimeout(
                    "target response was not received"
                )
            raise AssertionError(method)

        def close(self) -> None:
            pass

    monkeypatch.setattr(probe.bench, "CDPClient", FakeClient)
    row = probe.run_worker(
        "wss://example.test/devtools/browser",
        IDENTITY,
        tmp_path,
        1,
        1,
        1,
        threading.Barrier(1),
    )

    assert row["status"] == "timeout"
    assert row["failure_class"] == "transport_timeout"
    assert row["binding"]["verified"] is True
    assert row["target_state"] == "ambiguous"
    assert row["isolation_restored"] is False
    assert "cleanup could not be confirmed" in row["cleanup_error"]


def test_worker_identity_mismatch_is_binding_infra(monkeypatch, tmp_path) -> None:
    class FakeClient:
        def __init__(self, *_args, **_kwargs) -> None:
            self.call_count = 0
            self.error_count = 0
            self.disconnect_count = 0

        def connect(self) -> None:
            pass

        def command(self, method: str, *_args, **_kwargs) -> dict:
            self.call_count += 1
            assert method == "Browser.getVersion"
            return {**IDENTITY, "revision": "@wrong-endpoint"}

        def close(self) -> None:
            pass

    monkeypatch.setattr(probe.bench, "CDPClient", FakeClient)
    row = probe.run_worker(
        "wss://example.test/devtools/browser",
        IDENTITY,
        tmp_path,
        1,
        1,
        1,
        threading.Barrier(1),
    )

    assert row["status"] == "infra"
    assert row["failure_class"] == "binding_unverified"
    assert row["phase"] == "binding"
    assert row["binding"]["verified"] is False
    assert row["binding"]["actual"]["revision"] == "@wrong-endpoint"
    assert row["target_state"] == "not_started"
    assert row["isolation_restored"] is True


def test_worker_connect_timeout_is_not_functional_fail(monkeypatch, tmp_path) -> None:
    class FakeClient:
        def __init__(self, *_args, **_kwargs) -> None:
            self.call_count = 0
            self.error_count = 0
            self.disconnect_count = 0

        def connect(self) -> None:
            raise probe.bench.CDPTransportTimeout("TLS handshake timed out")

        def command(self, *_args, **_kwargs) -> dict:
            raise AssertionError("no CDP command should run after connect timeout")

        def close(self) -> None:
            pass

    monkeypatch.setattr(probe.bench, "CDPClient", FakeClient)
    row = probe.run_worker(
        "wss://example.test/devtools/browser",
        IDENTITY,
        tmp_path,
        1,
        1,
        1,
        threading.Barrier(1),
    )

    assert row["status"] == "timeout"
    assert row["failure_class"] == "transport_timeout"
    assert row["phase"] == "connect"
    assert row["binding"]["verified"] is False
    assert row["binding"]["same_connection_as_task"] is False
    assert row["target_state"] == "not_started"
    assert row["isolation_restored"] is True


def test_worker_browser_version_rejection_is_binding_infra(
    monkeypatch, tmp_path
) -> None:
    class FakeClient:
        def __init__(self, *_args, **_kwargs) -> None:
            self.call_count = 0
            self.error_count = 0
            self.disconnect_count = 0

        def connect(self) -> None:
            pass

        def command(self, method: str, *_args, **_kwargs) -> dict:
            self.call_count += 1
            assert method == "Browser.getVersion"
            raise probe.bench.CDPCommandError(
                method,
                {"code": -32601, "message": "method unavailable"},
            )

        def close(self) -> None:
            pass

    monkeypatch.setattr(probe.bench, "CDPClient", FakeClient)
    row = probe.run_worker(
        "wss://example.test/devtools/browser",
        IDENTITY,
        tmp_path,
        1,
        1,
        1,
        threading.Barrier(1),
    )

    assert row["status"] == "infra"
    assert row["failure_class"] == "binding_unverified"
    assert row["phase"] == "binding"
    assert row["binding"]["verified"] is False
    assert row["binding"]["same_connection_as_task"] is True
    assert row["target_state"] == "not_started"
    assert row["isolation_restored"] is True


def test_main_aborts_later_levels_after_binding_failure(
    monkeypatch, tmp_path
) -> None:
    output = tmp_path / "binding-failure"
    args = types.SimpleNamespace(
        output=output,
        endpoint="wss://example.test/devtools/browser",
        expected_identity=IDENTITY,
        level=[1, 3],
        rounds=1,
        cooldown_seconds=0.0,
    )
    calls = []

    def fake_worker(
        _endpoint,
        expected_identity,
        _output_dir,
        level,
        round_number,
        worker,
        _barrier,
    ):
        calls.append((level, round_number, worker))
        return {
            "level": level,
            "round": round_number,
            "worker": worker,
            "status": "infra",
            "failure_class": "binding_unverified",
            "phase": "binding",
            "binding": {
                "expected": expected_identity,
                "actual": {**IDENTITY, "revision": "@wrong"},
                "verified": False,
            },
            "duration_ms": 1,
            "isolation_restored": True,
            "cdp_call_count": 1,
            "cdp_error_count": 0,
            "ws_disconnect_count": 0,
        }

    monkeypatch.setattr(probe, "parse_args", lambda: args)
    monkeypatch.setattr(probe, "source_commit", lambda: "source")
    monkeypatch.setattr(
        probe,
        "capture_source_provenance",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(probe, "run_worker", fake_worker)

    assert probe.main() == 2
    assert calls == [(1, 1, 1)]
    summary = json.loads((output / "summary.json").read_text())
    assert summary["schema"] == "experimental.kitesurf_concurrency_probe.v3"
    assert summary["aborted"] is True
    assert summary["abort_kind"] == "binding"
    assert summary["evidence_complete"] is False
    assert summary["by_level"]["1"]["fail"] == 0
    assert summary["by_level"]["1"]["infra"] == 1
    assert summary["by_level"]["1"]["functional_attempts"] == 0
    assert summary["by_level"]["1"]["p50_ms"] is None
    assert summary["by_level"]["3"]["attempts"] == 0


def test_main_all_transport_rows_exit_nonzero_without_counting_fail(
    monkeypatch, tmp_path
) -> None:
    output = tmp_path / "transport-only"
    args = types.SimpleNamespace(
        output=output,
        endpoint="wss://example.test/devtools/browser",
        expected_identity=IDENTITY,
        level=[1],
        rounds=1,
        cooldown_seconds=0.0,
    )

    monkeypatch.setattr(probe, "parse_args", lambda: args)
    monkeypatch.setattr(probe, "source_commit", lambda: "source")
    monkeypatch.setattr(
        probe,
        "capture_source_provenance",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        probe,
        "run_worker",
        lambda *_args, **_kwargs: {
            "level": 1,
            "round": 1,
            "worker": 1,
            "status": "transport_error",
            "failure_class": "transport_error",
            "phase": "connect",
            "binding": {"verified": False},
            "duration_ms": 1,
            "isolation_restored": True,
            "cdp_call_count": 0,
            "cdp_error_count": 0,
            "ws_disconnect_count": 1,
        },
    )

    assert probe.main() == 2
    summary = json.loads((output / "summary.json").read_text())
    assert summary["aborted"] is False
    assert summary["evidence_complete"] is False
    assert summary["status_counts"]["transport_error"] == 1
    assert summary["by_level"]["1"]["fail"] == 0
    assert summary["by_level"]["1"]["transport_error"] == 1
