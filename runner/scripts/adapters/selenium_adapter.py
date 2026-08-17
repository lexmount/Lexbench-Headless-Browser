#!/usr/bin/env python3
"""Selenium scenario adapter.

The runner resolves the Browser × Selenium Binding Catalog before launching
any browser worker and passes the normalized route in the adapter payload.
This module consumes that route without reading the catalog or inferring a
transport from the engine name. Both current routes verify the
runner-discovered endpoint and then verify the live WebDriver session before
executing a scenario.

Speaks the abb_scenario_adapter/1 contract (PROTOCOL.md in this directory).
`driver.quit()` tears down only the WebDriver session; the engine process
owned by the benchmark worker survives.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import os
import pathlib
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from typing import Any

BENCH_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent.parent
SUPPORTED_ROUTE_IDS = frozenset({"native_webdriver", "chromedriver_cdp"})
EXPECTED_REF_FIELDS = frozenset(
    {"expect_product", "expect_ua", "browser_ws", "expect_product_live"}
)
ROUTE_CONTRACTS: dict[str, dict[str, Any]] = {
    "native_webdriver": {
        "client_protocol": "webdriver_classic",
        "client_endpoint_kind": "webdriver_classic_http",
        "browser_endpoint_kind": "webdriver_classic_http",
        "connect_mode": "connect_existing",
        "provider": "browser_native",
        "ordered_hops": [
            {
                "from": "scenario_adapter",
                "to": "selenium_client",
                "protocol": "selenium_api",
                "transport": "in_process",
                "endpoint_kind": "library_api",
            },
            {
                "from": "selenium_client",
                "to": "browser",
                "protocol": "webdriver_classic",
                "transport": "http",
                "endpoint_kind": "webdriver_classic_http",
            },
        ],
        "lifecycle": {
            "browser_owner": "runner_browser_manager",
            "bridge_owner": "none",
            "adapter_owner": "runner_per_attempt_subprocess",
        },
        "discovery": {
            "browser": {
                "kind": "http_json_version",
                "endpoint_kind": "cdp_http_discovery",
                "probe": "GET /json/version",
                "readiness_owner": "runner_browser_manager",
            },
            "client": {
                "kind": "webdriver_session",
                "endpoint_kind": "webdriver_classic_http",
                "probe": "POST /session and validate returned capabilities",
                "readiness_owner": "adapter_per_attempt",
            },
        },
    },
    "chromedriver_cdp": {
        "client_protocol": "webdriver_classic",
        "client_endpoint_kind": "chromedriver_http",
        "browser_endpoint_kind": "cdp_http_port",
        "connect_mode": "attach_existing",
        "provider": "chromedriver",
        "ordered_hops": [
            {
                "from": "scenario_adapter",
                "to": "selenium_client",
                "protocol": "selenium_api",
                "transport": "in_process",
                "endpoint_kind": "library_api",
            },
            {
                "from": "selenium_client",
                "to": "chromedriver",
                "protocol": "webdriver_classic",
                "transport": "http",
                "endpoint_kind": "chromedriver_http",
            },
            {
                "from": "chromedriver",
                "to": "browser",
                "protocol": "cdp",
                "transport": "debugger_address",
                "endpoint_kind": "cdp_http_port",
            },
        ],
        "lifecycle": {
            "browser_owner": "runner_browser_manager",
            "bridge_owner": "adapter_per_attempt_child",
            "adapter_owner": "runner_per_attempt_subprocess",
        },
        "discovery": {
            "browser": {
                "kind": "http_json_version",
                "endpoint_kind": "cdp_http_discovery",
                "probe": "GET /json/version",
                "readiness_owner": "runner_browser_manager",
            },
            "client": {
                "kind": "chromedriver_service",
                "endpoint_kind": "chromedriver_http",
                "probe": "start Selenium Service on temporary port then POST /session",
                "readiness_owner": "adapter_per_attempt",
            },
        },
    },
}

try:
    from importlib.metadata import version as _pkg_version

    CLIENT_VERSION = _pkg_version("selenium")
except Exception:  # pragma: no cover - metadata lookup is best effort
    CLIENT_VERSION = "unknown"

UNSUPPORTED_MARKERS = ("not found", "wasn't found", "unsupported", "unknown method", "not implemented", "not supported", "unknown command")


def emit(obj: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj))


def to_saved_string(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value == int(value):
        return str(int(value))
    if isinstance(value, (dict, list)):
        try:
            return json.dumps(value, ensure_ascii=False)
        except Exception:
            return str(value)
    return str(value)


def is_unsupported_message(msg: str) -> bool:
    lowered = str(msg or "").lower()
    return any(marker in lowered for marker in UNSUPPORTED_MARKERS)


def http_json(url: str, timeout_s: float) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=timeout_s) as resp:
        return json.load(resp)


def free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def file_sha256_12(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()[:12]


class BindingPayloadError(ValueError):
    """The runner-to-adapter binding fragment is missing or unsafe."""


class SeleniumClientConfigurationError(RuntimeError):
    """The pinned local Selenium API cannot construct the selected route."""


def _object(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise BindingPayloadError(f"{where} must be an object")
    return value


def _string(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BindingPayloadError(f"{where} must be a non-empty string")
    return value


def _string_fields(value: Any, where: str, fields: tuple[str, ...]) -> dict[str, Any]:
    row = _object(value, where)
    for field in fields:
        _string(row.get(field), f"{where}.{field}")
    return row


def _validate_assertions(value: Any, where: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise BindingPayloadError(f"{where} must be a non-empty list")
    rows: list[dict[str, Any]] = []
    for idx, item in enumerate(value):
        assertion_where = f"{where}[{idx}]"
        row = _string_fields(
            item,
            assertion_where,
            ("mechanism", "actual_path", "operator", "condition"),
        )
        has_ref = "expected_ref" in row
        has_literal = "expected_literal" in row
        if has_ref == has_literal:
            raise BindingPayloadError(
                f"{assertion_where} must declare exactly one of expected_ref or expected_literal"
            )
        if has_ref:
            expected_ref = _string(
                row["expected_ref"], f"{assertion_where}.expected_ref"
            )
            if expected_ref not in EXPECTED_REF_FIELDS:
                raise BindingPayloadError(
                    f"{assertion_where}.expected_ref `{expected_ref}` is not allowed"
                )
        else:
            _string(
                row["expected_literal"],
                f"{assertion_where}.expected_literal",
            )
        if row["operator"] != "equals":
            raise BindingPayloadError(
                f"{assertion_where}.operator `{row['operator']}` is unsupported"
            )
        if row["condition"] not in {"always", "expected_nonempty", "when_present"}:
            raise BindingPayloadError(
                f"{assertion_where}.condition `{row['condition']}` is unsupported"
            )
        if row.get("fallback_actual_path") is not None or row.get("fallback_operator") is not None:
            raise BindingPayloadError(
                f"{assertion_where} declares unsupported fallback assertion semantics"
            )
        rows.append(row)
    return rows


def _bridge_executable(binding: dict[str, Any]) -> pathlib.Path:
    pins = _object(binding.get("pins"), "binding.pins")
    bridges = pins.get("bridges")
    if not isinstance(bridges, list) or len(bridges) != 1:
        raise BindingPayloadError(
            "binding.pins.bridges must contain exactly one ChromeDriver pin"
        )
    bridge = _object(bridges[0], "binding.pins.bridges[0]")
    if (
        bridge.get("ref_id") != "bridge.chromedriver"
        or bridge.get("key") != "chromedriver"
    ):
        raise BindingPayloadError(
            "binding.pins.bridges[0] must identify `bridge.chromedriver`"
        )
    metadata = _object(
        bridge.get("metadata"), "binding.pins.bridges[0].metadata"
    )
    binary_path = _string(
        metadata.get("binary_path"),
        "binding.pins.bridges[0].metadata.binary_path",
    )
    _string(
        metadata.get("version"),
        "binding.pins.bridges[0].metadata.version",
    )
    expected_sha = _string(
        metadata.get("sha256_12"),
        "binding.pins.bridges[0].metadata.sha256_12",
    )
    if len(expected_sha) != 12 or any(
        character not in "0123456789abcdef" for character in expected_sha
    ):
        raise BindingPayloadError(
            "binding ChromeDriver sha256_12 must be 12 lowercase hex characters"
        )
    relative = pathlib.Path(binary_path)
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or relative.parts[:2] != ("build_artifacts", "chromedriver")
    ):
        raise BindingPayloadError(
            "binding ChromeDriver binary_path must stay under build_artifacts/chromedriver"
        )
    executable = pathlib.Path(
        _string(
            bridge.get("executable"),
            "binding.pins.bridges[0].executable",
        )
    )
    try:
        expected = (BENCH_ROOT / relative).resolve(strict=True)
        executable_resolved = executable.resolve(strict=True)
        executable_resolved.relative_to(BENCH_ROOT.resolve())
    except (OSError, ValueError) as exc:
        raise BindingPayloadError(
            "binding ChromeDriver executable is missing or escapes the repository"
        ) from exc
    if (
        not executable.is_absolute()
        or executable_resolved != expected
        or not executable_resolved.is_file()
        or not os.access(executable_resolved, os.X_OK)
    ):
        raise BindingPayloadError(
            "binding ChromeDriver executable does not match its pinned binary_path"
        )
    try:
        actual_sha = file_sha256_12(executable_resolved)
    except OSError as exc:
        raise BindingPayloadError(
            "binding ChromeDriver executable could not be read for digest verification"
        ) from exc
    if actual_sha != expected_sha:
        raise BindingPayloadError(
            "binding ChromeDriver executable sha256 mismatch: "
            f"expected {expected_sha}, got {actual_sha}"
        )
    return executable_resolved


def _expected_assertion_value(
    assertion: dict[str, Any],
    payload: dict[str, Any],
) -> str:
    if "expected_literal" in assertion:
        return str(assertion["expected_literal"])
    ref = assertion["expected_ref"]
    if ref not in payload:
        raise BindingPayloadError(
            f"binding identity assertion requires payload.{ref}"
        )
    value = payload[ref]
    if value is None:
        raise BindingPayloadError(
            f"binding identity assertion requires payload.{ref}"
        )
    return str(value)


def _require_assertion(
    assertions: list[dict[str, Any]],
    mechanism: str,
    actual_path: str,
    where: str,
) -> dict[str, Any]:
    matches = [
        row
        for row in assertions
        if row["mechanism"] == mechanism and row["actual_path"] == actual_path
    ]
    if len(matches) != 1:
        raise BindingPayloadError(
            f"{where} must contain exactly one {mechanism}:{actual_path} assertion"
        )
    return matches[0]


def validate_runtime_binding(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate the complete normalized binding before any driver is built."""
    if payload.get("protocol") != "abb_scenario_adapter/1":
        raise BindingPayloadError(
            "payload.protocol must be `abb_scenario_adapter/1`"
        )
    engine = _string(payload.get("engine"), "payload.engine")
    if payload.get("driver_key") != "selenium":
        raise BindingPayloadError("payload.driver_key must be `selenium`")
    binding = _object(payload.get("binding"), "payload.binding")
    binding_id = _string(binding.get("binding_id"), "binding.binding_id")
    browser_id = _string(binding.get("browser_id"), "binding.browser_id")
    driver_id = _string(binding.get("driver_id"), "binding.driver_id")
    if browser_id != engine:
        raise BindingPayloadError(
            f"binding.browser_id `{browser_id}` does not match payload.engine `{engine}`"
        )
    if driver_id != "selenium":
        raise BindingPayloadError(
            f"binding.driver_id `{driver_id}` must be `selenium`"
        )
    if binding_id != f"{browser_id}__{driver_id}":
        raise BindingPayloadError(
            f"binding.binding_id `{binding_id}` does not match its browser/driver IDs"
        )
    if binding.get("fallback_allowed") is not False:
        raise BindingPayloadError("binding.fallback_allowed must be false")

    route = _string_fields(
        binding.get("route"),
        "binding.route",
        (
            "route_id",
            "client_protocol",
            "client_endpoint_kind",
            "browser_endpoint_kind",
            "connect_mode",
            "provider",
        ),
    )
    route_id = route["route_id"]
    if route_id not in SUPPORTED_ROUTE_IDS:
        raise BindingPayloadError(f"unknown Selenium route `{route_id}`")
    if route["client_protocol"] != "webdriver_classic":
        raise BindingPayloadError(
            "binding.route.client_protocol must be `webdriver_classic`"
        )

    hops = route.get("ordered_hops")
    if not isinstance(hops, list) or not hops:
        raise BindingPayloadError(
            "binding.route.ordered_hops must be a non-empty list"
        )
    previous_to = None
    normalized_hops: list[dict[str, str]] = []
    for idx, item in enumerate(hops):
        hop = _string_fields(
            item,
            f"binding.route.ordered_hops[{idx}]",
            ("from", "to", "protocol", "transport", "endpoint_kind"),
        )
        if previous_to is not None and hop["from"] != previous_to:
            raise BindingPayloadError(
                "binding.route.ordered_hops must form a continuous route"
            )
        previous_to = hop["to"]
        normalized_hops.append(
            {
                key: hop[key]
                for key in ("from", "to", "protocol", "transport", "endpoint_kind")
            }
        )
    if previous_to != "browser":
        raise BindingPayloadError(
            "binding.route.ordered_hops must end at `browser`"
        )

    lifecycle = _string_fields(
        route.get("lifecycle"),
        "binding.route.lifecycle",
        ("browser_owner", "bridge_owner", "adapter_owner"),
    )
    if lifecycle["browser_owner"] != "runner_browser_manager":
        raise BindingPayloadError(
            "binding.route.lifecycle.browser_owner must remain `runner_browser_manager`"
        )
    if lifecycle["adapter_owner"] != "runner_per_attempt_subprocess":
        raise BindingPayloadError(
            "binding.route.lifecycle.adapter_owner must remain `runner_per_attempt_subprocess`"
        )
    normalized_lifecycle = {
        key: lifecycle[key]
        for key in ("browser_owner", "bridge_owner", "adapter_owner")
    }
    discovery = _object(route.get("discovery"), "binding.route.discovery")
    normalized_discovery: dict[str, dict[str, str]] = {}
    for point_name in ("browser", "client"):
        point = _string_fields(
            discovery.get(point_name),
            f"binding.route.discovery.{point_name}",
            ("kind", "endpoint_kind", "probe", "readiness_owner"),
        )
        normalized_discovery[point_name] = {
            key: point[key]
            for key in ("kind", "endpoint_kind", "probe", "readiness_owner")
        }
    contract = ROUTE_CONTRACTS[route_id]
    for field in (
        "client_protocol",
        "client_endpoint_kind",
        "browser_endpoint_kind",
        "connect_mode",
        "provider",
    ):
        if route[field] != contract[field]:
            raise BindingPayloadError(
                f"binding.route.{field} does not match route `{route_id}`"
            )
    if normalized_hops != contract["ordered_hops"]:
        raise BindingPayloadError(
            f"binding.route.ordered_hops does not match route `{route_id}`"
        )
    if normalized_lifecycle != contract["lifecycle"]:
        raise BindingPayloadError(
            f"binding.route.lifecycle does not match route `{route_id}`"
        )
    if normalized_discovery != contract["discovery"]:
        raise BindingPayloadError(
            f"binding.route.discovery does not match route `{route_id}`"
        )

    identity = _object(route.get("identity"), "binding.route.identity")
    http_assertions = _validate_assertions(
        identity.get("http_assertions"),
        "binding.route.identity.http_assertions",
    )
    live_assertions = _validate_assertions(
        identity.get("live_transport_assertions"),
        "binding.route.identity.live_transport_assertions",
    )
    for assertion_where, assertions in (
        ("binding.route.identity.http_assertions", http_assertions),
        (
            "binding.route.identity.live_transport_assertions",
            live_assertions,
        ),
    ):
        for idx, assertion in enumerate(assertions):
            if (
                assertion["condition"] == "always"
                and not _expected_assertion_value(assertion, payload).strip()
            ):
                raise BindingPayloadError(
                    f"{assertion_where}[{idx}] condition `always` requires "
                    "a non-empty expected value"
                )
    http_product_assertion = _require_assertion(
        http_assertions,
        "http_json_version",
        "Browser|Product",
        "binding.route.identity.http_assertions",
    )
    http_ua_assertion = _require_assertion(
        http_assertions,
        "http_json_version",
        "User-Agent",
        "binding.route.identity.http_assertions",
    )
    http_ws_assertion = _require_assertion(
        http_assertions,
        "http_json_version",
        "webSocketDebuggerUrl",
        "binding.route.identity.http_assertions",
    )
    if (
        len(http_assertions) != 3
        or http_product_assertion.get("expected_ref") != "expect_product"
        or http_product_assertion["condition"] != "always"
        or http_ua_assertion.get("expected_ref") != "expect_ua"
        or http_ua_assertion["condition"] != "expected_nonempty"
        or http_ws_assertion.get("expected_ref") != "browser_ws"
        or http_ws_assertion["condition"] != "when_present"
    ):
        raise BindingPayloadError(
            "binding.route.identity.http_assertions do not match the Selenium route contract"
        )

    pins = _object(binding.get("pins"), "binding.pins")
    browser_pin = _object(pins.get("browser"), "binding.pins.browser")
    if (
        browser_pin.get("ref_id") != f"browser.{browser_id}"
        or browser_pin.get("key") != browser_id
    ):
        raise BindingPayloadError(
            "binding.pins.browser does not match binding.browser_id"
        )
    driver_pin = _object(pins.get("driver"), "binding.pins.driver")
    driver_metadata = _object(
        driver_pin.get("metadata"), "binding.pins.driver.metadata"
    )
    pinned_client_version = driver_metadata.get("version")
    if (
        driver_pin.get("ref_id") != "driver.selenium"
        or driver_pin.get("key") != "selenium"
        or driver_metadata.get("pip_package") != "selenium"
        or not isinstance(pinned_client_version, str)
        or not pinned_client_version.strip()
    ):
        raise BindingPayloadError(
            "binding.pins.driver does not match the pinned Selenium client"
        )
    if CLIENT_VERSION == "unknown":
        raise BindingPayloadError("pinned Selenium client is not installed")
    if CLIENT_VERSION != pinned_client_version:
        raise BindingPayloadError(
            "installed Selenium client version does not match binding pin: "
            f"expected {pinned_client_version}, got {CLIENT_VERSION}"
        )
    bridges = pins.get("bridges")
    if not isinstance(bridges, list):
        raise BindingPayloadError("binding.pins.bridges must be a list")

    if route_id == "native_webdriver":
        if route["client_endpoint_kind"] != "webdriver_classic_http":
            raise BindingPayloadError(
                "native_webdriver client endpoint must be `webdriver_classic_http`"
            )
        if route["connect_mode"] != "connect_existing":
            raise BindingPayloadError(
                "native_webdriver connect mode must be `connect_existing`"
            )
        if lifecycle["bridge_owner"] != "none" or bridges:
            raise BindingPayloadError(
                "native_webdriver binding must not declare a bridge"
            )
        browser_name_assertion = _require_assertion(
            live_assertions,
            "webdriver_capabilities",
            "capabilities.browserName",
            "binding.route.identity.live_transport_assertions",
        )
        browser_version_assertion = _require_assertion(
            live_assertions,
            "webdriver_capabilities",
            "capabilities.browserVersion",
            "binding.route.identity.live_transport_assertions",
        )
        if (
            len(live_assertions) != 2
            or "expected_literal" not in browser_name_assertion
            or browser_name_assertion["condition"] != "always"
            or browser_version_assertion.get("expected_ref") != "expect_product_live"
            or browser_version_assertion["condition"] != "always"
        ):
            raise BindingPayloadError(
                "native_webdriver live identity assertions do not match the route contract"
            )
    else:
        if route["client_endpoint_kind"] != "chromedriver_http":
            raise BindingPayloadError(
                "chromedriver_cdp client endpoint must be `chromedriver_http`"
            )
        if route["connect_mode"] != "attach_existing":
            raise BindingPayloadError(
                "chromedriver_cdp connect mode must be `attach_existing`"
            )
        if lifecycle["bridge_owner"] == "none":
            raise BindingPayloadError(
                "chromedriver_cdp binding must declare bridge ownership"
            )
        _bridge_executable(binding)
        cdp_identity_assertion = _require_assertion(
            live_assertions,
            "selenium_cdp_extension",
            "execute_cdp_cmd.Browser.getVersion.product",
            "binding.route.identity.live_transport_assertions",
        )
        if (
            len(live_assertions) != 1
            or cdp_identity_assertion.get("expected_ref") != "expect_product_live"
            or cdp_identity_assertion["condition"] != "always"
        ):
            raise BindingPayloadError(
                "chromedriver_cdp live identity assertions do not match the route contract"
            )
    return binding


def native_webdriver_options(browser_name: str, connect_timeout_ms: int) -> Any:
    from selenium.webdriver.common.options import ArgOptions

    options = ArgOptions()
    options.set_capability("browserName", browser_name)
    options.timeouts = {"pageLoad": max(connect_timeout_ms, 10000), "script": 20000}
    return options


def webdriver_client_config(remote_server_addr: str, timeout_s: float) -> Any:
    from selenium.webdriver.remote.client_config import ClientConfig

    return ClientConfig(
        remote_server_addr=remote_server_addr,
        timeout=timeout_s,
        init_args_for_pool_manager={
            "init_args_for_pool_manager": {"retries": 0},
        },
    )


def validate_remote_constructor(
    remote_constructor: Any,
    command_executor: Any,
    options: Any,
) -> None:
    """Validate the local pinned API without starting a remote session."""
    if not callable(remote_constructor):
        raise SeleniumClientConfigurationError(
            "selenium.webdriver.Remote is not callable"
        )
    try:
        inspect.signature(remote_constructor).bind(
            command_executor=command_executor,
            options=options,
        )
    except (TypeError, ValueError) as exc:
        raise SeleniumClientConfigurationError(
            f"selenium.webdriver.Remote API is incompatible: {exc}"
        ) from exc


class SessionRequestTracker:
    """Track newSession when pinned Selenium reaches its real HTTP request."""

    class _ConnectionBoundary:
        def __init__(self, delegate: Any, owner: "SessionRequestTracker"):
            self.delegate = delegate
            self.owner = owner

        def request(self, *args: Any, **kwargs: Any) -> Any:
            if self.owner._new_session_active:
                self.owner.request_started = True
            return self.delegate.request(*args, **kwargs)

        def __getattr__(self, name: str) -> Any:
            return getattr(self.delegate, name)

    def __init__(self, delegate: Any):
        self.delegate = delegate
        self.request_started = False
        self._new_session_active = False
        connection = getattr(delegate, "_conn", None)
        if not callable(getattr(connection, "request", None)):
            raise SeleniumClientConfigurationError(
                "pinned Selenium executor does not expose its HTTP request boundary"
            )
        self._original_connection = connection
        self._connection_boundary = self._ConnectionBoundary(connection, self)
        # Selenium 4.46.0 has no public hook between local command/JSON setup
        # and urllib3's request call. The exact pinned executor is preflighted,
        # so wrapping this narrow transport boundary preserves Selenium's
        # request construction and response semantics for both Catalog routes.
        delegate._conn = self._connection_boundary

    def execute(self, *args: Any, **kwargs: Any) -> Any:
        command = args[0] if args else kwargs.get("command")
        if command == "newSession":
            self._new_session_active = True
            try:
                return self.delegate.execute(*args, **kwargs)
            finally:
                self._new_session_active = False
        return self.delegate.execute(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.delegate, name)

    def restore(self) -> None:
        """Remove the temporary transport probe without clobbering mutations."""
        if getattr(self.delegate, "_conn", None) is self._connection_boundary:
            self.delegate._conn = self._original_connection


def start_remote_session(
    remote_constructor: Any,
    executor: Any,
    options: Any,
) -> Any:
    """Classify constructor errors by whether NEW_SESSION reached the executor."""
    tracker = SessionRequestTracker(executor)
    try:
        validate_remote_constructor(remote_constructor, tracker, options)
        try:
            driver = remote_constructor(
                command_executor=tracker,
                options=options,
            )
        except SeleniumClientConfigurationError:
            raise
        except Exception as exc:
            if tracker.request_started:
                raise
            raise SeleniumClientConfigurationError(
                "pinned Selenium client failed before starting a session "
                f"request: {exc}"
            ) from exc
        if not tracker.request_started:
            raise SeleniumClientConfigurationError(
                "pinned Selenium client returned without starting a session request"
            )
        return driver
    finally:
        tracker.restore()


def start_native_webdriver(
    command_executor: str,
    browser_name: str,
    connect_timeout_ms: int,
) -> Any:
    try:
        from selenium import webdriver
        from selenium.webdriver.remote.remote_connection import RemoteConnection

        client_config = webdriver_client_config(
            command_executor,
            connect_timeout_ms / 1000.0,
        )
        executor = RemoteConnection(client_config=client_config)
        options = native_webdriver_options(browser_name, connect_timeout_ms)
    except SeleniumClientConfigurationError:
        raise
    except Exception as exc:
        raise SeleniumClientConfigurationError(
            f"pinned Selenium client API setup failed: {exc}"
        ) from exc
    return start_remote_session(webdriver.Remote, executor, options)


def chromedriver_status_ready(service_url: str, timeout_s: float) -> bool:
    """Return whether ChromeDriver's HTTP status endpoint reports readiness."""
    request = urllib.request.Request(
        f"{service_url}/status",
        headers={"Connection": "close"},
    )
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=max(0.001, timeout_s)) as response:
            if response.getcode() != 200:
                return False
            body = json.load(response)
    except Exception:
        return False
    if not isinstance(body, dict):
        return False
    ready = body.get("ready")
    if ready is None:
        value = body.get("value")
        ready = value.get("ready") if isinstance(value, dict) else None
    return ready is True


class ChromeDriverBridge:
    """Repository-owned, bounded lifecycle for one ChromeDriver child."""

    def __init__(self, executable: pathlib.Path, port: int):
        self.executable = executable
        self.port = port
        self.service_url = f"http://127.0.0.1:{port}"
        self.process: subprocess.Popen[bytes] | None = None

    def start_until(
        self,
        deadline: float,
        *,
        cleanup_deadline: float | None = None,
    ) -> None:
        if self.process is not None:
            raise SeleniumClientConfigurationError(
                "ChromeDriver bridge process was already started"
            )
        if cleanup_deadline is None:
            cleanup_deadline = deadline + 0.25
        try:
            # The bridge intentionally inherits the adapter process group. The
            # adapter owns normal cleanup; the runner can still reap the whole
            # per-attempt group if this process exits abruptly.
            self.process = subprocess.Popen(
                [
                    str(self.executable),
                    "--enable-chrome-logs",
                    f"--port={self.port}",
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
        except Exception as exc:
            raise SeleniumClientConfigurationError(
                f"ChromeDriver bridge process could not start: {exc}"
            ) from exc

        count = 0
        try:
            while True:
                returncode = self.process.poll()
                if returncode is not None:
                    raise SeleniumClientConfigurationError(
                        "ChromeDriver bridge exited before HTTP readiness "
                        f"with code {returncode}"
                    )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise SeleniumClientConfigurationError(
                        "ChromeDriver HTTP service did not become ready "
                        "before connect timeout"
                    )
                if chromedriver_status_ready(
                    self.service_url,
                    min(0.2, remaining),
                ):
                    return
                delay = min(
                    0.01 + 0.05 * count,
                    0.1,
                    max(0.0, deadline - time.monotonic()),
                )
                if delay:
                    time.sleep(delay)
                count += 1
        except BaseException:
            try:
                self.stop(deadline=cleanup_deadline)
            except BaseException:
                # Preserve the readiness/configuration exception. The runner's
                # process-group owner remains the final containment boundary.
                pass
            raise

    def stop(
        self,
        grace_s: float = 0.5,
        *,
        deadline: float | None = None,
    ) -> None:
        """Idempotently reap the direct bridge without Selenium's slow stop."""
        process = self.process
        if process is None:
            return
        if deadline is None:
            deadline = time.monotonic() + grace_s * 2
        interrupted: BaseException | None = None
        remaining = max(0.0, deadline - time.monotonic())
        term_wait_s = min(grace_s, remaining / 2)
        try:
            if process.poll() is not None:
                process.wait(timeout=0)
            else:
                process.terminate()
                process.wait(timeout=term_wait_s)
        except subprocess.TimeoutExpired:
            pass
        except BaseException as exc:
            interrupted = exc

        if process.poll() is None:
            try:
                process.kill()
            except Exception:
                pass
            except BaseException as exc:
                if interrupted is None:
                    interrupted = exc
        if process.poll() is None:
            try:
                process.wait(timeout=max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                pass
            except Exception:
                pass
            except BaseException as exc:
                if interrupted is None:
                    interrupted = exc
        if process.poll() is not None:
            self.process = None
        if interrupted is not None:
            raise interrupted


def stop_bridge_preserving_error(
    bridge: ChromeDriverBridge | None,
    deadline: float,
) -> None:
    """Best-effort bridge teardown for use while another exception is active."""
    if bridge is None:
        return
    try:
        bridge.stop(deadline=deadline)
    except BaseException:
        pass


def start_chromedriver_webdriver(
    executable: pathlib.Path,
    debugger_address: str,
    connect_timeout_ms: int,
    cleanup_deadline: float | None = None,
) -> tuple[Any, Any]:
    bridge = None
    connect_timeout_s = connect_timeout_ms / 1000.0
    deadline = time.monotonic() + connect_timeout_s
    if cleanup_deadline is None:
        cleanup_deadline = deadline + min(
            1.0,
            max(0.25, connect_timeout_s * 0.1),
        )
    try:
        from selenium import webdriver
        from selenium.webdriver.chromium.remote_connection import ChromiumRemoteConnection

        options = webdriver.ChromeOptions()
        options.timeouts = {"pageLoad": max(connect_timeout_ms, 10000), "script": 20000}
        options.debugger_address = debugger_address
        bridge = ChromeDriverBridge(executable, free_port())
    except SeleniumClientConfigurationError:
        stop_bridge_preserving_error(bridge, cleanup_deadline)
        raise
    except Exception as exc:
        stop_bridge_preserving_error(bridge, cleanup_deadline)
        raise SeleniumClientConfigurationError(
            f"pinned Selenium client API setup failed: {exc}"
        ) from exc

    try:
        bridge.start_until(deadline, cleanup_deadline=cleanup_deadline)
        remaining_s = deadline - time.monotonic()
        if remaining_s <= 0:
            raise SeleniumClientConfigurationError(
                "ChromeDriver session creation exceeded connect timeout"
            )
    except BaseException:
        stop_bridge_preserving_error(bridge, cleanup_deadline)
        raise

    try:
        client_config = webdriver_client_config(
            bridge.service_url,
            remaining_s,
        )
        executor = ChromiumRemoteConnection(
            remote_server_addr=bridge.service_url,
            vendor_prefix="goog",
            browser_name="chrome",
            client_config=client_config,
        )
    except SeleniumClientConfigurationError:
        stop_bridge_preserving_error(bridge, cleanup_deadline)
        raise
    except Exception as exc:
        stop_bridge_preserving_error(bridge, cleanup_deadline)
        raise SeleniumClientConfigurationError(
            f"pinned Selenium client API setup failed: {exc}"
        ) from exc
    except BaseException:
        stop_bridge_preserving_error(bridge, cleanup_deadline)
        raise

    try:
        driver = start_remote_session(webdriver.Remote, executor, options)
    except BaseException:
        stop_bridge_preserving_error(bridge, cleanup_deadline)
        raise
    return driver, bridge


def connect_webdriver(
    payload: dict[str, Any],
    binding: dict[str, Any],
    connect_timeout_ms: int,
    cleanup_deadline: float | None = None,
) -> tuple[Any, Any | None]:
    """Construct only the route described by the validated binding."""
    route_id = binding["route"]["route_id"]
    cdp_port = int(payload["cdp_port"])
    if route_id == "native_webdriver":
        assertion = _require_assertion(
            binding["route"]["identity"]["live_transport_assertions"],
            "webdriver_capabilities",
            "capabilities.browserName",
            "binding.route.identity.live_transport_assertions",
        )
        browser_name = _expected_assertion_value(assertion, payload)
        return (
            start_native_webdriver(
                f"http://127.0.0.1:{cdp_port}",
                browser_name,
                connect_timeout_ms,
            ),
            None,
        )
    if route_id == "chromedriver_cdp":
        executable = _bridge_executable(binding)
        return start_chromedriver_webdriver(
            executable,
            f"127.0.0.1:{cdp_port}",
            connect_timeout_ms,
            cleanup_deadline,
        )
    raise BindingPayloadError(f"unknown Selenium route `{route_id}`")


class Adapter:
    def __init__(self, payload: dict[str, Any]):
        self.payload = payload
        self.fixture_url = str(payload["task_url"])
        parts = urllib.parse.urlsplit(self.fixture_url)
        self.fixture_origin = f"{parts.scheme}://{parts.netloc}"
        self.fixture_host = parts.netloc
        self.artifact_dir = pathlib.Path(payload.get("artifact_dir") or ".")
        self.action_timeout_ms = int(payload.get("action_timeout_ms") or 8000)
        task_timeout_ms = int(payload.get("task_timeout_ms") or 30000)
        # Leave a 3s reserve for check evaluation and result emission.
        self.budget_deadline = time.monotonic() + (task_timeout_ms - 3000) / 1000.0
        self.driver = None
        self.route_id = str(payload["binding"]["route"]["route_id"])
        self.op_calls = 0
        self.op_errors = 0
        self.saved: dict[str, str] = {}
        self.step_results: list[dict[str, Any]] = []
        self.cdp_path = self.artifact_dir / "cdp.jsonl"

    def trace(self, obj: dict[str, Any]) -> None:
        try:
            with self.cdp_path.open("a", encoding="utf-8") as fh:
                obj["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                fh.write(json.dumps(obj) + "\n")
        except Exception:
            pass  # trace failures must not fail the probe

    def substitute(self, raw: Any) -> str:
        text = str(raw)
        text = text.replace("{fixture_url}", self.fixture_url)
        text = text.replace("{fixture_origin}", self.fixture_origin)
        text = text.replace("{fixture_host}", self.fixture_host)
        text = text.replace("{artifact_dir}", str(self.artifact_dir))
        return text

    def check_budget(self) -> None:
        if time.monotonic() > self.budget_deadline:
            raise RuntimeError("task budget exhausted before op could run")

    def call(self, fn):
        self.op_calls += 1
        self.check_budget()
        try:
            return fn()
        except Exception as exc:
            self.op_errors += 1
            raise RuntimeError(str(exc).splitlines()[0][:500] if str(exc) else type(exc).__name__) from None

    def eval_expr(self, expression: str) -> Any:
        # eval() of the raw program keeps completion-value semantics so
        # multi-statement expressions ("a; b; c") stay legal, matching the
        # raw Runtime.evaluate adapters.
        quoted = json.dumps(expression)
        return self.call(lambda: self.driver.execute_script(f"return eval({quoted});"))

    def sel_expr(self, sel: str, body: str) -> str:
        quoted = json.dumps(sel)
        escaped = sel.replace('"', '\\"')
        return (
            f'(() => {{ const el = document.querySelector({quoted}); '
            f'if (!el) throw new Error("no element matches {escaped}"); {body} }})()'
        )

    def poll_until(self, timeout_ms: int, expression: str, what: str) -> None:
        deadline = time.monotonic() + timeout_ms / 1000.0
        while True:
            value = None
            try:
                value = self.eval_expr(expression)
            except Exception:
                pass  # evaluation context may be mid-navigation; retry
            if value:
                return
            if time.monotonic() > deadline or time.monotonic() > self.budget_deadline:
                raise RuntimeError(f"timeout after {timeout_ms}ms waiting for {what}")
            time.sleep(0.05)

    def find(self, sel: str):
        from selenium.webdriver.common.by import By

        return self.call(lambda: self.driver.find_element(By.CSS_SELECTOR, sel))

    def run_op(self, step: dict[str, Any]) -> Any:
        op = step["op"]
        sel = self.substitute(step["selector"]) if step.get("selector") else None
        timeout = int(step["timeout_ms"]) if step.get("timeout_ms") else self.action_timeout_ms

        if op == "wait_ms":
            time.sleep(float(step.get("ms") or 100) / 1000.0)
            return None
        if op == "version":
            if self.route_id == "native_webdriver":
                return self.call(lambda: self.driver.capabilities.get("browserVersion"))
            return self.call(lambda: self.driver.execute_cdp_cmd("Browser.getVersion", {}).get("product"))
        if op == "user_agent":
            if self.route_id == "native_webdriver":
                return self.eval_expr("navigator.userAgent")
            return self.call(lambda: self.driver.execute_cdp_cmd("Browser.getVersion", {}).get("userAgent"))
        if op == "new_page":
            def new_tab():
                self.driver.switch_to.new_window("tab")
                return "page_created"

            return self.call(new_tab)
        if op == "goto":
            target = self.substitute(step.get("url") or "{fixture_url}")
            self.call(lambda: self.driver.get(target))
            self.poll_until(timeout, self.settle_expression(target), f"navigation to {target}")
            return "navigated"
        if op == "reload":
            self.eval_expr("window.__abb_reload_probe = 1, 'marked'")
            self.call(lambda: self.driver.refresh())
            self.poll_until(timeout, 'document.readyState === "complete" && !window.__abb_reload_probe', "reload to settle")
            return "reloaded"
        if op in ("go_back", "go_forward"):
            nav_nonce = f"np{time.time_ns()}"
            self.eval_expr(f"window.__abb_nav_probe = '{nav_nonce}|' + location.href, 'marked'")
            if op == "go_back":
                self.call(lambda: self.driver.back())
            else:
                self.call(lambda: self.driver.forward())
            self.poll_until(timeout, f'document.readyState === "complete" && window.__abb_nav_probe !== "{nav_nonce}|" + location.href', op)
            return "ok"
        if op == "click":
            times = int(step.get("times") or 1)
            for _ in range(times):
                element = self.find(sel)
                self.call(element.click)
            return f"clicked x{times}"
        if op == "fill":
            value = self.substitute("" if step.get("value") is None else step["value"])
            element = self.find(sel)
            self.call(element.clear)
            self.call(lambda: element.send_keys(value))
            return "filled"
        if op == "type":
            text = self.substitute("" if step.get("text") is None else step["text"])
            element = self.find(sel)
            self.call(lambda: element.send_keys(text))
            return "typed"
        if op == "press":
            from selenium.webdriver.common.keys import Keys

            key = str(step.get("key") or "")
            mapped = {"Enter": Keys.ENTER, "Tab": Keys.TAB, "Escape": Keys.ESCAPE, "Backspace": Keys.BACKSPACE}.get(key)
            if mapped is None:
                raise RuntimeError(f"unsupported key {key!r} for press")
            element = self.find(sel) if sel else None
            if element is None:
                raise RuntimeError("press without selector is not supported by the selenium adapter")
            self.call(lambda: element.send_keys(mapped))
            return f"pressed {key}"
        if op == "check":
            element = self.find(sel)
            already = self.call(element.is_selected)
            if not already:
                self.call(element.click)
            return "checked"
        if op == "select_option":
            from selenium.webdriver.support.ui import Select

            value = self.substitute(step.get("value"))
            element = self.find(sel)
            self.call(lambda: Select(element).select_by_value(value))
            return self.eval_expr(f"[document.querySelector({json.dumps(sel)}).value]")
        if op == "focus":
            self.eval_expr(self.sel_expr(sel, 'el.focus(); return "focused";'))
            return "focused"
        if op == "evaluate":
            return self.eval_expr(self.substitute(step["expression"]))
        if op == "wait_for_function":
            self.poll_until(timeout, self.substitute(step["expression"]), "predicate")
            return "predicate_true"
        if op == "wait_for_selector":
            quoted = json.dumps(sel)
            state = step.get("state")
            if state in ("hidden", "detached"):
                expression = (
                    f"(() => {{ const el = document.querySelector({quoted}); if (!el) return true; "
                    'const s = window.getComputedStyle(el); return s.display === "none" || s.visibility === "hidden"; })()'
                )
            elif state == "visible":
                expression = (
                    f"(() => {{ const el = document.querySelector({quoted}); if (!el) return false; "
                    'const s = window.getComputedStyle(el); return s.display !== "none" && s.visibility !== "hidden"; })()'
                )
            else:
                expression = f"!!document.querySelector({quoted})"
            self.poll_until(timeout, expression, f"selector {sel}")
            return "selector_ready"
        if op == "text_content":
            return self.eval_expr(self.sel_expr(sel, "return el.textContent;"))
        if op == "inner_text":
            element = self.find(sel)
            return self.call(lambda: element.text)
        if op == "get_attribute":
            element = self.find(sel)
            return self.call(lambda: element.get_attribute(step.get("name")))
        if op == "input_value":
            element = self.find(sel)
            return self.call(lambda: element.get_attribute("value"))
        if op == "count":
            return self.eval_expr(f"document.querySelectorAll({json.dumps(sel)}).length")
        if op == "is_visible":
            element = self.find(sel)
            return self.call(element.is_displayed)
        if op == "is_checked":
            element = self.find(sel)
            return self.call(element.is_selected)
        if op == "is_enabled":
            element = self.find(sel)
            return self.call(element.is_enabled)
        if op == "title":
            return self.call(lambda: self.driver.title)
        if op == "url":
            return self.call(lambda: self.driver.current_url)
        raise RuntimeError(f"unknown op {op!r}")

    def settle_expression(self, target: str) -> str:
        parts = urllib.parse.urlsplit(target)
        want_path = json.dumps(parts.path + (f"?{parts.query}" if parts.query else ""))
        return f'document.readyState === "complete" && (location.pathname + location.search) === {want_path}'

    def evaluate_check(self, check: dict[str, Any]) -> tuple[bool, str]:
        kind = check.get("kind")
        name = check.get("name")
        if kind == "saved_equals":
            value = self.saved.get(name)
            expected = str(check.get("expected"))
            return value == expected, f"{name}={json.dumps(value)} expected={json.dumps(expected)}"
        if kind in ("saved_contains", "saved_not_contains"):
            value = self.saved.get(name)
            want = self.substitute(str(check.get("expected")))
            contains = isinstance(value, str) and want in value
            ok = contains if kind == "saved_contains" else (isinstance(value, str) and not contains)
            clause = "must contain" if kind == "saved_contains" else "must NOT contain"
            shown = value[:300] if isinstance(value, str) else value
            return ok, f"{name}={json.dumps(shown)} {clause} {json.dumps(want)}"
        if kind == "saved_truthy":
            value = self.saved.get(name)
            truthy = value is not None and value not in ("", "undefined", "null", "false") and not str(value).startswith("ERROR:")
            return truthy, f"{name}={json.dumps(str(value)[:300] if value is not None else None)}"
        if kind in ("step_ok", "step_fails"):
            idx = int(check.get("step", -1))
            row = self.step_results[idx] if 0 <= idx < len(self.step_results) else {}
            err = row.get("error") or "none"
            if kind == "step_ok":
                return bool(row.get("ok")), f"step {idx} ok={bool(row.get('ok'))} error={err}"
            return row.get("ok") is False, f"step {idx} ok={bool(row.get('ok'))} (must fail) error={err}"
        if kind == "file_nonempty":
            file_path = pathlib.Path(self.substitute(check.get("path")))
            size = file_path.stat().st_size if file_path.exists() else 0
            return size > 0, f"{file_path} size={size}"
        if kind == "any_of":
            results = [self.evaluate_check(sub) for sub in check.get("checks") or []]
            evidence = " | ".join(f"{'pass' if ok else 'fail'}: {ev}" for ok, ev in results)
            return any(ok for ok, _ in results), evidence
        return False, f"unknown check kind {kind}"


def check_name(check: dict[str, Any], idx: int) -> str:
    return check.get("label") or check.get("kind") or f"check{idx}"


def verify_identity_assertions(
    assertions: list[dict[str, Any]],
    actual_values: dict[str, Any],
    payload: dict[str, Any],
    phase: str,
) -> list[dict[str, Any]]:
    """Evaluate the frozen Catalog's current equality assertions."""
    rows: list[dict[str, Any]] = []
    for assertion in assertions:
        actual_path = assertion["actual_path"]
        if actual_path not in actual_values:
            raise BindingPayloadError(
                f"{phase} identity assertion uses unsupported actual_path `{actual_path}`"
            )
        actual = actual_values[actual_path]
        expected = _expected_assertion_value(assertion, payload)
        condition = assertion["condition"]
        if condition == "expected_nonempty" and not expected:
            rows.append(
                {
                    "mechanism": assertion["mechanism"],
                    "actual_path": actual_path,
                    "status": "not_applicable",
                    "condition": condition,
                }
            )
            continue
        if condition == "when_present" and (actual is None or actual == ""):
            rows.append(
                {
                    "mechanism": assertion["mechanism"],
                    "actual_path": actual_path,
                    "status": "not_applicable",
                    "condition": condition,
                }
            )
            continue
        actual_text = "" if actual is None else str(actual)
        if actual_text != expected:
            raise BindingPayloadError(
                f"{phase} identity mismatch at {actual_path}: "
                f"actual={json.dumps(actual_text)} expected={json.dumps(expected)}"
            )
        rows.append(
            {
                "mechanism": assertion["mechanism"],
                "actual_path": actual_path,
                "status": "verified",
                "actual": actual_text,
                "expected": expected,
            }
        )
    return rows


def binding_observation(
    payload: dict[str, Any],
    binding: dict[str, Any],
) -> dict[str, Any]:
    route_id = binding["route"]["route_id"]
    return {
        "binding_id": binding["binding_id"],
        "browser_id": binding["browser_id"],
        "driver_id": binding["driver_id"],
        "route_id": route_id,
        # Legacy observation key retained for older result readers. Its value
        # now comes from the Catalog route, never from engine-name inference.
        "transport": route_id,
        "transport_policy": payload.get("transport_policy"),
        "fallback_allowed": binding["fallback_allowed"],
        "browser_ws": payload.get("browser_ws"),
        "expect_product": payload.get("expect_product"),
        "identity": {"http": [], "live": []},
        "verified": False,
        "gate": None,
        "client_version": CLIENT_VERSION,
    }


def emit_script_error(
    message: str,
    observation: dict[str, Any] | None = None,
    metrics: dict[str, Any] | None = None,
) -> None:
    emit(
        {
            "ok": False,
            "error": {"class": "script_error", "message": message},
            "observations": {"binding": observation} if observation is not None else {},
            "metrics": metrics or {},
        }
    )


def main() -> None:
    try:
        payload = json.loads(sys.stdin.read())
    except Exception as exc:
        emit_script_error(f"invalid payload JSON on stdin: {exc}")
        return
    if not isinstance(payload, dict):
        emit_script_error("payload must be a JSON object")
        return

    observation: dict[str, Any] | None = None
    try:
        binding = validate_runtime_binding(payload)
        browser_ws = _string(payload.get("browser_ws"), "payload.browser_ws")
        _string(payload.get("task_url"), "payload.task_url")
        raw_port = payload.get("cdp_port")
        if (
            not isinstance(raw_port, int)
            or isinstance(raw_port, bool)
            or not (1 <= raw_port <= 65535)
        ):
            raise BindingPayloadError(
                "payload.cdp_port must be an integer between 1 and 65535"
            )
        cdp_port = raw_port
        connect_timeout_ms = int(payload.get("connect_timeout_ms") or 15000)
        if connect_timeout_ms <= 0:
            raise BindingPayloadError("payload.connect_timeout_ms must be positive")
        steps = payload.get("steps") or []
        checks = payload.get("checks") or []
        if not isinstance(steps, list) or not isinstance(checks, list):
            raise BindingPayloadError("payload.steps and payload.checks must be lists")
        observation = binding_observation(payload, binding)
    except (BindingPayloadError, TypeError, ValueError) as exc:
        emit_script_error(f"invalid Selenium binding payload: {exc}", observation)
        return

    adapter = Adapter(payload)
    adapter.artifact_dir.mkdir(parents=True, exist_ok=True)
    adapter.cdp_path.write_text("", encoding="utf-8")

    # ---- Binding gate 1/2: HTTP identity from the Catalog assertions.
    try:
        version_info = http_json(f"http://127.0.0.1:{cdp_port}/json/version", 4.0)
    except Exception as exc:
        emit_script_error(
            f"binding gate: /json/version unreachable on port {cdp_port}: {exc}",
            observation,
        )
        return
    if not isinstance(version_info, dict):
        emit_script_error(
            "binding gate: /json/version must return a JSON object",
            observation,
        )
        return
    http_product = str(version_info.get("Browser") or version_info.get("Product") or "")
    http_ua = str(version_info.get("User-Agent") or "")
    ws_from_version = version_info.get("webSocketDebuggerUrl")
    try:
        observation["identity"]["http"] = verify_identity_assertions(
            binding["route"]["identity"]["http_assertions"],
            {
                "Browser|Product": http_product,
                "User-Agent": http_ua,
                "webSocketDebuggerUrl": ws_from_version,
            },
            payload,
            "HTTP",
        )
    except BindingPayloadError as exc:
        emit_script_error(f"binding gate: {exc}", observation)
        return
    observation["http_product"] = http_product
    observation["http_ua"] = http_ua
    observation["gate"] = "http_json_version"

    route_id = binding["route"]["route_id"]

    # ---- Create only the Catalog-selected WebDriver route, then gate 2/2.
    connect_error = None
    driver = None
    bridge_owner = None
    try:
        driver, bridge_owner = connect_webdriver(
            payload,
            binding,
            connect_timeout_ms,
            adapter.budget_deadline,
        )
    except BindingPayloadError as exc:
        emit_script_error(f"invalid Selenium binding payload: {exc}", observation)
        return
    except (
        SeleniumClientConfigurationError,
        ImportError,
    ) as exc:
        emit_script_error(f"Selenium client configuration error: {exc}", observation)
        return
    except Exception as exc:
        connect_error = str(exc).splitlines()[0][:1000] if str(exc) else type(exc).__name__
    adapter.trace({"direction": "selenium", "step": "connect", "ok": connect_error is None, "error": connect_error})

    if connect_error is not None:
        if route_id == "native_webdriver":
            connect_evidence = (
                f"selenium@{CLIENT_VERSION} could not create a native WebDriver session "
                f"at 127.0.0.1:{cdp_port}: {connect_error}"
            )
            skipped_evidence = "native WebDriver session was not created; scenario not executed"
        else:
            connect_evidence = (
                f"selenium@{CLIENT_VERSION}+chromedriver could not attach to "
                f"127.0.0.1:{cdp_port}: {connect_error}"
            )
            skipped_evidence = "chromedriver did not attach; scenario not executed"
        rows = [
            {"name": "driver_connect", "status": "fail", "evidence": connect_evidence}
        ] + [
            {"name": check_name(check, idx), "status": "fail", "evidence": skipped_evidence}
            for idx, check in enumerate(checks)
        ]
        if bridge_owner is not None:
            try:
                bridge_owner.stop()
            except Exception:
                pass
        emit(
            {
                "ok": True,
                "answer": f"0/{len(rows)} checks",
                "observations": {
                    "checks": rows,
                    "saved": {},
                    "binding": observation,
                    "connect_error": connect_error,
                    "failure_class": "cdp_semantic",
                },
                "metrics": {"cdp_call_count": 1, "cdp_error_count": 1, "ws_disconnect_count": 0},
            }
        )
        return

    adapter.driver = driver
    try:
        if route_id == "native_webdriver":
            capabilities = driver.capabilities or {}
            live_browser_name = str(capabilities.get("browserName") or "")
            live_product = str(capabilities.get("browserVersion") or "")
            live_values = {
                "capabilities.browserName": live_browser_name,
                "capabilities.browserVersion": live_product,
            }
            browser_name_assertion = _require_assertion(
                binding["route"]["identity"]["live_transport_assertions"],
                "webdriver_capabilities",
                "capabilities.browserName",
                "binding.route.identity.live_transport_assertions",
            )
            observation["expected_browser_name"] = _expected_assertion_value(
                browser_name_assertion,
                payload,
            )
            observation["live_browser_name"] = live_browser_name
            observation["live_check"] = "webdriver_session_capabilities"
        else:
            live_product = ""
            try:
                live_product = str(driver.execute_cdp_cmd("Browser.getVersion", {}).get("product") or "")
            except Exception:
                pass  # the mandatory assertion below fails closed
            live_values = {
                "execute_cdp_cmd.Browser.getVersion.product": live_product,
            }
            observation["live_check"] = "selenium_execute_cdp_browser_get_version"
        observation["expect_product_live"] = payload.get("expect_product_live")
        observation["live_product"] = live_product
        try:
            observation["identity"]["live"] = verify_identity_assertions(
                binding["route"]["identity"]["live_transport_assertions"],
                live_values,
                payload,
                "live transport",
            )
        except BindingPayloadError as exc:
            emit_script_error(
                f"binding gate: {exc} — the driver is not bound to the engine under test",
                observation,
                {
                    "cdp_call_count": adapter.op_calls,
                    "cdp_error_count": adapter.op_errors,
                    "ws_disconnect_count": 0,
                },
            )
            return
        observation["verified"] = True
        adapter.trace({"direction": "selenium", "step": "binding_verified", "product": live_product})

        for idx, step in enumerate(steps):
            try:
                value = adapter.run_op(step)
                result: dict[str, Any] = {"ok": True, "value": value}
            except Exception as exc:
                message = str(exc)[:1000] or type(exc).__name__
                result = {"ok": False, "error": message, "unsupported": is_unsupported_message(message)}
            adapter.step_results.append(result)
            adapter.trace({"direction": "selenium", "step": idx, "op": step.get("op"), "selector": step.get("selector"), "ok": result["ok"], "error": result.get("error")})
            if result["ok"] and step.get("expect_fail"):
                result["unexpected_success"] = True
            if step.get("save_as"):
                adapter.saved[step["save_as"]] = (
                    to_saved_string(result["value"]) if result["ok"] else f"ERROR: {result['error']}"
                )
                if result["ok"] and result["value"] is None:
                    adapter.saved[step["save_as"]] = "null"

        if route_id == "native_webdriver":
            connect_evidence = (
                f"selenium@{CLIENT_VERSION} bound directly to "
                f"{observation['live_browser_name']} {observation['live_product']}"
            )
        else:
            connect_evidence = (
                f"selenium@{CLIENT_VERSION}+chromedriver bound to "
                f"{observation['live_product']}"
            )
        rows = [{"name": "driver_connect", "status": "pass", "evidence": connect_evidence}]
        for idx, check in enumerate(checks):
            ok, evidence = adapter.evaluate_check(check)
            rows.append({"name": check_name(check, idx), "status": "pass" if ok else "fail", "evidence": evidence})
        passed = sum(1 for row in rows if row["status"] == "pass")
        emit(
            {
                "ok": True,
                "answer": adapter.saved.get("answer", f"{passed}/{len(rows)} checks"),
                "observations": {
                    "checks": rows,
                    "saved": adapter.saved,
                    "binding": observation,
                    "driver_ops": len(adapter.step_results),
                    "driver_op_errors": sum(1 for row in adapter.step_results if not row["ok"]),
                    "failure_class": "cdp_semantic",
                },
                "metrics": {"cdp_call_count": adapter.op_calls, "cdp_error_count": adapter.op_errors, "ws_disconnect_count": 0},
            }
        )
    except Exception as exc:
        message = str(exc)
        klass = "engine_unsupported" if is_unsupported_message(message) else "script_error"
        emit(
            {
                "ok": False,
                "error": {"class": klass, "message": message},
                "observations": {"saved": adapter.saved, "binding": observation},
                "metrics": {"cdp_call_count": adapter.op_calls, "cdp_error_count": adapter.op_errors, "ws_disconnect_count": 0},
            }
        )
    finally:
        if driver is not None:
            try:
                driver.quit()  # WebDriver session only; runner owns the browser
            except Exception:
                pass
        if bridge_owner is not None:
            try:
                bridge_owner.stop()
            except Exception:
                pass


if __name__ == "__main__":
    main()
