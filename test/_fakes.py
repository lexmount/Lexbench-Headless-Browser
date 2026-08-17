"""Shared fakes and factories for the runner test suite.

Covers TESTING.md §3 ("fake CDP 最小面") and §10 (conftest helpers).

FakeCDP is a stdlib-only threaded HTTP + WebSocket server implementing just
enough of the DevTools surface for runner.run.CDPClient / create_page_ws:

  - GET/PUT ``/json/version`` and ``/json/new?...`` return a JSON object with
    ``webSocketDebuggerUrl``.
  - ``ws://.../devtools/page/FAKE`` speaks enough RFC6455 (server side:
    unmasked frames out, masked frames in) to answer CDP commands with
    scripted responses keyed by method.

The factories at the bottom build task dicts / ResolvedTask objects / tmp
manifests so tests never touch the real ``tasks/`` or ``runs/`` trees.
"""
from __future__ import annotations

import base64
import hashlib
import json
import pathlib
import socket
import struct
import sys
import threading
from typing import Any, Callable

TEST_DIR = pathlib.Path(__file__).resolve().parent
BENCH_ROOT = TEST_DIR.parent
for _p in (str(BENCH_ROOT), str(TEST_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from runner import run as runner_run  # noqa: E402


WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

# Sentinel: when a responder returns CLOSE the fake server drops the TCP
# connection mid-command (client sees ConnectionError -> status "crash").
CLOSE = object()


def method_responder(overrides: dict[str, Any] | None = None) -> Callable[[str, dict], Any]:
    """Build a responder keyed by CDP method.

    Override values may be:
      - a dict like ``{"result": {...}}`` or ``{"error": {"message": ...}}``
      - the CLOSE sentinel (drop connection mid-command)
      - a callable(params) returning one of the above
    Unknown methods get an empty ``{"result": {}}`` (normal mode).
    """
    overrides = overrides or {}

    def responder(method: str, params: dict[str, Any]) -> Any:
        value = overrides.get(method, {"result": {}})
        if callable(value):
            value = value(params)
        return value

    return responder


class FakeCDP:
    def __init__(self, responder: Callable[[str, dict], Any] | None = None) -> None:
        self.responder = responder or method_responder()
        # Every inbound CDP command is recorded (method + sessionId + params) so
        # tests can assert step-level session routing. Appended from WS threads;
        # list.append is atomic under CPython so no explicit lock is needed here.
        self.requests: list[dict[str, Any]] = []
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(16)
        self.port = int(self._sock.getsockname()[1])
        self._running = True
        self._accept_thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._accept_thread.start()

    @property
    def ws_url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/devtools/page/FAKE"

    def stop(self) -> None:
        self._running = False
        try:
            self._sock.close()
        except OSError:
            pass

    # -- internals ---------------------------------------------------------

    def _accept_loop(self) -> None:
        while self._running:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        try:
            conn.settimeout(15)
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                data += chunk
            head = data.split(b"\r\n\r\n", 1)[0].decode("latin-1")
            request_line = head.split("\r\n", 1)[0]
            _method, path, _version = request_line.split(" ", 2)
            if path.startswith("/devtools/"):
                self._ws_session(conn, head)
            else:
                self._http_json(conn)
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _http_json(self, conn: socket.socket) -> None:
        payload = {
            "Browser": "FakeCDP/1.0",
            "Protocol-Version": "1.3",
            "webSocketDebuggerUrl": self.ws_url,
        }
        body = json.dumps(payload).encode("utf-8")
        conn.sendall(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: " + str(len(body)).encode("ascii") + b"\r\n"
            b"Connection: close\r\n\r\n" + body
        )

    def _ws_session(self, conn: socket.socket, head: str) -> None:
        key = ""
        for line in head.split("\r\n")[1:]:
            if line.lower().startswith("sec-websocket-key:"):
                key = line.split(":", 1)[1].strip()
        accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode("ascii")).digest()).decode("ascii")
        conn.sendall(
            (
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
            ).encode("ascii")
        )
        while True:
            frame = self._recv_frame(conn)
            if frame is None:
                return
            opcode, payload = frame
            if opcode == 8:  # close
                return
            if opcode == 9:  # ping
                self._send_frame(conn, payload, opcode=10)
                continue
            if opcode not in (1, 2):
                continue
            msg = json.loads(payload.decode("utf-8"))
            self.requests.append(
                {"method": msg.get("method"), "sessionId": msg.get("sessionId"), "params": msg.get("params") or {}}
            )
            reply = self.responder(msg.get("method"), msg.get("params") or {})
            if reply is CLOSE:
                # Abrupt close mid-command.
                try:
                    conn.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                conn.close()
                return
            # A reply may carry event lists (each event is {"method", "params",
            # optional "sessionId"}). "__events_before__" is emitted before the
            # command response to exercise command-time buffering; "__events__"
            # is emitted after it for ordinary wait_for_event flows.
            events_before = None
            events = None
            if isinstance(reply, dict) and ("__events_before__" in reply or "__events__" in reply):
                reply = dict(reply)
                events_before = reply.pop("__events_before__", None)
                events = reply.pop("__events__", None)
            out = {"id": msg.get("id")}
            out.update(reply)
            for event in events_before or []:
                self._send_frame(conn, json.dumps(event).encode("utf-8"))
            self._send_frame(conn, json.dumps(out).encode("utf-8"))
            for event in events or []:
                self._send_frame(conn, json.dumps(event).encode("utf-8"))

    @staticmethod
    def _recv_exact(conn: socket.socket, n: int) -> bytes | None:
        chunks = bytearray()
        while len(chunks) < n:
            try:
                chunk = conn.recv(n - len(chunks))
            except OSError:
                return None
            if not chunk:
                return None
            chunks.extend(chunk)
        return bytes(chunks)

    def _recv_frame(self, conn: socket.socket) -> tuple[int, bytes] | None:
        header = self._recv_exact(conn, 2)
        if header is None:
            return None
        first, second = header
        opcode = first & 0x0F
        length = second & 0x7F
        masked = bool(second & 0x80)
        if length == 126:
            ext = self._recv_exact(conn, 2)
            if ext is None:
                return None
            length = struct.unpack("!H", ext)[0]
        elif length == 127:
            ext = self._recv_exact(conn, 8)
            if ext is None:
                return None
            length = struct.unpack("!Q", ext)[0]
        mask = b""
        if masked:
            mask = self._recv_exact(conn, 4) or b""
            if len(mask) != 4:
                return None
        payload = b""
        if length:
            payload = self._recv_exact(conn, length) or b""
            if len(payload) != length:
                return None
        if masked:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return opcode, payload

    @staticmethod
    def _send_frame(conn: socket.socket, payload: bytes, opcode: int = 1) -> None:
        first = 0x80 | opcode
        length = len(payload)
        if length < 126:
            header = struct.pack("!BB", first, length)
        elif length < (1 << 16):
            header = struct.pack("!BBH", first, 126, length)
        else:
            header = struct.pack("!BBQ", first, 127, length)
        conn.sendall(header + payload)


class StubProc:
    """Minimal stand-in for subprocess.Popen inside BrowserProcess."""

    pid = 424242
    stdout = None
    stderr = None

    def poll(self) -> None:
        return None

    def wait(self, timeout: float | None = None) -> int:
        return 0


def stub_browser(port: int, engine: str = "moli") -> "runner_run.BrowserProcess":
    return runner_run.BrowserProcess(
        engine=engine,
        port=port,
        process=StubProc(),  # type: ignore[arg-type]
        version_info={"Browser": "FakeCDP/1.0"},
    )


# --- task / manifest factories ---------------------------------------------


def make_task_dict(**over: Any) -> dict[str, Any]:
    """A well-formed L1 raw_cdp task dict (overridable per test)."""
    base: dict[str, Any] = {
        "task_id": "unit_l1_task_001",
        "task_version": 1,
        "layer": "L1",
        "subset_id": "l1.raw_cdp",
        "description": "Verifies a basic raw CDP Runtime.evaluate flow for unit tests.",
        "features": ["cdp.runtime.evaluate"],
        "tags": ["purpose.smoke", "coverage.core", "version.v0_1"],
        "scene": {"kind": "about_blank"},
        "driver": {
            "kind": "raw_cdp",
            "steps": [
                {"method": "Runtime.enable"},
                {
                    "method": "Runtime.evaluate",
                    "params": {"expression": "1 + 2", "returnByValue": True},
                    "save_as": "value",
                },
            ],
        },
        "grader": {"kind": "inline_assertions", "checks": ["value_equals_3", "result_type_number"]},
        "timeouts": {"task_ms": 10000, "hard_kill_ms": 20000},
        "artifact_profile": "l1_standard",
    }
    base.update(over)
    return base


# This script really exists under BENCH_ROOT (validate_task resolves
# driver.script against the real bench root, not the tmp manifest root).
EXISTING_SCRIPT = "runner/scripts/storage_indexeddb_inventory_001.js"


def make_l2_task_dict(**over: Any) -> dict[str, Any]:
    """A well-formed L2 node_cdp_probe task dict (overridable per test)."""
    base: dict[str, Any] = {
        "task_id": "unit_l2_task_001",
        "task_version": 1,
        "layer": "L2",
        "subset_id": "l2.web_platform",
        "description": "Verifies a fixture-backed IndexedDB Web platform flow for unit tests.",
        "features": ["web.storage.indexeddb"],
        "tags": ["version.v0_1"],
        "scene": {
            "kind": "self_hosted_fixture",
            "url": "/storage/indexeddb_inventory?seed={seed}&session={session}",
        },
        "driver": {"kind": "node_cdp_probe", "script": EXISTING_SCRIPT},
        "grader": {"kind": "server_side", "endpoint": "/__grade__/storage_indexeddb_inventory_001"},
        "timeouts": {"task_ms": 15000, "hard_kill_ms": 30000},
        "artifact_profile": "l2_standard",
    }
    base.update(over)
    return base


def make_resolved(
    task: dict[str, Any] | None = None,
    subset_gate: str | None = "off",
    **task_over: Any,
) -> "runner_run.ResolvedTask":
    """Build a ResolvedTask directly (no files needed)."""
    obj = task if task is not None else make_task_dict(**task_over)
    return runner_run.ResolvedTask(
        layer=str(obj.get("layer", "")),
        subset_id=str(obj.get("subset_id", "")),
        task_id=str(obj.get("task_id", "")),
        task_version=int(obj.get("task_version", 0)),
        path=pathlib.Path("/nonexistent/unit-task.json"),
        rel_path="tasks/unit/unit-task.json",
        sha256="0" * 64,
        description=str(obj.get("description", "")),
        features=list(obj.get("features", [])),
        tags=list(obj.get("tags", [])),
        scene=dict(obj.get("scene", {})),
        driver=dict(obj.get("driver", {})),
        grader=dict(obj.get("grader", {})),
        chrome_gate=obj.get("chrome_gate"),
        subset_chrome_gate=subset_gate,
        launch_profile=str(
            obj.get("launch_profile", runner_run.DEFAULT_LAUNCH_PROFILE)
        ),
        artifact_profile=str(obj.get("artifact_profile", "")),
        task=obj,
    )


def default_manifest() -> dict[str, Any]:
    """A minimal, valid suite manifest for tmp bench trees."""
    return {
        "bench_id": "unit_bench",
        "bench_version": "0.0-test",
        "root_dir": "unit_bench",
        "fallback_allowed": False,
        "default_k_runs": 1,
        "site": {"kind": "self_hosted_fixture", "site_version": "test"},
        "engines": {
            "chrome": {"role": "gold_baseline"},
            "moli": {"role": "native_candidate"},
            "lightpanda": {"role": "native_candidate"},
            "obscura": {"role": "native_candidate"},
        },
        "layers": [
            {
                "layer_id": "L1",
                "name": "cdp_compatibility",
                "enabled": True,
                "subsets": [
                    {
                        "subset_id": "l1.raw_cdp",
                        "driver": "raw_cdp",
                        "chrome_gate": "off",
                        "task_glob": "tasks/L1/raw_cdp/*.json",
                        "allow_empty": True,
                    }
                ],
            },
            {
                "layer_id": "L2",
                "name": "web_platform_semantics",
                "enabled": True,
                "subsets": [
                    {
                        "subset_id": "l2.web_platform",
                        "driver": "node_cdp_probe",
                        "chrome_gate": "off",
                        "task_glob": "tasks/L2/web_platform/*.json",
                        "allow_empty": True,
                    }
                ],
            },
        ],
    }


def write_bench(
    root: pathlib.Path,
    l1_tasks: list[dict[str, Any]] = (),
    l2_tasks: list[dict[str, Any]] = (),
    manifest_mut: Callable[[dict[str, Any]], None] | None = None,
) -> pathlib.Path:
    """Materialize a tmp bench tree; returns the manifest path."""
    (root / "tasks/L1/raw_cdp").mkdir(parents=True, exist_ok=True)
    (root / "tasks/L2/web_platform").mkdir(parents=True, exist_ok=True)
    manifest = default_manifest()
    if manifest_mut:
        manifest_mut(manifest)
    for i, obj in enumerate(l1_tasks):
        path = root / "tasks/L1/raw_cdp" / f"{obj.get('task_id', f'task{i}')}.json"
        path.write_text(json.dumps(obj, indent=2), encoding="utf-8")
    for i, obj in enumerate(l2_tasks):
        path = root / "tasks/L2/web_platform" / f"{obj.get('task_id', f'task{i}')}.json"
        path.write_text(json.dumps(obj, indent=2), encoding="utf-8")
    manifest_path = root / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest_path
