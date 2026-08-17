"""Declarative Browser × Driver binding catalog.

This module deliberately has no dependency on :mod:`runner.run` at import
time.  The loader is safe for future runtime consumers, while repository
registry/pin parity is checked lazily by the ``check`` and ``render`` CLI
commands.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping, Sequence


BENCH_ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_CATALOG_PATH = BENCH_ROOT / "config" / "browser_driver_bindings.json"
DEFAULT_SCHEMA_PATH = BENCH_ROOT / "config" / "browser_driver_bindings.schema.json"
DEFAULT_RENDER_PATH = BENCH_ROOT / "generated" / "browser-driver-bindings.md"
HARNESS_PINS_PATH = BENCH_ROOT / "harness_pins.json"

BROWSER_IDS = ("chrome", "moli", "lightpanda", "obscura")
DRIVER_IDS = (
    "playwright",
    "puppeteer",
    "chrome_remote_interface",
    "cdp_use",
    "pydoll",
    "stagehand",
    "chrome_devtools_mcp",
    "chromedp",
    "rod",
    "agent_browser",
    "ferrum",
    "selenium",
    "chromiumoxide",
)
FORBIDDEN_RESULT_FIELDS = frozenset(
    {"supported", "known_pass", "known_fail", "expected_status", "skip_on_failure"}
)

ROOT_FIELDS = frozenset({"schema_version", "browser_ids", "driver_ids", "references", "routes", "bindings"})
REFERENCE_FIELDS = frozenset({"ref_id", "source", "key"})
ROUTE_FIELDS = frozenset(
    {
        "route_id",
        "client_protocol",
        "client_endpoint_kind",
        "browser_endpoint_kind",
        "connect_mode",
        "provider",
        "ordered_hops",
        "lifecycle",
        "discovery",
        "identity",
    }
)
HOP_FIELDS = frozenset({"from", "to", "protocol", "transport", "endpoint_kind"})
LIFECYCLE_FIELDS = frozenset({"browser_owner", "bridge_owner", "adapter_owner"})
DISCOVERY_FIELDS = frozenset({"browser", "client"})
DISCOVERY_POINT_FIELDS = frozenset({"kind", "endpoint_kind", "probe", "readiness_owner"})
IDENTITY_FIELDS = frozenset({"http_assertions", "live_transport_assertions"})
ASSERTION_FIELDS = frozenset(
    {
        "mechanism",
        "actual_path",
        "operator",
        "expected_ref",
        "expected_literal",
        "condition",
        "fallback_actual_path",
        "fallback_operator",
    }
)
BINDING_FIELDS = frozenset(
    {
        "binding_id",
        "browser_id",
        "driver_id",
        "route_ref",
        "unavailable_reason",
        "browser_pin_ref",
        "driver_pin_ref",
        "bridge_pin_refs",
        "fallback_allowed",
    }
)


class CatalogError(ValueError):
    """A readable catalog validation/query failure."""


def _json_equal(left: Any, right: Any) -> bool:
    """Compare JSON values without Python's bool/int equivalence."""
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(_json_equal(left[key], right[key]) for key in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(_json_equal(a, b) for a, b in zip(left, right))
    return left == right


def _json_pointer(document: Any, pointer: str) -> Any:
    if not pointer.startswith("#/"):
        raise CatalogError(f"schema $ref `{pointer}` must be a local JSON pointer")
    value = document
    for raw in pointer[2:].split("/"):
        token = raw.replace("~1", "/").replace("~0", "~")
        if not isinstance(value, dict) or token not in value:
            raise CatalogError(f"schema $ref `{pointer}` is dangling")
        value = value[token]
    return value


def _schema_refs(rule: Any) -> set[str]:
    """Collect references only from schema-bearing keywords, not literal data."""
    refs: set[str] = set()
    if not isinstance(rule, dict):
        return refs
    if isinstance(rule.get("$ref"), str):
        refs.add(rule["$ref"])
    for collection in ("$defs", "properties"):
        children = rule.get(collection, {})
        if isinstance(children, dict):
            for child in children.values():
                refs.update(_schema_refs(child))
    for keyword in ("items", "not"):
        if keyword in rule:
            refs.update(_schema_refs(rule[keyword]))
    branches = rule.get("oneOf", [])
    if isinstance(branches, list):
        for child in branches:
            refs.update(_schema_refs(child))
    return refs


def _validate_schema_graph(schema: dict[str, Any]) -> None:
    """Require local references to resolve and their full graph to be acyclic."""
    if not isinstance(schema.get("$defs", {}), dict):
        raise CatalogError("schema.$defs must be an object")
    graph: dict[str, set[str]] = {}
    pending = list(_schema_refs(schema))
    while pending:
        ref = pending.pop()
        if ref in graph:
            continue
        target = _json_pointer(schema, ref)
        _validate_schema_keywords(target, f"schema $ref `{ref}` target")
        children = _schema_refs(target)
        graph[ref] = children
        pending.extend(children - graph.keys())
    ordered_graph = {ref: tuple(sorted(children)) for ref, children in graph.items()}
    state: dict[str, int] = {}
    for start in sorted(ordered_graph):
        if state.get(start, 0) != 0:
            continue
        state[start] = 1
        trail = [start]
        positions = {start: 0}
        stack = [(start, 0)]
        while stack:
            ref, child_idx = stack[-1]
            children = ordered_graph.get(ref, ())
            if child_idx == len(children):
                stack.pop()
                trail.pop()
                positions.pop(ref)
                state[ref] = 2
                continue
            child = children[child_idx]
            stack[-1] = (ref, child_idx + 1)
            child_state = state.get(child, 0)
            if child_state == 0:
                state[child] = 1
                positions[child] = len(trail)
                trail.append(child)
                stack.append((child, 0))
            elif child_state == 1:
                cycle = (*trail[positions[child]:], child)
                raise CatalogError(f"schema $ref cycle detected: {' -> '.join(cycle)}")


def _validate_schema_keywords(rule: Any, where: str = "schema") -> None:
    """Fail closed if the committed schema uses a keyword this validator ignores."""
    if not isinstance(rule, dict):
        raise CatalogError(f"{where} must be an object")
    if "$ref" in rule and not isinstance(rule["$ref"], str):
        raise CatalogError(f"{where}.$ref must be a string")
    if "additionalProperties" in rule and not isinstance(rule["additionalProperties"], bool):
        raise CatalogError(f"{where}.additionalProperties must be a boolean")
    supported = {
        "$schema",
        "$id",
        "$ref",
        "$defs",
        "title",
        "type",
        "additionalProperties",
        "required",
        "properties",
        "const",
        "enum",
        "minItems",
        "maxItems",
        "items",
        "minLength",
        "pattern",
        "oneOf",
        "not",
    }
    unknown = sorted(set(rule) - supported)
    if unknown:
        raise CatalogError(f"{where} uses unsupported JSON Schema keyword(s): {', '.join(unknown)}")
    for collection in ("$defs", "properties"):
        children = rule.get(collection, {})
        if not isinstance(children, dict):
            raise CatalogError(f"{where}.{collection} must be an object")
        for name, child in children.items():
            _validate_schema_keywords(child, f"{where}.{collection}.{name}")
    if "items" in rule:
        _validate_schema_keywords(rule["items"], f"{where}.items")
    if "not" in rule:
        _validate_schema_keywords(rule["not"], f"{where}.not")
    for idx, child in enumerate(rule.get("oneOf", [])):
        _validate_schema_keywords(child, f"{where}.oneOf[{idx}]")


def _schema_type_matches(value: Any, expected: str) -> bool:
    types = {
        "object": lambda item: isinstance(item, dict),
        "array": lambda item: isinstance(item, list),
        "string": lambda item: isinstance(item, str),
        "integer": lambda item: isinstance(item, int) and not isinstance(item, bool),
        "number": lambda item: isinstance(item, (int, float)) and not isinstance(item, bool),
        "boolean": lambda item: isinstance(item, bool),
        "null": lambda item: item is None,
    }
    return expected in types and types[expected](value)


def _schema_errors(value: Any, rule: Any, schema: dict[str, Any], where: str = "catalog") -> list[str]:
    """Validate the JSON Schema keywords used by the committed v1 schema."""
    seen_refs: set[str] = set()
    while isinstance(rule, dict) and "$ref" in rule:
        ref = rule["$ref"]
        if ref in seen_refs:
            return [f"{where}: schema $ref cycle detected during validation"]
        seen_refs.add(ref)
        rule = _json_pointer(schema, ref)
    if not isinstance(rule, dict):
        return [f"{where}: schema rule must be an object"]

    errors: list[str] = []
    expected_type = rule.get("type")
    if expected_type is not None and not _schema_type_matches(value, expected_type):
        return [f"{where}: expected JSON type {expected_type}, got {type(value).__name__}"]
    if "const" in rule and not _json_equal(value, rule["const"]):
        errors.append(f"{where}: value does not equal schema const")
    if "enum" in rule and not any(_json_equal(value, item) for item in rule["enum"]):
        errors.append(f"{where}: value is not in schema enum")

    if isinstance(value, dict):
        for key in rule.get("required", []):
            if key not in value:
                errors.append(f"{where}: missing required field `{key}`")
        properties = rule.get("properties", {})
        for key, child in value.items():
            if key in properties:
                errors.extend(_schema_errors(child, properties[key], schema, f"{where}.{key}"))
            elif rule.get("additionalProperties") is False:
                errors.append(f"{where}: unknown field `{key}`")
    elif isinstance(value, list):
        if "minItems" in rule and len(value) < rule["minItems"]:
            errors.append(f"{where}: expected at least {rule['minItems']} items")
        if "maxItems" in rule and len(value) > rule["maxItems"]:
            errors.append(f"{where}: expected at most {rule['maxItems']} items")
        if "items" in rule:
            for idx, child in enumerate(value):
                errors.extend(_schema_errors(child, rule["items"], schema, f"{where}[{idx}]"))
    elif isinstance(value, str):
        if "minLength" in rule and len(value) < rule["minLength"]:
            errors.append(f"{where}: string is shorter than {rule['minLength']}")
        if "pattern" in rule and re.search(rule["pattern"], value) is None:
            errors.append(f"{where}: string does not match schema pattern")

    if "not" in rule and not _schema_errors(value, rule["not"], schema, where):
        errors.append(f"{where}: value matches forbidden schema branch")
    if "oneOf" in rule:
        matches = sum(not _schema_errors(value, branch, schema, where) for branch in rule["oneOf"])
        if matches != 1:
            errors.append(f"{where}: expected exactly one matching oneOf branch, got {matches}")
    return errors


def _load_schema(path: pathlib.Path) -> dict[str, Any]:
    try:
        schema = json.loads(path.read_text())
    except FileNotFoundError as exc:
        raise CatalogError(f"schema not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise CatalogError(f"invalid JSON schema in {path}: {exc}") from exc
    if not isinstance(schema, dict):
        raise CatalogError("binding schema root must be an object")
    _validate_schema_keywords(schema)
    _validate_schema_graph(schema)
    return schema


@dataclass(frozen=True)
class Reference:
    ref_id: str
    source: str
    key: str


@dataclass(frozen=True)
class Hop:
    source: str
    destination: str
    protocol: str
    transport: str
    endpoint_kind: str


@dataclass(frozen=True)
class Lifecycle:
    browser_owner: str
    bridge_owner: str
    adapter_owner: str


@dataclass(frozen=True)
class DiscoveryPoint:
    kind: str
    endpoint_kind: str
    probe: str
    readiness_owner: str


@dataclass(frozen=True)
class Discovery:
    browser: DiscoveryPoint
    client: DiscoveryPoint


@dataclass(frozen=True)
class Assertion:
    mechanism: str
    actual_path: str
    operator: str
    expected_ref: str | None
    expected_literal: str | None
    condition: str
    fallback_actual_path: str | None
    fallback_operator: str | None


@dataclass(frozen=True)
class Identity:
    http_assertions: tuple[Assertion, ...]
    live_transport_assertions: tuple[Assertion, ...]


@dataclass(frozen=True)
class Route:
    route_id: str
    client_protocol: str
    client_endpoint_kind: str
    browser_endpoint_kind: str
    connect_mode: str
    provider: str
    ordered_hops: tuple[Hop, ...]
    lifecycle: Lifecycle
    discovery: Discovery
    identity: Identity


@dataclass(frozen=True)
class Binding:
    binding_id: str
    browser_id: str
    driver_id: str
    route: Route | None
    unavailable_reason: str | None
    browser_pin: Reference
    driver_pin: Reference
    bridge_pins: tuple[Reference, ...]
    fallback_allowed: bool


@dataclass(frozen=True)
class Catalog:
    schema_version: int
    browser_ids: tuple[str, ...]
    driver_ids: tuple[str, ...]
    references: Mapping[str, Reference]
    routes: Mapping[str, Route]
    bindings: tuple[Binding, ...]
    _binding_index: Mapping[tuple[str, str], Binding]

    def require_binding(self, browser_id: str, driver_id: str) -> Binding:
        """Return the unique normalized record for one browser/driver pair."""
        if browser_id not in self.browser_ids:
            raise CatalogError(
                f"unknown browser_id `{browser_id}`; expected one of: {', '.join(self.browser_ids)}"
            )
        if driver_id not in self.driver_ids:
            raise CatalogError(
                f"unknown driver_id `{driver_id}`; expected one of: {', '.join(self.driver_ids)}"
            )
        try:
            return self._binding_index[(browser_id, driver_id)]
        except KeyError as exc:
            raise CatalogError(f"missing binding for ({browser_id}, {driver_id})") from exc


def _object(value: Any, where: str, fields: frozenset[str], required: frozenset[str] | None = None) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CatalogError(f"{where} must be an object")
    extra = sorted(set(value) - fields)
    if extra:
        raise CatalogError(f"{where} has unknown field(s): {', '.join(extra)}")
    missing = sorted((required or fields) - set(value))
    if missing:
        raise CatalogError(f"{where} is missing required field(s): {', '.join(missing)}")
    return value


def _string(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CatalogError(f"{where} must be a non-empty string")
    return value


def _string_list(value: Any, where: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list) or (not value and not allow_empty):
        suffix = "" if allow_empty else " non-empty"
        raise CatalogError(f"{where} must be a{suffix} list of strings")
    rows: list[str] = []
    for idx, item in enumerate(value):
        rows.append(_string(item, f"{where}[{idx}]"))
    return tuple(rows)


def _pin_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _reject_result_semantics(value: Any, where: str = "catalog") -> None:
    if isinstance(value, dict):
        forbidden = sorted(set(value) & FORBIDDEN_RESULT_FIELDS)
        if forbidden:
            raise CatalogError(
                f"{where} contains forbidden observed-compatibility field(s): {', '.join(forbidden)}"
            )
        for key, child in value.items():
            _reject_result_semantics(child, f"{where}.{key}")
    elif isinstance(value, list):
        for idx, child in enumerate(value):
            _reject_result_semantics(child, f"{where}[{idx}]")


def _unique_ids(rows: Any, where: str, field: str) -> list[dict[str, Any]]:
    if not isinstance(rows, list):
        raise CatalogError(f"{where} must be a list")
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for idx, value in enumerate(rows):
        if not isinstance(value, dict):
            raise CatalogError(f"{where}[{idx}] must be an object")
        item_id = _string(value.get(field), f"{where}[{idx}].{field}")
        if item_id in seen:
            raise CatalogError(f"{where} has duplicate {field} `{item_id}`")
        seen.add(item_id)
        result.append(value)
    return result


def _parse_reference(value: dict[str, Any], idx: int) -> Reference:
    where = f"references[{idx}]"
    row = _object(value, where, REFERENCE_FIELDS)
    source = _string(row["source"], f"{where}.source")
    if source not in {"runner.run.ENGINE_DEFS", "harness_pins.json.drivers"}:
        raise CatalogError(f"{where}.source `{source}` is not a recognized source of truth")
    return Reference(
        ref_id=_string(row["ref_id"], f"{where}.ref_id"),
        source=source,
        key=_string(row["key"], f"{where}.key"),
    )


def _parse_route(value: dict[str, Any], idx: int) -> Route:
    where = f"routes[{idx}]"
    row = _object(value, where, ROUTE_FIELDS)
    hops_raw = row["ordered_hops"]
    if not isinstance(hops_raw, list) or not hops_raw:
        raise CatalogError(f"{where}.ordered_hops must be a non-empty list")
    hops: list[Hop] = []
    for hop_idx, hop_value in enumerate(hops_raw):
        hop_where = f"{where}.ordered_hops[{hop_idx}]"
        hop = _object(hop_value, hop_where, HOP_FIELDS)
        hops.append(
            Hop(
                source=_string(hop["from"], f"{hop_where}.from"),
                destination=_string(hop["to"], f"{hop_where}.to"),
                protocol=_string(hop["protocol"], f"{hop_where}.protocol"),
                transport=_string(hop["transport"], f"{hop_where}.transport"),
                endpoint_kind=_string(hop["endpoint_kind"], f"{hop_where}.endpoint_kind"),
            )
        )
    for hop_idx in range(len(hops) - 1):
        if hops[hop_idx].destination != hops[hop_idx + 1].source:
            raise CatalogError(
                f"{where}.ordered_hops is disconnected between hop {hop_idx} and {hop_idx + 1}"
            )
    if hops[-1].destination != "browser":
        raise CatalogError(f"{where}.ordered_hops final destination must be `browser`")

    lifecycle_raw = _object(row["lifecycle"], f"{where}.lifecycle", LIFECYCLE_FIELDS)
    lifecycle = Lifecycle(
        browser_owner=_string(lifecycle_raw["browser_owner"], f"{where}.lifecycle.browser_owner"),
        bridge_owner=_string(lifecycle_raw["bridge_owner"], f"{where}.lifecycle.bridge_owner"),
        adapter_owner=_string(lifecycle_raw["adapter_owner"], f"{where}.lifecycle.adapter_owner"),
    )
    discovery_raw = _object(row["discovery"], f"{where}.discovery", DISCOVERY_FIELDS)

    def discovery_point(name: str) -> DiscoveryPoint:
        point_where = f"{where}.discovery.{name}"
        point = _object(discovery_raw[name], point_where, DISCOVERY_POINT_FIELDS)
        return DiscoveryPoint(
            kind=_string(point["kind"], f"{point_where}.kind"),
            endpoint_kind=_string(point["endpoint_kind"], f"{point_where}.endpoint_kind"),
            probe=_string(point["probe"], f"{point_where}.probe"),
            readiness_owner=_string(point["readiness_owner"], f"{point_where}.readiness_owner"),
        )

    discovery = Discovery(
        browser=discovery_point("browser"),
        client=discovery_point("client"),
    )
    identity_raw = _object(row["identity"], f"{where}.identity", IDENTITY_FIELDS)

    def assertions(name: str) -> tuple[Assertion, ...]:
        values = identity_raw[name]
        if not isinstance(values, list) or not values:
            raise CatalogError(f"{where}.identity.{name} must be a non-empty list")
        result: list[Assertion] = []
        for idx, value in enumerate(values):
            assertion_where = f"{where}.identity.{name}[{idx}]"
            assertion = _object(
                value,
                assertion_where,
                ASSERTION_FIELDS,
                frozenset({"mechanism", "actual_path", "operator", "condition"}),
            )
            has_ref = "expected_ref" in assertion
            has_literal = "expected_literal" in assertion
            if has_ref == has_literal:
                raise CatalogError(
                    f"{assertion_where} must declare exactly one of expected_ref or expected_literal"
                )
            fallback_path = assertion.get("fallback_actual_path")
            fallback_operator = assertion.get("fallback_operator")
            if (fallback_path is None) != (fallback_operator is None):
                raise CatalogError(
                    f"{assertion_where} fallback_actual_path and fallback_operator must appear together"
                )
            result.append(
                Assertion(
                    mechanism=_string(assertion["mechanism"], f"{assertion_where}.mechanism"),
                    actual_path=_string(assertion["actual_path"], f"{assertion_where}.actual_path"),
                    operator=_string(assertion["operator"], f"{assertion_where}.operator"),
                    expected_ref=(
                        _string(assertion["expected_ref"], f"{assertion_where}.expected_ref")
                        if has_ref
                        else None
                    ),
                    expected_literal=(
                        _string(assertion["expected_literal"], f"{assertion_where}.expected_literal")
                        if has_literal
                        else None
                    ),
                    condition=_string(assertion["condition"], f"{assertion_where}.condition"),
                    fallback_actual_path=(
                        _string(fallback_path, f"{assertion_where}.fallback_actual_path")
                        if fallback_path is not None
                        else None
                    ),
                    fallback_operator=(
                        _string(fallback_operator, f"{assertion_where}.fallback_operator")
                        if fallback_operator is not None
                        else None
                    ),
                )
            )
        return tuple(result)

    identity = Identity(
        http_assertions=assertions("http_assertions"),
        live_transport_assertions=assertions("live_transport_assertions"),
    )
    client_endpoint_kind = _string(
        row["client_endpoint_kind"], f"{where}.client_endpoint_kind"
    )
    browser_endpoint_kind = _string(
        row["browser_endpoint_kind"], f"{where}.browser_endpoint_kind"
    )
    hop_endpoints = {hop.endpoint_kind for hop in hops}
    if client_endpoint_kind not in hop_endpoints:
        raise CatalogError(
            f"{where}.client_endpoint_kind `{client_endpoint_kind}` is absent from ordered_hops"
        )
    if hops[-1].endpoint_kind != browser_endpoint_kind:
        raise CatalogError(
            f"{where}.browser_endpoint_kind must equal the final hop endpoint_kind "
            f"`{hops[-1].endpoint_kind}`"
        )
    if discovery.client.endpoint_kind != client_endpoint_kind:
        raise CatalogError(
            f"{where}.discovery.client.endpoint_kind must equal client_endpoint_kind"
        )
    if lifecycle.bridge_owner == "none" and row["provider"] != "browser_native":
        raise CatalogError(
            f"{where}.provider must be `browser_native` when bridge_owner is `none`"
        )
    if lifecycle.bridge_owner != "none" and row["provider"] == "browser_native":
        raise CatalogError(
            f"{where}.provider must describe the bridge when bridge_owner is not `none`"
        )
    return Route(
        route_id=_string(row["route_id"], f"{where}.route_id"),
        client_protocol=_string(row["client_protocol"], f"{where}.client_protocol"),
        client_endpoint_kind=client_endpoint_kind,
        browser_endpoint_kind=browser_endpoint_kind,
        connect_mode=_string(row["connect_mode"], f"{where}.connect_mode"),
        provider=_string(row["provider"], f"{where}.provider"),
        ordered_hops=tuple(hops),
        lifecycle=lifecycle,
        discovery=discovery,
        identity=identity,
    )


def _resolve_reference(ref_id: Any, references: Mapping[str, Reference], where: str) -> Reference:
    resolved_id = _string(ref_id, where)
    try:
        return references[resolved_id]
    except KeyError as exc:
        raise CatalogError(f"{where} has dangling reference `{resolved_id}`") from exc


def _parse_binding(
    value: dict[str, Any],
    idx: int,
    references: Mapping[str, Reference],
    routes: Mapping[str, Route],
) -> Binding:
    where = f"bindings[{idx}]"
    row = _object(
        value,
        where,
        BINDING_FIELDS,
        frozenset(
            {
                "binding_id",
                "browser_id",
                "driver_id",
                "browser_pin_ref",
                "driver_pin_ref",
                "bridge_pin_refs",
                "fallback_allowed",
            }
        ),
    )
    browser_id = _string(row["browser_id"], f"{where}.browser_id")
    driver_id = _string(row["driver_id"], f"{where}.driver_id")
    binding_id = _string(row["binding_id"], f"{where}.binding_id")
    expected_id = f"{browser_id}__{driver_id}"
    if binding_id != expected_id:
        raise CatalogError(f"{where}.binding_id must be `{expected_id}`, got `{binding_id}`")
    if browser_id not in BROWSER_IDS:
        raise CatalogError(f"{where}.browser_id `{browser_id}` is unknown")
    if driver_id not in DRIVER_IDS:
        raise CatalogError(f"{where}.driver_id `{driver_id}` is unknown")
    if row["fallback_allowed"] is not False:
        raise CatalogError(f"{where}.fallback_allowed must be false")

    has_route = "route_ref" in row
    has_reason = "unavailable_reason" in row
    if has_route == has_reason:
        raise CatalogError(f"{where} must declare exactly one of route_ref or unavailable_reason")
    route: Route | None = None
    unavailable_reason: str | None = None
    if has_route:
        route_id = _string(row["route_ref"], f"{where}.route_ref")
        try:
            route = routes[route_id]
        except KeyError as exc:
            raise CatalogError(f"{where}.route_ref has dangling reference `{route_id}`") from exc
    else:
        unavailable_reason = _string(row["unavailable_reason"], f"{where}.unavailable_reason")

    bridge_ref_ids = _string_list(
        row["bridge_pin_refs"], f"{where}.bridge_pin_refs", allow_empty=True
    )
    browser_pin = _resolve_reference(row["browser_pin_ref"], references, f"{where}.browser_pin_ref")
    driver_pin = _resolve_reference(row["driver_pin_ref"], references, f"{where}.driver_pin_ref")
    bridge_pins = tuple(
        _resolve_reference(ref_id, references, f"{where}.bridge_pin_refs[{ref_idx}]")
        for ref_idx, ref_id in enumerate(bridge_ref_ids)
    )
    if browser_pin.source != "runner.run.ENGINE_DEFS" or browser_pin.key != browser_id:
        raise CatalogError(
            f"{where}.browser_pin_ref must resolve to ENGINE_DEFS.{browser_id}"
        )
    if browser_pin.ref_id != f"browser.{browser_id}":
        raise CatalogError(f"{where}.browser_pin_ref must be `browser.{browser_id}`")
    if driver_pin.source != "harness_pins.json.drivers":
        raise CatalogError(f"{where}.driver_pin_ref must resolve into harness_pins.json drivers")
    if driver_pin.ref_id != f"driver.{driver_id}":
        raise CatalogError(f"{where}.driver_pin_ref must be `driver.{driver_id}`")
    if _pin_name(driver_id) not in _pin_name(driver_pin.key):
        raise CatalogError(
            f"{where}.driver_pin_ref key `{driver_pin.key}` does not identify driver `{driver_id}`"
        )
    if any(pin.source != "harness_pins.json.drivers" for pin in bridge_pins):
        raise CatalogError(f"{where}.bridge_pin_refs must resolve into harness_pins.json drivers")
    if len({pin.ref_id for pin in bridge_pins}) != len(bridge_pins):
        raise CatalogError(f"{where}.bridge_pin_refs must be unique")
    for pin in bridge_pins:
        bridge_alias = pin.ref_id.removeprefix("bridge.")
        if _pin_name(pin.key) not in _pin_name(bridge_alias):
            raise CatalogError(
                f"{where}.bridge pin `{pin.ref_id}` does not identify harness pin `{pin.key}`"
            )
        if route is not None:
            route_components = {
                _pin_name(name)
                for hop in route.ordered_hops
                for name in (hop.source, hop.destination)
                if name != "browser"
            }
            if not any(_pin_name(pin.key) in component for component in route_components):
                raise CatalogError(
                    f"{where}.bridge pin `{pin.key}` does not occur in route `{route.route_id}` hops"
                )
    if route is None:
        if bridge_pins:
            raise CatalogError(f"{where}.bridge_pin_refs must be empty when no route exists")
    elif route.lifecycle.bridge_owner == "none":
        if bridge_pins:
            raise CatalogError(
                f"{where}.bridge_pin_refs must be empty when route bridge_owner is `none`"
            )
    else:
        if not bridge_pins:
            raise CatalogError(
                f"{where}.bridge_pin_refs must be non-empty when route bridge_owner is not `none`"
            )

    return Binding(
        binding_id=binding_id,
        browser_id=browser_id,
        driver_id=driver_id,
        route=route,
        unavailable_reason=unavailable_reason,
        browser_pin=browser_pin,
        driver_pin=driver_pin,
        bridge_pins=bridge_pins,
        fallback_allowed=False,
    )


def _validate_golden_routes(catalog: Catalog) -> None:
    expected = {
        "moli": "native_webdriver",
        "chrome": "chromedriver_cdp",
        "lightpanda": "chromedriver_cdp",
    }
    for browser_id, route_id in expected.items():
        binding = catalog.require_binding(browser_id, "selenium")
        actual = binding.route.route_id if binding.route else None
        if actual != route_id:
            raise CatalogError(
                f"golden route ({browser_id}, selenium) must resolve to `{route_id}`, got `{actual}`"
            )
    if catalog.require_binding("moli", "selenium").bridge_pins:
        raise CatalogError("golden route (moli, selenium) must not reference ChromeDriver")
    for browser_id in ("chrome", "lightpanda"):
        keys = {pin.key for pin in catalog.require_binding(browser_id, "selenium").bridge_pins}
        if keys != {"chromedriver"}:
            raise CatalogError(
                f"golden route ({browser_id}, selenium) must reference exactly the chromedriver pin"
            )
    obscura = catalog.require_binding("obscura", "selenium")
    if obscura.route is not None or not obscura.unavailable_reason:
        raise CatalogError(
            "golden route (obscura, selenium) must be a fail-closed unavailable binding"
        )
    obscura_pydoll = catalog.require_binding("obscura", "pydoll")
    if (
        obscura_pydoll.route is None
        or obscura_pydoll.route.route_id != "pydoll_obscura_cdp"
    ):
        raise CatalogError(
            "golden route (obscura, pydoll) must use the audited flattened-session bootstrap"
        )


def load_catalog(
    path: pathlib.Path | str = DEFAULT_CATALOG_PATH,
    schema_path: pathlib.Path | str = DEFAULT_SCHEMA_PATH,
) -> Catalog:
    """Load, validate, normalize, and index a binding catalog."""
    catalog_path = pathlib.Path(path)
    try:
        raw = json.loads(catalog_path.read_text())
    except FileNotFoundError as exc:
        raise CatalogError(f"catalog not found: {catalog_path}") from exc
    except json.JSONDecodeError as exc:
        raise CatalogError(f"invalid JSON in {catalog_path}: {exc}") from exc

    _reject_result_semantics(raw)
    schema = _load_schema(pathlib.Path(schema_path))
    schema_errors = _schema_errors(raw, schema, schema)
    if schema_errors:
        detail = "\n".join(f"- {error}" for error in schema_errors)
        raise CatalogError(f"JSON Schema validation failed:\n{detail}")
    root = _object(raw, "catalog", ROOT_FIELDS)
    if root["schema_version"] != 1:
        raise CatalogError(f"catalog.schema_version must be 1, got {root['schema_version']!r}")
    browser_ids = _string_list(root["browser_ids"], "catalog.browser_ids")
    driver_ids = _string_list(root["driver_ids"], "catalog.driver_ids")
    if browser_ids != BROWSER_IDS:
        raise CatalogError(f"catalog.browser_ids must be exactly {list(BROWSER_IDS)!r}")
    if driver_ids != DRIVER_IDS:
        raise CatalogError(f"catalog.driver_ids must be exactly {list(DRIVER_IDS)!r}")

    reference_rows = _unique_ids(root["references"], "references", "ref_id")
    references_mut = {
        ref.ref_id: ref for idx, row in enumerate(reference_rows) for ref in [_parse_reference(row, idx)]
    }
    route_rows = _unique_ids(root["routes"], "routes", "route_id")
    routes_mut = {
        route.route_id: route for idx, row in enumerate(route_rows) for route in [_parse_route(row, idx)]
    }
    references: Mapping[str, Reference] = MappingProxyType(references_mut)
    routes: Mapping[str, Route] = MappingProxyType(routes_mut)

    binding_rows = _unique_ids(root["bindings"], "bindings", "binding_id")
    bindings = tuple(
        _parse_binding(row, idx, references, routes) for idx, row in enumerate(binding_rows)
    )
    index_mut: dict[tuple[str, str], Binding] = {}
    for binding in bindings:
        pair = (binding.browser_id, binding.driver_id)
        if pair in index_mut:
            raise CatalogError(
                f"bindings has duplicate pair ({binding.browser_id}, {binding.driver_id})"
            )
        index_mut[pair] = binding
    expected_pairs = {(browser, driver) for browser in BROWSER_IDS for driver in DRIVER_IDS}
    missing = sorted(expected_pairs - set(index_mut))
    extra = sorted(set(index_mut) - expected_pairs)
    if missing:
        rendered = ", ".join(f"({browser}, {driver})" for browser, driver in missing)
        raise CatalogError(f"catalog is missing binding pair(s): {rendered}")
    if extra:
        rendered = ", ".join(f"({browser}, {driver})" for browser, driver in extra)
        raise CatalogError(f"catalog contains unexpected binding pair(s): {rendered}")
    expected_binding_count = len(BROWSER_IDS) * len(DRIVER_IDS)
    if len(bindings) != expected_binding_count:
        raise CatalogError(
            f"catalog must contain exactly {expected_binding_count} bindings, "
            f"got {len(bindings)}"
        )

    catalog = Catalog(
        schema_version=1,
        browser_ids=browser_ids,
        driver_ids=driver_ids,
        references=references,
        routes=routes,
        bindings=bindings,
        _binding_index=MappingProxyType(index_mut),
    )
    return catalog


def validate_repository_parity(
    catalog: Catalog,
    pins_path: pathlib.Path = HARNESS_PINS_PATH,
    schema_path: pathlib.Path = DEFAULT_SCHEMA_PATH,
) -> None:
    """Check external references and current registries without import-time coupling."""
    schema = _load_schema(schema_path)
    if schema.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
        raise CatalogError("binding schema must declare JSON Schema draft 2020-12")
    if schema.get("additionalProperties") is not False:
        raise CatalogError("binding schema root must set additionalProperties=false")

    try:
        pins = json.loads(pins_path.read_text())
    except FileNotFoundError as exc:
        raise CatalogError(f"harness pins not found: {pins_path}") from exc
    except json.JSONDecodeError as exc:
        raise CatalogError(f"invalid JSON in {pins_path}: {exc}") from exc
    driver_pins = pins.get("drivers")
    if not isinstance(driver_pins, dict):
        raise CatalogError("harness_pins.json.drivers must be an object")
    for reference in catalog.references.values():
        if reference.source == "harness_pins.json.drivers" and reference.key not in driver_pins:
            raise CatalogError(
                f"reference `{reference.ref_id}` points to missing harness pin `{reference.key}`"
            )

    # These imports are intentionally local.  Merely importing runner.bindings
    # never imports the runtime monolith.
    from runner import run as runner_run
    from runner import scenario

    if tuple(runner_run.ENGINE_ORDER) != catalog.browser_ids:
        raise CatalogError(
            f"browser roster drift: catalog={catalog.browser_ids!r}, ENGINE_ORDER={tuple(runner_run.ENGINE_ORDER)!r}"
        )
    if tuple(scenario.DRIVER_REGISTRY) != catalog.driver_ids:
        raise CatalogError(
            "driver roster drift: catalog and runner.scenario.DRIVER_REGISTRY differ"
        )
    for reference in catalog.references.values():
        if (
            reference.source == "runner.run.ENGINE_DEFS"
            and reference.key not in runner_run.ENGINE_DEFS
        ):
            raise CatalogError(
                f"reference `{reference.ref_id}` points to missing ENGINE_DEFS key `{reference.key}`"
            )
    _validate_golden_routes(catalog)


def _hop_summary(route: Route) -> str:
    chain = [route.ordered_hops[0].source]
    for hop in route.ordered_hops:
        chain.append(hop.destination)
    return " → ".join(chain)


def _assertion_summary(assertion: Assertion) -> str:
    expected = assertion.expected_ref or f'"{assertion.expected_literal}"'
    text = (
        f"{assertion.mechanism}:{assertion.actual_path} "
        f"{assertion.operator} {expected}"
    )
    if assertion.condition != "always":
        text += f" [{assertion.condition}]"
    if assertion.fallback_actual_path:
        text += (
            f" (fallback {assertion.fallback_actual_path} "
            f"{assertion.fallback_operator} {expected})"
        )
    return text


def _markdown_cell(value: Any) -> str:
    return str(value).replace("\\", "\\\\").replace("|", "\\|").replace("\n", "<br>")


def _markdown_row(cells: Sequence[Any]) -> str:
    return "| " + " | ".join(_markdown_cell(cell) for cell in cells) + " |"


def render_catalog(catalog: Catalog) -> str:
    """Render the deterministic human-readable route matrix."""
    lines = [
        "# Browser × Driver Binding Matrix",
        "",
        "> Generated by `python3 -m runner.bindings render` from",
        "> `config/browser_driver_bindings.json`. Do not edit this file manually.",
        "> A route describes how the benchmark attempts to connect; it is not an",
        "> observed compatibility result.",
        "",
        "The catalog covers exactly Chrome, Moli, Lightpanda and Obscura across the 13",
        "driver keys in `runner.scenario.DRIVER_REGISTRY` (52 pairs).",
        "`fallback_allowed` is false for every pair.",
        "",
        "## Route definitions",
        "",
        "| Route | Provider | Client endpoint | Browser endpoint | Lifecycle | Readiness | HTTP identity | Live identity | Ordered hops |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for route in catalog.routes.values():
        lifecycle = (
            f"browser={route.lifecycle.browser_owner}; "
            f"bridge={route.lifecycle.bridge_owner}; "
            f"adapter={route.lifecycle.adapter_owner}"
        )
        readiness = (
            f"browser: {route.discovery.browser.kind} / {route.discovery.browser.endpoint_kind} / "
            f"{route.discovery.browser.probe}; client: {route.discovery.client.kind} / "
            f"{route.discovery.client.endpoint_kind} / {route.discovery.client.probe}"
        )
        http_identity = "; ".join(
            _assertion_summary(assertion) for assertion in route.identity.http_assertions
        )
        live_identity = "; ".join(
            _assertion_summary(assertion)
            for assertion in route.identity.live_transport_assertions
        )
        lines.append(
            _markdown_row(
                (
                    f"`{route.route_id}`",
                    f"`{route.provider}`",
                    f"`{route.client_endpoint_kind}`",
                    f"`{route.browser_endpoint_kind}`",
                    lifecycle,
                    readiness,
                    http_identity,
                    live_identity,
                    _hop_summary(route),
                )
            )
        )
    lines.append("")
    for browser_id in catalog.browser_ids:
        lines.extend(
            [
                f"## {browser_id}",
                "",
                "| Driver | Route | Client protocol | Client endpoint | Browser endpoint | Connect mode | Pin references |",
                "|---|---|---|---|---|---|---|",
            ]
        )
        for driver_id in catalog.driver_ids:
            binding = catalog.require_binding(browser_id, driver_id)
            pins = [
                binding.browser_pin.ref_id,
                binding.driver_pin.ref_id,
                *(pin.ref_id for pin in binding.bridge_pins),
            ]
            if binding.route is None:
                lines.append(
                    _markdown_row(
                        (
                            f"`{driver_id}`",
                            f"unavailable: {binding.unavailable_reason}",
                            "—",
                            "—",
                            "—",
                            "—",
                            ", ".join(f"`{pin}`" for pin in pins),
                        )
                    )
                )
                continue
            route = binding.route
            lines.append(
                _markdown_row(
                    (
                        f"`{driver_id}`",
                        f"`{route.route_id}`",
                        f"`{route.client_protocol}`",
                        f"`{route.client_endpoint_kind}`",
                        f"`{route.browser_endpoint_kind}`",
                        f"`{route.connect_mode}`",
                        ", ".join(f"`{pin}`" for pin in pins),
                    )
                )
            )
        lines.append("")
    lines.extend(
        [
            "## Interpretation boundary",
            "",
            "- Browser launch/build facts remain owned by `runner.run.ENGINE_DEFS`.",
            "- Driver and bridge versions remain owned by `harness_pins.json`.",
            "- Scenario columns remain owned by `runner.scenario.DRIVER_REGISTRY`.",
            "- This matrix owns only route, hop, endpoint and lifecycle facts.",
            "- Pass/fail, known gaps, skips, task selection and scoring are run evidence",
            "  and are intentionally absent from this catalog.",
            "",
        ]
    )
    return "\n".join(lines)


def write_rendered(
    catalog: Catalog,
    output_path: pathlib.Path = DEFAULT_RENDER_PATH,
    *,
    check: bool = False,
) -> None:
    expected = render_catalog(catalog)
    if check:
        try:
            actual = output_path.read_text()
        except FileNotFoundError as exc:
            raise CatalogError(f"generated matrix is missing: {output_path}") from exc
        if actual != expected:
            raise CatalogError(
                f"generated matrix drift: run `python3 -m runner.bindings render` to update {output_path}"
            )
        return
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(expected)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate and render browser/driver bindings")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="validate catalog, references, pins, and registry parity")
    render = sub.add_parser("render", help="render the deterministic Markdown matrix")
    render.add_argument("--check", action="store_true", help="fail if the generated matrix drifted")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        catalog = load_catalog()
        validate_repository_parity(catalog)
        if args.command == "check":
            print(
                f"binding catalog OK: {len(catalog.browser_ids)} browsers × "
                f"{len(catalog.driver_ids)} drivers = {len(catalog.bindings)} bindings"
            )
        elif args.command == "render":
            write_rendered(catalog, check=args.check)
            if args.check:
                print(f"binding matrix in sync: {DEFAULT_RENDER_PATH.relative_to(BENCH_ROOT)}")
            else:
                print(f"rendered {DEFAULT_RENDER_PATH.relative_to(BENCH_ROOT)}")
        return 0
    except CatalogError as exc:
        print(f"binding catalog error: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
