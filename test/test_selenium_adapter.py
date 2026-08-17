from __future__ import annotations

import copy
import hashlib
import http.server
import io
import json
import pathlib
import signal
import socket
import subprocess
import sys
import textwrap
import threading
import time

import pytest

from runner.scripts.adapters import selenium_adapter


def make_executable(root: pathlib.Path) -> pathlib.Path:
    executable = root / "build_artifacts" / "chromedriver" / "bin" / "chromedriver"
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    return executable.resolve()


def make_http_chromedriver(
    root: pathlib.Path,
    *,
    ready: bool = True,
    reject_session: bool = False,
    ignore_sigterm: bool = False,
) -> tuple[pathlib.Path, pathlib.Path, pathlib.Path]:
    executable = root / "fake_chromedriver"
    state_path = root / "fake_chromedriver_state.json"
    request_path = root / "fake_chromedriver_requests.log"
    executable.write_text(
        textwrap.dedent(
            f"""\
            #!{sys.executable}
            import http.server
            import json
            import pathlib
            import signal
            import sys

            READY = {ready!r}
            REJECT_SESSION = {reject_session!r}
            IGNORE_SIGTERM = {ignore_sigterm!r}
            STATE_PATH = pathlib.Path({str(state_path)!r})
            REQUEST_PATH = pathlib.Path({str(request_path)!r})
            port = int(next(arg.split("=", 1)[1] for arg in sys.argv[1:] if arg.startswith("--port=")))
            if IGNORE_SIGTERM:
                signal.signal(signal.SIGTERM, signal.SIG_IGN)

            class Handler(http.server.BaseHTTPRequestHandler):
                def send_json(self, status, body):
                    payload = json.dumps(body).encode("utf-8")
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)

                def do_GET(self):
                    if self.path == "/status":
                        self.send_json(200, {{"value": {{"ready": READY, "message": "fake"}}}})
                    else:
                        self.send_json(404, {{"value": {{"error": "unknown command"}}}})

                def do_POST(self):
                    REQUEST_PATH.open("a", encoding="utf-8").write(f"POST {{self.path}}\\n")
                    if self.path != "/session":
                        self.send_json(404, {{"value": {{"error": "unknown command"}}}})
                    elif REJECT_SESSION:
                        self.send_json(500, {{"value": {{
                            "error": "session not created",
                            "message": "fake session rejection",
                            "stacktrace": "",
                        }}}})
                    else:
                        self.send_json(200, {{"value": {{
                            "sessionId": "fake-session",
                            "capabilities": {{
                                "browserName": "chrome",
                                "browserVersion": "Fake/1.0",
                            }},
                        }}}})

                def do_DELETE(self):
                    REQUEST_PATH.open("a", encoding="utf-8").write(f"DELETE {{self.path}}\\n")
                    self.send_json(200, {{"value": None}})

                def log_message(self, *args):
                    pass

            server = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
            server.daemon_threads = True
            STATE_PATH.write_text(json.dumps({{
                "pid": __import__("os").getpid(),
                "port": port,
                "argv": sys.argv[1:],
            }}), encoding="utf-8")
            server.serve_forever()
            """
        ),
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable.resolve(), state_path, request_path


def port_is_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.1):
            return True
    except OSError:
        return False


def wait_for_process_absent(pid: int, timeout_s: float = 2.0) -> None:
    stat_path = pathlib.Path(f"/proc/{pid}/stat")
    deadline = time.monotonic() + timeout_s
    while stat_path.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not stat_path.exists(), f"process {pid} was not reaped"


def assertion(
    mechanism: str,
    actual_path: str,
    expected_ref: str | None,
    condition: str,
    *,
    expected_literal: str | None = None,
) -> dict:
    row = {
        "mechanism": mechanism,
        "actual_path": actual_path,
        "operator": "equals",
        "condition": condition,
    }
    if expected_ref is not None:
        row["expected_ref"] = expected_ref
    else:
        row["expected_literal"] = expected_literal
    return row


def binding_payload(
    engine: str,
    route_id: str,
    root: pathlib.Path,
) -> dict:
    route = copy.deepcopy(selenium_adapter.ROUTE_CONTRACTS[route_id])
    route["route_id"] = route_id
    route["identity"] = {
        "http_assertions": [
            assertion("http_json_version", "Browser|Product", "expect_product", "always"),
            assertion("http_json_version", "User-Agent", "expect_ua", "expected_nonempty"),
            assertion("http_json_version", "webSocketDebuggerUrl", "browser_ws", "when_present"),
        ],
        "live_transport_assertions": (
            [
                assertion(
                    "webdriver_capabilities",
                    "capabilities.browserName",
                    None,
                    "always",
                    expected_literal="moli",
                ),
                assertion(
                    "webdriver_capabilities",
                    "capabilities.browserVersion",
                    "expect_product_live",
                    "always",
                ),
            ]
            if route_id == "native_webdriver"
            else [
                assertion(
                    "selenium_cdp_extension",
                    "execute_cdp_cmd.Browser.getVersion.product",
                    "expect_product_live",
                    "always",
                )
            ]
        ),
    }
    bridges = []
    if route_id == "chromedriver_cdp":
        executable = make_executable(root)
        bridges = [
            {
                "ref_id": "bridge.chromedriver",
                "key": "chromedriver",
                "metadata": {
                    "version": "150.0.0.0",
                    "binary_path": "build_artifacts/chromedriver/bin/chromedriver",
                    "sha256_12": hashlib.sha256(executable.read_bytes()).hexdigest()[:12],
                },
                "executable": str(executable),
            }
        ]
    return {
        "binding_id": f"{engine}__selenium",
        "browser_id": engine,
        "driver_id": "selenium",
        "route": route,
        "pins": {
            "browser": {"ref_id": f"browser.{engine}", "key": engine},
            "driver": {
                "ref_id": "driver.selenium",
                "key": "selenium",
                "metadata": {"version": "4.46.0", "pip_package": "selenium"},
            },
            "bridges": bridges,
        },
        "fallback_allowed": False,
    }


def adapter_payload(
    engine: str,
    route_id: str,
    root: pathlib.Path,
    *,
    transport_policy: str | None = "engine_native",
) -> dict:
    return {
        "protocol": "abb_scenario_adapter/1",
        "driver_kind": "webdriver_selenium",
        "driver_key": "selenium",
        "engine": engine,
        "binding": binding_payload(engine, route_id, root),
        "browser_ws": "ws://127.0.0.1:9333/devtools/browser/abc",
        "cdp_port": 9333,
        "expect_product": "Moli/0.1.0",
        "expect_ua": "MoliUA/1.0",
        "expect_product_live": "MoliLive/0.1",
        "transport_policy": transport_policy,
        "task_url": "http://127.0.0.1:18080/l1/core",
        "steps": [],
        "checks": [{"kind": "saved_truthy", "name": "answer"}],
        "connect_timeout_ms": 15_000,
        "task_timeout_ms": 30_000,
        "artifact_dir": str(root / "artifacts"),
    }


class HangingSessionHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        self.server.request_count += 1
        time.sleep(2)
        try:
            self.send_response(500)
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *args):
        pass


@pytest.fixture
def hanging_session_server():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), HangingSessionHandler)
    server.daemon_threads = True
    server.request_count = 0
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}", server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_native_webdriver_options_use_binding_browser_name_without_chrome_capabilities():
    capabilities = selenium_adapter.native_webdriver_options(
        "moli",
        connect_timeout_ms=15_000,
    ).to_capabilities()

    assert capabilities["browserName"] == "moli"
    assert capabilities["timeouts"] == {
        "pageLoad": 15_000,
        "script": 20_000,
    }
    assert "goog:chromeOptions" not in capabilities


def test_native_session_creation_honors_connect_timeout(hanging_session_server):
    server_url, server = hanging_session_server
    started = time.monotonic()
    with pytest.raises(Exception):
        selenium_adapter.start_native_webdriver(
            server_url,
            "moli",
            100,
        )
    assert time.monotonic() - started < 0.8
    assert server.request_count == 1


def test_session_request_tracker_marks_only_after_request_construction():
    commands: list[str] = []
    requests: list[tuple] = []

    class Connection:
        def request(self, *args, **kwargs):
            requests.append((args, kwargs))
            return {"value": None}

    class Executor:
        def __init__(self):
            self._conn = Connection()

        def execute(self, command, params):
            json.dumps(params)
            commands.append(command)
            if command == "newSession":
                return self._conn.request("POST", "/session")
            return {"value": None}

    executor = Executor()
    original_connection = executor._conn
    tracker = selenium_adapter.SessionRequestTracker(executor)
    tracker.execute("status", {})
    assert tracker.request_started is False
    with pytest.raises(TypeError):
        tracker.execute("newSession", {"not_json": {object()}})
    assert tracker.request_started is False
    tracker.execute("newSession", {})
    assert tracker.request_started is True
    assert commands == ["status", "newSession"]
    assert len(requests) == 1
    tracker.restore()
    assert executor._conn is original_connection


def test_session_request_tracker_marks_transport_error_and_restores_boundary():
    class Connection:
        def request(self, *args, **kwargs):
            raise ConnectionError("request reached transport")

    class Executor:
        def __init__(self):
            self._conn = Connection()

        def execute(self, command, params):
            return self._conn.request("POST", "/session")

    executor = Executor()
    original_connection = executor._conn
    tracker = selenium_adapter.SessionRequestTracker(executor)

    with pytest.raises(ConnectionError, match="reached transport"):
        tracker.execute("newSession", {})

    assert tracker.request_started is True
    tracker.restore()
    assert executor._conn is original_connection


def test_session_request_tracker_rejects_missing_transport_boundary():
    with pytest.raises(
        selenium_adapter.SeleniumClientConfigurationError,
        match="does not expose its HTTP request boundary",
    ):
        selenium_adapter.SessionRequestTracker(object())


def test_repository_bridge_uses_http_status_and_closes_port_idempotently(tmp_path):
    executable, state_path, request_path = make_http_chromedriver(tmp_path)
    port = selenium_adapter.free_port()
    bridge = selenium_adapter.ChromeDriverBridge(executable, port)

    process = None
    try:
        bridge.start_until(time.monotonic() + 2)
        process = bridge.process
        assert process is not None
        state = json.loads(state_path.read_text(encoding="utf-8"))
        assert state["pid"] == process.pid
        assert state["port"] == port
        assert state["argv"] == ["--enable-chrome-logs", f"--port={port}"]
        assert port_is_open(port)
        assert not request_path.exists()

        bridge.stop()
        bridge.stop()

        assert process.poll() is not None
        wait_for_process_absent(process.pid)
        assert not port_is_open(port)
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=1)


def test_repository_bridge_rejects_tcp_only_not_ready_service(tmp_path):
    executable, state_path, request_path = make_http_chromedriver(
        tmp_path,
        ready=False,
    )
    port = selenium_adapter.free_port()
    bridge = selenium_adapter.ChromeDriverBridge(executable, port)
    started = time.monotonic()

    with pytest.raises(
        selenium_adapter.SeleniumClientConfigurationError,
        match="HTTP service did not become ready",
    ):
        bridge.start_until(time.monotonic() + 1.0)

    assert time.monotonic() - started < 1.5
    state = json.loads(state_path.read_text(encoding="utf-8"))
    wait_for_process_absent(state["pid"])
    assert not port_is_open(port)
    assert not request_path.exists()


def test_repository_bridge_escalates_sigterm_and_reaps_before_deadline(tmp_path):
    executable, state_path, _ = make_http_chromedriver(
        tmp_path,
        ignore_sigterm=True,
    )
    port = selenium_adapter.free_port()
    bridge = selenium_adapter.ChromeDriverBridge(executable, port)
    process = None
    try:
        bridge.start_until(time.monotonic() + 2)
        process = bridge.process
        assert process is not None
        assert json.loads(state_path.read_text(encoding="utf-8"))["pid"] == process.pid
        assert port_is_open(port)

        started = time.monotonic()
        deadline = started + 0.5
        bridge.stop(deadline=deadline)
        elapsed = time.monotonic() - started
        bridge.stop(deadline=deadline)

        assert elapsed <= 0.7
        assert bridge.process is None
        assert process.returncode == -signal.SIGKILL
        wait_for_process_absent(process.pid)
        assert not port_is_open(port)
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=1)


def test_repository_bridge_cleanup_interrupt_preserves_interrupt_and_reaps(
    tmp_path,
    monkeypatch,
):
    executable, _, _ = make_http_chromedriver(
        tmp_path,
        ignore_sigterm=True,
    )
    port = selenium_adapter.free_port()
    bridge = selenium_adapter.ChromeDriverBridge(executable, port)
    process = None
    try:
        bridge.start_until(time.monotonic() + 2)
        process = bridge.process
        assert process is not None
        real_wait = process.wait
        interruption = KeyboardInterrupt()
        calls = 0

        def interrupting_wait(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise interruption
            return real_wait(*args, **kwargs)

        monkeypatch.setattr(process, "wait", interrupting_wait)
        with pytest.raises(KeyboardInterrupt) as raised:
            bridge.stop(deadline=time.monotonic() + 0.5)

        assert raised.value is interruption
        assert bridge.process is None
        assert process.returncode == -signal.SIGKILL
        wait_for_process_absent(process.pid)
        assert not port_is_open(port)
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            real_wait = getattr(process, "wait")
            real_wait(timeout=1)


def test_repository_bridge_early_exit_is_local_configuration_error(tmp_path):
    executable = make_executable(tmp_path)
    bridge = selenium_adapter.ChromeDriverBridge(
        executable,
        selenium_adapter.free_port(),
    )

    with pytest.raises(
        selenium_adapter.SeleniumClientConfigurationError,
        match="exited before HTTP readiness with code 0",
    ):
        bridge.start_until(time.monotonic() + 1)

    bridge.stop()


def test_real_new_session_success_uses_owned_bridge(tmp_path):
    executable, state_path, request_path = make_http_chromedriver(tmp_path)

    driver = bridge = process = None
    try:
        driver, bridge = selenium_adapter.start_chromedriver_webdriver(
            executable,
            "127.0.0.1:9333",
            2000,
        )
        process = bridge.process
        assert process is not None
        assert driver.session_id == "fake-session"
        assert "POST /session" in request_path.read_text(encoding="utf-8")
        assert json.loads(state_path.read_text(encoding="utf-8"))["pid"] == process.pid

        driver.quit()
        driver = None
        bridge.stop()
        bridge.stop()

        wait_for_process_absent(process.pid)
        assert not port_is_open(bridge.port)
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass
        if bridge is not None:
            bridge.stop()
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=1)


def test_real_post_new_session_failure_is_compatibility_exception_and_reaps_bridge(
    tmp_path,
):
    executable, state_path, request_path = make_http_chromedriver(
        tmp_path,
        reject_session=True,
    )

    with pytest.raises(Exception, match="fake session rejection") as raised:
        selenium_adapter.start_chromedriver_webdriver(
            executable,
            "127.0.0.1:9333",
            1000,
        )

    assert not isinstance(
        raised.value,
        selenium_adapter.SeleniumClientConfigurationError,
    )
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert "POST /session" in request_path.read_text(encoding="utf-8")
    wait_for_process_absent(state["pid"])
    assert not port_is_open(state["port"])


def test_repository_bridge_keyboard_interrupt_reaps_real_process(
    tmp_path,
    monkeypatch,
):
    executable, _, _ = make_http_chromedriver(tmp_path, ready=False)
    bridge = selenium_adapter.ChromeDriverBridge(
        executable,
        selenium_adapter.free_port(),
    )
    processes: list[subprocess.Popen] = []
    real_popen = selenium_adapter.subprocess.Popen

    def capturing_popen(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(selenium_adapter.subprocess, "Popen", capturing_popen)
    monkeypatch.setattr(
        selenium_adapter,
        "chromedriver_status_ready",
        lambda *args: (_ for _ in ()).throw(KeyboardInterrupt()),
    )

    with pytest.raises(KeyboardInterrupt):
        bridge.start_until(time.monotonic() + 1)

    assert len(processes) == 1
    wait_for_process_absent(processes[0].pid)


def test_real_bridge_startup_failure_is_structured_script_error(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(selenium_adapter, "BENCH_ROOT", tmp_path)
    payload = adapter_payload("chrome", "chromedriver_cdp", tmp_path)
    emitted: list[dict] = []
    monkeypatch.setattr(
        selenium_adapter.sys,
        "stdin",
        io.StringIO(json.dumps(payload)),
    )
    monkeypatch.setattr(selenium_adapter, "emit", emitted.append)
    monkeypatch.setattr(
        selenium_adapter,
        "http_json",
        lambda *args: {
            "Browser": payload["expect_product"],
            "User-Agent": payload["expect_ua"],
            "webSocketDebuggerUrl": payload["browser_ws"],
        },
    )

    selenium_adapter.main()

    result = emitted[0]
    assert result["ok"] is False
    assert result["error"]["class"] == "script_error"
    assert "exited before HTTP readiness with code 0" in result["error"]["message"]
    assert "checks" not in result["observations"]


def test_real_post_new_session_failure_is_main_compatibility_evidence(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(selenium_adapter, "BENCH_ROOT", tmp_path)
    payload = adapter_payload("chrome", "chromedriver_cdp", tmp_path)
    fake_executable, state_path, request_path = make_http_chromedriver(
        tmp_path,
        reject_session=True,
    )
    pinned_executable = pathlib.Path(
        payload["binding"]["pins"]["bridges"][0]["executable"]
    )
    pinned_executable.write_bytes(fake_executable.read_bytes())
    pinned_executable.chmod(0o755)
    payload["binding"]["pins"]["bridges"][0]["metadata"]["sha256_12"] = (
        hashlib.sha256(pinned_executable.read_bytes()).hexdigest()[:12]
    )
    emitted: list[dict] = []
    monkeypatch.setattr(
        selenium_adapter.sys,
        "stdin",
        io.StringIO(json.dumps(payload)),
    )
    monkeypatch.setattr(selenium_adapter, "emit", emitted.append)
    monkeypatch.setattr(
        selenium_adapter,
        "http_json",
        lambda *args: {
            "Browser": payload["expect_product"],
            "User-Agent": payload["expect_ua"],
            "webSocketDebuggerUrl": payload["browser_ws"],
        },
    )

    selenium_adapter.main()

    result = emitted[0]
    assert result["ok"] is True
    assert result["observations"]["failure_class"] == "cdp_semantic"
    assert "fake session rejection" in result["observations"]["connect_error"]
    assert result["observations"]["checks"][0]["name"] == "driver_connect"
    assert result["observations"]["checks"][0]["status"] == "fail"
    assert "POST /session" in request_path.read_text(encoding="utf-8")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    wait_for_process_absent(state["pid"])
    assert not port_is_open(state["port"])


def test_bridge_session_creation_honors_timeout_and_stops_bridge(
    tmp_path,
    monkeypatch,
    hanging_session_server,
):
    server_url, server = hanging_session_server
    executable = make_executable(tmp_path)
    stop_calls: list[bool] = []

    class FakeBridge:
        def __init__(self, path, port):
            self.path = path
            self.port = port
            self.service_url = server_url

        def start_until(self, deadline, *, cleanup_deadline=None):
            pass

        def stop(self, *, deadline=None):
            stop_calls.append(True)

    monkeypatch.setattr(
        selenium_adapter,
        "ChromeDriverBridge",
        FakeBridge,
    )
    started = time.monotonic()
    with pytest.raises(Exception):
        selenium_adapter.start_chromedriver_webdriver(
            executable,
            "127.0.0.1:9333",
            300,
        )
    assert time.monotonic() - started < 1.0
    assert server.request_count == 1
    assert stop_calls == [True]


def test_bridge_http_readiness_shares_connect_deadline_and_stops_once(
    tmp_path,
    monkeypatch,
):
    executable = make_executable(tmp_path)
    stop_calls: list[bool] = []
    starts: list[str] = []

    class NeverReadyBridge:
        def __init__(self, path, port):
            self.path = path
            self.port = port
            self.service_url = f"http://127.0.0.1:{port}"
            self.stopped = False

        def start_until(self, deadline, *, cleanup_deadline=None):
            starts.append(str(self.path))
            while time.monotonic() < deadline:
                time.sleep(0.005)
            self.stop(deadline=cleanup_deadline)
            raise selenium_adapter.SeleniumClientConfigurationError(
                "ChromeDriver HTTP service did not become ready before connect timeout"
            )

        def stop(self, *, deadline=None):
            if not self.stopped:
                self.stopped = True
                stop_calls.append(True)

    monkeypatch.setattr(
        selenium_adapter,
        "ChromeDriverBridge",
        NeverReadyBridge,
    )
    monkeypatch.setattr(
        "selenium.webdriver.Remote",
        lambda **kwargs: pytest.fail("session POST must not start before service readiness"),
    )

    started = time.monotonic()
    with pytest.raises(
        selenium_adapter.SeleniumClientConfigurationError,
        match="did not become ready",
    ):
        selenium_adapter.start_chromedriver_webdriver(
            executable,
            "127.0.0.1:9333",
            100,
        )
    assert time.monotonic() - started < 0.35
    assert starts == [str(executable)]
    assert stop_calls == [True]


def test_normalized_route_overrides_engine_and_legacy_transport_policy(tmp_path, monkeypatch):
    monkeypatch.setattr(selenium_adapter, "BENCH_ROOT", tmp_path)
    calls: list[tuple] = []
    monkeypatch.setattr(
        selenium_adapter,
        "start_native_webdriver",
        lambda endpoint, browser_name, timeout: calls.append(
            ("native", endpoint, browser_name, timeout)
        )
        or "native-driver",
    )
    monkeypatch.setattr(
        selenium_adapter,
        "start_chromedriver_webdriver",
        lambda executable, endpoint, timeout, cleanup_deadline: calls.append(
            ("bridge", executable, endpoint, timeout, cleanup_deadline)
        )
        or ("bridge-driver", "service"),
    )

    # Chrome follows a synthetic native binding; no engine-name inference.
    chrome_native = adapter_payload(
        "chrome",
        "native_webdriver",
        tmp_path,
        transport_policy="chromedriver_cdp",
    )
    native_binding = selenium_adapter.validate_runtime_binding(chrome_native)
    assert selenium_adapter.connect_webdriver(chrome_native, native_binding, 1234) == (
        "native-driver",
        None,
    )

    # Moli follows a synthetic bridge binding even though legacy metadata says native.
    moli_bridge = adapter_payload(
        "moli",
        "chromedriver_cdp",
        tmp_path,
        transport_policy="engine_native",
    )
    bridge_binding = selenium_adapter.validate_runtime_binding(moli_bridge)
    assert selenium_adapter.connect_webdriver(moli_bridge, bridge_binding, 4321) == (
        "bridge-driver",
        "service",
    )
    assert [row[0] for row in calls] == ["native", "bridge"]
    assert calls[-1][-1] is None


def test_native_route_never_resolves_or_checks_chromedriver(tmp_path, monkeypatch):
    monkeypatch.setattr(selenium_adapter, "BENCH_ROOT", tmp_path)
    payload = adapter_payload("moli", "native_webdriver", tmp_path)
    monkeypatch.setattr(
        selenium_adapter,
        "_bridge_executable",
        lambda binding: pytest.fail("native route must not inspect ChromeDriver"),
    )
    monkeypatch.setattr(
        selenium_adapter,
        "start_native_webdriver",
        lambda *args: "native-driver",
    )
    binding = selenium_adapter.validate_runtime_binding(payload)
    assert selenium_adapter.connect_webdriver(payload, binding, 15000) == (
        "native-driver",
        None,
    )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda payload: payload.pop("binding"), "payload.binding must be an object"),
        (
            lambda payload: payload["binding"].update(browser_id="chrome"),
            "does not match payload.engine",
        ),
        (
            lambda payload: payload["binding"].update(driver_id="playwright"),
            "must be `selenium`",
        ),
        (
            lambda payload: payload["binding"]["route"].update(route_id="mystery"),
            "unknown Selenium route",
        ),
        (
            lambda payload: payload["binding"]["route"].pop("ordered_hops"),
            "ordered_hops must be a non-empty list",
        ),
        (
            lambda payload: payload["binding"].update(fallback_allowed=True),
            "fallback_allowed must be false",
        ),
    ],
)
def test_invalid_binding_is_rejected_without_fallback(
    tmp_path,
    monkeypatch,
    mutation,
    message,
):
    monkeypatch.setattr(selenium_adapter, "BENCH_ROOT", tmp_path)
    payload = adapter_payload("moli", "native_webdriver", tmp_path)
    mutation(payload)
    with pytest.raises(selenium_adapter.BindingPayloadError, match=message):
        selenium_adapter.validate_runtime_binding(payload)


def test_semantically_mutated_route_is_rejected_before_driver_construction(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(selenium_adapter, "BENCH_ROOT", tmp_path)
    payload = adapter_payload("moli", "native_webdriver", tmp_path)
    payload["binding"]["route"]["provider"] = "chromedriver"
    monkeypatch.setattr(
        selenium_adapter,
        "start_native_webdriver",
        lambda *args: pytest.fail("invalid binding must fail before construction"),
    )
    with pytest.raises(
        selenium_adapter.BindingPayloadError,
        match="provider does not match",
    ):
        selenium_adapter.validate_runtime_binding(payload)


def test_missing_binding_fails_before_http_or_driver_construction(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(selenium_adapter, "BENCH_ROOT", tmp_path)
    payload = adapter_payload("moli", "native_webdriver", tmp_path)
    payload.pop("binding")
    emitted: list[dict] = []
    monkeypatch.setattr(selenium_adapter.sys, "stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(selenium_adapter, "emit", emitted.append)
    monkeypatch.setattr(
        selenium_adapter,
        "http_json",
        lambda *args: pytest.fail("invalid binding must fail before HTTP"),
    )
    monkeypatch.setattr(
        selenium_adapter,
        "connect_webdriver",
        lambda *args: pytest.fail("invalid binding must fail before driver construction"),
    )

    selenium_adapter.main()

    assert emitted[0]["ok"] is False
    assert emitted[0]["error"]["class"] == "script_error"
    assert "payload.binding" in emitted[0]["error"]["message"]


def test_connect_failure_is_compatibility_evidence_not_skip_or_script_error(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(selenium_adapter, "BENCH_ROOT", tmp_path)
    payload = adapter_payload("moli", "native_webdriver", tmp_path)
    emitted: list[dict] = []
    monkeypatch.setattr(selenium_adapter.sys, "stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(selenium_adapter, "emit", emitted.append)
    monkeypatch.setattr(
        selenium_adapter,
        "http_json",
        lambda *args: {
            "Browser": payload["expect_product"],
            "User-Agent": payload["expect_ua"],
            "webSocketDebuggerUrl": payload["browser_ws"],
        },
    )
    monkeypatch.setattr(
        selenium_adapter,
        "connect_webdriver",
        lambda *args: (_ for _ in ()).throw(RuntimeError("session rejected")),
    )

    selenium_adapter.main()

    result = emitted[0]
    assert result["ok"] is True
    assert result["observations"]["connect_error"] == "session rejected"
    assert result["observations"]["binding"]["binding_id"] == "moli__selenium"
    assert result["observations"]["binding"]["route_id"] == "native_webdriver"
    assert result["observations"]["checks"][0]["name"] == "driver_connect"
    assert result["observations"]["checks"][0]["status"] == "fail"
    assert "skip" not in result


def test_missing_client_import_is_script_error_not_compatibility(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(selenium_adapter, "BENCH_ROOT", tmp_path)
    payload = adapter_payload("moli", "native_webdriver", tmp_path)
    emitted: list[dict] = []
    monkeypatch.setattr(
        selenium_adapter.sys,
        "stdin",
        io.StringIO(json.dumps(payload)),
    )
    monkeypatch.setattr(selenium_adapter, "emit", emitted.append)
    monkeypatch.setattr(
        selenium_adapter,
        "http_json",
        lambda *args: {
            "Browser": payload["expect_product"],
            "User-Agent": payload["expect_ua"],
            "webSocketDebuggerUrl": payload["browser_ws"],
        },
    )
    monkeypatch.setattr(
        selenium_adapter,
        "connect_webdriver",
        lambda *args: (_ for _ in ()).throw(
            ModuleNotFoundError("No module named 'selenium'")
        ),
    )

    selenium_adapter.main()

    result = emitted[0]
    assert result["ok"] is False
    assert result["error"]["class"] == "script_error"
    assert "Selenium client configuration error" in result["error"]["message"]
    assert result["observations"]["binding"]["binding_id"] == "moli__selenium"
    assert "checks" not in result.get("observations", {})


@pytest.mark.parametrize(
    "setup_error",
    [
        pytest.param(TypeError("bad ClientConfig signature"), id="type-error"),
        pytest.param(RuntimeError("corrupt local client"), id="runtime-error"),
        pytest.param(ValueError("invalid local option"), id="value-error"),
        pytest.param(OSError("local metadata unavailable"), id="os-error"),
    ],
)
def test_deterministic_local_client_api_failure_is_script_error(
    tmp_path,
    monkeypatch,
    setup_error,
):
    monkeypatch.setattr(selenium_adapter, "BENCH_ROOT", tmp_path)
    payload = adapter_payload("moli", "native_webdriver", tmp_path)
    emitted: list[dict] = []
    monkeypatch.setattr(
        selenium_adapter.sys,
        "stdin",
        io.StringIO(json.dumps(payload)),
    )
    monkeypatch.setattr(selenium_adapter, "emit", emitted.append)
    monkeypatch.setattr(
        selenium_adapter,
        "http_json",
        lambda *args: {
            "Browser": payload["expect_product"],
            "User-Agent": payload["expect_ua"],
            "webSocketDebuggerUrl": payload["browser_ws"],
        },
    )
    monkeypatch.setattr(
        selenium_adapter,
        "webdriver_client_config",
        lambda *args: (_ for _ in ()).throw(setup_error),
    )

    selenium_adapter.main()

    result = emitted[0]
    assert result["ok"] is False
    assert result["error"]["class"] == "script_error"
    assert "Selenium client configuration error" in result["error"]["message"]
    assert "API setup failed" in result["error"]["message"]


@pytest.mark.parametrize(
    ("route_id", "engine"),
    [
        ("native_webdriver", "moli"),
        ("chromedriver_cdp", "chrome"),
    ],
)
@pytest.mark.parametrize("phase", ["before-request", "after-request"])
def test_session_request_boundary_classifies_errors_and_cleans_bridge_once(
    tmp_path,
    monkeypatch,
    route_id,
    engine,
    phase,
):
    monkeypatch.setattr(selenium_adapter, "BENCH_ROOT", tmp_path)
    payload = adapter_payload(engine, route_id, tmp_path)
    emitted: list[dict] = []
    stop_calls: list[bool] = []
    session_error = (
        RuntimeError("corrupt local Remote constructor")
        if phase == "before-request"
        else AttributeError("malformed remote session response")
    )

    class FakeExecutor:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self._conn = self

        def request(self, *args, **kwargs):
            raise session_error

        def execute(self, command, params):
            if command == "newSession":
                return self._conn.request("POST", "/session")
            raise session_error

    class FakeBridge:
        def __init__(self, path, port):
            self.path = path
            self.port = port
            self.service_url = f"http://127.0.0.1:{port}"

        def start_until(self, deadline, *, cleanup_deadline=None):
            pass

        def stop(self, *, deadline=None):
            if not stop_calls:
                stop_calls.append(True)

    def remote_constructor(*, command_executor, options):
        if phase == "before-request":
            raise session_error
        return command_executor.execute("newSession", {"capabilities": {}})

    monkeypatch.setattr(
        selenium_adapter.sys,
        "stdin",
        io.StringIO(json.dumps(payload)),
    )
    monkeypatch.setattr(selenium_adapter, "emit", emitted.append)
    monkeypatch.setattr(
        selenium_adapter,
        "http_json",
        lambda *args: {
            "Browser": payload["expect_product"],
            "User-Agent": payload["expect_ua"],
            "webSocketDebuggerUrl": payload["browser_ws"],
        },
    )
    monkeypatch.setattr("selenium.webdriver.Remote", remote_constructor)
    if route_id == "native_webdriver":
        monkeypatch.setattr(
            "selenium.webdriver.remote.remote_connection.RemoteConnection",
            FakeExecutor,
        )
    else:
        monkeypatch.setattr(
            selenium_adapter,
            "ChromeDriverBridge",
            FakeBridge,
        )
        monkeypatch.setattr(
            "selenium.webdriver.chromium.remote_connection.ChromiumRemoteConnection",
            FakeExecutor,
        )

    selenium_adapter.main()

    result = emitted[0]
    if phase == "before-request":
        assert result["ok"] is False
        assert result["error"]["class"] == "script_error"
        assert "before starting a session request" in result["error"]["message"]
    else:
        assert result["ok"] is True
        assert result["observations"]["connect_error"] == str(session_error)
        assert result["observations"]["checks"][0]["name"] == "driver_connect"
        assert result["observations"]["checks"][0]["status"] == "fail"
        assert result["observations"]["failure_class"] == "cdp_semantic"
    assert stop_calls == ([True] if route_id == "chromedriver_cdp" else [])


def test_binding_rejects_installed_client_version_mismatch(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(selenium_adapter, "BENCH_ROOT", tmp_path)
    monkeypatch.setattr(selenium_adapter, "CLIENT_VERSION", "4.45.0")
    payload = adapter_payload("moli", "native_webdriver", tmp_path)

    with pytest.raises(
        selenium_adapter.BindingPayloadError,
        match="installed Selenium client version does not match binding pin",
    ):
        selenium_adapter.validate_runtime_binding(payload)


@pytest.mark.parametrize(
    ("route_id", "engine"),
    [
        ("native_webdriver", "moli"),
        ("chromedriver_cdp", "lightpanda"),
    ],
)
def test_successful_main_verifies_both_identity_layers_and_cleans_up_once(
    tmp_path,
    monkeypatch,
    route_id,
    engine,
):
    monkeypatch.setattr(selenium_adapter, "BENCH_ROOT", tmp_path)
    payload = adapter_payload(engine, route_id, tmp_path)
    payload["checks"] = []
    emitted: list[dict] = []
    quit_calls: list[bool] = []
    stop_calls: list[bool] = []

    class FakeDriver:
        capabilities = {
            "browserName": "moli",
            "browserVersion": payload["expect_product_live"],
        }

        def execute_cdp_cmd(self, method, params):
            assert method == "Browser.getVersion"
            assert params == {}
            return {"product": payload["expect_product_live"]}

        def quit(self):
            quit_calls.append(True)

    class FakeService:
        def stop(self):
            stop_calls.append(True)

    monkeypatch.setattr(selenium_adapter.sys, "stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(selenium_adapter, "emit", emitted.append)
    monkeypatch.setattr(
        selenium_adapter,
        "http_json",
        lambda *args: {
            "Browser": payload["expect_product"],
            "User-Agent": payload["expect_ua"],
            "webSocketDebuggerUrl": payload["browser_ws"],
        },
    )
    monkeypatch.setattr(
        selenium_adapter,
        "connect_webdriver",
        lambda *args: (
            FakeDriver(),
            FakeService() if route_id == "chromedriver_cdp" else None,
        ),
    )

    selenium_adapter.main()

    result = emitted[0]
    assert result["ok"] is True
    observation = result["observations"]["binding"]
    assert observation["binding_id"] == f"{engine}__selenium"
    assert observation["route_id"] == route_id
    assert observation["verified"] is True
    assert all(row["status"] in {"verified", "not_applicable"} for row in observation["identity"]["http"])
    assert all(row["status"] == "verified" for row in observation["identity"]["live"])
    assert quit_calls == [True]
    assert stop_calls == ([True] if route_id == "chromedriver_cdp" else [])


def test_http_identity_mismatch_fails_before_webdriver_construction(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(selenium_adapter, "BENCH_ROOT", tmp_path)
    payload = adapter_payload("moli", "native_webdriver", tmp_path)
    emitted: list[dict] = []
    monkeypatch.setattr(selenium_adapter.sys, "stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(selenium_adapter, "emit", emitted.append)
    monkeypatch.setattr(
        selenium_adapter,
        "http_json",
        lambda *args: {
            "Browser": "Wrong/1.0",
            "User-Agent": payload["expect_ua"],
            "webSocketDebuggerUrl": payload["browser_ws"],
        },
    )
    monkeypatch.setattr(
        selenium_adapter,
        "connect_webdriver",
        lambda *args: pytest.fail("HTTP mismatch must fail before construction"),
    )

    selenium_adapter.main()

    assert emitted[0]["ok"] is False
    assert emitted[0]["error"]["class"] == "script_error"
    assert "HTTP identity mismatch" in emitted[0]["error"]["message"]


def test_non_object_json_version_fails_with_structured_script_error(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(selenium_adapter, "BENCH_ROOT", tmp_path)
    payload = adapter_payload("moli", "native_webdriver", tmp_path)
    emitted: list[dict] = []
    monkeypatch.setattr(selenium_adapter.sys, "stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(selenium_adapter, "emit", emitted.append)
    monkeypatch.setattr(selenium_adapter, "http_json", lambda *args: [])
    monkeypatch.setattr(
        selenium_adapter,
        "connect_webdriver",
        lambda *args: pytest.fail("invalid discovery payload must fail before construction"),
    )

    selenium_adapter.main()

    assert emitted[0]["ok"] is False
    assert emitted[0]["error"] == {
        "class": "script_error",
        "message": "binding gate: /json/version must return a JSON object",
    }
    assert emitted[0]["observations"]["binding"]["binding_id"] == "moli__selenium"


def test_live_identity_mismatch_quits_session_and_stops_service_once(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(selenium_adapter, "BENCH_ROOT", tmp_path)
    payload = adapter_payload("lightpanda", "chromedriver_cdp", tmp_path)
    emitted: list[dict] = []
    quit_calls: list[bool] = []
    stop_calls: list[bool] = []

    class FakeDriver:
        def execute_cdp_cmd(self, method, params):
            return {"product": "Wrong/1.0"}

        def quit(self):
            quit_calls.append(True)

    class FakeService:
        def stop(self):
            stop_calls.append(True)

    monkeypatch.setattr(selenium_adapter.sys, "stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(selenium_adapter, "emit", emitted.append)
    monkeypatch.setattr(
        selenium_adapter,
        "http_json",
        lambda *args: {
            "Browser": payload["expect_product"],
            "User-Agent": payload["expect_ua"],
            "webSocketDebuggerUrl": payload["browser_ws"],
        },
    )
    monkeypatch.setattr(
        selenium_adapter,
        "connect_webdriver",
        lambda *args: (FakeDriver(), FakeService()),
    )

    selenium_adapter.main()

    assert emitted[0]["ok"] is False
    assert emitted[0]["error"]["class"] == "script_error"
    assert "live transport identity mismatch" in emitted[0]["error"]["message"]
    assert quit_calls == [True]
    assert stop_calls == [True]


def test_always_identity_assertion_rejects_blank_expected_ref_before_io(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(selenium_adapter, "BENCH_ROOT", tmp_path)
    payload = adapter_payload("chrome", "chromedriver_cdp", tmp_path)
    payload["expect_product_live"] = ""
    monkeypatch.setattr(
        selenium_adapter,
        "http_json",
        lambda *args: pytest.fail("blank mandatory identity must fail before HTTP"),
    )
    monkeypatch.setattr(
        selenium_adapter,
        "connect_webdriver",
        lambda *args: pytest.fail(
            "blank mandatory identity must fail before driver construction"
        ),
    )

    with pytest.raises(
        selenium_adapter.BindingPayloadError,
        match=r"condition `always` requires a non-empty expected value",
    ):
        selenium_adapter.validate_runtime_binding(payload)


def test_live_cdp_identity_error_cannot_verify_and_cleans_up_once(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(selenium_adapter, "BENCH_ROOT", tmp_path)
    payload = adapter_payload("chrome", "chromedriver_cdp", tmp_path)
    emitted: list[dict] = []
    quit_calls: list[bool] = []
    stop_calls: list[bool] = []

    class FakeDriver:
        def execute_cdp_cmd(self, method, params):
            raise RuntimeError("CDP identity unavailable")

        def quit(self):
            quit_calls.append(True)

    class FakeService:
        def stop(self):
            stop_calls.append(True)

    monkeypatch.setattr(
        selenium_adapter.sys,
        "stdin",
        io.StringIO(json.dumps(payload)),
    )
    monkeypatch.setattr(selenium_adapter, "emit", emitted.append)
    monkeypatch.setattr(
        selenium_adapter,
        "http_json",
        lambda *args: {
            "Browser": payload["expect_product"],
            "User-Agent": payload["expect_ua"],
            "webSocketDebuggerUrl": payload["browser_ws"],
        },
    )
    monkeypatch.setattr(
        selenium_adapter,
        "connect_webdriver",
        lambda *args: (FakeDriver(), FakeService()),
    )

    selenium_adapter.main()

    assert emitted[0]["ok"] is False
    assert emitted[0]["error"]["class"] == "script_error"
    assert "live transport identity mismatch" in emitted[0]["error"]["message"]
    assert emitted[0]["observations"]["binding"]["verified"] is False
    assert quit_calls == [True]
    assert stop_calls == [True]


def test_unsupported_fallback_assertion_semantics_fail_closed(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(selenium_adapter, "BENCH_ROOT", tmp_path)
    payload = adapter_payload("moli", "native_webdriver", tmp_path)
    payload["binding"]["route"]["identity"]["live_transport_assertions"][0].update(
        fallback_actual_path="navigator.userAgent",
        fallback_operator="equals",
    )

    with pytest.raises(
        selenium_adapter.BindingPayloadError,
        match="unsupported fallback assertion semantics",
    ):
        selenium_adapter.validate_runtime_binding(payload)


def test_chromedriver_digest_mismatch_is_rejected_before_construction(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(selenium_adapter, "BENCH_ROOT", tmp_path)
    payload = adapter_payload("chrome", "chromedriver_cdp", tmp_path)
    payload["binding"]["pins"]["bridges"][0]["metadata"]["sha256_12"] = "000000000000"
    monkeypatch.setattr(
        selenium_adapter,
        "start_chromedriver_webdriver",
        lambda *args: pytest.fail("digest mismatch must fail before construction"),
    )

    with pytest.raises(
        selenium_adapter.BindingPayloadError,
        match="sha256 mismatch",
    ):
        selenium_adapter.validate_runtime_binding(payload)


def test_chromedriver_bridge_is_stopped_if_session_constructor_raises(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(selenium_adapter, "BENCH_ROOT", tmp_path)
    executable = make_executable(tmp_path)
    stopped: list[bool] = []

    class FakeOptions:
        timeouts = None
        debugger_address = None

    class FakeBridge:
        def __init__(self, path, port):
            self.path = path
            self.port = port
            self.service_url = "http://127.0.0.1:4444"

        def start_until(self, deadline, *, cleanup_deadline=None):
            pass

        def stop(self, *, deadline=None):
            stopped.append(True)

    monkeypatch.setattr(
        "selenium.webdriver.ChromeOptions",
        FakeOptions,
    )
    monkeypatch.setattr(
        selenium_adapter,
        "ChromeDriverBridge",
        FakeBridge,
    )
    monkeypatch.setattr(
        "selenium.webdriver.chromium.remote_connection.ChromiumRemoteConnection",
        lambda **kwargs: type(
            "FakeConnection",
            (),
            {
                "_conn": type(
                    "Transport",
                    (),
                    {"request": lambda self, *args, **kwargs: None},
                )(),
                "execute": lambda self, *args, **kwargs: None,
            },
        )(),
    )
    monkeypatch.setattr(
        "selenium.webdriver.Remote",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("constructor failed")),
    )

    with pytest.raises(
        selenium_adapter.SeleniumClientConfigurationError,
        match="before starting a session request",
    ):
        selenium_adapter.start_chromedriver_webdriver(
            executable,
            "127.0.0.1:9333",
            15000,
        )
    assert stopped == [True]


def test_chromedriver_constructor_receives_pinned_executable_and_debugger_address(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(selenium_adapter, "BENCH_ROOT", tmp_path)
    executable = make_executable(tmp_path)
    captured: dict = {}

    class FakeOptions:
        timeouts = None
        debugger_address = None

    class FakeBridge:
        def __init__(self, path, port):
            captured["bridge"] = {"executable": path, "port": port}
            self.path = path
            self.port = port
            self.service_url = "http://127.0.0.1:4444"

        def start_until(self, deadline, *, cleanup_deadline=None):
            captured["bridge_started"] = True

        def stop(self, *, deadline=None):
            pass

    class FakeDriver:
        command_executor = None

    expected_driver = FakeDriver()

    def fake_remote(*, command_executor, options):
        captured["options"] = options
        captured["executor_object"] = command_executor
        command_executor.execute("newSession", {"capabilities": {}})
        expected_driver.command_executor = command_executor
        return expected_driver

    class FakeConnection:
        def __init__(self, **kwargs):
            captured["connection"] = kwargs
            self._conn = self

        def request(self, *args, **kwargs):
            captured.setdefault("commands", []).append(
                ("newSession", {"capabilities": {}})
            )
            return {"value": {"sessionId": "fake", "capabilities": {}}}

        def execute(self, command, params):
            if command == "newSession":
                return self._conn.request("POST", "/session")
            captured.setdefault("commands", []).append((command, params))
            return {"value": {"sessionId": "fake", "capabilities": {}}}

    monkeypatch.setattr("selenium.webdriver.ChromeOptions", FakeOptions)
    monkeypatch.setattr(selenium_adapter, "ChromeDriverBridge", FakeBridge)
    monkeypatch.setattr(
        "selenium.webdriver.chromium.remote_connection.ChromiumRemoteConnection",
        FakeConnection,
    )
    monkeypatch.setattr("selenium.webdriver.Remote", fake_remote)

    driver, bridge = selenium_adapter.start_chromedriver_webdriver(
        executable,
        "127.0.0.1:9333",
        15000,
    )

    assert driver is expected_driver
    assert captured["bridge_started"] is True
    assert captured["bridge"]["executable"] == executable
    assert isinstance(captured["bridge"]["port"], int)
    assert captured["connection"]["remote_server_addr"] == bridge.service_url
    assert captured["connection"]["vendor_prefix"] == "goog"
    assert captured["connection"]["browser_name"] == "chrome"
    assert captured["connection"]["client_config"].timeout == pytest.approx(15, abs=0.01)
    assert isinstance(
        captured["executor_object"],
        selenium_adapter.SessionRequestTracker,
    )
    assert captured["executor_object"].delegate.__class__ is FakeConnection
    assert (
        captured["executor_object"].delegate._conn
        is captured["executor_object"].delegate
    )
    assert driver.command_executor is captured["executor_object"]
    driver.command_executor.execute("laterCommand", {})
    assert [command for command, _ in captured["commands"]] == [
        "newSession",
        "laterCommand",
    ]
    assert captured["options"].debugger_address == "127.0.0.1:9333"
    assert captured["options"].timeouts == {
        "pageLoad": 15000,
        "script": 20000,
    }


def test_native_identity_ops_use_webdriver_session_surface(tmp_path):
    calls: list[str] = []

    class NativeDriver:
        capabilities = {"browserVersion": "Chrome/145.0.0.0"}

        def execute_script(self, script):
            calls.append(script)
            return "MoliNativeUA/1.0"

        def execute_cdp_cmd(self, method, params):
            raise AssertionError(f"native WebDriver must not call CDP: {method} {params}")

    payload = adapter_payload("moli", "native_webdriver", tmp_path)
    adapter = selenium_adapter.Adapter(payload)
    adapter.driver = NativeDriver()

    assert adapter.run_op({"op": "version"}) == "Chrome/145.0.0.0"
    assert adapter.run_op({"op": "user_agent"}) == "MoliNativeUA/1.0"
    assert calls == ['return eval("navigator.userAgent");']


def test_chromedriver_identity_ops_keep_cdp_surface(tmp_path):
    calls: list[tuple[str, dict]] = []

    class ChromedriverDriver:
        def execute_cdp_cmd(self, method, params):
            calls.append((method, params))
            return {"product": "Chrome/150.0.0.0", "userAgent": "ChromeUA/150"}

    payload = adapter_payload("chrome", "chromedriver_cdp", tmp_path)
    adapter = selenium_adapter.Adapter(payload)
    adapter.driver = ChromedriverDriver()

    assert adapter.run_op({"op": "version"}) == "Chrome/150.0.0.0"
    assert adapter.run_op({"op": "user_agent"}) == "ChromeUA/150"
    assert calls == [
        ("Browser.getVersion", {}),
        ("Browser.getVersion", {}),
    ]
