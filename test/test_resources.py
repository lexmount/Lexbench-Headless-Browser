from __future__ import annotations

import json
import os
import pathlib
import signal
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request

import pytest

from runner import resources
from runner import run as runner_run
from _fakes import CLOSE, make_l2_task_dict, make_resolved, stub_browser


def test_process_sampler_calibrates_cpu_and_complete_tree_pss():
    script = """
import subprocess
import sys
import time
child = subprocess.Popen([
    sys.executable,
    "-c",
    "import time; payload=bytearray(24*1024*1024); time.sleep(3)",
])
deadline = time.monotonic() + 0.35
value = 0
while time.monotonic() < deadline:
    value = (value * 33 + 17) % 1000003
time.sleep(2)
"""
    proc = subprocess.Popen(
        [sys.executable, "-c", script],
        start_new_session=True,
    )
    sampler = resources.EngineProcessSampler(
        proc.pid,
        cgroup=None,
        sample_interval_ms=20,
    )
    try:
        sampler.start()
        time.sleep(0.65)
        summary, samples = sampler.stop(650)
    finally:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=5)

    assert summary["measurement_backend"]["cpu"] == "proc_tree"
    assert summary["cpu_total_ms"] > 50
    assert summary["pss_peak_bytes"] > 12 * 1024 * 1024
    assert summary["process_count_peak"] >= 2
    assert summary["samples_seen"] >= 3
    assert samples[0]["phase"] == "baseline"
    assert samples[-1]["phase"] == "end"


def test_process_tree_snapshot_retries_transient_pss_race(monkeypatch):
    calls = []

    def capture(_root_pid, _proc_root):
        calls.append(len(calls) + 1)
        return {
            "root_alive": True,
            "pss_bytes": None if len(calls) == 1 else 1234,
            "pss_errors": ["42:FileNotFoundError"] if len(calls) == 1 else [],
        }

    monkeypatch.setattr(resources, "_process_tree_snapshot_once", capture)
    snapshot = resources.process_tree_snapshot(42)
    assert snapshot["pss_bytes"] == 1234
    assert snapshot["pss_scan_attempts"] == 2
    assert calls == [1, 2]


def test_process_tree_pss_treats_confirmed_zombie_as_zero(monkeypatch):
    zombie = resources.ProcStat(
        pid=42,
        ppid=1,
        session=1,
        state="Z",
        user_ticks=2,
        system_ticks=1,
        start_ticks=99,
    )
    monkeypatch.setattr(resources, "read_process_tree", lambda _pid, _root: {42: zombie})
    monkeypatch.setattr(resources, "read_pss_bytes", lambda *_args: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(resources, "_read_text", lambda _path: "stat")
    monkeypatch.setattr(resources, "parse_proc_stat", lambda _text: zombie)

    snapshot = resources._process_tree_snapshot_once(42, pathlib.Path("/proc"))
    assert snapshot["pss_bytes"] == 0
    assert snapshot["pss_errors"] == []
    assert snapshot["pss_zero_address_space_pids"] == [42]


def test_cgroup_v2_cpu_and_memory_backend_when_delegated():
    group, reason = resources.CgroupV2Group.create(
        f"pytest-resource-{time.time_ns()}"
    )
    if group is None:
        pytest.skip(reason or "cgroup v2 delegation unavailable")
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import time; x=bytearray(8*1024*1024); "
            "end=time.monotonic()+0.25; "
            "exec('while time.monotonic() < end:\\n pass'); time.sleep(2)",
        ],
        start_new_session=True,
    )
    try:
        assert group.add_process_tree(proc.pid) == []
        sampler = resources.EngineProcessSampler(
            proc.pid,
            group,
            sample_interval_ms=20,
        )
        sampler.start()
        time.sleep(0.45)
        summary, _ = sampler.stop(450)
        assert summary["measurement_backend"]["cpu"] == "cgroup_v2"
        assert summary["cpu_total_ms"] > 20
        assert summary["cgroup_memory_peak_bytes"] is not None
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        finally:
            group.cleanup()


def test_fixture_traffic_exact_payload_and_harness_exclusion(tmp_path):
    tracker = resources.FixtureTrafficTracker()
    server = runner_run.FixtureServer(
        fixtures_dir=tmp_path,
        expected_answers={},
        traffic_tracker=tracker,
    )
    base = server.start()
    try:
        tracker.begin_attempt("attempt-1", "session-1")
        request_body = b"Q" * 4096
        request = urllib.request.Request(
            base + "/__resource__/echo?response_bytes=2048",
            data=request_body,
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            assert len(response.read()) == 2048
        measured = tracker.end_attempt("attempt-1")
        assert measured["available"] is True
        assert measured["fixture_request_count"] == 1
        assert measured["fixture_app_rx_body_bytes"] == 4096
        assert measured["fixture_app_tx_body_bytes"] == 2048
        assert measured["fixture_app_rx_header_bytes"] > 0
        assert measured["fixture_app_tx_header_bytes"] > 0

        tracker.begin_attempt("attempt-2", "session-2")
        grader = urllib.request.Request(
            base + "/__grade__/expected_answer",
            data=b'{"task_id":"missing","answer":""}',
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(grader, timeout=5) as response:
            response.read()
        excluded = tracker.end_attempt("attempt-2")
        assert excluded["fixture_request_count"] == 0
        assert excluded["excluded_harness_request_count"] == 1
    finally:
        server.stop()


def test_fixture_traffic_redirect_sse_websocket_upload_download(tmp_path):
    download_body = b"D" * 3072
    landing_body = b"<h1>redirect landed</h1>"
    (tmp_path / "download.bin").write_bytes(download_body)
    (tmp_path / "landing.html").write_bytes(landing_body)

    tracker = resources.FixtureTrafficTracker()
    server = runner_run.FixtureServer(
        fixtures_dir=tmp_path,
        expected_answers={},
        traffic_tracker=tracker,
    )
    server.routes["/v0_4/redirect/landing"] = "landing.html"
    base = server.start()
    try:
        tracker.begin_attempt("protocols", "protocol-session")

        with urllib.request.urlopen(base + "/fixtures/download.bin", timeout=5) as response:
            assert response.read() == download_body

        with urllib.request.urlopen(base + "/v0_4/redirect/hop?n=1", timeout=5) as response:
            assert response.read() == landing_body

        with urllib.request.urlopen(base + "/__sse__/messages", timeout=5) as response:
            sse_body = response.read()
        assert sse_body == b"".join(
            f"data: wsd-sse-{index}\n\n".encode("ascii") for index in range(3)
        )

        boundary = "abb-resource-boundary"
        upload_payload = b"U" * 1536
        upload_body = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="known.bin"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode("ascii") + upload_payload + f"\r\n--{boundary}--\r\n".encode("ascii")
        upload = urllib.request.Request(
            base + "/v0_4/upload",
            data=upload_body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        with urllib.request.urlopen(upload, timeout=5) as response:
            upload_response = response.read()
        assert b"uploaded:known.bin:1536:" in upload_response

        parsed = urllib.parse.urlparse(base)
        ws_payload = b"known-websocket-payload"
        with socket.create_connection((parsed.hostname, parsed.port), timeout=5) as sock:
            sock.sendall(
                (
                    "GET /__ws__/echo HTTP/1.1\r\n"
                    f"Host: {parsed.hostname}:{parsed.port}\r\n"
                    "Upgrade: websocket\r\n"
                    "Connection: Upgrade\r\n"
                    "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                    "Sec-WebSocket-Version: 13\r\n\r\n"
                ).encode("ascii")
            )
            handshake = b""
            while b"\r\n\r\n" not in handshake:
                handshake += sock.recv(4096)
            assert handshake.startswith(b"HTTP/1.0 101") or handshake.startswith(b"HTTP/1.1 101")
            runner_run._ws_write_frame(sock, ws_payload)
            opcode, echoed = runner_run._ws_read_frame(sock)
            assert opcode == 0x1
            assert echoed == b"echo:" + ws_payload
            runner_run._ws_write_frame(sock, b"", opcode=0x8)
            runner_run._ws_read_frame(sock)

        measured = tracker.end_attempt("protocols")
        assert measured["available"] is True
        assert measured["fixture_request_count"] == 7
        assert measured["fixture_app_rx_body_bytes"] == len(upload_body) + len(ws_payload)
        assert measured["fixture_app_tx_body_bytes"] == (
            len(download_body)
            + len(landing_body)
            + len(sse_body)
            + len(upload_response)
            + len(b"echo:")
            + len(ws_payload)
        )
    finally:
        server.stop()


def test_host_pollution_flags_swap_psi_and_process_growth():
    base = {
        "timestamp": "start",
        "memory": {
            "total_bytes": 1000,
            "available_bytes": 900,
            "swap_used_bytes": 0,
        },
        "psi": {
            "cpu": {"some": {"avg10": 0.0}},
            "memory": {"full": {"avg10": 0.0}},
        },
        "vmstat": {"pswpin_pages": 0, "pswpout_pages": 0},
        "bench_descendant_process_count": 2,
    }
    polluted = {
        "timestamp": "end",
        "memory": {
            "total_bytes": 1000,
            "available_bytes": 20,
            "swap_used_bytes": 128,
        },
        "psi": {
            "cpu": {"some": {"avg10": 55.0}},
            "memory": {"full": {"avg10": 2.0}},
        },
        "vmstat": {"pswpin_pages": 1, "pswpout_pages": 3},
        "bench_descendant_process_count": 20,
    }
    result = resources.evaluate_host_pollution([base, polluted])
    assert result["polluted"] is True
    assert {
        "swap_in_use",
        "swap_activity",
        "low_memory_available",
        "memory_pressure",
        "cpu_pressure",
        "bench_process_growth",
    }.issubset(result["flags"])


def _resource_row(engine: str, attempt: int, status: str = "pass"):
    return {
        "task_id": "resource_smoke",
        "layer": "L1",
        "subset_id": "l1.raw_cdp",
        "driver": "raw_cdp",
        "engine": engine,
        "attempt": attempt,
        "status": status,
        "duration_ms": 100,
        "resource": {
            "cpu_total_ms": 50 + attempt,
            "cpu_user_ms": 40 + attempt,
            "cpu_system_ms": 10,
            "avg_cores": 0.5,
            "pss_baseline_bytes": 10_000_000,
            "pss_peak_bytes": 11_000_000,
            "pss_end_bytes": 10_500_000,
            "pss_peak_delta_bytes": 1_000_000,
            "process_count_peak": 3,
            "process_count_end": 2,
            "fixture_traffic": {
                "available": True,
                "fixture_app_rx_body_bytes": 100,
                "fixture_app_tx_body_bytes": 200,
            },
        },
    }


def test_resource_summary_uses_only_all_pass_intersection():
    rows = [
        _resource_row(engine, attempt)
        for attempt in range(1, 6)
        for engine in ("chrome", "moli", "lightpanda")
    ]
    rows.extend(
        [
            _resource_row("chrome", 6),
            _resource_row("moli", 6, status="fail"),
            _resource_row("lightpanda", 6),
        ]
    )
    manifest = {
        "run_id": "resource-test",
        "selected_engines": ["chrome", "moli", "lightpanda"],
        "k_runs": 5,
        "score_mode": "independent",
        "runner": {"jobs": 1, "browser_reuse": "per_run_process_per_engine"},
        "resolved_tasks": [
            {
                "task_id": "resource_smoke",
                "layer": "L1",
                "subset_id": "l1.raw_cdp",
                "driver": "raw_cdp",
                "tags": ["family.runtime"],
            }
        ],
        "resource_profile": {
            "mode": "engine",
            "engine_order": "balanced_rotation",
            "sample_interval_ms": 250,
        },
        "host": {},
        "engines": {},
    }
    summary = resources.summarize_resources(
        manifest,
        rows,
        {"polluted": False, "flags": []},
        {"acceptable": True},
    )
    assert summary["resource_comparison_eligible"] is True
    assert summary["all_pass_intersection"]["attempts"] == 5
    assert summary["excluded"]["status_by_engine"]["moli"] == {"fail": 1}
    assert summary["by_engine"]["chrome"]["metrics"]["cpu_total_ms"]["n"] == 5
    assert (
        summary["stratified"]["family"]["family.runtime"]["by_engine"]["chrome"]
        ["metrics"]["cpu_total_ms"]["n"]
        == 5
    )


def test_resource_summary_supports_candidate_trio_with_obscura():
    engines = ("moli", "lightpanda", "obscura")
    rows = [
        _resource_row(engine, attempt)
        for attempt in range(1, 6)
        for engine in engines
    ]
    manifest = {
        "run_id": "candidate-resource-test",
        "selected_engines": list(engines),
        "k_runs": 5,
        "score_mode": "independent",
        "runner": {"jobs": 1, "browser_reuse": "per_run_process_per_engine"},
        "resolved_tasks": [
            {
                "task_id": "resource_smoke",
                "layer": "L1",
                "subset_id": "l1.raw_cdp",
                "driver": "raw_cdp",
                "tags": ["family.runtime"],
            }
        ],
        "resource_profile": {
            "mode": "engine",
            "engine_order": "balanced_rotation",
            "sample_interval_ms": 250,
        },
        "host": {},
        "engines": {engine: {} for engine in engines},
    }

    summary = resources.summarize_resources(
        manifest,
        rows,
        {"polluted": False, "flags": []},
        {"acceptable": True},
    )

    assert summary["resource_comparison_eligible"] is True
    assert summary["engines"] == list(engines)
    assert summary["all_pass_intersection"]["attempts"] == 5
    assert summary["by_engine"]["obscura"]["intersection_attempts"] == 5


def test_duration_calibration_requires_matching_provenance():
    rows = [
        {
            "task_id": "resource_smoke",
            "engine": engine,
            "attempt": attempt,
            "status": "pass",
            "duration_ms": 101,
            "resource": {"collection_wall_ms": 105},
        }
        for attempt in range(1, 6)
        for engine in ("chrome", "moli", "lightpanda")
    ]
    baseline_rows = [{**row, "duration_ms": 100} for row in rows]

    def manifest(mode: str, seed: str = "same-seed"):
        return {
            "seed": seed,
            "k_runs": 5,
            "selected_engines": ["chrome", "moli", "lightpanda"],
            "score_mode": "independent",
            "resolved_tasks": [{"task_id": "resource_smoke", "sha256": "abc"}],
            "runner": {
                "jobs": 1,
                "browser_reuse": "per_run_process_per_engine",
                "harness_pins": {"drivers": {}},
                "source": {"tree_sha256": "runner-source"},
            },
            "engines": {"chrome": {}, "moli": {}, "lightpanda": {}},
            "host": {"kernel": "test"},
            "resource_profile": {
                "mode": mode,
                "engine_order": "balanced_rotation",
                "engine_order_algorithm": "cyclic_task_attempt_seed_offset_v1",
            },
        }

    accepted = resources.duration_calibration(
        rows,
        baseline_rows,
        10,
        profiled_manifest=manifest("engine"),
        baseline_manifest=manifest("baseline"),
    )
    assert accepted["acceptable"] is True
    assert accepted["provenance_mismatches"] == []

    rejected = resources.duration_calibration(
        rows,
        baseline_rows,
        10,
        profiled_manifest=manifest("engine"),
        baseline_manifest=manifest("baseline", seed="different"),
    )
    assert rejected["acceptable"] is False
    assert rejected["provenance_mismatches"] == ["seed"]


def test_resource_failure_never_changes_functional_status(
    tmp_path, fake_cdp
):
    fake = fake_cdp(
        {
            "Runtime.evaluate": {
                "result": {"result": {"type": "number", "value": 3}}
            }
        }
    )
    browser = stub_browser(fake.port)
    runtime = runner_run.ResourceRuntime(tmp_path / "run", "resource-fake", 20)
    task = make_resolved(subset_gate="off")
    result = runner_run.run_driver_attempt(
        tmp_path / "run",
        tmp_path / "run" / "results.jsonl",
        "resource-fake",
        task,
        "moli",
        1,
        "seed",
        browser,
        {"required": False, "status": "off", "chrome_attempt_ref": None},
        score_eligible=True,
        fixture_base_url=None,
        resource_runtime=runtime,
    )
    assert result["status"] == "pass"
    assert result["resource"]["unavailable"]["pss"]


def test_engine_exit_during_resource_stop_overrides_pass_and_persists(
    tmp_path, fake_cdp, monkeypatch
):
    fake = fake_cdp(
        {
            "Runtime.evaluate": {
                "result": {"result": {"type": "number", "value": 3}}
            }
        }
    )
    browser = stub_browser(fake.port)

    class ExitsDuringSamplerStop:
        pid = 424242
        returncode = None

        def poll(self):
            return self.returncode

    browser.process = ExitsDuringSamplerStop()

    class ExitOnStopSampler:
        def __init__(self, *_args, **_kwargs):
            pass

        def start(self):
            pass

        def stop(self, _duration_ms):
            browser.process.returncode = -signal.SIGSEGV
            return (
                {
                    "schema": resources.ENGINE_RESOURCE_SCHEMA,
                    "scope": "engine_scope",
                    "quality_flags": ["engine_root_exited"],
                },
                [],
            )

    monkeypatch.setattr(
        runner_run.resource_metrics,
        "EngineProcessSampler",
        ExitOnStopSampler,
    )
    run_dir = tmp_path / "run"
    runtime = runner_run.ResourceRuntime(run_dir, "resource-exit", 20)
    task = make_resolved(subset_gate="off")
    result = runner_run.run_driver_attempt(
        run_dir,
        run_dir / "results.jsonl",
        "resource-exit",
        task,
        "moli",
        1,
        "seed",
        browser,
        {"required": False, "status": "off", "chrome_attempt_ref": None},
        score_eligible=True,
        fixture_base_url=None,
        resource_runtime=runtime,
    )

    assert result["status"] == "crash"
    assert result["failure"]["origin"] == "engine_process"
    assert result["failure"]["process"]["signal_name"] == "SIGSEGV"
    assert result["failure"]["secondary_status"] == "pass"
    assert "secondary_failure" not in result["failure"]
    assert result["resource"]["quality_flags"] == ["engine_root_exited"]
    artifact_dir = run_dir / result["artifact_dir"]
    assert json.loads((artifact_dir / "run.json").read_text()) == result
    grader = json.loads((artifact_dir / "grader.json").read_text())
    assert grader["ok"] is False
    assert grader["failure"] == result["failure"]
    assert runner_run.read_jsonl(run_dir / "results.jsonl") == [result]


@pytest.mark.parametrize("driver_error", [False, True])
def test_rawws_closes_owned_target_on_normal_and_error_paths(
    fake_cdp, driver_error
):
    state = {"targets": set()}

    def create(_params):
        state["targets"].add("TARGET-1")
        return {"result": {"targetId": "TARGET-1"}}

    def close(params):
        state["targets"].discard(params["targetId"])
        return {"result": {"success": True}}

    fake = fake_cdp(
        {
            "Target.createTarget": create,
            "Target.attachToTarget": {
                "result": {"sessionId": "SESSION-1"}
            },
            "Target.closeTarget": close,
        }
    )
    rawws = pathlib.Path(runner_run.BENCH_ROOT / "runner/scripts/lib/rawws.js")
    script = f"""
const {{ CdpSession }} = require({json.dumps(str(rawws))});
(async () => {{
  const session = new CdpSession({json.dumps(fake.ws_url)}, null);
  await session.connect();
  try {{
    await session.openPage({fake.port});
    if ({str(driver_error).lower()}) throw new Error("synthetic driver error");
  }} catch (err) {{
  }} finally {{
    await session.close();
  }}
}})().catch((err) => {{ console.error(err); process.exit(1); }});
"""
    proc = subprocess.run(
        ["node", "-e", script],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
    )
    assert proc.returncode == 0, proc.stderr
    assert state["targets"] == set()
    methods = [request["method"] for request in fake.requests]
    assert methods[-1] == "Target.closeTarget"


def test_rawws_closes_json_new_fallback_target(tmp_path, fake_cdp):
    fake = fake_cdp(
        {
            "Target.createTarget": {
                "error": {"code": -32601, "message": "unsupported"}
            }
        }
    )
    rawws = pathlib.Path(runner_run.BENCH_ROOT / "runner/scripts/lib/rawws.js")
    trace = tmp_path / "cdp.jsonl"
    script = f"""
const {{ CdpSession }} = require({json.dumps(str(rawws))});
(async () => {{
  const session = new CdpSession(
    {json.dumps(fake.ws_url)},
    {json.dumps(str(trace))}
  );
  await session.connect();
  await session.openPage({fake.port});
  await session.close();
}})().catch((err) => {{ console.error(err); process.exit(1); }});
"""
    proc = subprocess.run(
        ["node", "-e", script],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
    )
    assert proc.returncode == 0, proc.stderr
    events = [
        json.loads(line)
        for line in trace.read_text(encoding="utf-8").splitlines()
    ]
    cleanup = [event for event in events if event.get("direction") == "cleanup"]
    assert cleanup[-1]["method"] == "HTTP /json/close"
    assert cleanup[-1]["closed"] is True


def test_runner_timeout_cleanup_closes_probe_target(
    tmp_path, fake_cdp, fixture_server
):
    state = {"targets": set()}

    def targets(_params):
        return {
            "result": {
                "targetInfos": [
                    {"targetId": target, "type": "page"}
                    for target in sorted(state["targets"])
                ]
            }
        }

    def create(_params):
        state["targets"].add("LEAK-1")
        return {"result": {"targetId": "LEAK-1"}}

    def close(params):
        state["targets"].discard(params["targetId"])
        return {"result": {"success": True}}

    fake = fake_cdp(
        {
            "Target.getTargets": targets,
            "Target.createTarget": create,
            "Target.attachToTarget": {"result": {"sessionId": "SESSION-1"}},
            "Target.closeTarget": close,
        }
    )
    rawws = pathlib.Path(runner_run.BENCH_ROOT / "runner/scripts/lib/rawws.js")
    hanging_script = tmp_path / "hanging_probe.js"
    hanging_script.write_text(
        f"""
const {{ CdpSession }} = require({json.dumps(str(rawws))});
(async () => {{
  const session = new CdpSession(process.env.BROWSER_WS, null);
  await session.connect();
  await session.openPage(Number(process.env.CDP_PORT));
  setInterval(() => {{}}, 1000);
}})();
""",
        encoding="utf-8",
    )
    task = make_resolved(
        task=make_l2_task_dict(
            task_id="target_timeout_cleanup",
            driver={"kind": "node_cdp_probe", "script": str(hanging_script)},
            # Leave enough room for a loaded CI host to start Node and create
            # the target before exercising the hard-timeout cleanup path.
            timeouts={"task_ms": 2000, "hard_kill_ms": 4000},
        ),
        subset_gate="off",
    )
    browser = stub_browser(fake.port)
    browser.version_info["webSocketDebuggerUrl"] = fake.ws_url
    server = fixture_server(expected_answers={})
    result = runner_run.run_driver_attempt(
        tmp_path / "run",
        tmp_path / "run" / "results.jsonl",
        "target-timeout",
        task,
        "moli",
        1,
        "seed",
        browser,
        {"required": False, "status": "off", "chrome_attempt_ref": None},
        score_eligible=True,
        fixture_base_url=server.base_url,
    )
    assert result["status"] == "timeout"
    assert state["targets"] == set()
    cleanup_path = tmp_path / "run" / result["artifact_dir"] / "target_cleanup.json"
    cleanup = json.loads(cleanup_path.read_text(encoding="utf-8"))
    assert "Target.closeTarget" in [
        request["method"] for request in fake.requests
    ]
    assert cleanup["new_targets"] == ["LEAK-1"]
    assert cleanup["closed_targets"] == ["LEAK-1"]
    assert cleanup["errors"] == []


def test_target_cleanup_reconnects_after_close_disconnect(fake_cdp):
    state = {"targets": {"LEAK-RETRY"}, "close_calls": 0}

    def targets(_params):
        return {
            "result": {
                "targetInfos": [
                    {"targetId": target, "type": "page"}
                    for target in sorted(state["targets"])
                ]
            }
        }

    def close(params):
        state["close_calls"] += 1
        if state["close_calls"] == 1:
            return CLOSE
        state["targets"].discard(params["targetId"])
        return {"result": {"success": True}}

    fake = fake_cdp(
        {
            "Target.getTargets": targets,
            "Target.closeTarget": close,
        }
    )
    browser = stub_browser(fake.port)
    browser.version_info["webSocketDebuggerUrl"] = fake.ws_url

    cleanup = runner_run.cleanup_new_page_targets(browser, set())

    assert state["close_calls"] == 2
    assert state["targets"] == set()
    assert cleanup["new_targets"] == ["LEAK-RETRY"]
    assert cleanup["closed_targets"] == ["LEAK-RETRY"]
    assert cleanup["retry_closed_targets"] == ["LEAK-RETRY"]
    assert cleanup["remaining_new_targets"] == []
    assert cleanup["final"] == []
    assert any(error.startswith("LEAK-RETRY:") for error in cleanup["errors"])


def test_target_cleanup_failure_does_not_replace_driver_result(
    tmp_path, monkeypatch
):
    artifact_dir = tmp_path / "artifact"
    artifact_dir.mkdir()
    task = make_resolved(task=make_l2_task_dict(), subset_gate="off")
    browser = stub_browser(9222)
    browser.version_info["webSocketDebuggerUrl"] = "ws://127.0.0.1:9222/devtools/browser/fake"
    expected = {
        "ok": True,
        "status": "pass",
        "answer": "driver-result",
        "observations": {},
        "grader": {"ok": True, "checks": []},
        "metrics": {},
    }
    monkeypatch.setattr(runner_run, "browser_page_target_ids", lambda _browser: set())
    monkeypatch.setattr(
        runner_run, "run_node_driver_process", lambda *_args, **_kwargs: expected
    )
    monkeypatch.setattr(
        runner_run,
        "cleanup_new_page_targets",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("cleanup failed")),
    )

    result = runner_run.run_node_cdp_probe_driver(
        task,
        browser,
        artifact_dir,
        "http://127.0.0.1:9999",
        "cleanup-failure",
        "moli",
        1,
        "seed",
    )
    assert result is expected


@pytest.mark.parametrize(
    ("driver_kind", "runner_name"),
    [
        ("framework_playwright", "run_framework_driver"),
        ("tool_agent_browser", "run_scenario_adapter_driver"),
    ],
)
def test_outer_target_guard_cleans_framework_and_adapter_targets(
    tmp_path,
    fake_cdp,
    fixture_server,
    monkeypatch,
    driver_kind,
    runner_name,
):
    state = {"targets": set()}

    def targets(_params):
        return {
            "result": {
                "targetInfos": [
                    {"targetId": target, "type": "page"}
                    for target in sorted(state["targets"])
                ]
            }
        }

    def close(params):
        state["targets"].discard(params["targetId"])
        return {"result": {"success": True}}

    fake = fake_cdp(
        {
            "Target.getTargets": targets,
            "Target.closeTarget": close,
        }
    )
    browser = stub_browser(fake.port)
    browser.version_info["webSocketDebuggerUrl"] = fake.ws_url
    task = make_resolved(
        task=make_l2_task_dict(
            task_id=f"outer_cleanup_{driver_kind}",
            scene={
                "kind": "self_hosted_fixture",
                "url": "/fixtures/stub.html?seed={seed}&session={session}",
            },
            driver={
                "kind": driver_kind,
                "steps": [{"op": "goto"}],
                "checks": [{"kind": "step_ok", "step": 0}],
            },
        ),
        subset_gate="off",
    )

    def synthetic_driver(*_args, **_kwargs):
        state["targets"].add("LEAK-OUTER")
        return {
            "ok": True,
            "answer": "ok",
            "observations": {},
            "grader": {"ok": True, "checks": []},
            "metrics": {},
        }

    monkeypatch.setattr(runner_run, runner_name, synthetic_driver)
    server = fixture_server(expected_answers={})
    result = runner_run.run_driver_attempt(
        tmp_path / "run",
        tmp_path / "run" / "results.jsonl",
        "outer-cleanup",
        task,
        "moli",
        1,
        "seed",
        browser,
        {"required": False, "status": "off", "chrome_attempt_ref": None},
        score_eligible=True,
        fixture_base_url=server.base_url,
    )

    assert result["status"] == "pass"
    assert state["targets"] == set()
    cleanup_path = (
        tmp_path / "run" / result["artifact_dir"] / "target_cleanup.json"
    )
    cleanup = json.loads(cleanup_path.read_text(encoding="utf-8"))
    assert cleanup["new_targets"] == ["LEAK-OUTER"]
    assert cleanup["closed_targets"] == ["LEAK-OUTER"]
    assert cleanup["remaining_new_targets"] == []
    assert cleanup["final"] == []
    assert cleanup["errors"] == []


def test_agent_browser_namespace_cleanup_removes_only_disposable_configs(
    tmp_path,
):
    env = {
        "AGENT_BROWSER_SOCKET_DIR": str(tmp_path),
        "AGENT_BROWSER_NAMESPACE": "a0123456789ab",
    }
    run_dir = (
        tmp_path
        / "namespaces"
        / env["AGENT_BROWSER_NAMESPACE"]
        / "run"
    )
    run_dir.mkdir(parents=True)
    (run_dir / "abb-session.config").write_text("{}", encoding="utf-8")
    runner_run.remove_agent_browser_namespace_state(env)
    assert not run_dir.parent.exists()

    run_dir.mkdir(parents=True)
    (run_dir / "abb-session.config").write_text("{}", encoding="utf-8")
    (run_dir / "abb-session.pid").write_text("123", encoding="utf-8")
    runner_run.remove_agent_browser_namespace_state(env)
    assert (run_dir / "abb-session.config").exists()
    assert (run_dir / "abb-session.pid").exists()


def test_successful_l1_agent_browser_probe_gets_outer_close_confirmation(
    tmp_path,
    monkeypatch,
):
    artifact_dir = tmp_path / "artifact"
    artifact_dir.mkdir()
    task = make_resolved(
        task=make_l2_task_dict(
            task_id="agent_browser_outer_close",
            driver={
                "kind": "node_cdp_probe",
                "script": "runner/scripts/l1_ab_probe.js",
            },
        ),
        subset_gate="off",
    )
    browser = stub_browser(9222)
    browser.version_info["webSocketDebuggerUrl"] = (
        "ws://127.0.0.1:9222/devtools/browser/fake"
    )
    expected = {
        "ok": True,
        "answer": "ok",
        "observations": {},
        "grader": {"ok": True, "checks": []},
        "metrics": {},
    }
    closes = []
    removals = []
    monkeypatch.setattr(
        runner_run, "run_node_driver_process", lambda *_a, **_k: expected
    )
    monkeypatch.setattr(
        runner_run, "browser_page_target_ids", lambda _browser: set()
    )
    monkeypatch.setattr(
        runner_run,
        "cleanup_new_page_targets",
        lambda *_a, **_k: {"errors": []},
    )
    monkeypatch.setattr(
        runner_run,
        "force_close_agent_browser_attempt",
        lambda env: closes.append(dict(env)),
    )
    monkeypatch.setattr(
        runner_run,
        "remove_agent_browser_namespace_state",
        lambda env: removals.append(dict(env)),
    )

    result = runner_run.run_node_cdp_probe_driver(
        task,
        browser,
        artifact_dir,
        "http://127.0.0.1:9999",
        "outer-close",
        "lightpanda",
        1,
        "seed",
    )

    assert result is expected
    assert len(closes) == 1
    assert len(removals) == 1
    assert closes[0]["AGENT_BROWSER_NAMESPACE"].startswith("a")
    assert removals[0]["AGENT_BROWSER_NAMESPACE"] == (
        closes[0]["AGENT_BROWSER_NAMESPACE"]
    )


def test_successful_agent_browser_adapter_gets_outer_close_confirmation(
    tmp_path,
    monkeypatch,
):
    artifact_dir = tmp_path / "artifact"
    artifact_dir.mkdir()
    task = make_resolved(
        task=make_l2_task_dict(
            task_id="agent_browser_adapter_outer_close",
            scene={
                "kind": "self_hosted_fixture",
                "url": "/fixtures/stub.html?seed={seed}&session={session}",
            },
            driver={
                "kind": "tool_agent_browser",
                "steps": [{"op": "goto"}],
                "checks": [{"kind": "step_ok", "step": 0}],
            },
        ),
        subset_gate="off",
    )
    browser = stub_browser(9222)
    browser.version_info.update(
        {
            "webSocketDebuggerUrl": (
                "ws://127.0.0.1:9222/devtools/browser/fake"
            ),
            "Browser": "Lightpanda/1.0",
            "User-Agent": "fake",
        }
    )
    expected = {
        "ok": True,
        "answer": "ok",
        "observations": {},
        "grader": {"ok": True, "checks": []},
        "metrics": {},
    }
    closes = []
    removals = []
    monkeypatch.setattr(
        runner_run, "browser_cdp_product", lambda _browser: "Lightpanda/1.0"
    )
    monkeypatch.setattr(
        runner_run, "run_driver_subprocess", lambda *_a, **_k: expected
    )
    monkeypatch.setattr(
        runner_run,
        "force_close_agent_browser_attempt",
        lambda env: closes.append(dict(env)),
    )
    monkeypatch.setattr(
        runner_run,
        "remove_agent_browser_namespace_state",
        lambda env: removals.append(dict(env)),
    )

    result = runner_run.run_scenario_adapter_driver(
        task,
        browser,
        artifact_dir,
        "http://127.0.0.1:9999",
        "outer-close",
        "lightpanda",
        1,
        "seed",
    )

    assert result is expected
    assert len(closes) == 1
    assert len(removals) == 1
    assert closes[0]["AGENT_BROWSER_NAMESPACE"].startswith("a")
    assert removals[0]["AGENT_BROWSER_NAMESPACE"] == (
        closes[0]["AGENT_BROWSER_NAMESPACE"]
    )
