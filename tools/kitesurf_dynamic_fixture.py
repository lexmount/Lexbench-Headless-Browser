#!/usr/bin/env python3
"""Pin and verify the public deployment of the benchmark FixtureServer.

The Kitesurf browser cannot reach the runner-owned loopback server, so the
experimental probes use a public tunnel.  A URL is not a content identity.
This module verifies every registered static route plus deterministic HTTP,
grader, API, SSE, and WebSocket behavior against a committed contract before
any browser task is scored.
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import datetime as dt
import hashlib
import http.cookiejar
import json
import pathlib
import re
import socket
import ssl
import struct
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from runner import run as bench  # noqa: E402


DEFAULT_MANIFEST = REPO_ROOT / "config/kitesurf_dynamic_fixture.json"
MANIFEST_SCHEMA = "experimental.kitesurf_dynamic_fixture.v1"
REPORT_SCHEMA = "experimental.kitesurf_dynamic_fixture_verification.v1"
SHA256_RE = re.compile(r"[0-9a-f]{64}")
HTTP_HEADER_NAME_RE = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_REQUEST_BYTES = 1024 * 1024
DEFAULT_TIMEOUT_S = 10.0
DEFAULT_WORKERS = 8
USER_AGENT = "Agent-Browser-Bench-dynamic-fixture-verifier/1"


class DynamicFixtureError(RuntimeError):
    """A malformed contract or a deployment mismatch."""

    def __init__(self, message: str, report: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.report = report


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return _sha256(payload)


def _validated_base_url(value: str) -> str:
    base_url = value.rstrip("/")
    try:
        parsed = urllib.parse.urlparse(base_url)
        hostname = parsed.hostname
        parsed.port
    except ValueError as exc:
        raise DynamicFixtureError(f"fixture base URL is malformed: {exc}") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or any(ord(character) < 0x20 for character in base_url)
    ):
        raise DynamicFixtureError(
            "fixture base URL must be credential-free HTTP(S) without query "
            "or fragment"
        )
    return base_url


def _fixture_url(base_url: str, path: str) -> str:
    if not path.startswith("/") or path.startswith("//"):
        raise DynamicFixtureError(f"fixture contract path is unsafe: {path!r}")
    return base_url + path


def _json_request(
    probe_id: str,
    path: str,
    payload: dict[str, Any],
    *,
    session: str | None = None,
    response_headers: list[str] | None = None,
    response_header_patterns: dict[str, str] | None = None,
) -> dict[str, Any]:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    row: dict[str, Any] = {
        "id": probe_id,
        "kind": "http",
        "request": {
            "method": "POST",
            "path": path,
            "headers": {"Content-Type": "application/json"},
            "body_base64": base64.b64encode(body).decode("ascii"),
            "response_headers": response_headers or ["content-type"],
        },
    }
    if session is not None:
        row["session"] = session
    if response_header_patterns:
        row["request"]["response_header_patterns"] = response_header_patterns
    return row


def dynamic_probe_requests() -> list[dict[str, Any]]:
    """Return ordered deterministic probes for code-backed fixture routes."""

    multipart_boundary = "ABBFixtureContractBoundary"
    multipart_body = (
        f"--{multipart_boundary}\r\n"
        'Content-Disposition: form-data; name="file"; '
        'filename="contract.txt"\r\n'
        "Content-Type: text/plain\r\n\r\n"
        "fixture-contract-upload\n"
        f"\r\n--{multipart_boundary}--\r\n"
    ).encode("utf-8")
    probes: list[dict[str, Any]] = [
        {
            "id": "favicon",
            "kind": "http",
            "request": {
                "method": "GET",
                "path": "/favicon.ico",
                "headers": {},
                "response_headers": [],
            },
        },
        {
            "id": "deployment_contract",
            "kind": "http",
            "request": {
                "method": "GET",
                "path": "/__fixture__/deployment-contract",
                "headers": {},
                "response_headers": ["content-type", "cache-control"],
            },
        },
        {
            "id": "auth_denied",
            "kind": "http",
            "request": {
                "method": "GET",
                "path": "/__auth__/fixture-contract",
                "headers": {},
                "response_headers": ["content-type", "www-authenticate"],
            },
        },
        {
            "id": "auth_granted",
            "kind": "http",
            "request": {
                "method": "GET",
                "path": "/__auth__/fixture-contract",
                "headers": {
                    "Authorization": "Basic "
                    + base64.b64encode(b"bench:secret").decode("ascii")
                },
                "response_headers": ["content-type"],
            },
        },
        {
            "id": "cache_host",
            "kind": "http",
            "request": {
                "method": "GET",
                "path": "/__cache__/host",
                "headers": {},
                "response_headers": ["content-type", "cache-control"],
            },
        },
        {
            "id": "cache_asset",
            "kind": "http",
            "request": {
                "method": "GET",
                "path": "/__cache__/immutable.js",
                "headers": {},
                "response_headers": ["content-type", "cache-control", "etag"],
            },
        },
        {
            "id": "sse_messages",
            "kind": "http",
            "request": {
                "method": "GET",
                "path": "/__sse__/messages",
                "headers": {},
                "response_headers": ["content-type", "cache-control"],
            },
        },
        {
            "id": "websocket_echo",
            "kind": "websocket_echo",
            "request": {
                "path": "/__ws__/echo",
                "message_base64": base64.b64encode(
                    b"fixture-contract"
                ).decode("ascii"),
                "response_headers": [
                    "upgrade",
                    "connection",
                    "sec-websocket-accept",
                ],
            },
        },
        {
            "id": "redirect_hop",
            "kind": "http",
            "request": {
                "method": "GET",
                "path": "/v0_4/redirect/hop?n=2",
                "headers": {},
                "response_headers": ["location"],
            },
        },
        {
            "id": "echo_headers",
            "kind": "http",
            "request": {
                "method": "GET",
                "path": "/v0_4/echo_headers",
                "headers": {"X-ABB-Fixture-Contract": "pinned-v1"},
                "response_headers": ["content-type"],
            },
        },
        {
            "id": "secure_denied",
            "kind": "http",
            "request": {
                "method": "GET",
                "path": "/v0_4/net/secure",
                "headers": {},
                "response_headers": ["content-type", "www-authenticate"],
            },
        },
        {
            "id": "secure_granted",
            "kind": "http",
            "request": {
                "method": "GET",
                "path": "/v0_4/net/secure",
                "headers": {"Authorization": "Bearer tok-53"},
                "response_headers": ["content-type"],
            },
        },
        {
            "id": "slow_document",
            "kind": "http",
            "request": {
                "method": "GET",
                "path": "/v0_4/slow?ms=0",
                "headers": {},
                "response_headers": ["content-type", "cache-control"],
            },
        },
        {
            "id": "inventory_document",
            "kind": "http",
            "request": {
                "method": "GET",
                "path": (
                    "/storage/indexeddb_inventory"
                    "?seed=fixture-contract-seed"
                    "&session=fixture-contract-document"
                ),
                "headers": {},
                "response_headers": ["content-type"],
            },
        },
        _json_request(
            "inventory_event",
            "/__event__/storage_indexeddb_inventory_001",
            {
                "event": "fixture_contract_event",
                "session": "fixture-contract-event",
            },
        ),
        _json_request(
            "inventory_grader_no_events",
            "/__grade__/storage_indexeddb_inventory_001",
            {
                "answer": str(
                    bench.FixtureServer.expected_count("fixture-contract-seed")
                ),
                "observations": {
                    "indexeddb_read_observed": True,
                    "indexeddb_write_observed": True,
                    "session": "fixture-contract-no-events",
                },
                "seed": "fixture-contract-seed",
                "session": "fixture-contract-no-events",
                "task_id": "storage_indexeddb_inventory_001",
            },
        ),
        {
            "id": "resource_echo",
            "kind": "http",
            "request": {
                "method": "POST",
                "path": "/__resource__/echo?response_bytes=17",
                "headers": {"Content-Type": "application/octet-stream"},
                "body_base64": base64.b64encode(
                    b"fixture-contract-request"
                ).decode("ascii"),
                "response_headers": ["content-type"],
            },
        },
        {
            "id": "upload_receipt",
            "kind": "http",
            "request": {
                "method": "POST",
                "path": "/v0_4/upload",
                "headers": {
                    "Content-Type": f"multipart/form-data; boundary={multipart_boundary}"
                },
                "body_base64": base64.b64encode(multipart_body).decode("ascii"),
                "response_headers": ["content-type"],
            },
        },
        {
            "id": "app_locked",
            "kind": "http",
            "session": "app",
            "request": {
                "method": "GET",
                "path": "/v0_4/app/api/items",
                "headers": {},
                "response_headers": ["content-type"],
            },
        },
        _json_request(
            "app_login",
            "/v0_4/app/api/login",
            {"user": "fixture-contract"},
            session="app",
            response_header_patterns={
                "set-cookie": r"^abb_app_sid=[0-9a-f]{16}; Path=/$"
            },
        ),
        {
            "id": "app_items",
            "kind": "http",
            "session": "app",
            "request": {
                "method": "GET",
                "path": "/v0_4/app/api/items",
                "headers": {},
                "response_headers": ["content-type"],
            },
        },
        _json_request(
            "app_cart_add",
            "/v0_4/app/api/cart",
            {"action": "add", "item": "widget-a"},
            session="app",
        ),
        {
            "id": "app_cart_view",
            "kind": "http",
            "session": "app",
            "request": {
                "method": "GET",
                "path": "/v0_4/app/api/cart",
                "headers": {},
                "response_headers": ["content-type"],
            },
        },
        _json_request(
            "app_checkout",
            "/v0_4/app/api/checkout",
            {},
            session="app",
        ),
        _json_request(
            "app_logout",
            "/v0_4/app/api/logout",
            {},
            session="app",
            response_header_patterns={
                "set-cookie": r"^abb_app_sid=; Path=/; Max-Age=0$"
            },
        ),
        _json_request(
            "grader_expected_pass",
            "/__grade__/expected_answer",
            {"answer": "ops:3:74", "task_id": "v3_pw_flow_records_stat"},
        ),
        _json_request(
            "grader_expected_fail",
            "/__grade__/expected_answer",
            {"answer": "wrong", "task_id": "v3_pw_flow_records_stat"},
        ),
        {
            "id": "missing_route",
            "kind": "http",
            "request": {
                "method": "GET",
                "path": "/__fixture_contract_missing__",
                "headers": {},
                "response_headers": [],
            },
        },
    ]
    return probes


def _response_headers(
    headers: Any,
    names: list[str],
) -> dict[str, str | None]:
    return {
        str(name).lower(): headers.get(str(name))
        for name in names
    }


def _http_opener(cookie_jar: http.cookiejar.CookieJar | None = None) -> Any:
    handlers: list[Any] = [_NoRedirect()]
    if cookie_jar is not None:
        handlers.insert(0, urllib.request.HTTPCookieProcessor(cookie_jar))
    return urllib.request.build_opener(*handlers)


def _fetch_http(
    base_url: str,
    request_spec: dict[str, Any],
    timeout_s: float,
    *,
    opener: Any | None = None,
) -> tuple[dict[str, Any], dict[str, str | None]]:
    path = str(request_spec["path"])
    url = _fixture_url(base_url, path)
    headers = {
        "Accept": "*/*",
        "Accept-Encoding": "identity",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "User-Agent": USER_AGENT,
        **{
            str(name): str(value)
            for name, value in (request_spec.get("headers") or {}).items()
        },
    }
    body_encoded = request_spec.get("body_base64")
    body = base64.b64decode(body_encoded, validate=True) if body_encoded else None
    request = urllib.request.Request(
        url,
        data=body,
        headers=headers,
        method=str(request_spec.get("method") or "GET"),
    )
    active_opener = opener or _http_opener()
    response: Any
    try:
        response = active_opener.open(request, timeout=timeout_s)
    except urllib.error.HTTPError as exc:
        response = exc
    try:
        response_body = response.read(MAX_RESPONSE_BYTES + 1)
        if len(response_body) > MAX_RESPONSE_BYTES:
            raise DynamicFixtureError(
                f"fixture response exceeds {MAX_RESPONSE_BYTES} bytes: {url}"
            )
        final_url = str(response.geturl())
        selected_headers = _response_headers(
            response.headers,
            [str(name) for name in request_spec.get("response_headers") or []],
        )
        pattern_headers = _response_headers(
            response.headers,
            [
                str(name)
                for name in (
                    request_spec.get("response_header_patterns") or {}
                )
            ],
        )
        actual = {
            "status": int(response.getcode()),
            "size": len(response_body),
            "sha256": _sha256(response_body),
            "headers": selected_headers,
            "final_url": final_url,
        }
        return actual, pattern_headers
    finally:
        response.close()


def _recv_exact(sock: socket.socket, length: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < length:
        chunk = sock.recv(length - len(chunks))
        if not chunk:
            raise DynamicFixtureError("WebSocket fixture closed mid-frame")
        chunks.extend(chunk)
    return bytes(chunks)


def _read_ws_frame(sock: socket.socket) -> tuple[bool, int, bytes]:
    first, second = _recv_exact(sock, 2)
    if first & 0x70:
        raise DynamicFixtureError("WebSocket fixture returned unsupported RSV bits")
    finished = bool(first & 0x80)
    opcode = first & 0x0F
    length = second & 0x7F
    if length == 126:
        length = struct.unpack("!H", _recv_exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", _recv_exact(sock, 8))[0]
    if opcode >= 0x8 and (not finished or length > 125):
        raise DynamicFixtureError("WebSocket fixture returned an invalid control frame")
    if length > MAX_RESPONSE_BYTES:
        raise DynamicFixtureError("WebSocket fixture response is too large")
    if second & 0x80:
        raise DynamicFixtureError("WebSocket fixture returned a masked server frame")
    return finished, opcode, _recv_exact(sock, length)


def _read_ws_message(sock: socket.socket) -> tuple[int, bytes]:
    message_opcode: int | None = None
    chunks = bytearray()
    for _ in range(32):
        finished, opcode, payload = _read_ws_frame(sock)
        if opcode == 0x9:  # ping
            _write_masked_ws_frame(sock, payload, opcode=0xA)
            continue
        if opcode == 0xA:  # unsolicited pong
            continue
        if opcode == 0x8:
            raise DynamicFixtureError("WebSocket fixture closed before echoing")
        if opcode in {0x1, 0x2}:
            if message_opcode is not None:
                raise DynamicFixtureError("WebSocket fixture interleaved messages")
            message_opcode = opcode
        elif opcode != 0x0 or message_opcode is None:
            raise DynamicFixtureError(
                f"WebSocket fixture returned unexpected opcode {opcode}"
            )
        chunks.extend(payload)
        if len(chunks) > MAX_RESPONSE_BYTES:
            raise DynamicFixtureError("WebSocket fixture response is too large")
        if finished:
            return message_opcode, bytes(chunks)
    raise DynamicFixtureError("WebSocket fixture message has too many fragments")


def _write_masked_ws_frame(
    sock: socket.socket,
    payload: bytes,
    *,
    opcode: int,
) -> None:
    if len(payload) >= 126:
        raise DynamicFixtureError("fixture WebSocket probe payload is too large")
    mask = b"\x01\x02\x03\x04"
    encoded = bytes(
        value ^ mask[index % 4]
        for index, value in enumerate(payload)
    )
    sock.sendall(bytes([0x80 | opcode, 0x80 | len(payload)]) + mask + encoded)


def _fetch_websocket(
    base_url: str,
    request_spec: dict[str, Any],
    timeout_s: float,
) -> tuple[dict[str, Any], dict[str, str | None]]:
    parsed = urllib.parse.urlparse(base_url)
    host = str(parsed.hostname)
    secure = parsed.scheme == "https"
    port = parsed.port or (443 if secure else 80)
    base_path = parsed.path.rstrip("/")
    request_path = base_path + str(request_spec["path"])
    key = base64.b64encode(bytes(range(16))).decode("ascii")
    header_host = f"[{host}]" if ":" in host else host
    host_header = (
        header_host
        if port == (443 if secure else 80)
        else f"{header_host}:{port}"
    )
    raw_sock = socket.create_connection((host, port), timeout=timeout_s)
    sock: socket.socket = raw_sock
    try:
        if secure:
            sock = ssl.create_default_context().wrap_socket(
                raw_sock,
                server_hostname=host,
            )
        sock.settimeout(timeout_s)
        handshake = (
            f"GET {request_path} HTTP/1.1\r\n"
            f"Host: {host_header}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            f"User-Agent: {USER_AGENT}\r\n"
            "\r\n"
        ).encode("ascii")
        sock.sendall(handshake)
        header = bytearray()
        while b"\r\n\r\n" not in header:
            chunk = sock.recv(4096)
            if not chunk:
                raise DynamicFixtureError(
                    "WebSocket fixture closed during handshake"
                )
            header.extend(chunk)
            if len(header) > 64 * 1024:
                raise DynamicFixtureError("WebSocket fixture handshake is too large")
        header_blob, trailing = bytes(header).split(b"\r\n\r\n", 1)
        if trailing:
            raise DynamicFixtureError(
                "unexpected WebSocket frame bytes before the verifier request"
            )
        lines = header_blob.decode("iso-8859-1").split("\r\n")
        status_parts = lines[0].split(" ", 2)
        status = int(status_parts[1])
        response_headers: dict[str, str] = {}
        for line in lines[1:]:
            name, separator, value = line.partition(":")
            if separator:
                response_headers[name.strip().lower()] = value.strip()
        message = base64.b64decode(
            str(request_spec["message_base64"]),
            validate=True,
        )
        _write_masked_ws_frame(sock, message, opcode=0x1)
        opcode, payload = _read_ws_message(sock)
        _write_masked_ws_frame(sock, b"", opcode=0x8)
        if opcode != 0x1:
            raise DynamicFixtureError(
                f"WebSocket fixture returned opcode {opcode}, expected text"
            )
        actual = {
            "status": status,
            "size": len(payload),
            "sha256": _sha256(payload),
            "headers": {
                str(name).lower(): response_headers.get(str(name).lower())
                for name in request_spec.get("response_headers") or []
            },
            "final_url": _fixture_url(base_url, str(request_spec["path"])),
        }
        return actual, {}
    finally:
        try:
            sock.close()
        finally:
            if sock is not raw_sock:
                raw_sock.close()


# RFC 9110 defines the Connection and Upgrade field values as case-insensitive
# tokens. A proxy in front of the FixtureServer (a Cloudflare quick tunnel, for
# example) may normalize their case while forwarding the handshake verbatim, so
# comparing them byte-for-byte would reject a deployment that is contractually
# identical. Only these two tokens are folded; every other pinned header value
# still has to match exactly.
_CASE_INSENSITIVE_TOKEN_HEADERS = frozenset({"connection", "upgrade"})


def _canonical_headers(headers: Any) -> Any:
    if not isinstance(headers, dict):
        return headers
    return {
        name: (
            value.lower()
            if name in _CASE_INSENSITIVE_TOKEN_HEADERS and isinstance(value, str)
            else value
        )
        for name, value in headers.items()
    }


def _stable_response(actual: dict[str, Any]) -> dict[str, Any]:
    stable = {
        key: actual[key]
        for key in ("status", "size", "sha256", "headers")
    }
    stable["headers"] = _canonical_headers(stable["headers"])
    return stable


def _run_dynamic_probe(
    base_url: str,
    probe: dict[str, Any],
    timeout_s: float,
    sessions: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, str | None]]:
    request = probe["request"]
    if probe["kind"] == "websocket_echo":
        return _fetch_websocket(base_url, request, timeout_s)
    session = str(probe.get("session") or "")
    opener = None
    if session:
        opener = sessions.setdefault(
            session,
            _http_opener(http.cookiejar.CookieJar()),
        )
    return _fetch_http(base_url, request, timeout_s, opener=opener)


def build_manifest() -> dict[str, Any]:
    """Generate the committed contract from a local FixtureServer instance."""

    fixture = bench.FixtureServer()
    deployment_contract = fixture.deployment_contract()
    static_routes = deployment_contract["static_routes"]
    local_base = fixture.start()
    sessions: dict[str, Any] = {}
    dynamic: list[dict[str, Any]] = []
    try:
        for probe in dynamic_probe_requests():
            actual, pattern_headers = _run_dynamic_probe(
                local_base,
                probe,
                DEFAULT_TIMEOUT_S,
                sessions,
            )
            expect = _stable_response(actual)
            patterns = probe["request"].get("response_header_patterns") or {}
            for name, pattern in patterns.items():
                value = pattern_headers.get(str(name).lower())
                if value is None or re.fullmatch(str(pattern), value) is None:
                    raise DynamicFixtureError(
                        f"local fixture header {name} does not match {pattern!r}"
                    )
            if patterns:
                expect["header_patterns"] = {
                    str(name).lower(): str(pattern)
                    for name, pattern in patterns.items()
                }
            dynamic.append({**probe, "expect": expect})
    finally:
        fixture.stop()
    contract = {
        "static_routes": static_routes,
        "dynamic_probes": dynamic,
    }
    return {
        "schema": MANIFEST_SCHEMA,
        "source": {
            "repository": "https://github.com/lexmount/Lexbench-Headless-Browser",
            "fixture_root": "fixtures",
            "implementation": "runner.run.FixtureServer",
            "deployment_contract": {
                "schema": deployment_contract["schema"],
                "implementation": deployment_contract["implementation"],
                "fixture_root": deployment_contract["fixture_root"],
                "expected_answers": deployment_contract["expected_answers"],
                "static_routes": {
                    "count": len(static_routes),
                    "sha256": _canonical_sha256(static_routes),
                },
            },
        },
        "contract_sha256": _canonical_sha256(contract),
        **contract,
    }


def _validated_headers(value: Any, label: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise DynamicFixtureError(f"{label} headers must be an object")
    result: dict[str, str] = {}
    for raw_name, raw_value in value.items():
        if (
            not isinstance(raw_name, str)
            or not HTTP_HEADER_NAME_RE.fullmatch(raw_name)
            or not isinstance(raw_value, str)
            or "\r" in raw_value
            or "\n" in raw_value
        ):
            raise DynamicFixtureError(f"{label} contains an invalid header")
        result[raw_name] = raw_value
    return result


def _validated_path(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) > 4096
        or not value.startswith("/")
        or value.startswith("//")
    ):
        raise DynamicFixtureError(f"{label} path is unsafe")
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme or parsed.netloc or parsed.fragment or any(
        ord(character) < 0x20 for character in value
    ):
        raise DynamicFixtureError(f"{label} path is unsafe")
    return value


def _validated_response(value: Any, label: str) -> None:
    if not isinstance(value, dict):
        raise DynamicFixtureError(f"{label} response must be an object")
    status = value.get("status")
    size = value.get("size")
    if isinstance(status, bool) or not isinstance(status, int) or not 100 <= status <= 599:
        raise DynamicFixtureError(f"{label} response has an invalid status")
    if (
        isinstance(size, bool)
        or not isinstance(size, int)
        or not 0 <= size <= MAX_RESPONSE_BYTES
    ):
        raise DynamicFixtureError(f"{label} response has an invalid size")
    if not SHA256_RE.fullmatch(str(value.get("sha256") or "")):
        raise DynamicFixtureError(f"{label} response has an invalid SHA-256")
    headers = _validated_headers(value.get("headers"), label)
    if any(name != name.lower() for name in headers):
        raise DynamicFixtureError(f"{label} response headers must be lowercase")
    patterns = value.get("header_patterns") or {}
    if not isinstance(patterns, dict):
        raise DynamicFixtureError(f"{label} header_patterns must be an object")
    for name, pattern in patterns.items():
        if (
            not isinstance(name, str)
            or name != name.lower()
            or not HTTP_HEADER_NAME_RE.fullmatch(name)
            or not isinstance(pattern, str)
            or len(pattern) > 512
        ):
            raise DynamicFixtureError(f"{label} contains an invalid header pattern")
        try:
            re.compile(pattern)
        except re.error as exc:
            raise DynamicFixtureError(
                f"{label} contains an invalid header pattern: {exc}"
            ) from exc


def _validated_request(value: Any, kind: str, label: str) -> None:
    if not isinstance(value, dict):
        raise DynamicFixtureError(f"{label} request must be an object")
    _validated_path(value.get("path"), label)
    headers = _validated_headers(value.get("headers") or {}, label)
    if len(headers) > 64:
        raise DynamicFixtureError(f"{label} request has too many headers")
    response_headers = value.get("response_headers") or []
    if (
        not isinstance(response_headers, list)
        or any(
            not isinstance(name, str)
            or name != name.lower()
            or not HTTP_HEADER_NAME_RE.fullmatch(name)
            for name in response_headers
        )
        or len(response_headers) != len(set(response_headers))
    ):
        raise DynamicFixtureError(f"{label} response_headers are invalid")
    patterns = value.get("response_header_patterns") or {}
    if not isinstance(patterns, dict):
        raise DynamicFixtureError(f"{label} response_header_patterns must be an object")
    for name, pattern in patterns.items():
        if (
            not isinstance(name, str)
            or name != name.lower()
            or not HTTP_HEADER_NAME_RE.fullmatch(name)
            or not isinstance(pattern, str)
            or len(pattern) > 512
        ):
            raise DynamicFixtureError(
                f"{label} contains an invalid response header pattern"
            )
        try:
            re.compile(pattern)
        except re.error as exc:
            raise DynamicFixtureError(
                f"{label} contains an invalid response header pattern: {exc}"
            ) from exc
    encoded_fields = (
        ["message_base64"] if kind == "websocket_echo" else ["body_base64"]
    )
    for field in encoded_fields:
        encoded = value.get(field)
        if encoded is None and field == "body_base64":
            continue
        if not isinstance(encoded, str):
            raise DynamicFixtureError(f"{label} {field} must be base64 text")
        try:
            decoded = base64.b64decode(encoded, validate=True)
        except ValueError as exc:
            raise DynamicFixtureError(f"{label} {field} is invalid base64") from exc
        if len(decoded) > MAX_REQUEST_BYTES:
            raise DynamicFixtureError(f"{label} {field} is too large")
    if kind == "http":
        method = value.get("method")
        if method not in {"GET", "POST"}:
            raise DynamicFixtureError(f"{label} HTTP method is invalid")


def _validated_manifest(path: pathlib.Path) -> tuple[dict[str, Any], str]:
    try:
        if path.stat().st_size > 4 * 1024 * 1024:
            raise DynamicFixtureError(
                f"dynamic fixture manifest is too large: {path}"
            )
        raw = path.read_bytes()
        manifest = json.loads(raw)
    except DynamicFixtureError:
        raise
    except (OSError, json.JSONDecodeError) as exc:
        raise DynamicFixtureError(
            f"dynamic fixture manifest is not readable JSON: {path}: {exc}"
        ) from exc
    if not isinstance(manifest, dict) or manifest.get("schema") != MANIFEST_SCHEMA:
        raise DynamicFixtureError(
            f"unsupported dynamic fixture manifest schema in {path}"
        )
    if not isinstance(manifest.get("source"), dict):
        raise DynamicFixtureError("dynamic fixture source must be an object")
    static_routes = manifest.get("static_routes")
    dynamic_probes = manifest.get("dynamic_probes")
    if not isinstance(static_routes, list) or not 1 <= len(static_routes) <= 4096:
        raise DynamicFixtureError("dynamic fixture static_routes must be non-empty")
    if not isinstance(dynamic_probes, list) or not 1 <= len(dynamic_probes) <= 256:
        raise DynamicFixtureError("dynamic fixture dynamic_probes must be non-empty")
    route_paths: set[str] = set()
    for row in static_routes:
        if not isinstance(row, dict):
            raise DynamicFixtureError("dynamic fixture static route must be an object")
        route = _validated_path(row.get("path"), "static route")
        if route in route_paths:
            raise DynamicFixtureError(f"duplicate static route: {route!r}")
        route_paths.add(route)
        source = row.get("source")
        if (
            not isinstance(source, str)
            or not source
            or pathlib.PurePosixPath(source).is_absolute()
            or ".." in pathlib.PurePosixPath(source).parts
        ):
            raise DynamicFixtureError(f"invalid static route source: {route}")
        _validated_response(row, f"static route {route}")
    probe_ids: set[str] = set()
    for probe in dynamic_probes:
        if not isinstance(probe, dict):
            raise DynamicFixtureError("dynamic fixture probe must be an object")
        probe_id = str(probe.get("id") or "")
        request = probe.get("request")
        expect = probe.get("expect")
        if (
            re.fullmatch(r"[a-z0-9_]{1,128}", probe_id) is None
            or probe_id in probe_ids
        ):
            raise DynamicFixtureError(f"missing or duplicate dynamic probe id: {probe_id}")
        probe_ids.add(probe_id)
        kind = probe.get("kind")
        if kind not in {"http", "websocket_echo"}:
            raise DynamicFixtureError(f"dynamic probe has invalid kind: {probe_id}")
        session = probe.get("session")
        if session is not None and (
            not isinstance(session, str)
            or not session
            or len(session) > 128
        ):
            raise DynamicFixtureError(f"dynamic probe has invalid session: {probe_id}")
        _validated_request(request, str(kind), f"dynamic probe {probe_id}")
        _validated_response(expect, f"dynamic probe {probe_id}")
        if (request.get("response_header_patterns") or {}) != (
            expect.get("header_patterns") or {}
        ):
            raise DynamicFixtureError(
                f"dynamic probe header patterns disagree: {probe_id}"
            )
    contract = {
        "static_routes": static_routes,
        "dynamic_probes": dynamic_probes,
    }
    actual_contract_sha256 = _canonical_sha256(contract)
    if manifest.get("contract_sha256") != actual_contract_sha256:
        raise DynamicFixtureError("dynamic fixture contract_sha256 does not match content")
    return manifest, _sha256(raw)


def _response_verdict(
    expected: dict[str, Any],
    actual: dict[str, Any],
    pattern_headers: dict[str, str | None],
) -> tuple[bool, dict[str, Any]]:
    patterns = expected.get("header_patterns") or {}
    pattern_results = {
        str(name): {
            "pattern": str(pattern),
            "matched": bool(
                pattern_headers.get(str(name)) is not None
                and re.fullmatch(
                    str(pattern),
                    str(pattern_headers.get(str(name))),
                )
            ),
        }
        for name, pattern in patterns.items()
    }
    stable_actual = _stable_response(actual)
    stable_expected = {
        key: expected[key]
        for key in ("status", "size", "sha256", "headers")
    }
    stable_expected["headers"] = _canonical_headers(stable_expected["headers"])
    verified = stable_actual == stable_expected and all(
        row["matched"] for row in pattern_results.values()
    )
    return verified, pattern_results


def _write_report(path: pathlib.Path | None, report: dict[str, Any]) -> None:
    if path is None:
        return
    path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def verify_dynamic_fixture(
    base_url: str,
    manifest_path: pathlib.Path = DEFAULT_MANIFEST,
    *,
    report_path: pathlib.Path | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    workers: int = DEFAULT_WORKERS,
) -> dict[str, Any]:
    """Fail unless the remote FixtureServer matches every committed response."""

    if timeout_s <= 0 or timeout_s > 30:
        raise DynamicFixtureError("fixture timeout must be in (0, 30]")
    if workers <= 0 or workers > 32:
        raise DynamicFixtureError("fixture verifier workers must be in [1, 32]")
    base_url = _validated_base_url(base_url)
    resolved_manifest = manifest_path.resolve()
    manifest, manifest_sha256 = _validated_manifest(resolved_manifest)
    local_manifest = build_manifest()
    local_static_verified = (
        local_manifest["static_routes"] == manifest["static_routes"]
    )
    local_dynamic_verified = (
        local_manifest["dynamic_probes"] == manifest["dynamic_probes"]
    )
    local_source_verified = local_manifest["source"] == manifest["source"]
    local_contract_verified = bool(
        local_static_verified
        and local_dynamic_verified
        and local_source_verified
        and local_manifest["contract_sha256"] == manifest["contract_sha256"]
    )
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "created_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "verified": False,
        "base_url": base_url,
        "manifest_path": str(resolved_manifest),
        "manifest_sha256": manifest_sha256,
        "contract_sha256": manifest["contract_sha256"],
        "source": manifest["source"],
        "local_contract_verified": local_contract_verified,
        "local_static_contract_verified": local_static_verified,
        "local_dynamic_contract_verified": local_dynamic_verified,
        "local_source_contract_verified": local_source_verified,
        "local_contract_sha256": local_manifest["contract_sha256"],
        "static_route_count": len(manifest["static_routes"]),
        "verified_static_route_count": 0,
        "dynamic_probe_count": len(manifest["dynamic_probes"]),
        "verified_dynamic_probe_count": 0,
        "static_routes": [],
        "dynamic_probes": [],
    }
    if not local_contract_verified:
        report["source_error"] = (
            "committed dynamic fixture manifest does not match the checked-out "
            "FixtureServer implementation, grader registry, routes, or files"
        )
        _write_report(report_path, report)
        raise DynamicFixtureError(str(report["source_error"]), report)

    def verify_static(expected: dict[str, Any]) -> dict[str, Any]:
        request_spec = {
            "method": "GET",
            "path": expected["path"],
            "headers": {},
            "response_headers": list(expected["headers"]),
        }
        row: dict[str, Any] = {
            "path": expected["path"],
            "source": expected["source"],
            "url": _fixture_url(base_url, expected["path"]),
            "expected": {
                key: expected[key]
                for key in ("status", "size", "sha256", "headers")
            },
            "verified": False,
        }
        try:
            actual, _patterns = _fetch_http(
                base_url,
                request_spec,
                timeout_s,
            )
            row["actual"] = actual
            expected_stable = dict(row["expected"])
            expected_stable["headers"] = _canonical_headers(
                expected_stable.get("headers")
            )
            row["verified"] = bool(
                _stable_response(actual) == expected_stable
                and actual["final_url"] == row["url"]
            )
            if not row["verified"]:
                row["error"] = "static response does not match committed contract"
        except Exception as exc:
            row["error"] = f"{type(exc).__name__}: {exc}"
        return row

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        static_rows = list(
            executor.map(verify_static, manifest["static_routes"])
        )
    report["static_routes"] = static_rows
    report["verified_static_route_count"] = sum(
        row["verified"] for row in static_rows
    )

    sessions: dict[str, Any] = {}
    dynamic_rows: list[dict[str, Any]] = []
    for probe in manifest["dynamic_probes"]:
        url = _fixture_url(base_url, str(probe["request"]["path"]))
        row = {
            "id": probe["id"],
            "kind": probe["kind"],
            "url": url,
            "expected": probe["expect"],
            "verified": False,
        }
        try:
            actual, pattern_headers = _run_dynamic_probe(
                base_url,
                probe,
                timeout_s,
                sessions,
            )
            verified, pattern_results = _response_verdict(
                probe["expect"],
                actual,
                pattern_headers,
            )
            row["actual"] = actual
            if pattern_results:
                row["header_patterns"] = pattern_results
            row["verified"] = bool(
                verified and actual["final_url"] == url
            )
            if not row["verified"]:
                row["error"] = "dynamic response does not match committed contract"
        except Exception as exc:
            row["error"] = f"{type(exc).__name__}: {exc}"
        dynamic_rows.append(row)
    report["dynamic_probes"] = dynamic_rows
    report["verified_dynamic_probe_count"] = sum(
        row["verified"] for row in dynamic_rows
    )
    report["verified"] = bool(
        report["verified_static_route_count"] == report["static_route_count"]
        and report["verified_dynamic_probe_count"] == report["dynamic_probe_count"]
    )
    _write_report(report_path, report)
    if not report["verified"]:
        failures = [
            row["path"] for row in static_rows if not row["verified"]
        ] + [
            str(row["id"]) for row in dynamic_rows if not row["verified"]
        ]
        raise DynamicFixtureError(
            "dynamic fixture verification failed for: " + ", ".join(failures),
            report,
        )
    return report


def compact_verification(
    report: dict[str, Any],
    report_path: pathlib.Path,
) -> dict[str, Any]:
    raw = report_path.read_bytes()
    return {
        "verified": report["verified"],
        "local_contract_verified": report["local_contract_verified"],
        "manifest_sha256": report["manifest_sha256"],
        "contract_sha256": report["contract_sha256"],
        "report": report_path.name,
        "report_sha256": _sha256(raw),
        "static_routes": {
            "verified": report["verified_static_route_count"],
            "required": report["static_route_count"],
        },
        "dynamic_probes": {
            "verified": report["verified_dynamic_probe_count"],
            "required": report["dynamic_probe_count"],
        },
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    generate = subparsers.add_parser("generate")
    generate.add_argument("--output", default="-")
    verify = subparsers.add_parser("verify")
    verify.add_argument("--base-url", required=True)
    verify.add_argument("--manifest", type=pathlib.Path, default=DEFAULT_MANIFEST)
    verify.add_argument("--output", type=pathlib.Path, required=True)
    verify.add_argument("--timeout-s", type=float, default=DEFAULT_TIMEOUT_S)
    verify.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.command == "generate":
        payload = json.dumps(build_manifest(), indent=2, sort_keys=True) + "\n"
        if args.output == "-":
            sys.stdout.write(payload)
        else:
            pathlib.Path(args.output).write_text(payload, encoding="utf-8")
        return 0
    report = verify_dynamic_fixture(
        args.base_url,
        args.manifest,
        report_path=args.output,
        timeout_s=args.timeout_s,
        workers=args.workers,
    )
    print(
        "verified "
        f"{report['verified_static_route_count']}/{report['static_route_count']} "
        "static routes and "
        f"{report['verified_dynamic_probe_count']}/{report['dynamic_probe_count']} "
        "dynamic probes"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except DynamicFixtureError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2)
