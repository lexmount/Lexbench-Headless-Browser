import json
import os
import pathlib
import shutil
import subprocess
import types
import urllib.request

import pytest

from runner import run as bench
from tools import kitesurf_driver_probe as driver_probe
from tools import kitesurf_l1_probe as l1_probe
from tools import kitesurf_l2_sample_probe as l2_sample
from tools.kitesurf_driver_probe import (
    IdentityShim,
    driver_name,
    is_transport_connect_failure,
    run_task,
)


ENDPOINT = "wss://kitesurf.cloudflare.app/devtools/browser"
IDENTITY = {
    "product": "Chrome/145.0.0.0",
    "protocolVersion": "1.3",
    "revision": "@kitesurf",
    "userAgent": "fixture-agent",
}


def test_identity_shim_serves_pinned_remote_endpoint() -> None:
    shim = IdentityShim(ENDPOINT, IDENTITY)
    shim.start()
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{shim.port}/json/version",
            timeout=2,
        ) as response:
            payload = json.load(response)
    finally:
        shim.stop()

    assert payload == {
        "Browser": "Chrome/145.0.0.0",
        "Protocol-Version": "1.3",
        "User-Agent": "fixture-agent",
        "webSocketDebuggerUrl": ENDPOINT,
    }


def test_identity_preflight_rejects_incomplete_response(
    monkeypatch, tmp_path
) -> None:
    class FakeClient:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            pass

        def command(self, method: str) -> dict:
            assert method == "Browser.getVersion"
            return {
                "product": "Chrome/145.0.0.0",
                "protocolVersion": "1.3",
            }

    monkeypatch.setattr(l1_probe.bench, "CDPClient", FakeClient)

    with pytest.raises(bench.BenchError, match="preflight.*revision"):
        l1_probe.fetch_identity(ENDPOINT, tmp_path, IDENTITY)


def test_identity_preflight_rejects_complete_wrong_engine() -> None:
    observed = {**IDENTITY, "revision": "@wrong-engine"}

    with pytest.raises(bench.BenchError, match="identity mismatch.*revision"):
        bench.require_matching_remote_cdp_identity(
            observed,
            IDENTITY,
            label="Kitesurf preflight",
        )


def test_remote_framework_driver_passes_preflight_identity(
    monkeypatch,
    tmp_path,
) -> None:
    captured = {}

    def fake_run_node_driver_process(*args):
        captured["env"] = args[2]
        return {"ok": True}

    monkeypatch.setattr(bench, "run_node_driver_process", fake_run_node_driver_process)
    monkeypatch.setattr(bench, "browser_cdp_product", lambda browser: IDENTITY["product"])
    task = types.SimpleNamespace(
        driver={"kind": "framework_playwright", "steps": [], "checks": []},
        scene={"url": "/v1/core/"},
        task={"timeouts": {"task_ms": 30000}},
        task_id="remote_gate",
    )
    browser = bench.BrowserProcess(
        engine="kitesurf",
        port=43210,
        process=None,
        version_info={
            **IDENTITY,
            "Browser": IDENTITY["product"],
            "webSocketDebuggerUrl": ENDPOINT,
            "transport": "remote_cdp",
        },
    )

    bench.run_framework_driver(
        task,
        browser,
        tmp_path,
        "https://fixtures.example",
        "run",
        "kitesurf",
        1,
        "seed",
    )

    remote = json.loads(captured["env"]["REMOTE_CDP_IDENTITY_JSON"])
    assert remote == {
        "product": "Chrome/145.0.0.0",
        "protocolVersion": "1.3",
        "revision": "@kitesurf",
    }


def test_remote_node_probe_receives_exact_per_attempt_identity(
    monkeypatch,
    tmp_path,
) -> None:
    captured = {}

    def fake_run_node_driver_process(*args):
        captured["env"] = args[2]
        return {"ok": True}

    monkeypatch.setattr(bench, "run_node_driver_process", fake_run_node_driver_process)
    monkeypatch.setattr(bench, "browser_page_target_ids", lambda _browser: set())
    monkeypatch.setattr(
        bench,
        "cleanup_new_page_targets",
        lambda *_args: {"errors": []},
    )
    task = types.SimpleNamespace(
        driver={
            "kind": "node_cdp_probe",
            "script": "runner/scripts/l2_fixture_probe.js",
            "env": {
                "REMOTE_CDP_IDENTITY_JSON": '{"product":"spoofed"}'
            },
        },
        scene={"url": "/v1/core/"},
        task={"timeouts": {"task_ms": 30000}},
        task_id="remote_l2_gate",
    )
    browser = bench.BrowserProcess(
        engine="kitesurf",
        port=43210,
        process=None,
        version_info={
            **IDENTITY,
            "Browser": IDENTITY["product"],
            "webSocketDebuggerUrl": ENDPOINT,
            "transport": "remote_cdp",
        },
    )

    bench.run_node_cdp_probe_driver(
        task,
        browser,
        tmp_path,
        "https://fixtures.example",
        "run",
        "kitesurf",
        1,
        "seed",
    )

    assert json.loads(captured["env"]["REMOTE_CDP_IDENTITY_JSON"]) == {
        "product": "Chrome/145.0.0.0",
        "protocolVersion": "1.3",
        "revision": "@kitesurf",
    }


@pytest.mark.parametrize(
    "script",
    [
        "runner/scripts/l2_fixture_probe.js",
        "runner/scripts/storage_indexeddb_inventory_001.js",
    ],
    ids=["generic-l2", "indexeddb"],
)
def test_local_node_probe_clears_inherited_remote_identity(
    monkeypatch,
    tmp_path,
    script,
) -> None:
    captured = {}
    stale_identity = json.dumps(
        {
            "product": "Chrome/145.0.0.0",
            "protocolVersion": "1.3",
            "revision": "@stale-kitesurf",
        }
    )
    monkeypatch.setenv("REMOTE_CDP_IDENTITY_JSON", stale_identity)

    def fake_run_node_driver_process(*args):
        captured["env"] = args[2]
        return {"ok": True, "observations": {}, "metrics": {}}

    monkeypatch.setattr(
        bench,
        "run_node_driver_process",
        fake_run_node_driver_process,
    )
    monkeypatch.setattr(bench, "browser_page_target_ids", lambda _browser: set())
    monkeypatch.setattr(
        bench,
        "cleanup_new_page_targets",
        lambda *_args: {"confirmed": True, "errors": []},
    )
    task = types.SimpleNamespace(
        driver={
            "kind": "node_cdp_probe",
            "script": script,
            "env": {"REMOTE_CDP_IDENTITY_JSON": stale_identity},
        },
        scene={"url": "/v1/core/"},
        task={"timeouts": {"task_ms": 30000}},
        task_id="local_identity_hygiene",
    )
    browser = bench.BrowserProcess(
        engine="moli",
        port=43210,
        process=None,
        version_info={
            "Browser": "Chrome/145.0.0.0",
            "webSocketDebuggerUrl": (
                "ws://127.0.0.1:43210/devtools/browser/local"
            ),
        },
    )
    artifact_dir = tmp_path / pathlib.Path(script).stem
    artifact_dir.mkdir()

    result = bench.run_node_cdp_probe_driver(
        task,
        browser,
        artifact_dir,
        "https://fixtures.example",
        "run",
        "moli",
        1,
        "seed",
    )

    assert result["ok"] is True
    assert "REMOTE_CDP_IDENTITY_JSON" not in captured["env"]


def test_l2_result_row_persists_attempt_binding(monkeypatch, tmp_path) -> None:
    binding = {
        "transport": "remote_cdp",
        "expected": {
            "product": IDENTITY["product"],
            "protocolVersion": IDENTITY["protocolVersion"],
            "revision": IDENTITY["revision"],
        },
        "actual": {
            "product": IDENTITY["product"],
            "protocolVersion": IDENTITY["protocolVersion"],
            "revision": IDENTITY["revision"],
        },
        "verified": True,
        "same_connection_as_task": True,
        "reconnect_allowed": False,
    }
    monkeypatch.setattr(
        bench,
        "run_node_cdp_probe_driver",
        lambda *_args, **_kwargs: {
            "ok": True,
            "answer": "ok",
            "observations": {"binding": binding, "fixture": "ready"},
            "metrics": {"cdp_call_count": 4},
            "grader": {"ok": True},
        },
    )
    task = types.SimpleNamespace(
        driver={"kind": "node_cdp_probe"},
        scene={"url": "/v1/core/"},
        task={"task_version": "1"},
        task_id="l2_binding_row",
    )
    capability = {
        "capability_id": "cap.test",
        "category": "test",
        "observable": "answer",
        "role": "primary",
    }

    row = l2_sample.run_task(
        task,
        1,
        types.SimpleNamespace(),
        "https://fixtures.example",
        tmp_path,
        "run",
        capability,
    )

    assert row["status"] == "pass"
    assert row["binding"] == binding
    assert row["observations"]["binding"]["same_connection_as_task"] is True


def test_remote_node_probe_same_connection_cleanup_failure_overrides_pass(
    monkeypatch, tmp_path
) -> None:
    task = types.SimpleNamespace(
        driver={
            "kind": "node_cdp_probe",
            "script": "runner/scripts/l2_fixture_probe.js",
        },
        scene={"url": "/v1/core/"},
        task={"timeouts": {"task_ms": 30000}},
        task_id="remote_outer_cleanup",
    )
    browser = bench.BrowserProcess(
        engine="kitesurf",
        port=43210,
        process=None,
        version_info={
            **IDENTITY,
            "Browser": IDENTITY["product"],
            "webSocketDebuggerUrl": ENDPOINT,
            "transport": "remote_cdp",
        },
    )
    monkeypatch.setattr(
        bench,
        "cleanup_new_page_targets",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("remote cleanup must not use cross-connection snapshots")
        ),
    )
    monkeypatch.setattr(
        bench,
        "run_node_driver_process",
        lambda *_args, **_kwargs: {
            "ok": True,
            "status": "pass",
            "answer": "primary-pass",
            "observations": {
                "target_cleanup": {"confirmed": False},
                "isolation_restored": False,
            },
            "grader": {"ok": True, "checks": []},
            "metrics": {},
        },
    )

    result = bench.run_node_cdp_probe_driver(
        task,
        browser,
        tmp_path,
        "https://fixtures.example",
        "run",
        "kitesurf",
        1,
        "seed",
    )

    assert result["ok"] is False
    assert result["status"] == "infra"
    assert result["observations"]["isolation_restored"] is False
    assert result["observations"]["outer_target_cleanup"]["confirmed"] is False


def test_remote_node_probe_preserves_timeout_with_cleanup_evidence(
    monkeypatch, tmp_path
) -> None:
    task = types.SimpleNamespace(
        driver={
            "kind": "node_cdp_probe",
            "script": "runner/scripts/l2_fixture_probe.js",
        },
        scene={"url": "/v1/core/"},
        task={"timeouts": {"task_ms": 30000}},
        task_id="remote_timeout_cleanup",
    )
    browser = bench.BrowserProcess(
        engine="kitesurf",
        port=43210,
        process=None,
        version_info={
            **IDENTITY,
            "Browser": IDENTITY["product"],
            "webSocketDebuggerUrl": ENDPOINT,
            "transport": "remote_cdp",
        },
    )
    artifact_dir = tmp_path / "artifact"
    artifact_dir.mkdir()

    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(["node", "probe.js"], 30)

    monkeypatch.setattr(bench, "run_node_driver_process", timeout)

    with pytest.raises(subprocess.TimeoutExpired) as raised:
        bench.run_node_cdp_probe_driver(
            task,
            browser,
            artifact_dir,
            "https://fixtures.example",
            "run",
            "kitesurf",
            1,
            "seed",
        )

    observations = raised.value.cdp_observations
    assert observations["isolation_restored"] is False
    assert observations["outer_target_cleanup"]["confirmed"] is False


def test_remote_raw_driver_requires_close_success_before_return(
    tmp_path, fake_cdp
) -> None:
    fake = fake_cdp(
        {
            "Browser.getVersion": {"result": IDENTITY},
            "Target.createTarget": {"result": {"targetId": "RAW-LEAK"}},
            "Target.attachToTarget": {"result": {"sessionId": "RAW-SESSION"}},
            "Target.closeTarget": {"result": {"success": False}},
        }
    )
    browser = bench.BrowserProcess(
        engine="kitesurf",
        port=fake.port,
        process=None,
        version_info={
            **IDENTITY,
            "Browser": IDENTITY["product"],
            "webSocketDebuggerUrl": fake.ws_url,
            "transport": "remote_cdp",
        },
    )
    task = types.SimpleNamespace(
        scene={"kind": "about_blank"},
        task={"timeouts": {"task_ms": 5000}},
        driver={"steps": []},
        grader={"checks": [{"kind": "no_error"}]},
    )

    with pytest.raises(bench.BenchError, match="cleanup.*not explicitly confirmed") as raised:
        bench.run_raw_cdp_driver(task, browser, tmp_path)

    observations = raised.value.cdp_observations
    assert observations["target_cleanup"]["confirmed"] is False
    assert observations["isolation_restored"] is False
    assert [request["method"] for request in fake.requests].count(
        "Target.closeTarget"
    ) == 2


@pytest.mark.parametrize("isolation_restored", [False, None])
def test_l2_sample_aborts_remaining_attempts_without_explicit_isolation(
    monkeypatch, tmp_path, isolation_restored
) -> None:
    output_dir = tmp_path / "l2-abort"
    args = types.SimpleNamespace(
        endpoint=ENDPOINT,
        expected_identity=dict(IDENTITY),
        fixture_base_url="https://fixtures.example",
        fixture_manifest=tmp_path / "fixture-contract.json",
        task=["one", "two"],
        reruns=2,
        delay_ms=0,
        output=output_dir,
    )
    tasks = [types.SimpleNamespace(task_id="one"), types.SimpleNamespace(task_id="two")]
    capability_map = {
        task.task_id: {
            "capability_id": f"cap.{task.task_id}",
            "category": "test",
            "observable": "answer",
            "role": "primary",
        }
        for task in tasks
    }
    calls = []

    monkeypatch.setattr(l2_sample, "parse_args", lambda: args)
    monkeypatch.setattr(l2_sample, "load_tasks", lambda _args: tasks)
    monkeypatch.setattr(l2_sample, "load_capability_map", lambda: capability_map)
    monkeypatch.setattr(l2_sample, "source_commit", lambda: "test-source")
    monkeypatch.setattr(l2_sample, "capture_source_provenance", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        l2_sample,
        "verify_dynamic_fixture",
        lambda *_args, **_kwargs: {"verified": True},
    )
    monkeypatch.setattr(
        l2_sample,
        "compact_verification",
        lambda *_args, **_kwargs: {"verified": True},
    )
    monkeypatch.setattr(l2_sample, "fetch_identity", lambda *_args: dict(IDENTITY))
    monkeypatch.setattr(l2_sample, "remote_browser", lambda *_args: object())

    def fake_run_task(task, attempt, *_args, **_kwargs):
        calls.append((task.task_id, attempt))
        return {
            "task_id": task.task_id,
            "attempt": attempt,
            "status": "infra",
            "duration_ms": 1,
            "failure": {"class": "infra", "detail": "cleanup unconfirmed"},
            "binding": {"verified": True},
            "capability": capability_map[task.task_id],
            "cdp_call_count": 1,
            "cdp_error_count": 0,
            "ws_disconnect_count": 0,
            "isolation_restored": isolation_restored,
        }

    monkeypatch.setattr(l2_sample, "run_task", fake_run_task)

    assert l2_sample.main() == 2
    assert calls == [("one", 1)]
    summary = json.loads((output_dir / "summary.json").read_text())
    assert summary["aborted"] is True
    assert summary["requested_attempts"] == 4
    assert summary["attempts"] == 1


@pytest.mark.parametrize("isolation_restored", [False, None])
def test_l1_probe_aborts_remaining_tasks_without_explicit_isolation(
    monkeypatch, tmp_path, isolation_restored
) -> None:
    output_dir = tmp_path / "l1-abort"
    args = types.SimpleNamespace(
        endpoint=ENDPOINT,
        expected_identity=dict(IDENTITY),
        fixture_base_url=None,
        fixture_manifest=tmp_path / "fixture-contract.json",
        task=None,
        feature=None,
        limit=None,
        delay_ms=0,
        stop_after_transport_errors=3,
        output=output_dir,
        list=False,
    )
    tasks = [types.SimpleNamespace(task_id="one"), types.SimpleNamespace(task_id="two")]
    calls = []

    monkeypatch.setattr(l1_probe, "parse_args", lambda: args)
    monkeypatch.setattr(l1_probe, "validate_endpoint", lambda _endpoint: None)
    monkeypatch.setattr(l1_probe, "load_tasks", lambda _args: (tasks, 0))
    monkeypatch.setattr(l1_probe, "source_commit", lambda: "test-source")
    monkeypatch.setattr(l1_probe, "capture_source_provenance", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(l1_probe, "fetch_identity", lambda *_args: dict(IDENTITY))
    monkeypatch.setattr(l1_probe, "remote_browser", lambda *_args: object())

    def fake_run_task(task, *_args, **_kwargs):
        calls.append(task.task_id)
        return {
            "task_id": task.task_id,
            "status": "infra",
            "duration_ms": 1,
            "failure": {"class": "infra", "detail": "cleanup unconfirmed"},
            "methods": ["Runtime.evaluate"],
            "features": ["cdp.runtime.evaluate"],
            "cdp_call_count": 1,
            "cdp_error_count": 0,
            "ws_disconnect_count": 0,
            "isolation_restored": isolation_restored,
        }

    monkeypatch.setattr(l1_probe, "run_task", fake_run_task)

    assert l1_probe.main() == 2
    assert calls == ["one"]
    summary = json.loads((output_dir / "summary.json").read_text())
    assert summary["scope"]["stopped_early"] is True
    assert summary["scope"]["completed_tasks"] == 1
    assert "cleanup" in summary["scope"]["isolation_abort_reason"]


@pytest.mark.parametrize("isolation_restored", [True, False])
def test_l1_transport_timeout_keeps_timeout_status_and_breaker_marker(
    monkeypatch,
    tmp_path,
    isolation_restored,
) -> None:
    timeout = bench.CDPTransportTimeout("TLS handshake timed out")
    timeout.cdp_metrics = {
        "cdp_call_count": 1,
        "cdp_error_count": 1,
        "ws_disconnect_count": 0,
    }
    timeout.cdp_observations = {
        "target_cleanup": {
            "confirmed": isolation_restored,
            "target_created": False,
        },
        "isolation_restored": isolation_restored,
    }
    monkeypatch.setattr(
        bench,
        "run_raw_cdp_driver",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(timeout),
    )
    task = types.SimpleNamespace(
        task_id="transport_timeout",
        task={"title": "transport timeout"},
        features=["cdp.runtime.evaluate"],
        driver={"steps": [{"method": "Runtime.evaluate"}]},
        scene={"kind": "about_blank"},
    )

    row = l1_probe.run_task(task, object(), tmp_path / "artifact")

    assert row["status"] == "timeout"
    assert row["transport_failure"] is True
    assert row["failure"]["detail"] == "TLS handshake timed out"
    assert row["isolation_restored"] is isolation_restored


def test_l1_transport_timeout_counts_toward_consecutive_breaker(
    monkeypatch,
    tmp_path,
) -> None:
    output_dir = tmp_path / "l1-transport-breaker"
    args = types.SimpleNamespace(
        endpoint=ENDPOINT,
        expected_identity=dict(IDENTITY),
        fixture_base_url=None,
        fixture_manifest=tmp_path / "fixture-contract.json",
        task=None,
        feature=None,
        limit=None,
        delay_ms=0,
        stop_after_transport_errors=2,
        output=output_dir,
        list=False,
    )
    tasks = [
        types.SimpleNamespace(task_id=task_id)
        for task_id in ("one", "two", "must_not_start")
    ]
    calls = []

    monkeypatch.setattr(l1_probe, "parse_args", lambda: args)
    monkeypatch.setattr(l1_probe, "validate_endpoint", lambda _endpoint: None)
    monkeypatch.setattr(l1_probe, "load_tasks", lambda _args: (tasks, 0))
    monkeypatch.setattr(l1_probe, "source_commit", lambda: "test-source")
    monkeypatch.setattr(
        l1_probe,
        "capture_source_provenance",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        l1_probe,
        "fetch_identity",
        lambda *_args: dict(IDENTITY),
    )
    monkeypatch.setattr(l1_probe, "remote_browser", lambda *_args: object())

    def fake_run_task(task, *_args, **_kwargs):
        calls.append(task.task_id)
        return {
            "task_id": task.task_id,
            "status": "timeout",
            "transport_failure": True,
            "duration_ms": 1,
            "failure": {"class": "infra", "detail": "TCP timed out"},
            "methods": ["Runtime.evaluate"],
            "features": ["cdp.runtime.evaluate"],
            "cdp_call_count": 1,
            "cdp_error_count": 1,
            "ws_disconnect_count": 0,
            "isolation_restored": True,
        }

    monkeypatch.setattr(l1_probe, "run_task", fake_run_task)

    assert l1_probe.main() == 2
    assert calls == ["one", "two"]
    summary = json.loads((output_dir / "summary.json").read_text())
    assert summary["scope"]["stopped_early"] is True
    assert summary["scope"]["completed_tasks"] == 2
    assert summary["status_counts"] == {"timeout": 2}


def test_l1_fixture_mismatch_fails_before_endpoint_identity(
    monkeypatch,
    tmp_path,
) -> None:
    output_dir = tmp_path / "l1-fixture-mismatch"
    args = types.SimpleNamespace(
        endpoint=ENDPOINT,
        expected_identity=dict(IDENTITY),
        fixture_base_url="https://fixtures.example",
        fixture_manifest=tmp_path / "fixture-contract.json",
        task=None,
        feature=None,
        limit=None,
        delay_ms=0,
        stop_after_transport_errors=3,
        output=output_dir,
        list=False,
    )
    monkeypatch.setattr(l1_probe, "parse_args", lambda: args)
    monkeypatch.setattr(l1_probe, "validate_endpoint", lambda _endpoint: None)
    monkeypatch.setattr(
        l1_probe,
        "load_tasks",
        lambda _args: ([types.SimpleNamespace(task_id="must_not_start")], 0),
    )
    monkeypatch.setattr(l1_probe, "source_commit", lambda: "test-source")
    monkeypatch.setattr(
        l1_probe,
        "capture_source_provenance",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        l1_probe,
        "verify_dynamic_fixture",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            l1_probe.DynamicFixtureError("fixture hash mismatch")
        ),
    )
    monkeypatch.setattr(
        l1_probe,
        "fetch_identity",
        lambda *_args: pytest.fail("endpoint identity must follow fixture preflight"),
    )

    with pytest.raises(bench.BenchError, match="fixture hash mismatch"):
        l1_probe.main()

    assert (output_dir / "provenance.json").is_file()
    assert not (output_dir / "results.jsonl").exists()


def test_l2_fixture_mismatch_fails_before_endpoint_identity(
    monkeypatch,
    tmp_path,
) -> None:
    output_dir = tmp_path / "l2-fixture-mismatch"
    args = types.SimpleNamespace(
        endpoint=ENDPOINT,
        expected_identity=dict(IDENTITY),
        fixture_base_url="https://fixtures.example",
        fixture_manifest=tmp_path / "fixture-contract.json",
        task=["must_not_start"],
        reruns=1,
        delay_ms=0,
        output=output_dir,
    )
    task = types.SimpleNamespace(task_id="must_not_start")
    monkeypatch.setattr(l2_sample, "parse_args", lambda: args)
    monkeypatch.setattr(l2_sample, "load_tasks", lambda _args: [task])
    monkeypatch.setattr(
        l2_sample,
        "load_capability_map",
        lambda: {
            task.task_id: {
                "capability_id": "cap.test",
                "category": "test",
                "observable": "answer",
                "role": "primary",
            }
        },
    )
    monkeypatch.setattr(l2_sample, "source_commit", lambda: "test-source")
    monkeypatch.setattr(
        l2_sample,
        "capture_source_provenance",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        l2_sample,
        "verify_dynamic_fixture",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            l2_sample.DynamicFixtureError("fixture hash mismatch")
        ),
    )
    monkeypatch.setattr(
        l2_sample,
        "fetch_identity",
        lambda *_args: pytest.fail("endpoint identity must follow fixture preflight"),
    )

    with pytest.raises(bench.BenchError, match="fixture hash mismatch"):
        l2_sample.main()

    assert (output_dir / "provenance.json").is_file()
    assert not (output_dir / "results.jsonl").exists()


def test_driver_fixture_mismatch_fails_before_endpoint_identity(
    monkeypatch,
    tmp_path,
) -> None:
    output_dir = tmp_path / "driver-fixture-mismatch"
    args = types.SimpleNamespace(
        endpoint=ENDPOINT,
        expected_identity=dict(IDENTITY),
        fixture_base_url="https://fixtures.example",
        fixture_manifest=tmp_path / "fixture-contract.json",
        driver=["playwright"],
        task=None,
        feature=None,
        scenario=None,
        scenario_only=False,
        limit_per_driver=None,
        reruns=1,
        delay_ms=0,
        stop_after_transport_errors=3,
        output=output_dir,
        list=False,
    )
    monkeypatch.setattr(driver_probe, "parse_args", lambda: args)
    monkeypatch.setattr(
        driver_probe,
        "load_tasks",
        lambda _args: [types.SimpleNamespace(task_id="must_not_start")],
    )
    monkeypatch.setattr(driver_probe, "source_commit", lambda: "test-source")
    monkeypatch.setattr(
        driver_probe,
        "selected_adapter_source_paths",
        lambda _tasks: (),
    )
    monkeypatch.setattr(
        driver_probe,
        "selected_adapter_executable_paths",
        lambda _tasks: (),
    )
    monkeypatch.setattr(
        driver_probe,
        "capture_source_provenance",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        driver_probe,
        "verify_dynamic_fixture",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            driver_probe.DynamicFixtureError("fixture hash mismatch")
        ),
    )
    monkeypatch.setattr(
        driver_probe,
        "fetch_identity",
        lambda *_args: pytest.fail("endpoint identity must follow fixture preflight"),
    )

    with pytest.raises(bench.BenchError, match="fixture hash mismatch"):
        driver_probe.main()

    assert (output_dir / "provenance.json").is_file()
    assert not (output_dir / "results.jsonl").exists()


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required")
@pytest.mark.parametrize(
    ("close_output", "close_confirmed"),
    [
        (json.dumps({"success": True, "data": {}}), True),
        (json.dumps({"success": False, "data": {}}), False),
        ("", False),
        ('warning\n{"success": false}', False),
        ("true", False),
    ],
    ids=["success", "reported-failure", "empty", "malformed", "non-object"],
)
def test_remote_agent_browser_attempt_is_excluded_without_live_identity(
    tmp_path,
    fake_cdp,
    close_output,
    close_confirmed,
) -> None:
    fake = fake_cdp()
    calls = tmp_path / "ab_calls.jsonl"
    fake_ab = tmp_path / "fake-agent-browser"
    fake_ab.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "args = sys.argv[1:]\n"
        "command = args[args.index('--json') + 1:]\n"
        "with open(os.environ['FAKE_AB_CALLS'], 'a', encoding='utf-8') as handle:\n"
        "    handle.write(json.dumps(command) + '\\n')\n"
        "if command == ['close']:\n"
        "    print(os.environ['FAKE_AB_CLOSE_OUTPUT'], end='')\n"
        "else:\n"
        "    data = {'cdpUrl': os.environ['FAKE_CDP_URL']} if command == ['get', 'cdp-url'] else {}\n"
        "    print(json.dumps({'success': True, 'data': data}))\n",
        encoding="utf-8",
    )
    fake_ab.chmod(0o755)
    artifact_dir = tmp_path / "artifact"
    payload = {
        "protocol": "abb_scenario_adapter/1",
        "browser_ws": fake.ws_url,
        "cdp_port": fake.port,
        "remote_cdp": True,
        "expect_product": "FakeCDP/1.0",
        "expect_ua": "",
        "task_url": "https://fixtures.example/v1/core/",
        "steps": [],
        "checks": [],
        "artifact_dir": str(artifact_dir),
        "run_id": "run",
        "task_id": "remote_agent_browser",
        "engine": "kitesurf",
        "attempt": 1,
        "task_timeout_ms": 30000,
    }
    env = dict(os.environ)
    env.update(
        {
            "AB_BIN": str(fake_ab),
            "FAKE_AB_CALLS": str(calls),
            "FAKE_CDP_URL": fake.ws_url,
            "FAKE_AB_CLOSE_OUTPUT": close_output,
        }
    )
    adapter = (
        bench.BENCH_ROOT
        / "runner"
        / "scripts"
        / "adapters"
        / "ab_scenario_adapter.js"
    )
    proc = subprocess.run(
        ["node", str(adapter)],
        cwd=bench.BENCH_ROOT,
        env=env,
        input=json.dumps(payload),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    output = json.loads(proc.stdout)
    assert output["ok"] is False
    assert "live engine identity cannot be verified" in output["error"]["message"]
    binding = output["observations"]["binding"]
    assert binding["verified"] is False
    assert binding["excluded"] is True
    assert binding["gate"] == "remote_live_identity_unavailable"
    assert output["observations"]["formal_score_eligible"] is False
    exclusion = output["observations"]["binding_exclusion_isolation"]
    assert exclusion == {
        "schema": "abb.binding_exclusion_isolation.v1",
        "driver": "agent_browser",
        "phase": "driver_session_closed",
        "scenario_started": False,
        "target_creation_requested": False,
        "cleanup": {
            "backend": "agent_browser_named_session_close",
            "required": True,
            "confirmed": close_confirmed,
            "same_named_session_as_attempt": True,
            "session": exclusion["cleanup"]["session"],
        },
    }
    assert output["observations"]["isolation_restored"] is close_confirmed
    commands = [json.loads(line) for line in calls.read_text().splitlines()]
    assert commands == [
        ["connect", fake.ws_url],
        ["get", "cdp-url"],
        ["close"],
    ]


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required")
def test_remote_mcp_is_excluded_before_driver_start(tmp_path) -> None:
    artifact_dir = tmp_path / "mcp-artifact"
    payload = {
        "browser_ws": ENDPOINT,
        "remote_cdp": True,
        "task_url": "https://fixtures.example/v1/core/",
        "steps": [],
        "checks": [],
        "artifact_dir": str(artifact_dir),
        "task_timeout_ms": 30000,
    }
    adapter = (
        bench.BENCH_ROOT
        / "runner"
        / "scripts"
        / "adapters"
        / "cdt_mcp_adapter.js"
    )

    proc = subprocess.run(
        ["node", str(adapter)],
        cwd=bench.BENCH_ROOT,
        input=json.dumps(payload),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    output = json.loads(proc.stdout)
    assert output["ok"] is False
    assert output["observations"]["failure_class"] == "binding_unverified"
    assert output["observations"]["isolation_restored"] is True
    assert output["observations"]["binding_exclusion_isolation"] == {
        "schema": "abb.binding_exclusion_isolation.v1",
        "driver": "chrome_devtools_mcp",
        "phase": "before_driver_start",
        "scenario_started": False,
        "target_creation_requested": False,
        "cleanup": {
            "backend": "not_started",
            "required": False,
            "confirmed": True,
        },
    }


@pytest.mark.parametrize(
    (
        "driver",
        "kind",
        "phase",
        "cleanup_backend",
        "cleanup_required",
        "cleanup_confirmed",
    ),
    [
        (
            "agent_browser",
            "tool_agent_browser",
            "driver_session_closed",
            "agent_browser_named_session_close",
            True,
            True,
        ),
        (
            "agent_browser",
            "tool_agent_browser",
            "driver_session_closed",
            "agent_browser_named_session_close",
            True,
            False,
        ),
        (
            "chrome_devtools_mcp",
            "mcp_chrome_devtools",
            "before_driver_start",
            "not_started",
            False,
            True,
        ),
    ],
)
def test_driver_probe_requires_audited_binding_exclusion_isolation(
    monkeypatch,
    tmp_path,
    driver,
    kind,
    phase,
    cleanup_backend,
    cleanup_required,
    cleanup_confirmed,
) -> None:
    cleanup = {
        "backend": cleanup_backend,
        "required": cleanup_required,
        "confirmed": cleanup_confirmed,
    }
    if driver == "agent_browser":
        cleanup.update(
            same_named_session_as_attempt=True,
            session="test-session",
        )
    monkeypatch.setattr(
        bench,
        "run_scenario_adapter_driver",
        lambda *_args, **_kwargs: {
            "ok": False,
            "status": "infra",
            "error": {
                "class": "script_error",
                "message": "live identity unavailable",
            },
            "observations": {
                "binding": {"verified": False, "excluded": True},
                "failure_class": "binding_unverified",
                "binding_exclusion_isolation": {
                    "schema": "abb.binding_exclusion_isolation.v1",
                    "driver": driver,
                    "phase": phase,
                    "scenario_started": False,
                    "target_creation_requested": False,
                    "cleanup": cleanup,
                },
                "isolation_restored": cleanup_confirmed,
            },
            "metrics": {},
        },
    )
    task = types.SimpleNamespace(
        driver={"kind": kind},
        task={"title": "binding exclusion"},
        task_id="binding_exclusion",
        subset_id=driver_probe.DRIVER_SUBSETS[driver],
        features=[],
        scene={"url": "/v1/core/"},
    )

    row = run_task(
        task,
        1,
        types.SimpleNamespace(),
        "https://fixtures.example",
        tmp_path,
        "run",
    )

    assert row["status"] == "infra"
    assert row["isolation_restored"] is cleanup_confirmed
    assert ("cleanup_contract_error" in row["observations"]) is (
        not cleanup_confirmed
    )


def test_agent_browser_exception_preserves_original_timeout(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(
        bench,
        "run_scenario_adapter_driver",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            subprocess.TimeoutExpired(["node", "ab_scenario_adapter.js"], 30)
        ),
    )
    task = types.SimpleNamespace(
        driver={"kind": "tool_agent_browser"},
        task={"title": "timeout"},
        task_id="agent_browser_timeout",
        subset_id="l1.agent_browser_scenarios",
        features=[],
        scene={"url": "/v1/core/"},
    )

    row = run_task(
        task,
        1,
        types.SimpleNamespace(),
        "https://fixtures.example",
        tmp_path,
        "run",
    )

    assert row["status"] == "timeout"
    assert row["isolation_restored"] is False
    assert row["observations"]["primary_status"] == "timeout"


def test_unknown_remote_driver_kind_fails_cleanly() -> None:
    task = types.SimpleNamespace(driver={"kind": "thin_unknown_driver"})

    with pytest.raises(bench.BenchError, match="unknown driver kind"):
        driver_name(task)


@pytest.mark.parametrize(
    ("kind", "relative_binary"),
    [
        ("thin_chromedp", "runner/scripts/adapters/chromedp_adapter/chromedp_adapter"),
        ("thin_rod", "runner/scripts/adapters/rod_adapter/rod_adapter"),
        (
            "thin_chromiumoxide",
            "runner/scripts/adapters/chromiumoxide_adapter/target/debug/chromiumoxide_adapter",
        ),
    ],
)
def test_compiled_adapter_binary_is_in_run_provenance(
    kind,
    relative_binary,
) -> None:
    task = types.SimpleNamespace(driver={"kind": kind})

    assert driver_probe.selected_adapter_executable_paths([task]) == (
        bench.BENCH_ROOT / relative_binary,
    )


def test_stagehand_ownership_helper_is_in_selected_route_provenance() -> None:
    task = types.SimpleNamespace(driver={"kind": "framework_stagehand"})

    paths = driver_probe.selected_adapter_source_paths([task])

    assert (
        bench.BENCH_ROOT / "runner/scripts/lib/stagehand_ownership.js"
        in paths
    )


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required")
def test_stagehand_init_pages_are_owned_only_for_fresh_remote_attempts() -> None:
    helper = bench.BENCH_ROOT / "runner/scripts/lib/stagehand_ownership.js"
    script = f"""
const {{ selectStagehandInitOwnedPages }} = require({json.dumps(str(helper))});
const runnerOwned = {{name: "runner-owned"}};
const remoteInitPage = {{name: "remote-init-page"}};
const localVisible = [runnerOwned];
const remoteVisible = [remoteInitPage];
process.stdout.write(JSON.stringify({{
  local: selectStagehandInitOwnedPages(localVisible, false).map((page) => page.name),
  remote: selectStagehandInitOwnedPages(remoteVisible, true).map((page) => page.name),
  local_input_length: localVisible.length,
  remote_input_length: remoteVisible.length,
}}));
"""
    proc = subprocess.run(
        ["node", "-e", script],
        cwd=bench.BENCH_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {
        "local": [],
        "remote": ["remote-init-page"],
        "local_input_length": 1,
        "remote_input_length": 1,
    }


def test_remote_scenario_payload_marks_direct_cdp_transport(
    monkeypatch,
    tmp_path,
) -> None:
    captured = {}

    def fake_run_driver_subprocess(*args, **kwargs):
        captured["payload"] = json.loads(kwargs["stdin_text"])
        return {"ok": True}

    monkeypatch.setattr(bench, "run_driver_subprocess", fake_run_driver_subprocess)
    monkeypatch.setattr(bench, "browser_cdp_product", lambda browser: IDENTITY["product"])
    task = types.SimpleNamespace(
        driver={
            "kind": "thin_chrome_remote_interface",
            "steps": [],
            "checks": [],
        },
        scene={"url": "/v1/core/"},
        task={"timeouts": {"task_ms": 30000}},
        task_id="remote_scenario_gate",
    )
    browser = bench.BrowserProcess(
        engine="kitesurf",
        port=43210,
        process=None,
        version_info={
            **IDENTITY,
            "Browser": IDENTITY["product"],
            "webSocketDebuggerUrl": ENDPOINT,
            "transport": "remote_cdp",
        },
    )

    bench.run_scenario_adapter_driver(
        task,
        browser,
        tmp_path,
        "https://fixtures.example",
        "run",
        "kitesurf",
        1,
        "seed",
    )

    assert captured["payload"]["remote_cdp"] is True
    assert captured["payload"]["browser_ws"] == ENDPOINT
    assert captured["payload"]["expected_remote_identity"] == {
        "product": IDENTITY["product"],
        "protocolVersion": IDENTITY["protocolVersion"],
        "revision": IDENTITY["revision"],
    }


def test_remote_scenario_adapter_rejects_product_only_verified_binding() -> None:
    output = {
        "ok": True,
        "answer": "1/1 checks",
        "observations": {
            "binding": {
                "verified": True,
                "live_product": IDENTITY["product"],
            }
        },
        "metrics": {},
    }

    result = bench.enforce_remote_scenario_adapter_identity(
        output,
        {
            "product": IDENTITY["product"],
            "protocolVersion": IDENTITY["protocolVersion"],
            "revision": IDENTITY["revision"],
        },
        driver_key="legacy_adapter",
    )

    assert result["ok"] is False
    assert result["status"] == "infra"
    binding = result["observations"]["binding"]
    assert binding["verified"] is False
    assert binding["excluded"] is True
    assert binding["gate"] == "remote_full_identity_unverified"


@pytest.mark.parametrize(
    ("connect_error", "is_transport"),
    [
        ("Browser.getVersion method unavailable", False),
        ("dial tcp: connection refused", True),
    ],
    ids=["identity-gate-semantic", "network-transport"],
)
def test_remote_scenario_adapter_rejects_unverified_gradable_output(
    connect_error,
    is_transport,
) -> None:
    output = {
        "ok": True,
        "answer": "0/1 checks",
        "grader": {
            "ok": False,
            "failure": {"class": "cdp_semantic", "detail": connect_error},
        },
        "observations": {
            "binding": {"verified": False},
            "connect_error": connect_error,
            "failure_class": "cdp_semantic",
            "target_cleanup": {
                "confirmed": True,
                "same_connection_as_task": True,
            },
            "isolation_restored": True,
        },
        "metrics": {},
    }

    result = bench.enforce_remote_scenario_adapter_identity(
        output,
        {
            "product": IDENTITY["product"],
            "protocolVersion": IDENTITY["protocolVersion"],
            "revision": IDENTITY["revision"],
        },
        driver_key="unverified_adapter",
    )

    assert result["ok"] is False
    assert result["status"] == "infra"
    observations = result["observations"]
    assert observations["failure_class"] == "binding_unverified"
    assert observations["connect_error"] == connect_error
    assert observations["binding"]["verified"] is False
    assert observations["binding"]["excluded"] is True
    assert observations["rejected_driver_output"] is output
    assert (
        is_transport_connect_failure(
            observations["connect_error"],
            result["failure"],
            observations["failure_class"],
        )
        is is_transport
    )


def test_remote_scenario_adapter_accepts_full_same_connection_binding() -> None:
    identity = {
        "product": IDENTITY["product"],
        "protocolVersion": IDENTITY["protocolVersion"],
        "revision": IDENTITY["revision"],
    }
    output = {
        "ok": True,
        "observations": {
            "binding": {
                "verified": True,
                "expected": identity,
                "actual": identity,
                "compared_fields": [
                    "product",
                    "protocolVersion",
                    "revision",
                ],
                "same_connection_as_task": True,
                "reconnect_allowed": False,
            }
        },
    }

    assert (
        bench.enforce_remote_scenario_adapter_identity(
            output, identity, driver_key="strict_adapter"
        )
        is output
    )


def test_remote_driver_cleanup_contract_rejects_unconfirmed_pass() -> None:
    output = {
        "ok": True,
        "answer": "1/1 checks",
        "observations": {"binding": {"verified": True}},
        "metrics": {"cdp_call_count": 1},
    }

    result = bench.enforce_remote_driver_cleanup(
        output,
        driver_key="test_driver",
    )

    assert result["ok"] is False
    assert result["status"] == "infra"
    assert result["observations"]["isolation_restored"] is False
    assert result["observations"]["primary_outcome"] is output


def test_remote_driver_cleanup_contract_accepts_same_connection_ack() -> None:
    output = {
        "ok": True,
        "observations": {
            "target_cleanup": {
                "confirmed": True,
                "same_connection_as_task": True,
            },
            "isolation_restored": True,
        },
    }

    assert (
        bench.enforce_remote_driver_cleanup(output, driver_key="test_driver")
        is output
    )


def test_driver_probe_defense_in_depth_rejects_pass_without_cleanup(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(
        bench,
        "run_framework_driver",
        lambda *_args, **_kwargs: {
            "ok": True,
            "answer": "primary-pass",
            "observations": {"binding": {"verified": True}},
            "metrics": {},
        },
    )
    task = types.SimpleNamespace(
        driver={"kind": "framework_playwright"},
        task={"title": "cleanup contract"},
        task_id="cleanup_contract",
        subset_id="l1.playwright",
        features=[],
        scene={"url": "/v1/core/"},
    )

    row = run_task(
        task,
        1,
        types.SimpleNamespace(),
        "https://fixtures.example",
        tmp_path,
        "run",
    )

    assert row["status"] == "infra"
    assert row["isolation_restored"] is False
    assert "same-connection" in row["failure"]["detail"]


@pytest.mark.parametrize("isolation_restored", [False, None])
def test_driver_probe_aborts_without_explicitly_confirmed_cleanup(
    monkeypatch,
    tmp_path,
    isolation_restored,
) -> None:
    output_dir = tmp_path / "driver-abort"
    args = types.SimpleNamespace(
        endpoint=ENDPOINT,
        expected_identity=dict(IDENTITY),
        fixture_base_url="https://fixtures.example",
        fixture_manifest=tmp_path / "fixture-contract.json",
        driver=["playwright"],
        task=None,
        feature=None,
        scenario=None,
        scenario_only=False,
        limit_per_driver=None,
        reruns=2,
        delay_ms=0,
        stop_after_transport_errors=3,
        output=output_dir,
        list=False,
    )
    tasks = [
        types.SimpleNamespace(
            driver={"kind": "framework_playwright"},
            task={"title": task_id},
            task_id=task_id,
            subset_id="l1.playwright",
            features=[],
            scene={"url": "/v1/core/"},
        )
        for task_id in ("one", "two")
    ]
    calls = []

    class FakeIdentityShim:
        port = 43210

        def __init__(self, *_args) -> None:
            pass

        def start(self) -> None:
            pass

        def stop(self) -> None:
            pass

    monkeypatch.setattr(driver_probe, "parse_args", lambda: args)
    monkeypatch.setattr(driver_probe, "load_tasks", lambda _args: tasks)
    monkeypatch.setattr(driver_probe, "source_commit", lambda: "test-source")
    monkeypatch.setattr(
        driver_probe,
        "capture_source_provenance",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        driver_probe,
        "verify_dynamic_fixture",
        lambda *_args, **_kwargs: {"verified": True},
    )
    monkeypatch.setattr(
        driver_probe,
        "compact_verification",
        lambda *_args, **_kwargs: {"verified": True},
    )
    monkeypatch.setattr(driver_probe, "fetch_identity", lambda *_args: dict(IDENTITY))
    monkeypatch.setattr(driver_probe, "IdentityShim", FakeIdentityShim)
    monkeypatch.setattr(driver_probe, "remote_browser", lambda *_args: object())

    def fake_run_task(task, attempt, *_args, **_kwargs):
        calls.append((task.task_id, attempt))
        return {
            "driver": "playwright",
            "task_id": task.task_id,
            "attempt": attempt,
            "status": "infra",
            "duration_ms": 1,
            "failure": {"class": "infra", "detail": "cleanup unconfirmed"},
            "isolation_restored": isolation_restored,
            "cdp_call_count": 1,
            "cdp_error_count": 0,
            "ws_disconnect_count": 0,
        }

    monkeypatch.setattr(driver_probe, "run_task", fake_run_task)

    assert driver_probe.main() == 2
    assert calls == [("one", 1)]
    summary = json.loads((output_dir / "summary.json").read_text())
    assert summary["scope"]["stopped_early"] is True
    assert "did not confirm target cleanup" in summary["scope"]["isolation_abort_reason"]


def test_driver_probe_timeout_without_cleanup_evidence_forces_isolation_abort(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(
        bench,
        "run_framework_driver",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            subprocess.TimeoutExpired(["node", "adapter.js"], 30)
        ),
    )
    task = types.SimpleNamespace(
        driver={"kind": "framework_playwright"},
        task={"title": "timeout without cleanup"},
        task_id="timeout_without_cleanup",
        subset_id="l1.playwright",
        features=[],
        scene={"url": "/v1/core/"},
    )

    row = run_task(
        task,
        1,
        types.SimpleNamespace(),
        "https://fixtures.example",
        tmp_path,
        "run",
    )

    assert row["status"] == "timeout"
    assert row["isolation_restored"] is False
    assert row["observations"]["primary_status"] == "timeout"
    assert "same-connection" in row["observations"]["cleanup_contract_error"]


def test_remote_driver_connect_timeout_is_transport_error(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(
        bench,
        "run_framework_driver",
        lambda *_args, **_kwargs: {
            "ok": False,
            "grader": {
                "failure": {
                    "class": "cdp_semantic",
                    "detail": "script-reported checks failed",
                }
            },
            "observations": {
                "connect_error": "Timeout 15000ms exceeded waiting for connection"
            },
        },
    )
    task = types.SimpleNamespace(
        driver={"kind": "framework_playwright"},
        task={"title": "remote timeout"},
        task_id="remote_timeout",
        subset_id="l1.playwright",
        features=[],
        scene={"url": "/v1/core/"},
    )

    row = run_task(
        task,
        1,
        types.SimpleNamespace(),
        "https://fixtures.example",
        tmp_path,
        "run",
    )

    assert row["status"] == "transport_error"
    assert row["failure"]["detail"].startswith("Timeout 15000ms")


def test_real_adapter_shaped_network_timeout_overrides_semantic_label(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(
        bench,
        "run_framework_driver",
        lambda *_args, **_kwargs: {
            "ok": False,
            "status": "fail",
            "failure": {
                "class": "cdp_semantic",
                "detail": "framework probe failed during connect",
            },
            "observations": {
                "connect_error": "WebSocket error: Timeout 15000ms exceeded",
                "failure_class": "cdp_semantic",
            },
        },
    )
    task = types.SimpleNamespace(
        driver={"kind": "framework_playwright"},
        task={"title": "adapter-shaped timeout"},
        task_id="adapter_timeout",
        subset_id="l1.playwright",
        features=[],
        scene={"url": "/v1/core/"},
    )

    row = run_task(
        task,
        1,
        types.SimpleNamespace(),
        "https://fixtures.example",
        tmp_path,
        "run",
    )

    assert row["status"] == "transport_error"
    assert row["failure"]["detail"].startswith("WebSocket error: Timeout")


def test_post_connect_semantic_timeout_is_not_a_transport_failure() -> None:
    assert not is_transport_connect_failure(
        None,
        {
            "class": "cdp_semantic",
            "detail": "required Network.loadingFinished event timeout",
        },
        "cdp_semantic",
    )


@pytest.mark.parametrize(
    "detail",
    [
        "page reported a TLS certificate error",
        "application displayed network error",
        "expected text was connection closed",
    ],
)
def test_post_connect_semantic_transport_words_do_not_reclassify_failure(
    detail,
) -> None:
    assert not is_transport_connect_failure(
        None,
        {"class": "cdp_semantic", "detail": detail},
        "cdp_semantic",
    )


def test_binding_unverified_exclusion_is_not_a_transport_failure() -> None:
    assert not is_transport_connect_failure(
        "binding unverified: live identity operation unavailable",
        {"class": "script_error", "detail": "attempt excluded"},
        "binding_unverified",
    )


def test_binding_unverified_does_not_hide_recognized_network_failure() -> None:
    assert is_transport_connect_failure(
        "WebSocket error: ETIMEDOUT during handshake",
        {"class": "script_error", "detail": "attempt excluded"},
        "binding_unverified",
    )


@pytest.mark.parametrize(
    "connect_error",
    [
        "dial tcp 127.0.0.1:9222: connect: connection refused",
        "read tcp 10.0.0.2:443: connection reset by peer",
        "Failed to open TCP connection (Connection refused - connect(2))",
        "[Errno 111] Connect call failed ('127.0.0.1', 9222)",
        "OSError: [Errno 113] No route to host",
        "socket: temporary failure in name resolution",
    ],
)
def test_native_socket_connect_errors_are_transport_failures(
    connect_error,
) -> None:
    assert is_transport_connect_failure(
        connect_error,
        {"class": "cdp_semantic", "detail": "adapter connect failed"},
        "cdp_semantic",
    )


def test_remote_driver_semantic_connect_failure_stays_semantic(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(
        bench,
        "run_framework_driver",
        lambda *_args, **_kwargs: {
            "ok": False,
            "grader": {
                "failure": {
                    "class": "cdp_semantic",
                    "detail": "script-reported checks failed",
                }
            },
            "observations": {
                "connect_error": "WebSocket connected but init command unsupported",
                "failure_class": "cdp_semantic",
                "target_cleanup": {
                    "confirmed": True,
                    "same_connection_as_task": True,
                    "target_created": False,
                },
                "isolation_restored": True,
            },
        },
    )
    task = types.SimpleNamespace(
        driver={"kind": "framework_playwright"},
        task={"title": "remote semantic init failure"},
        task_id="remote_semantic_init",
        subset_id="l1.playwright",
        features=[],
        scene={"url": "/v1/core/"},
    )

    row = run_task(
        task,
        1,
        types.SimpleNamespace(),
        "https://fixtures.example",
        tmp_path,
        "run",
    )

    assert row["status"] == "fail"
    assert row["failure"]["class"] == "cdp_semantic"
