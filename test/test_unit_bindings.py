from __future__ import annotations

import copy
import dataclasses
import json
import pathlib
import re
import subprocess
import sys
from types import SimpleNamespace

import pytest

from runner import bindings
from runner import run as runner_run


def _raw() -> dict:
    return json.loads(bindings.DEFAULT_CATALOG_PATH.read_text())


def _write(tmp_path: pathlib.Path, raw: dict) -> pathlib.Path:
    path = tmp_path / "bindings.json"
    path.write_text(json.dumps(raw))
    return path


def _load_mutated(tmp_path: pathlib.Path, mutate) -> bindings.Catalog:
    raw = _raw()
    mutate(raw)
    return bindings.load_catalog(_write(tmp_path, raw))


def test_catalog_has_exact_frozen_rosters_and_cartesian_product():
    catalog = bindings.load_catalog()
    assert catalog.browser_ids == ("chrome", "moli", "lightpanda", "obscura")
    assert catalog.driver_ids == bindings.DRIVER_IDS
    assert len(catalog.bindings) == 52
    assert {
        (binding.browser_id, binding.driver_id) for binding in catalog.bindings
    } == {
        (browser_id, driver_id)
        for browser_id in bindings.BROWSER_IDS
        for driver_id in bindings.DRIVER_IDS
    }
    unavailable = {
        (binding.browser_id, binding.driver_id)
        for binding in catalog.bindings
        if binding.route is None
    }
    assert unavailable == {
        ("obscura", "chrome_devtools_mcp"),
        ("obscura", "selenium"),
    }
    assert all(binding.fallback_allowed is False for binding in catalog.bindings)


def test_require_binding_returns_normalized_immutable_value():
    catalog = bindings.load_catalog()
    binding = catalog.require_binding("chrome", "playwright")
    assert isinstance(binding, bindings.Binding)
    assert binding.binding_id == "chrome__playwright"
    assert binding.route is not None
    assert binding.route.route_id == "playwright_cdp"
    assert isinstance(binding.route.ordered_hops, tuple)
    assert binding.browser_pin.key == "chrome"
    assert binding.driver_pin.key == "playwright-core"
    with pytest.raises(dataclasses.FrozenInstanceError):
        binding.browser_id = "moli"  # type: ignore[misc]
    with pytest.raises(TypeError):
        catalog.routes["other"] = binding.route  # type: ignore[index]


def test_require_binding_rejects_unknown_browser_and_driver():
    catalog = bindings.load_catalog()
    with pytest.raises(bindings.CatalogError, match="unknown browser_id"):
        catalog.require_binding("firefox", "playwright")
    with pytest.raises(bindings.CatalogError, match="unknown driver_id"):
        catalog.require_binding("chrome", "raw_cdp")


def test_selenium_golden_routes():
    catalog = bindings.load_catalog()
    moli = catalog.require_binding("moli", "selenium")
    chrome = catalog.require_binding("chrome", "selenium")
    lightpanda = catalog.require_binding("lightpanda", "selenium")
    obscura = catalog.require_binding("obscura", "selenium")
    assert moli.route and moli.route.route_id == "native_webdriver"
    assert moli.route.client_protocol == "webdriver_classic"
    assert moli.route.client_endpoint_kind == "webdriver_classic_http"
    assert moli.route.browser_endpoint_kind == "webdriver_classic_http"
    assert moli.bridge_pins == ()
    for binding in (chrome, lightpanda):
        assert binding.route and binding.route.route_id == "chromedriver_cdp"
        assert binding.route.client_endpoint_kind == "chromedriver_http"
        assert binding.route.browser_endpoint_kind == "cdp_http_port"
        assert [pin.key for pin in binding.bridge_pins] == ["chromedriver"]
    assert obscura.route is None
    assert "no WebDriver server" in obscura.unavailable_reason
    assert obscura.bridge_pins == ()


def test_obscura_pydoll_uses_audited_bootstrap_route():
    binding = bindings.load_catalog().require_binding("obscura", "pydoll")
    assert binding.route is not None
    assert binding.route.route_id == "pydoll_obscura_cdp"
    assert (
        binding.route.discovery.client.probe
        == "Pydoll 2.23.1 connection handler then Target.createTarget and "
        "Target.attachToTarget(flatten=true)"
    )
    assert binding.route.client_protocol == "pydoll_api_over_cdp"
    assert binding.route.lifecycle.bridge_owner == "none"


def test_mcp_live_identity_is_ua_based_but_agent_browser_uses_explicit_port():
    catalog = bindings.load_catalog()
    mcp = catalog.require_binding("chrome", "chrome_devtools_mcp").route
    assert mcp is not None
    assert any(
        "navigator.userAgent" in assertion.actual_path
        for assertion in mcp.identity.live_transport_assertions
    )
    agent_browser = catalog.require_binding("chrome", "agent_browser").route
    assert agent_browser is not None
    assert [
        (
            assertion.actual_path,
            assertion.expected_ref,
            assertion.condition,
        )
        for assertion in agent_browser.identity.live_transport_assertions
    ] == [("get.cdp_url", "browser_ws", "always")]


def test_native_webdriver_identity_is_machine_readable():
    route = bindings.load_catalog().require_binding("moli", "selenium").route
    assert route is not None
    assertions = {
        assertion.actual_path: assertion
        for assertion in route.identity.live_transport_assertions
    }
    assert assertions["capabilities.browserName"].expected_literal == "moli"
    assert assertions["capabilities.browserName"].operator == "equals"
    assert assertions["capabilities.browserVersion"].expected_ref == "expect_product_live"


def test_bridged_routes_expose_unambiguous_endpoint_and_readiness_scopes():
    catalog = bindings.load_catalog()
    cases = {
        "chrome_devtools_mcp": ("mcp_stdio", "cdp_browser_websocket", "mcp_initialize"),
        "agent_browser": ("isolated_daemon_session", "cdp_http_port", "agent_browser_connect"),
    }
    for driver_id, expected in cases.items():
        route = catalog.require_binding("chrome", driver_id).route
        assert route is not None
        assert (
            route.client_endpoint_kind,
            route.browser_endpoint_kind,
            route.discovery.client.kind,
        ) == expected
        assert route.discovery.client.endpoint_kind == route.client_endpoint_kind

    native = catalog.require_binding("moli", "selenium").route
    chromedriver = catalog.require_binding("chrome", "selenium").route
    assert native is not None and chromedriver is not None
    assert native.discovery.client.kind == "webdriver_session"
    assert native.discovery.client.endpoint_kind == "webdriver_classic_http"
    assert chromedriver.discovery.client.kind == "chromedriver_service"
    assert chromedriver.discovery.client.endpoint_kind == "chromedriver_http"
    assert all(
        route.discovery.browser.kind == "http_json_version"
        and route.discovery.browser.endpoint_kind == "cdp_http_discovery"
        for route in catalog.routes.values()
    )


def test_mcp_route_includes_its_internally_bundled_puppeteer_layer():
    binding = bindings.load_catalog().require_binding("chrome", "chrome_devtools_mcp")
    assert binding.route is not None
    components = [
        component
        for hop in binding.route.ordered_hops
        for component in (hop.source, hop.destination)
    ]
    assert "chrome_devtools_mcp_bundled_puppeteer_core" in components
    assert [pin.key for pin in binding.bridge_pins] == ["chrome-devtools-mcp"]
    assert binding.driver_pin.key == "chrome-devtools-mcp"


def test_playwright_fallback_models_expected_contains_actual_direction():
    route = bindings.load_catalog().require_binding("chrome", "playwright").route
    assert route is not None
    assertion = route.identity.live_transport_assertions[0]
    assert assertion.fallback_actual_path == "framework.browser.version"
    assert assertion.fallback_operator == "is_contained_by"
    assert assertion.expected_ref == "expect_product_live"


def test_repository_registry_pin_and_schema_parity():
    catalog = bindings.load_catalog()
    bindings.validate_repository_parity(catalog)


def test_obscura_unavailable_routes_resolve_before_driver_launch():
    tasks = [
        SimpleNamespace(driver={"kind": "mcp_chrome_devtools"}),
        SimpleNamespace(driver={"kind": "webdriver_selenium"}),
    ]
    resolved = runner_run.resolve_unavailable_runtime_bindings(
        tasks, ["moli", "obscura"]
    )
    assert set(resolved) == {
        ("obscura", "chrome_devtools_mcp"),
        ("obscura", "selenium"),
    }
    for payload in resolved.values():
        assert payload["classification"] == "protocol_incompatibility"
        assert payload["fallback_allowed"] is False
        assert payload["verified"] is False
        assert payload["route"] is None


def test_obscura_only_selenium_does_not_require_installed_client(monkeypatch):
    task = SimpleNamespace(driver={"kind": "webdriver_selenium"})
    monkeypatch.setattr(
        runner_run,
        "_validate_installed_selenium_client",
        lambda _pins: (_ for _ in ()).throw(
            AssertionError("unavailable route must not load Selenium")
        ),
    )
    resolved = runner_run.resolve_selenium_runtime_bindings(
        [task], ["obscura"]
    )
    assert resolved["obscura"]["route"] is None
    assert resolved["obscura"]["classification"] == "protocol_incompatibility"


def test_selenium_runtime_binding_uses_effective_active_set_pins(monkeypatch):
    task = SimpleNamespace(driver={"kind": "webdriver_selenium"})
    active_driver_pins = {
        "selenium": {
            "version": "4.46.0",
            "pip_package": "selenium",
        },
        "chromedriver": {
            "version": "151.0.7922.47",
            "binary_path": "build_artifacts/chromedriver/bin/chromedriver",
            "sha256_12": "f9bf92b10a62",
        },
    }
    monkeypatch.setattr(
        runner_run,
        "effective_harness_pins",
        lambda: {"drivers": active_driver_pins},
    )
    monkeypatch.setattr(
        runner_run,
        "_validate_installed_selenium_client",
        lambda pins: pins is active_driver_pins
        or (_ for _ in ()).throw(AssertionError("stale harness pins")),
    )
    monkeypatch.setattr(
        runner_run,
        "normalize_selenium_binding",
        lambda binding, pins: {
            "binding_id": binding.binding_id,
            "chromedriver_version": pins["chromedriver"]["version"],
        },
    )

    resolved = runner_run.resolve_selenium_runtime_bindings(
        [task], ["lightpanda"]
    )

    assert resolved["lightpanda"] == {
        "binding_id": "lightpanda__selenium",
        "chromedriver_version": "151.0.7922.47",
    }


def test_schema_closes_every_typed_object():
    schema = json.loads(bindings.DEFAULT_SCHEMA_PATH.read_text())

    def walk(value):
        if isinstance(value, dict):
            if value.get("type") == "object":
                assert value.get("additionalProperties") is False
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(schema)


def test_json_schema_rejects_boolean_schema_version(tmp_path):
    def mutate(raw):
        raw["schema_version"] = True

    with pytest.raises(bindings.CatalogError, match="schema_version: value does not equal schema const"):
        _load_mutated(tmp_path, mutate)


def test_dangling_schema_ref_is_rejected(tmp_path):
    schema = json.loads(bindings.DEFAULT_SCHEMA_PATH.read_text())
    schema["$defs"]["reference"]["properties"]["ref_id"]["$ref"] = "#/$defs/missing"
    path = tmp_path / "schema.json"
    path.write_text(json.dumps(schema))
    with pytest.raises(bindings.CatalogError, match="schema \\$ref.*dangling"):
        bindings.load_catalog(schema_path=path)


def test_cyclic_schema_ref_is_rejected(tmp_path):
    schema = json.loads(bindings.DEFAULT_SCHEMA_PATH.read_text())
    schema["$defs"]["nonEmptyString"] = {"$ref": "#/$defs/nonEmptyString"}
    path = tmp_path / "schema.json"
    path.write_text(json.dumps(schema))
    with pytest.raises(bindings.CatalogError, match="schema \\$ref cycle"):
        bindings.load_catalog(schema_path=path)


def test_cyclic_schema_ref_through_non_defs_pointer_is_rejected(tmp_path):
    schema = json.loads(bindings.DEFAULT_SCHEMA_PATH.read_text())
    schema["properties"]["schema_version"] = {"$ref": "#/$defs/cycle_a"}
    schema["$defs"]["cycle_a"] = {"$ref": "#/properties/schema_version"}
    path = tmp_path / "schema.json"
    path.write_text(json.dumps(schema))
    with pytest.raises(bindings.CatalogError, match="schema \\$ref cycle"):
        bindings.load_catalog(schema_path=path)


def test_acyclic_schema_ref_through_non_defs_pointer_is_accepted(tmp_path):
    schema = json.loads(bindings.DEFAULT_SCHEMA_PATH.read_text())
    schema["properties"]["schema_version"] = {"$ref": "#/$defs/schemaVersion"}
    schema["$defs"]["schemaVersion"] = {"const": 1}
    path = tmp_path / "schema.json"
    path.write_text(json.dumps(schema))
    assert bindings.load_catalog(schema_path=path).schema_version == 1


def test_non_string_schema_ref_is_rejected_readably(tmp_path):
    schema = json.loads(bindings.DEFAULT_SCHEMA_PATH.read_text())
    schema["properties"]["schema_version"] = {"$ref": 1}
    path = tmp_path / "schema.json"
    path.write_text(json.dumps(schema))
    with pytest.raises(bindings.CatalogError, match="schema.properties.schema_version.\\$ref must be a string"):
        bindings.load_catalog(schema_path=path)


@pytest.mark.parametrize(
    "literal_rule",
    [
        {"const": {"$ref": "#/properties/unused"}},
        {"enum": [{"$ref": "#/properties/unused"}]},
    ],
)
def test_ref_shaped_schema_literals_are_not_graph_edges(tmp_path, literal_rule):
    schema = json.loads(bindings.DEFAULT_SCHEMA_PATH.read_text())
    schema["properties"]["unused"] = literal_rule
    path = tmp_path / "schema.json"
    path.write_text(json.dumps(schema))
    assert bindings.load_catalog(schema_path=path).schema_version == 1


def test_schema_ref_cycle_discovered_through_referenced_literal_is_rejected(tmp_path):
    schema = json.loads(bindings.DEFAULT_SCHEMA_PATH.read_text())
    schema["properties"]["schema_version"] = {"$ref": "#/properties/a/const"}
    schema["properties"]["a"] = {"const": {"$ref": "#/properties/b/const"}}
    schema["properties"]["b"] = {"const": {"$ref": "#/properties/a/const"}}
    path = tmp_path / "schema.json"
    path.write_text(json.dumps(schema))
    with pytest.raises(bindings.CatalogError, match="schema \\$ref cycle"):
        bindings.load_catalog(schema_path=path)


@pytest.mark.parametrize(
    ("target", "message"),
    [
        ({"$ref": 1}, "target.\\$ref must be a string"),
        ({"allOf": []}, "target uses unsupported JSON Schema keyword"),
    ],
)
def test_referenced_literal_target_is_validated_as_schema(tmp_path, target, message):
    schema = json.loads(bindings.DEFAULT_SCHEMA_PATH.read_text())
    schema["properties"]["schema_version"] = {"$ref": "#/properties/target/const"}
    schema["properties"]["target"] = {"const": target}
    path = tmp_path / "schema.json"
    path.write_text(json.dumps(schema))
    with pytest.raises(bindings.CatalogError, match=message):
        bindings.load_catalog(schema_path=path)


def test_schema_valued_additional_properties_is_rejected_fail_closed(tmp_path):
    schema = json.loads(bindings.DEFAULT_SCHEMA_PATH.read_text())
    schema["additionalProperties"] = {"$ref": "#/$defs/missing"}
    path = tmp_path / "schema.json"
    path.write_text(json.dumps(schema))
    with pytest.raises(bindings.CatalogError, match="additionalProperties must be a boolean"):
        bindings.load_catalog(schema_path=path)


def test_long_acyclic_schema_ref_chain_does_not_overflow(tmp_path):
    schema = json.loads(bindings.DEFAULT_SCHEMA_PATH.read_text())
    chain_length = 1200
    schema["properties"]["schema_version"] = {"$ref": "#/$defs/chain0"}
    schema["$defs"].update(
        {
            f"chain{idx}": (
                {"$ref": f"#/$defs/chain{idx + 1}"}
                if idx + 1 < chain_length
                else {"const": 1}
            )
            for idx in range(chain_length)
        }
    )
    path = tmp_path / "schema.json"
    path.write_text(json.dumps(schema))
    assert bindings.load_catalog(schema_path=path).schema_version == 1


def test_unsupported_schema_keyword_is_rejected(tmp_path):
    schema = json.loads(bindings.DEFAULT_SCHEMA_PATH.read_text())
    schema["allOf"] = []
    path = tmp_path / "schema.json"
    path.write_text(json.dumps(schema))
    with pytest.raises(bindings.CatalogError, match="unsupported JSON Schema keyword"):
        bindings.load_catalog(schema_path=path)


def test_import_does_not_import_runtime_monolith():
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import runner.bindings; "
            "assert 'runner.run' not in sys.modules; "
            "assert 'runner.scenario' not in sys.modules",
        ],
        cwd=bindings.BENCH_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_missing_pair_is_rejected(tmp_path):
    with pytest.raises(bindings.CatalogError, match="expected at least 52 items"):
        _load_mutated(tmp_path, lambda raw: raw["bindings"].pop())


def test_duplicate_pair_is_rejected(tmp_path):
    def mutate(raw):
        raw["bindings"].append(copy.deepcopy(raw["bindings"][0]))

    with pytest.raises(bindings.CatalogError, match="expected at most 52 items"):
        _load_mutated(tmp_path, mutate)


def test_unknown_browser_is_rejected(tmp_path):
    def mutate(raw):
        row = raw["bindings"][0]
        row["browser_id"] = "firefox"
        row["binding_id"] = "firefox__playwright"

    with pytest.raises(bindings.CatalogError, match="browser_id: value is not in schema enum"):
        _load_mutated(tmp_path, mutate)


def test_unknown_driver_is_rejected(tmp_path):
    def mutate(raw):
        row = raw["bindings"][0]
        row["driver_id"] = "raw_cdp"
        row["binding_id"] = "chrome__raw_cdp"

    with pytest.raises(bindings.CatalogError, match="driver_id: value is not in schema enum"):
        _load_mutated(tmp_path, mutate)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("route_ref", "missing_route", "dangling reference `missing_route`"),
        ("driver_pin_ref", "driver.missing", "dangling reference `driver.missing`"),
        ("browser_pin_ref", "browser.missing", "dangling reference `browser.missing`"),
    ],
)
def test_dangling_references_are_rejected(tmp_path, field, value, message):
    def mutate(raw):
        raw["bindings"][0][field] = value

    with pytest.raises(bindings.CatalogError, match=message):
        _load_mutated(tmp_path, mutate)


def test_dangling_bridge_reference_is_rejected(tmp_path):
    def mutate(raw):
        raw["bindings"][0]["bridge_pin_refs"] = ["bridge.missing"]

    with pytest.raises(bindings.CatalogError, match="dangling reference `bridge.missing`"):
        _load_mutated(tmp_path, mutate)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda raw: raw.update({"extra": True}),
        lambda raw: raw["bindings"][0].update({"extra": True}),
        lambda raw: raw["routes"][0]["identity"].update({"extra": True}),
        lambda raw: raw["routes"][0]["ordered_hops"][0].update({"extra": True}),
    ],
)
def test_extra_fields_are_rejected_at_every_closed_level(tmp_path, mutate):
    with pytest.raises(bindings.CatalogError, match="unknown field"):
        _load_mutated(tmp_path, mutate)


@pytest.mark.parametrize("field", sorted(bindings.FORBIDDEN_RESULT_FIELDS))
def test_observed_compatibility_fields_are_forbidden_recursively(tmp_path, field):
    def mutate(raw):
        raw["bindings"][0][field] = True

    with pytest.raises(bindings.CatalogError, match="forbidden observed-compatibility"):
        _load_mutated(tmp_path, mutate)


def test_fallback_must_be_false(tmp_path):
    def mutate(raw):
        raw["bindings"][0]["fallback_allowed"] = True

    with pytest.raises(bindings.CatalogError, match="fallback_allowed: value does not equal schema const"):
        _load_mutated(tmp_path, mutate)


def test_unavailable_binding_requires_explicit_reason(tmp_path):
    def mutate(raw):
        raw["bindings"][0].pop("route_ref")

    with pytest.raises(bindings.CatalogError, match="expected exactly one matching oneOf branch"):
        _load_mutated(tmp_path, mutate)


def test_reference_must_resolve_to_correct_browser_pin(tmp_path):
    def mutate(raw):
        raw["bindings"][0]["browser_pin_ref"] = "browser.moli"

    with pytest.raises(bindings.CatalogError, match="must resolve to ENGINE_DEFS.chrome"):
        _load_mutated(tmp_path, mutate)


def test_driver_reference_must_resolve_to_its_own_pin(tmp_path):
    def mutate(raw):
        raw["bindings"][0]["driver_pin_ref"] = "driver.puppeteer"

    with pytest.raises(bindings.CatalogError, match="driver_pin_ref must be `driver.playwright`"):
        _load_mutated(tmp_path, mutate)


def test_bridge_reference_must_match_route_shape(tmp_path):
    def mutate(raw):
        raw["bindings"][0]["bridge_pin_refs"] = ["bridge.chromedriver"]

    with pytest.raises(bindings.CatalogError, match="does not occur in route"):
        _load_mutated(tmp_path, mutate)


def test_bridge_reference_is_required_by_route_lifecycle(tmp_path):
    def mutate(raw):
        row = next(
            binding
            for binding in raw["bindings"]
            if binding["driver_id"] == "chrome_devtools_mcp"
        )
        row["bridge_pin_refs"] = []

    with pytest.raises(bindings.CatalogError, match="must be non-empty"):
        _load_mutated(tmp_path, mutate)


def test_bridge_reference_must_identify_a_component_in_the_route(tmp_path):
    def mutate(raw):
        row = next(
            binding
            for binding in raw["bindings"]
            if binding["driver_id"] == "chrome_devtools_mcp"
        )
        row["bridge_pin_refs"] = ["bridge.agent_browser_daemon"]

    with pytest.raises(bindings.CatalogError, match="does not occur in route"):
        _load_mutated(tmp_path, mutate)


def test_duplicate_bridge_references_are_rejected(tmp_path):
    def mutate(raw):
        row = next(
            binding
            for binding in raw["bindings"]
            if binding["driver_id"] == "chrome_devtools_mcp"
        )
        row["bridge_pin_refs"].append(row["bridge_pin_refs"][0])

    with pytest.raises(bindings.CatalogError, match="must be unique"):
        _load_mutated(tmp_path, mutate)


def test_driver_alias_cannot_point_to_a_different_existing_pin(tmp_path):
    def mutate(raw):
        reference = next(ref for ref in raw["references"] if ref["ref_id"] == "driver.playwright")
        reference["key"] = "puppeteer-core"

    with pytest.raises(bindings.CatalogError, match="does not identify driver `playwright`"):
        _load_mutated(tmp_path, mutate)


def test_route_vocabulary_rejects_launch_mode(tmp_path):
    def mutate(raw):
        raw["routes"][0]["connect_mode"] = "launch_new"

    with pytest.raises(bindings.CatalogError, match="connect_mode: value is not in schema enum"):
        _load_mutated(tmp_path, mutate)


def test_disconnected_hop_chain_is_rejected(tmp_path):
    def mutate(raw):
        raw["routes"][0]["ordered_hops"][0]["to"] = "wrong_component"

    with pytest.raises(bindings.CatalogError, match="ordered_hops is disconnected"):
        _load_mutated(tmp_path, mutate)


def test_browser_endpoint_must_match_final_hop(tmp_path):
    def mutate(raw):
        raw["routes"][0]["browser_endpoint_kind"] = "cdp_http_port"

    with pytest.raises(bindings.CatalogError, match="must equal the final hop endpoint_kind"):
        _load_mutated(tmp_path, mutate)


def test_client_readiness_endpoint_must_match_client_endpoint(tmp_path):
    def mutate(raw):
        raw["routes"][0]["discovery"]["client"]["endpoint_kind"] = "mcp_stdio"

    with pytest.raises(bindings.CatalogError, match="must equal client_endpoint_kind"):
        _load_mutated(tmp_path, mutate)


def test_golden_routes_are_checked_by_repository_parity(tmp_path):
    def mutate(raw):
        row = next(
            binding
            for binding in raw["bindings"]
            if binding["browser_id"] == "moli" and binding["driver_id"] == "selenium"
        )
        row["route_ref"] = "chromedriver_cdp"
        row["bridge_pin_refs"] = ["bridge.chromedriver"]

    catalog = _load_mutated(tmp_path, mutate)
    with pytest.raises(bindings.CatalogError, match="golden route \\(moli, selenium\\)"):
        bindings.validate_repository_parity(catalog)


def test_render_is_deterministic_and_committed_output_is_in_sync():
    catalog = bindings.load_catalog()
    rendered = bindings.render_catalog(catalog)
    assert rendered == bindings.render_catalog(catalog)
    assert "Browser\\|Product" in rendered
    route_rows = rendered.split("## Route definitions", 1)[1].split("## chrome", 1)[0]
    for line in route_rows.splitlines():
        if line.startswith("| `"):
            assert len(re.findall(r"(?<!\\)\|", line)) == 10
    bindings.write_rendered(catalog, check=True)


def test_render_check_detects_manual_drift(tmp_path):
    catalog = bindings.load_catalog()
    path = tmp_path / "matrix.md"
    path.write_text("manually edited\n")
    with pytest.raises(bindings.CatalogError, match="generated matrix drift"):
        bindings.write_rendered(catalog, path, check=True)
