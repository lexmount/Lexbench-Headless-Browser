#!/usr/bin/env python3
"""Strict Chrome/Moli/Kitesurf probes that complement Web Platform Tests.

Every case attempt opens exactly one browser-level CDP connection, verifies the
browser identity on that connection, and performs one agent-relevant closure
without reconnect or fallback.  The cases intentionally avoid screenshots,
PDFs, pixel output, and broad method-acceptance enumeration.
"""

from __future__ import annotations

import argparse
import base64
import collections
import datetime as dt
import hashlib
import json
import pathlib
import sys
import time
from dataclasses import dataclass
from typing import Any, Callable


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from runner import run as bench  # noqa: E402
from tools.kitesurf_common import (  # noqa: E402
    capture_source_provenance,
    write_json,
)
from tools.kitesurf_static_fixture import (  # noqa: E402
    DEFAULT_MANIFEST as DEFAULT_FIXTURE_MANIFEST,
    FixtureVerificationError,
    deployment_base_url,
    verify_fixture,
)


DEFAULT_SEED = "official20260709"
RUNTIME_FIXTURE_PATHS = {
    "fetch_interception_lifecycle": (
        "v1/network/index.html",
        "v1/network/style.css",
        "v1/network/pixel.svg",
        "v1/network/script.js",
        "v1/network/data.json",
    ),
    "fetch_fulfill_lifecycle": (
        "v1/network/index.html",
        "v1/network/style.css",
        "v1/network/pixel.svg",
        "v1/network/script.js",
    ),
    "fetch_promise_control_flow": (
        "v1/network/index.html",
        "v1/network/style.css",
        "v1/network/pixel.svg",
        "v1/network/script.js",
        "v1/network/data.json",
    ),
    "nested_frame_context_routing": (
        "v1/frames/index.html",
        "v1/frames/child.html",
        "v1/frames/grandchild.html",
    ),
}
RUNTIME_FIXTURE_CASES = frozenset(RUNTIME_FIXTURE_PATHS)


CASE_META = {
    "target_session_isolation": {
        "wpt_overlap": "none",
        "agent_relevance": "critical",
        "closure": "two targets -> two flat sessions -> isolated state -> close/invalidate -> surviving session recovery",
    },
    "remote_object_lifecycle": {
        "wpt_overlap": "none",
        "agent_relevance": "critical",
        "closure": "remote handle -> properties -> release -> stale-handle rejection -> same-session recovery",
    },
    "fetch_interception_lifecycle": {
        "wpt_overlap": "primitive_only",
        "agent_relevance": "critical",
        "closure": "pause event ID -> continue -> network terminal event -> page-visible response -> same-session recovery",
    },
    "fetch_fulfill_lifecycle": {
        "wpt_overlap": "none",
        "agent_relevance": "critical",
        "closure": "pause event ID -> synthetic status/headers/body -> page-visible response -> same-session recovery",
    },
    "fetch_promise_control_flow": {
        "wpt_overlap": "none",
        "agent_relevance": "critical",
        "closure": "non-awaited Runtime promise handle -> pause event ID -> continue -> Runtime.awaitPromise -> same-session recovery",
    },
    "nested_frame_context_routing": {
        "wpt_overlap": "primitive_only",
        "agent_relevance": "critical",
        "closure": "three-level frame tree -> isolated worlds -> event envelopes -> frame-scoped DOM state",
    },
}


@dataclass
class ProbeFailure(Exception):
    layer: str
    code: str
    detail: str
    observations: dict[str, Any] | None = None

    def __str__(self) -> str:
        return f"{self.layer}/{self.code}: {self.detail}"


def parse_mapping(value: str, option: str) -> tuple[str, str]:
    name, separator, mapped = value.partition("=")
    if not separator or not name.strip() or not mapped.strip():
        raise argparse.ArgumentTypeError(f"{option} must use NAME=VALUE")
    return name.strip(), mapped.strip()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--engine",
        action="append",
        required=True,
        help="engine and credential-free browser WSS/WS endpoint as NAME=URL",
    )
    parser.add_argument(
        "--expect-product",
        action="append",
        default=[],
        help="required exact Browser.getVersion product as NAME=PRODUCT",
    )
    parser.add_argument(
        "--expect-protocol-version",
        action="append",
        default=[],
        help="required exact Browser.getVersion protocolVersion as NAME=VERSION",
    )
    parser.add_argument(
        "--expect-revision",
        action="append",
        default=[],
        help="required exact Browser.getVersion revision as NAME=REVISION",
    )
    parser.add_argument("--fixture-base-url", default=deployment_base_url())
    parser.add_argument(
        "--fixture-manifest",
        type=pathlib.Path,
        default=DEFAULT_FIXTURE_MANIFEST,
    )
    parser.add_argument("--attempts", type=int, default=3)
    parser.add_argument("--seed", default=DEFAULT_SEED)
    parser.add_argument("--case", action="append", choices=sorted(CASE_META))
    parser.add_argument("--timeout-s", type=float, default=12.0)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    args = parser.parse_args()
    if not 1 <= args.attempts <= 5:
        parser.error("--attempts must be between 1 and 5")
    if args.timeout_s <= 0 or args.timeout_s > 30:
        parser.error("--timeout-s must be in (0, 30]")
    args.engines = dict(parse_mapping(value, "--engine") for value in args.engine)
    if len(args.engines) != len(args.engine):
        parser.error("--engine names must be unique")
    identity_options = {
        "product": ("--expect-product", args.expect_product),
        "protocolVersion": (
            "--expect-protocol-version",
            args.expect_protocol_version,
        ),
        "revision": ("--expect-revision", args.expect_revision),
    }
    identity_maps: dict[str, dict[str, str]] = {}
    for field, (option, raw_values) in identity_options.items():
        pairs = [parse_mapping(value, option) for value in raw_values]
        mapped = dict(pairs)
        if len(mapped) != len(pairs):
            parser.error(f"{option} engine names must be unique")
        missing = sorted(set(args.engines) - set(mapped))
        unknown = sorted(set(mapped) - set(args.engines))
        if missing or unknown:
            details = []
            if missing:
                details.append("missing " + ", ".join(missing))
            if unknown:
                details.append("unknown " + ", ".join(unknown))
            parser.error(f"{option} must match every --engine: " + "; ".join(details))
        identity_maps[field] = mapped
    args.expected_identities = {
        name: {
            field: identity_maps[field][name]
            for field in bench.REMOTE_CDP_IDENTITY_FIELDS
        }
        for name in args.engines
    }
    for name, endpoint in args.engines.items():
        parsed = bench.urllib.parse.urlparse(endpoint)
        if (
            parsed.scheme not in {"ws", "wss"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            parser.error(f"--engine {name} endpoint must be credential-free WS(S)")
    args.fixture_base_url = args.fixture_base_url.rstrip("/")
    parsed_fixture = bench.urllib.parse.urlparse(args.fixture_base_url)
    if parsed_fixture.scheme not in {"http", "https"} or not parsed_fixture.hostname:
        parser.error("--fixture-base-url must be absolute HTTP(S)")
    args.cases = args.case or list(CASE_META)
    return args


def source_provenance() -> dict[str, Any]:
    return capture_source_provenance(
        pathlib.Path(__file__),
        extra_paths=(
            REPO_ROOT / "runner/run.py",
            REPO_ROOT / "tools/kitesurf_static_fixture.py",
            DEFAULT_FIXTURE_MANIFEST,
        ),
    )


def token_for(seed: str, engine: str, case_id: str, attempt: int) -> str:
    return hashlib.sha256(
        f"{seed}:{engine}:{case_id}:{attempt}".encode("utf-8")
    ).hexdigest()[:16]


def command(
    client: bench.CDPClient,
    method: str,
    params: dict[str, Any] | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    try:
        return client.command(method, params, session_id=session_id)
    except bench.CDPCommandError:
        raise
    except TimeoutError as exc:
        raise ProbeFailure(
            "transport", "command_response_timeout", f"{method}: {exc or type(exc).__name__}"
        ) from exc
    except (ConnectionError, OSError) as exc:
        raise ProbeFailure(
            "transport", "connection_lost", f"{method}: {type(exc).__name__}: {exc}"
        ) from exc


def event(
    client: bench.CDPClient,
    method: str,
    *,
    match: Any = None,
    session_id: str | None = None,
    timeout_s: float = 6.0,
) -> dict[str, Any]:
    try:
        return client.wait_for_event(
            method, match=match, session_id=session_id, timeout_s=timeout_s
        )
    except bench.CDPTransportTimeout as exc:
        raise ProbeFailure(
            "transport",
            "event_transport_timeout",
            f"waiting for {method}: {exc or type(exc).__name__}",
        ) from exc
    except TimeoutError as exc:
        raise ProbeFailure(
            "protocol", "required_event_timeout", f"{method} not observed in {timeout_s:.1f}s"
        ) from exc
    except (ConnectionError, OSError) as exc:
        raise ProbeFailure(
            "transport", "connection_lost", f"waiting for {method}: {type(exc).__name__}: {exc}"
        ) from exc


def _effective_url_origin(parsed: Any) -> tuple[str, str | None, int | None]:
    port = parsed.port
    if port is None:
        port = {"http": 80, "https": 443}.get(str(parsed.scheme).lower())
    return str(parsed.scheme).lower(), parsed.hostname, port


def fixture_path_for_url(base_url: str, response_url: str) -> str | None:
    base = bench.urllib.parse.urlparse(base_url.rstrip("/") + "/")
    observed = bench.urllib.parse.urlparse(response_url)

    if _effective_url_origin(observed) != _effective_url_origin(base):
        return None
    if not observed.path.startswith(base.path):
        return None
    relative = bench.urllib.parse.unquote(observed.path[len(base.path) :])
    if not relative or relative.endswith("/"):
        relative += "index.html"
    return relative


class RuntimeFixtureVerifier:
    """Hash fixture response bodies consumed on the task's CDP session."""

    def __init__(
        self,
        base_url: str,
        preflight_report: dict[str, Any] | None,
        *,
        required_paths: tuple[str, ...] = (),
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.required_paths = tuple(dict.fromkeys(required_paths))
        self.required = bool(self.required_paths)
        files = (preflight_report or {}).get("files") or []
        self.expected = {
            str(row["path"]): {
                "size": int(row["expected_size"]),
                "sha256": str(row["expected_sha256"]),
            }
            for row in files
            if isinstance(row, dict)
            and row.get("path")
            and row.get("expected_size") is not None
            and row.get("expected_sha256")
        }
        missing_manifest_paths = sorted(
            set(self.required_paths).difference(self.expected)
        )
        if missing_manifest_paths:
            raise ProbeFailure(
                "binding",
                "runtime_fixture_manifest_missing",
                "runtime fixture manifest is missing required paths: "
                + ", ".join(missing_manifest_paths),
            )
        self.enabled_sessions: set[str] = set()
        self.target_sessions: dict[str, str] = {}
        self.processed_request_ids: set[str] = set()
        self.records: list[dict[str, Any]] = []

    def prepare_navigation(
        self,
        client: bench.CDPClient,
        session_id: str,
        url: str,
    ) -> None:
        if not self.required:
            return
        if fixture_path_for_url(self.base_url, url) not in self.expected:
            return
        if session_id not in self.enabled_sessions:
            command(client, "Network.enable", session_id=session_id)
            self.enabled_sessions.add(session_id)

    def register_target(self, target_id: str, session_id: str) -> None:
        self.target_sessions[target_id] = session_id

    def _body_for_request(
        self,
        client: bench.CDPClient,
        session_id: str,
        request_id: str,
    ) -> dict[str, Any]:
        try:
            return command(
                client,
                "Network.getResponseBody",
                {"requestId": request_id},
                session_id,
            )
        except bench.CDPCommandError:
            # A response event can precede loadingFinished. Wait once on this
            # same session, then retry without reconnecting or refetching.
            client.wait_for_event(
                "Network.loadingFinished",
                match={"requestId": request_id},
                session_id=session_id,
                timeout_s=3.0,
            )
            return command(
                client,
                "Network.getResponseBody",
                {"requestId": request_id},
                session_id,
            )

    def collect_session(
        self,
        client: bench.CDPClient,
        session_id: str,
    ) -> None:
        if not self.required or session_id not in self.enabled_sessions:
            return
        pump = getattr(client, "pump_pending_events", None)
        if callable(pump):
            try:
                pump(timeout_s=0.1)
            except Exception:
                # Body retrieval below remains the authoritative gate and will
                # record an unavailable/mismatched response if needed.
                pass
        for envelope in list(getattr(client, "events", [])):
            if (
                envelope.get("method") != "Network.responseReceived"
                or envelope.get("sessionId") != session_id
            ):
                continue
            params = envelope.get("params") or {}
            response = params.get("response") or {}
            request_id = str(params.get("requestId") or "")
            response_url = str(response.get("url") or "")
            parsed = bench.urllib.parse.urlparse(response_url)
            # This case intentionally replaces data.json through
            # Fetch.fulfillRequest; it is harness-owned synthetic content.
            if "fulfill" in bench.urllib.parse.parse_qs(parsed.query):
                continue
            relative = fixture_path_for_url(self.base_url, response_url)
            expected = self.expected.get(relative or "")
            if (
                not request_id
                or relative is None
                or request_id in self.processed_request_ids
            ):
                continue
            self.processed_request_ids.add(request_id)
            if expected is None:
                self.records.append(
                    {
                        "path": relative,
                        "url": response_url,
                        "request_id": request_id,
                        "session_id": session_id,
                        "status": response.get("status"),
                        "verified": False,
                        "error": "browser response path is absent from pinned fixture manifest",
                    }
                )
                continue
            record: dict[str, Any] = {
                "path": relative,
                "url": response_url,
                "request_id": request_id,
                "session_id": session_id,
                "status": response.get("status"),
                "expected_size": expected["size"],
                "expected_sha256": expected["sha256"],
                "verified": False,
            }
            try:
                body_result = self._body_for_request(client, session_id, request_id)
                body_text = str(body_result.get("body") or "")
                body = (
                    base64.b64decode(body_text, validate=True)
                    if body_result.get("base64Encoded") is True
                    else body_text.encode("utf-8")
                )
                record.update(
                    {
                        "actual_size": len(body),
                        "actual_sha256": hashlib.sha256(body).hexdigest(),
                    }
                )
                record["verified"] = bool(
                    record["status"] == 200
                    and record["actual_size"] == record["expected_size"]
                    and record["actual_sha256"] == record["expected_sha256"]
                )
                if not record["verified"]:
                    record["error"] = (
                        "browser response does not match pinned fixture"
                    )
            except Exception as exc:
                record["error"] = f"{type(exc).__name__}: {exc}"
            self.records.append(record)

    def collect_target(
        self,
        client: bench.CDPClient,
        target_id: str,
    ) -> None:
        session_id = self.target_sessions.get(target_id)
        if session_id:
            self.collect_session(client, session_id)

    def finish(self, client: bench.CDPClient) -> dict[str, Any]:
        for session_id in sorted(set(self.target_sessions.values())):
            self.collect_session(client, session_id)
        observed_paths = {str(row["path"]) for row in self.records}
        required_paths = set(self.required_paths)
        missing_paths = sorted(required_paths.difference(observed_paths))
        unexpected_paths = sorted(observed_paths.difference(required_paths))
        verified = bool(
            not self.required
            or (
                not missing_paths
                and not unexpected_paths
                and self.records
                and all(row["verified"] for row in self.records)
            )
        )
        return {
            "schema": "experimental.issue136.runtime_fixture_verification.v2",
            "required": self.required,
            "verified": verified,
            "required_paths": list(self.required_paths),
            "missing_paths": missing_paths,
            "unexpected_paths": unexpected_paths,
            "response_count": len(self.records),
            "paths": sorted(observed_paths),
            "responses": self.records,
        }


def evaluate(
    client: bench.CDPClient,
    session_id: str,
    expression: str,
    *,
    return_by_value: bool = True,
    await_promise: bool = False,
    context_id: int | None = None,
) -> Any:
    params: dict[str, Any] = {
        "expression": expression,
        "returnByValue": return_by_value,
        "awaitPromise": await_promise,
    }
    if context_id is not None:
        params["contextId"] = context_id
    result = command(client, "Runtime.evaluate", params, session_id)
    if result.get("exceptionDetails"):
        raise ProbeFailure(
            "page_semantic",
            "javascript_exception",
            json.dumps(result["exceptionDetails"], sort_keys=True)[:1000],
        )
    remote = result.get("result") or {}
    return remote.get("value") if return_by_value else remote


def poll_value(
    client: bench.CDPClient,
    session_id: str,
    expression: str,
    predicate: Callable[[Any], bool],
    *,
    timeout_s: float = 8.0,
) -> Any:
    deadline = time.monotonic() + timeout_s
    last: Any = None
    while time.monotonic() < deadline:
        last = evaluate(client, session_id, expression)
        if predicate(last):
            return last
        time.sleep(0.1)
    raise ProbeFailure(
        "page_semantic",
        "state_timeout",
        f"predicate not satisfied; last={json.dumps(last, sort_keys=True)[:800]}",
    )


def new_page(client: bench.CDPClient) -> tuple[str, str]:
    try:
        created = command(client, "Target.createTarget", {"url": "about:blank"})
        if not isinstance(created, dict):
            raise ProbeFailure(
                "protocol",
                "malformed_create_response",
                f"expected object, got {type(created).__name__}",
            )
        target_id = str(created.get("targetId") or "")
        if not target_id:
            raise ProbeFailure(
                "protocol",
                "missing_target_id",
                json.dumps(created, sort_keys=True),
            )
    except bench.CDPCommandError:
        # A normal CDP error envelope proves createTarget was rejected. Any
        # transport, parse, shape, or wrapper failure can have lost a success
        # response after the target was created and is therefore ambiguous.
        raise
    except Exception:
        client._issue136_target_creation_ambiguous = True
        raise
    tracked = getattr(client, "_issue136_target_ids", None)
    if tracked is None:
        tracked = set()
        client._issue136_target_ids = tracked
    tracked.add(target_id)
    attached = command(
        client,
        "Target.attachToTarget",
        {"targetId": target_id, "flatten": True},
    )
    session_id = str(attached.get("sessionId") or "")
    if not session_id:
        raise ProbeFailure("protocol", "missing_session_id", json.dumps(attached, sort_keys=True))
    runtime_fixture = getattr(client, "_issue136_runtime_fixture", None)
    if isinstance(runtime_fixture, RuntimeFixtureVerifier):
        runtime_fixture.register_target(target_id, session_id)
    return target_id, session_id


def close_target(client: bench.CDPClient, target_id: str) -> dict[str, Any]:
    runtime_fixture = getattr(client, "_issue136_runtime_fixture", None)
    if isinstance(runtime_fixture, RuntimeFixtureVerifier):
        runtime_fixture.collect_target(client, target_id)
    result = command(client, "Target.closeTarget", {"targetId": target_id})
    if result.get("success") is not True:
        raise ProbeFailure(
            "protocol",
            "target_close_unconfirmed",
            f"target {target_id!r}: {json.dumps(result, sort_keys=True)}",
        )
    getattr(client, "_issue136_target_ids", set()).discard(target_id)
    return result


def cleanup_targets(client: bench.CDPClient) -> list[str]:
    """Close tracked targets and retain every target not confirmed closed."""

    errors: list[str] = []
    tracked = getattr(client, "_issue136_target_ids", set())
    for target_id in sorted(list(tracked)):
        try:
            result = client.command(
                "Target.closeTarget", {"targetId": target_id}
            )
            if result.get("success") is not True:
                errors.append(
                    f"{target_id}: Target.closeTarget returned "
                    + json.dumps(result, sort_keys=True)
                )
                continue
            tracked.discard(target_id)
        except Exception as exc:
            errors.append(f"{target_id}: {type(exc).__name__}: {exc}")
    return errors


def assert_recovery(
    client: bench.CDPClient,
    session_id: str,
    observations: dict[str, Any],
    expression: str = "6 * 7",
) -> None:
    value = evaluate(client, session_id, expression)
    observations["recovery_expression"] = expression
    observations["recovery_value"] = value
    if value != 42:
        raise ProbeFailure(
            "protocol",
            "same_session_recovery_failed",
            f"{expression} returned {value!r}, expected 42",
            observations,
        )


def navigate_ready(
    client: bench.CDPClient, session_id: str, url: str
) -> dict[str, Any]:
    command(client, "Page.enable", session_id=session_id)
    command(client, "Runtime.enable", session_id=session_id)
    runtime_fixture = getattr(client, "_issue136_runtime_fixture", None)
    if isinstance(runtime_fixture, RuntimeFixtureVerifier):
        runtime_fixture.prepare_navigation(client, session_id, url)
    navigation = command(client, "Page.navigate", {"url": url}, session_id)
    error_text = navigation.get("errorText")
    if error_text:
        raise ProbeFailure("page_semantic", "navigation_error", str(error_text))
    poll_value(
        client,
        session_id,
        "({ready:document.readyState,href:location.href})",
        lambda value: isinstance(value, dict)
        and value.get("ready") in {"interactive", "complete"}
        and str(value.get("href") or "").startswith(url.split("?", 1)[0]),
    )
    return navigation


def case_target_session_isolation(
    client: bench.CDPClient, fixture: str, token: str
) -> dict[str, Any]:
    del fixture
    command(client, "Target.setDiscoverTargets", {"discover": True})
    target_a, session_a = new_page(client)
    target_b, session_b = new_page(client)
    observations: dict[str, Any] = {
        "target_ids": [target_a, target_b],
        "session_ids": [session_a, session_b],
    }
    if target_a == target_b or session_a == session_b:
        raise ProbeFailure("protocol", "identity_alias", "two targets/sessions were not distinct", observations)
    command(client, "Runtime.enable", session_id=session_a)
    command(client, "Runtime.enable", session_id=session_b)
    value_a = f"A-{token}"
    value_b = f"B-{token}"
    evaluate(client, session_a, f"globalThis.__orthogonalMarker={json.dumps(value_a)}")
    evaluate(client, session_b, f"globalThis.__orthogonalMarker={json.dumps(value_b)}")
    reads = [
        evaluate(client, session_a, "globalThis.__orthogonalMarker"),
        evaluate(client, session_b, "globalThis.__orthogonalMarker"),
    ]
    observations["isolated_reads"] = reads
    if reads != [value_a, value_b]:
        raise ProbeFailure("protocol", "session_state_cross_talk", f"reads={reads!r}", observations)

    close_result = close_target(client, target_a)
    observations["close_result"] = close_result
    destroyed = event(
        client,
        "Target.targetDestroyed",
        match={"targetId": target_a},
        timeout_s=4.0,
    )
    observations["destroyed_event"] = destroyed.get("params")
    surviving = evaluate(client, session_b, "globalThis.__orthogonalMarker")
    observations["surviving_read"] = surviving
    if surviving != value_b:
        raise ProbeFailure("protocol", "surviving_session_corrupted", repr(surviving), observations)
    stale_error: str | None = None
    stale_result: dict[str, Any] | None = None
    try:
        stale_result = command(
            client,
            "Runtime.evaluate",
            {"expression": "1", "returnByValue": True},
            session_a,
        )
    except bench.CDPCommandError as exc:
        stale_error = str(exc)
    observations["closed_session_error"] = stale_error
    observations["closed_session_result"] = stale_result
    if stale_error is None:
        raise ProbeFailure(
            "protocol", "closed_session_remained_usable", "Runtime.evaluate unexpectedly succeeded", observations
        )
    close_target(client, target_b)
    return observations


def case_remote_object_lifecycle(
    client: bench.CDPClient, fixture: str, token: str
) -> dict[str, Any]:
    del fixture
    target_id, session_id = new_page(client)
    command(client, "Runtime.enable", session_id=session_id)
    remote = evaluate(
        client,
        session_id,
        f"({{marker:{json.dumps(token)}, nested:{{value:42}}}})",
        return_by_value=False,
    )
    object_id = str(remote.get("objectId") or "")
    if not object_id:
        raise ProbeFailure("protocol", "missing_object_id", json.dumps(remote, sort_keys=True))
    before = command(
        client,
        "Runtime.getProperties",
        {"objectId": object_id, "ownProperties": True},
        session_id,
    )
    marker_values = [
        (item.get("value") or {}).get("value")
        for item in before.get("result") or []
        if item.get("name") == "marker"
    ]
    observations: dict[str, Any] = {
        "target_id": target_id,
        "session_id": session_id,
        "object_id": object_id,
        "marker_values": marker_values,
    }
    if marker_values != [token]:
        raise ProbeFailure("protocol", "property_read_mismatch", repr(marker_values), observations)
    command(client, "Runtime.releaseObject", {"objectId": object_id}, session_id)
    released_error: str | None = None
    stale_result: dict[str, Any] | None = None
    try:
        stale_result = command(
            client,
            "Runtime.getProperties",
            {"objectId": object_id, "ownProperties": True},
            session_id,
        )
    except bench.CDPCommandError as exc:
        released_error = str(exc)
    observations["released_handle_error"] = released_error
    observations["released_handle_result"] = stale_result
    assert_recovery(client, session_id, observations, "21 * 2")
    if released_error is None:
        raise ProbeFailure(
            "protocol", "released_handle_remained_usable", json.dumps(stale_result, sort_keys=True), observations
        )
    close_target(client, target_id)
    return observations


def case_fetch_interception_lifecycle(
    client: bench.CDPClient, fixture: str, token: str
) -> dict[str, Any]:
    target_id, session_id = new_page(client)
    navigate_ready(client, session_id, f"{fixture}/v1/network/")
    command(client, "Network.enable", session_id=session_id)
    command(
        client,
        "Fetch.enable",
        {"patterns": [{"urlPattern": "*data.json*", "requestStage": "Request"}]},
        session_id,
    )
    expression = f"""
      (() => {{
        globalThis.__orthogonalFetch = {{stage:'started'}};
        fetch('./data.json?orthogonal={token}')
          .then(async response => {{
            const data = await response.json();
            globalThis.__orthogonalFetch = {{stage:'done', status:response.status, value:data.value}};
          }})
          .catch(error => {{
            globalThis.__orthogonalFetch = {{stage:'error', error:String(error)}};
          }});
        return globalThis.__orthogonalFetch;
      }})()
    """
    evaluate(client, session_id, expression)
    paused = event(client, "Fetch.requestPaused", session_id=session_id, timeout_s=6.0)
    params = paused.get("params")
    if not isinstance(params, dict):
        raise ProbeFailure("protocol", "event_params_missing", "Fetch.requestPaused has no params")
    request_id = str(params.get("requestId") or "")
    network_id = str(params.get("networkId") or "")
    request_url = str((params.get("request") or {}).get("url") or "")
    observations: dict[str, Any] = {
        "target_id": target_id,
        "session_id": session_id,
        "request_id": request_id,
        "network_id": network_id,
        "request_url": request_url,
    }
    if not request_id or f"orthogonal={token}" not in request_url:
        raise ProbeFailure("protocol", "paused_request_shape_mismatch", json.dumps(params, sort_keys=True), observations)
    try:
        command(client, "Fetch.continueRequest", {"requestId": request_id}, session_id)
    except bench.CDPCommandError as exc:
        observations["continue_error"] = str(exc)
        assert_recovery(client, session_id, observations)
        raise ProbeFailure(
            "protocol", "interception_id_rejected", str(exc), observations
        ) from exc
    state = poll_value(
        client,
        session_id,
        "globalThis.__orthogonalFetch",
        lambda value: isinstance(value, dict) and value.get("stage") in {"done", "error"},
        timeout_s=8.0,
    )
    observations["page_state"] = state
    if state != {"stage": "done", "status": 200, "value": 73}:
        raise ProbeFailure("page_semantic", "fetch_state_mismatch", json.dumps(state, sort_keys=True), observations)
    if network_id:
        finished = event(
            client,
            "Network.loadingFinished",
            match={"requestId": network_id},
            session_id=session_id,
            timeout_s=6.0,
        )
        observations["loading_finished"] = finished.get("params")
    else:
        raise ProbeFailure(
            "protocol", "missing_network_id", "Fetch.requestPaused omitted networkId", observations
        )
    assert_recovery(client, session_id, observations)
    command(client, "Fetch.disable", session_id=session_id)
    close_target(client, target_id)
    return observations


def case_fetch_fulfill_lifecycle(
    client: bench.CDPClient, fixture: str, token: str
) -> dict[str, Any]:
    target_id, session_id = new_page(client)
    navigate_ready(client, session_id, f"{fixture}/v1/network/")
    command(client, "Network.enable", session_id=session_id)
    command(
        client,
        "Fetch.enable",
        {"patterns": [{"urlPattern": "*data.json*", "requestStage": "Request"}]},
        session_id,
    )
    expression = f"""
      (() => {{
        globalThis.__orthogonalFulfill = {{stage:'started'}};
        fetch('./data.json?fulfill={token}')
          .then(async response => {{
            const data = await response.json();
            globalThis.__orthogonalFulfill = {{
              stage:'done',
              status:response.status,
              value:data.value,
              marker:data.marker,
              header:response.headers.get('x-abb-probe')
            }};
          }})
          .catch(error => {{
            globalThis.__orthogonalFulfill = {{stage:'error', error:String(error)}};
          }});
        return globalThis.__orthogonalFulfill;
      }})()
    """
    evaluate(client, session_id, expression)
    paused = event(client, "Fetch.requestPaused", session_id=session_id, timeout_s=6.0)
    params = paused.get("params")
    if not isinstance(params, dict):
        raise ProbeFailure("protocol", "event_params_missing", "Fetch.requestPaused has no params")
    request_id = str(params.get("requestId") or "")
    request_url = str((params.get("request") or {}).get("url") or "")
    observations: dict[str, Any] = {
        "target_id": target_id,
        "session_id": session_id,
        "request_id": request_id,
        "request_url": request_url,
    }
    if not request_id or f"fulfill={token}" not in request_url:
        raise ProbeFailure(
            "protocol", "paused_request_shape_mismatch", json.dumps(params, sort_keys=True), observations
        )
    payload = json.dumps(
        {"fixture": "synthetic", "value": 909, "marker": token},
        separators=(",", ":"),
    ).encode("utf-8")
    try:
        command(
            client,
            "Fetch.fulfillRequest",
            {
                "requestId": request_id,
                "responseCode": 201,
                "responsePhrase": "Created",
                "responseHeaders": [
                    {"name": "content-type", "value": "application/json"},
                    {"name": "x-abb-probe", "value": token},
                    {"name": "cache-control", "value": "no-store"},
                ],
                "body": base64.b64encode(payload).decode("ascii"),
            },
            session_id,
        )
    except bench.CDPCommandError as exc:
        observations["fulfill_error"] = str(exc)
        assert_recovery(client, session_id, observations)
        raise ProbeFailure(
            "protocol", "interception_id_rejected", str(exc), observations
        ) from exc
    state = poll_value(
        client,
        session_id,
        "globalThis.__orthogonalFulfill",
        lambda value: isinstance(value, dict) and value.get("stage") in {"done", "error"},
        timeout_s=8.0,
    )
    observations["page_state"] = state
    expected = {
        "stage": "done",
        "status": 201,
        "value": 909,
        "marker": token,
        "header": token,
    }
    if state != expected:
        raise ProbeFailure(
            "page_semantic", "synthetic_response_mismatch", json.dumps(state, sort_keys=True), observations
        )
    assert_recovery(client, session_id, observations)
    command(client, "Fetch.disable", session_id=session_id)
    close_target(client, target_id)
    return observations


def case_fetch_promise_control_flow(
    client: bench.CDPClient, fixture: str, token: str
) -> dict[str, Any]:
    target_id, session_id = new_page(client)
    navigate_ready(client, session_id, f"{fixture}/v1/network/")
    command(client, "Network.enable", session_id=session_id)
    command(
        client,
        "Fetch.enable",
        {"patterns": [{"urlPattern": "*data.json*", "requestStage": "Request"}]},
        session_id,
    )
    started = time.perf_counter()
    initial = command(
        client,
        "Runtime.evaluate",
        {
            "expression": f"fetch('./data.json?promise={token}').then(response => response.json())",
            "returnByValue": False,
            "awaitPromise": False,
        },
        session_id,
    )
    evaluate_duration_ms = int((time.perf_counter() - started) * 1000)
    remote = initial.get("result") or {}
    object_id = str(remote.get("objectId") or "")
    is_promise = remote.get("subtype") == "promise" and bool(object_id)
    paused = event(client, "Fetch.requestPaused", session_id=session_id, timeout_s=6.0)
    params = paused.get("params")
    if not isinstance(params, dict):
        raise ProbeFailure("protocol", "event_params_missing", "Fetch.requestPaused has no params")
    request_id = str(params.get("requestId") or "")
    request_url = str((params.get("request") or {}).get("url") or "")
    observations: dict[str, Any] = {
        "target_id": target_id,
        "session_id": session_id,
        "runtime_evaluate_duration_ms": evaluate_duration_ms,
        "runtime_result": remote,
        "request_id": request_id,
        "request_url": request_url,
    }
    if not request_id or f"promise={token}" not in request_url:
        raise ProbeFailure(
            "protocol", "paused_request_shape_mismatch", json.dumps(params, sort_keys=True), observations
        )
    continue_error: str | None = None
    try:
        command(client, "Fetch.continueRequest", {"requestId": request_id}, session_id)
    except bench.CDPCommandError as exc:
        continue_error = str(exc)
    observations["continue_error"] = continue_error
    assert_recovery(client, session_id, observations)
    if not is_promise:
        raise ProbeFailure(
            "protocol",
            "non_awaited_promise_not_exposed",
            f"Runtime.evaluate returned {json.dumps(remote, sort_keys=True)}",
            observations,
        )
    if continue_error is not None:
        raise ProbeFailure(
            "protocol",
            "interception_id_rejected_after_promise_evaluate",
            continue_error,
            observations,
        )
    awaited = command(
        client,
        "Runtime.awaitPromise",
        {"promiseObjectId": object_id, "returnByValue": True},
        session_id,
    )
    if awaited.get("exceptionDetails"):
        raise ProbeFailure(
            "page_semantic", "promise_rejected", json.dumps(awaited["exceptionDetails"], sort_keys=True), observations
        )
    value = (awaited.get("result") or {}).get("value")
    observations["awaited_value"] = value
    if value != {"fixture": "network-v1", "value": 73, "items": ["alpha", "beta", "gamma"]}:
        raise ProbeFailure(
            "page_semantic", "awaited_response_mismatch", json.dumps(value, sort_keys=True), observations
        )
    command(client, "Runtime.releaseObject", {"objectId": object_id}, session_id)
    command(client, "Fetch.disable", session_id=session_id)
    close_target(client, target_id)
    return observations


def flatten_frame_tree(node: dict[str, Any]) -> list[dict[str, Any]]:
    values = [node.get("frame") or {}]
    for child in node.get("childFrames") or []:
        values.extend(flatten_frame_tree(child))
    return values


def case_nested_frame_context_routing(
    client: bench.CDPClient, fixture: str, token: str
) -> dict[str, Any]:
    target_id, session_id = new_page(client)
    navigate_ready(client, session_id, f"{fixture}/v1/frames/")
    deadline = time.monotonic() + 8.0
    frames: list[dict[str, Any]] = []
    expected_fixtures_by_url = {
        f"{fixture}/v1/frames/": "frames-v1",
        f"{fixture}/v1/frames/child.html": "frame-child-v1",
        f"{fixture}/v1/frames/grandchild.html": "frame-grandchild-v1",
    }
    expected_frame_urls = set(expected_fixtures_by_url)
    while time.monotonic() < deadline:
        tree = command(client, "Page.getFrameTree", session_id=session_id)
        root = tree.get("frameTree")
        frames = flatten_frame_tree(root) if isinstance(root, dict) else []
        if (
            len(frames) == 3
            and {str(frame.get("url") or "") for frame in frames}
            == expected_frame_urls
        ):
            break
        time.sleep(0.1)
    observations: dict[str, Any] = {
        "target_id": target_id,
        "session_id": session_id,
        "frames": frames,
        "expected_frame_urls": sorted(expected_frame_urls),
    }
    if len(frames) != 3 or len({frame.get("id") for frame in frames}) != 3:
        raise ProbeFailure(
            "protocol", "frame_tree_incomplete", f"expected 3 distinct frames, got {len(frames)}", observations
        )
    actual_frame_urls = {str(frame.get("url") or "") for frame in frames}
    if actual_frame_urls != expected_frame_urls:
        raise ProbeFailure(
            "binding",
            "fixture_frames_not_ready",
            f"expected URLs {sorted(expected_frame_urls)!r}, got {sorted(actual_frame_urls)!r}",
            observations,
        )

    contexts: list[int] = []
    scoped_values: list[Any] = []
    scoped_by_frame: list[dict[str, Any]] = []
    envelopes: list[dict[str, Any]] = []
    observations["execution_context_ids"] = contexts
    observations["event_envelopes"] = envelopes
    observations["scoped_values"] = scoped_values
    observations["scoped_by_frame"] = scoped_by_frame
    for index, frame in enumerate(frames):
        frame_id = str(frame.get("id") or "")
        frame_url = str(frame.get("url") or "")
        world = command(
            client,
            "Page.createIsolatedWorld",
            {
                "frameId": frame_id,
                "worldName": f"abb-orthogonal-{token}-{index}",
                "grantUniveralAccess": False,
            },
            session_id,
        )
        context_id = world.get("executionContextId")
        if not isinstance(context_id, int):
            raise ProbeFailure("protocol", "missing_execution_context_id", json.dumps(world), observations)
        contexts.append(context_id)
        created = event(
            client,
            "Runtime.executionContextCreated",
            match={"context": {"id": context_id}},
            session_id=session_id,
            timeout_s=4.0,
        )
        params = created.get("params")
        if not isinstance(params, dict):
            raise ProbeFailure(
                "protocol", "event_params_missing", "Runtime.executionContextCreated has no params", observations
            )
        context = params.get("context") or {}
        aux_data = context.get("auxData") or {}
        envelopes.append(
            {
                "wire_keys": sorted(created),
                "context_id": context.get("id"),
                "frame_id": aux_data.get("frameId"),
                "frame_url": frame_url,
            }
        )
        value = evaluate(
            client,
            session_id,
            "({fixture:document.body.dataset.fixture, href:location.href, text:(document.querySelector('h1,h2,p')?.textContent||'').trim()})",
            context_id=context_id,
        )
        scoped_values.append(value)
        scoped_by_frame.append(
            {"frame_id": frame_id, "frame_url": frame_url, "value": value}
        )
        if aux_data.get("frameId") != frame_id:
            raise ProbeFailure(
                "protocol", "execution_context_frame_mismatch", json.dumps(envelopes[-1]), observations
            )
        expected_fixture = expected_fixtures_by_url[frame_url]
        if (
            not isinstance(value, dict)
            or value.get("href") != frame_url
            or value.get("fixture") != expected_fixture
        ):
            raise ProbeFailure(
                "protocol",
                "frame_scoped_state_mismatch",
                (
                    f"frame {frame_id!r} at {frame_url!r} expected fixture "
                    f"{expected_fixture!r}, got {value!r}"
                ),
                observations,
            )
    if len(set(contexts)) != 3:
        raise ProbeFailure("protocol", "execution_context_alias", repr(contexts), observations)
    assert_recovery(client, session_id, observations, "40 + 2")
    close_target(client, target_id)
    return observations


CASES: dict[str, Callable[[bench.CDPClient, str, str], dict[str, Any]]] = {
    "target_session_isolation": case_target_session_isolation,
    "remote_object_lifecycle": case_remote_object_lifecycle,
    "fetch_interception_lifecycle": case_fetch_interception_lifecycle,
    "fetch_fulfill_lifecycle": case_fetch_fulfill_lifecycle,
    "fetch_promise_control_flow": case_fetch_promise_control_flow,
    "nested_frame_context_routing": case_nested_frame_context_routing,
}


def run_attempt(
    *,
    engine: str,
    endpoint: str,
    expected_identity: dict[str, str],
    case_id: str,
    attempt: int,
    fixture: str,
    seed: str,
    timeout_s: float,
    artifact_dir: pathlib.Path,
    fixture_verification: dict[str, Any] | None = None,
) -> dict[str, Any]:
    artifact_dir.mkdir(parents=True, exist_ok=False)
    started = time.perf_counter()
    token = token_for(seed, engine, case_id, attempt)
    status = "infra"
    failure: dict[str, Any] | None = None
    observations: dict[str, Any] = {}
    identity: dict[str, Any] | None = None
    trace_path = artifact_dir / "cdp.jsonl"
    client = bench.CDPClient(endpoint, trace_path, timeout_s=timeout_s)
    runtime_fixture: RuntimeFixtureVerifier | None = None
    try:
        runtime_fixture = RuntimeFixtureVerifier(
            fixture,
            fixture_verification,
            required_paths=RUNTIME_FIXTURE_PATHS.get(case_id, ()),
        )
        client._issue136_runtime_fixture = runtime_fixture
        try:
            client.connect()
        except Exception as exc:
            raise ProbeFailure(
                "transport", "connect_failed", f"{type(exc).__name__}: {exc}"
            ) from exc
        identity = command(client, "Browser.getVersion")
        try:
            expected = bench.require_remote_cdp_identity(
                expected_identity,
                label=f"expected identity for {engine}",
            )
            actual = bench.require_remote_cdp_identity(
                identity,
                label=f"observed identity for {engine}",
            )
        except bench.BenchError as exc:
            raise ProbeFailure("binding", "identity_missing", str(exc)) from exc
        mismatches = {
            field: {"expected": expected[field], "actual": actual[field]}
            for field in bench.REMOTE_CDP_IDENTITY_FIELDS
            if actual[field] != expected[field]
        }
        if mismatches:
            raise ProbeFailure(
                "binding",
                "identity_mismatch",
                json.dumps(mismatches, sort_keys=True),
            )
        observations = CASES[case_id](client, fixture, token)
        status = "pass"
    except ProbeFailure as exc:
        status = "fail" if exc.layer in {"protocol", "page_semantic"} else "infra"
        failure = {"layer": exc.layer, "code": exc.code, "detail": exc.detail}
        if exc.observations:
            observations.update(exc.observations)
    except bench.CDPCommandError as exc:
        status = "fail"
        failure = {
            "layer": "protocol",
            "code": "unexpected_cdp_error",
            "detail": str(exc),
            "error": exc.error,
        }
    except Exception as exc:
        status = "infra"
        failure = {
            "layer": "harness",
            "code": "unhandled_exception",
            "detail": f"{type(exc).__name__}: {exc}",
        }
    finally:
        runtime_fixture_result: dict[str, Any]
        if runtime_fixture is None:
            runtime_fixture_result = {
                "schema": "experimental.issue136.runtime_fixture_verification.v2",
                "required": case_id in RUNTIME_FIXTURE_CASES,
                "verified": False,
                "required_paths": list(RUNTIME_FIXTURE_PATHS.get(case_id, ())),
                "missing_paths": list(RUNTIME_FIXTURE_PATHS.get(case_id, ())),
                "unexpected_paths": [],
                "response_count": 0,
                "paths": [],
                "responses": [],
                "error": "runtime fixture verifier was not initialized",
            }
        else:
            try:
                runtime_fixture_result = runtime_fixture.finish(client)
            except Exception as exc:
                observed_paths = {
                    str(row["path"]) for row in runtime_fixture.records
                }
                required_paths = set(runtime_fixture.required_paths)
                runtime_fixture_result = {
                    "schema": "experimental.issue136.runtime_fixture_verification.v2",
                    "required": runtime_fixture.required,
                    "verified": False,
                    "required_paths": list(runtime_fixture.required_paths),
                    "missing_paths": sorted(
                        required_paths.difference(observed_paths)
                    ),
                    "unexpected_paths": sorted(
                        observed_paths.difference(required_paths)
                    ),
                    "response_count": len(runtime_fixture.records),
                    "paths": sorted(observed_paths),
                    "responses": runtime_fixture.records,
                    "error": f"{type(exc).__name__}: {exc}",
                }
        observations["runtime_fixture_verification"] = runtime_fixture_result
        if (
            runtime_fixture_result.get("required") is True
            and runtime_fixture_result.get("verified") is not True
        ):
            primary_failure = failure
            status = "infra"
            failure = {
                "layer": "binding",
                "code": "runtime_fixture_unverified",
                "detail": (
                    "browser-consumed fixture response did not match the "
                    "pinned manifest"
                ),
            }
            if primary_failure is not None:
                failure["primary_failure"] = primary_failure
        try:
            cleanup_errors = cleanup_targets(client)
        except Exception as exc:
            cleanup_errors = [
                f"cleanup harness failure: {type(exc).__name__}: {exc}"
            ]
        remaining_targets = sorted(
            getattr(client, "_issue136_target_ids", set())
        )
        target_creation_ambiguous = bool(
            getattr(client, "_issue136_target_creation_ambiguous", False)
        )
        cleanup_confirmed = bool(
            not cleanup_errors
            and not remaining_targets
            and not target_creation_ambiguous
        )
        observations["cleanup_confirmed"] = cleanup_confirmed
        if cleanup_errors:
            observations["cleanup_errors"] = cleanup_errors
        if remaining_targets:
            observations["remaining_target_ids"] = remaining_targets
        if target_creation_ambiguous:
            observations["target_creation_ambiguous"] = True
        if not cleanup_confirmed:
            primary_failure = failure
            status = "infra"
            failure = {
                "layer": "harness",
                "code": "target_cleanup_unconfirmed",
                "detail": "; ".join(cleanup_errors)
                or (
                    "Target.createTarget outcome was ambiguous and returned no "
                    "targetId"
                    if target_creation_ambiguous
                    else "tracked targets remained after cleanup"
                ),
                "remaining_target_ids": remaining_targets,
            }
            if primary_failure is not None:
                failure["primary_failure"] = primary_failure
        client.close()
    return {
        "engine": engine,
        "endpoint": endpoint,
        "case_id": case_id,
        "case_meta": CASE_META[case_id],
        "attempt": attempt,
        "status": status,
        "failure": failure,
        "identity": identity,
        "expected_identity": expected_identity,
        "token": token,
        "duration_ms": int((time.perf_counter() - started) * 1000),
        "connection_policy": "one fresh browser-level connection; identity and case on same connection; zero reconnects; zero fallbacks",
        "observations": observations,
        "metrics": {
            "cdp_call_count": client.call_count,
            "cdp_error_count": client.error_count,
            "ws_disconnect_count": client.disconnect_count,
        },
        "trace": str(trace_path.relative_to(artifact_dir.parents[3])),
    }


def build_summary(
    args: argparse.Namespace, rows: list[dict[str, Any]], provenance: dict[str, Any]
) -> dict[str, Any]:
    runtime_fixture_rows = [
        (row.get("observations") or {}).get("runtime_fixture_verification")
        for row in rows
        if (
            (
                (row.get("observations") or {}).get(
                    "runtime_fixture_verification"
                )
                or {}
            ).get("required")
            is True
        )
    ]
    runtime_fixture_required = any(
        case_id in RUNTIME_FIXTURE_CASES for case_id in args.cases
    )
    runtime_fixture_verified = bool(
        not runtime_fixture_required
        or (
            runtime_fixture_rows
            and all(item.get("verified") is True for item in runtime_fixture_rows)
        )
    )
    by_engine: dict[str, Any] = {}
    for engine in args.engines:
        engine_rows = [row for row in rows if row["engine"] == engine]
        by_case = {}
        for case_id in args.cases:
            case_rows = [row for row in engine_rows if row["case_id"] == case_id]
            by_case[case_id] = dict(
                sorted(collections.Counter(row["status"] for row in case_rows).items())
            )
        by_engine[engine] = {
            "status_counts": dict(
                sorted(collections.Counter(row["status"] for row in engine_rows).items())
            ),
            "by_case": by_case,
            "products": sorted(
                {
                    str((row.get("identity") or {}).get("product"))
                    for row in engine_rows
                    if (row.get("identity") or {}).get("product")
                }
            ),
            "protocol_versions": sorted(
                {
                    str((row.get("identity") or {}).get("protocolVersion"))
                    for row in engine_rows
                    if (row.get("identity") or {}).get("protocolVersion")
                }
            ),
            "revisions": sorted(
                {
                    str((row.get("identity") or {}).get("revision"))
                    for row in engine_rows
                    if (row.get("identity") or {}).get("revision")
                }
            ),
        }
    return {
        "schema": "experimental.issue136.wpt_orthogonal_probe.v2",
        "created_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "formal_score_eligible": False,
        "scope": {
            "engines": list(args.engines),
            "cases": args.cases,
            "attempts_per_case": args.attempts,
            "total_attempts": len(rows),
            "concurrency": 1,
            "seed": args.seed,
            "fixture_base_url": args.fixture_base_url,
            "fixture_verification": {
                "verified": bool(
                    args.fixture_verification["verified"]
                    and runtime_fixture_verified
                ),
                "preflight_verified": args.fixture_verification["verified"],
                "runtime_required": runtime_fixture_required,
                "runtime_verified": runtime_fixture_verified,
                "manifest_sha256": args.fixture_verification[
                    "manifest_sha256"
                ],
                "source": args.fixture_verification["source"],
                "file_count": args.fixture_verification["file_count"],
                "runtime_attempts": len(runtime_fixture_rows),
                "runtime_response_count": sum(
                    int(item.get("response_count") or 0)
                    for item in runtime_fixture_rows
                ),
                "runtime_paths": sorted(
                    {
                        str(path)
                        for item in runtime_fixture_rows
                        for path in item.get("paths") or []
                    }
                ),
            },
            "explicit_exclusions": [
                "screenshots",
                "PDF generation",
                "pixel/rendering comparison",
                "broad CDP method enumeration",
                "performance comparison across local and public-WSS transports",
            ],
        },
        "source": provenance,
        "by_engine": by_engine,
        "failure_layers": dict(
            sorted(
                collections.Counter(
                    (row.get("failure") or {}).get("layer")
                    for row in rows
                    if row.get("failure")
                ).items()
            )
        ),
    }


def main() -> int:
    args = parse_args()
    output_dir = args.output.resolve()
    if output_dir.exists():
        raise bench.BenchError(f"output already exists: {output_dir}")
    output_dir.mkdir(parents=True)
    provenance = source_provenance()
    write_json(output_dir / "provenance.json", provenance)
    try:
        args.fixture_verification = verify_fixture(
            args.fixture_base_url,
            args.fixture_manifest,
            report_path=output_dir / "fixture_preflight_verification.json",
        )
    except FixtureVerificationError as exc:
        raise bench.BenchError(str(exc)) from exc
    results_path = output_dir / "results.jsonl"
    rows: list[dict[str, Any]] = []
    total = len(args.engines) * len(args.cases) * args.attempts
    sequence = 0
    isolation_abort: str | None = None
    for engine, endpoint in args.engines.items():
        for case_id in args.cases:
            for attempt in range(1, args.attempts + 1):
                sequence += 1
                row = run_attempt(
                    engine=engine,
                    endpoint=endpoint,
                    expected_identity=args.expected_identities[engine],
                    case_id=case_id,
                    attempt=attempt,
                    fixture=args.fixture_base_url,
                    seed=args.seed,
                    timeout_s=args.timeout_s,
                    fixture_verification=args.fixture_verification,
                    artifact_dir=output_dir
                    / "artifacts"
                    / engine
                    / case_id
                    / str(attempt),
                )
                row["sequence"] = sequence
                rows.append(row)
                with results_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(row, sort_keys=True) + "\n")
                detail = (row.get("failure") or {}).get("code") or ""
                print(
                    f"[{sequence:02d}/{total:02d}] {row['status']:<5} "
                    f"{row['duration_ms']:>6} ms {engine:<9} {case_id:<31} {detail}",
                    flush=True,
                )
                if row["observations"].get("cleanup_confirmed") is False:
                    isolation_abort = (
                        f"target cleanup unconfirmed after {engine}/"
                        f"{case_id}/attempt-{attempt}; subsequent attempts aborted"
                    )
                    print(
                        f"abort: {isolation_abort}",
                        file=sys.stderr,
                        flush=True,
                    )
                    break
                runtime_fixture = row["observations"].get(
                    "runtime_fixture_verification"
                ) or {}
                if (
                    runtime_fixture.get("required") is True
                    and runtime_fixture.get("verified") is not True
                ):
                    isolation_abort = (
                        f"runtime fixture unverified after {engine}/"
                        f"{case_id}/attempt-{attempt}; subsequent attempts aborted"
                    )
                    print(
                        f"abort: {isolation_abort}",
                        file=sys.stderr,
                        flush=True,
                    )
                    break
            if isolation_abort is not None:
                break
        if isolation_abort is not None:
            break
    summary = build_summary(args, rows, provenance)
    summary["scope"]["requested_total_attempts"] = total
    summary["scope"]["completed_attempts"] = len(rows)
    summary["aborted"] = isolation_abort is not None
    summary["abort_reason"] = isolation_abort
    fixture_scope = summary["scope"]["fixture_verification"]
    combined_fixture_verification = {
        "schema": "experimental.issue136.fixture_verification.v2",
        "verified": fixture_scope["verified"],
        "manifest_sha256": fixture_scope["manifest_sha256"],
        "source": fixture_scope["source"],
        "file_count": fixture_scope["file_count"],
        "preflight": {
            "verified": fixture_scope["preflight_verified"],
            "report": "fixture_preflight_verification.json",
        },
        "runtime": {
            "required": fixture_scope["runtime_required"],
            "verified": fixture_scope["runtime_verified"],
            "attempts": fixture_scope["runtime_attempts"],
            "response_count": fixture_scope["runtime_response_count"],
            "paths": fixture_scope["runtime_paths"],
            "evidence": "results.jsonl observations.runtime_fixture_verification",
        },
    }
    write_json(
        output_dir / "fixture_verification.json",
        combined_fixture_verification,
    )
    fixture_scope["report"] = "fixture_verification.json"
    write_json(output_dir / "summary.json", summary)
    print(f"output={output_dir}")
    return (
        0
        if isolation_abort is None
        and all(row["status"] != "infra" for row in rows)
        else 2
    )


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except bench.BenchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2)
