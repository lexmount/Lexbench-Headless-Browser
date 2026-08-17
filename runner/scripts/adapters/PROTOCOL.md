# Scenario adapter protocol (`abb_scenario_adapter/1`)

This protocol supports the scenario × driver × engine matrix.

A **scenario adapter** is a small program — one per driver, in the driver's own
language — that replays a driver-agnostic scenario (the op vocabulary defined by
`runner/scripts/framework_probe.js` and `tasks/scenarios/README.md`) against the
engine under test using that driver's real client library. The runner talks to
every adapter through the same language-neutral contract:

- **stdin**: one JSON object (the *payload*), then EOF.
- **stdout**: one JSON object (the *result*). Nothing else may be written to
  stdout; diagnostics go to stderr (the runner archives both).
- **exit code**: 0 unless the adapter itself crashed. Scenario failures are
  reported *in* the result, not via the exit code.

`framework_probe.js` predates this protocol and receives the same information
via environment variables; the contract below is the forward path for every new
driver (Node, Python, Go, Rust, Ruby).

## Payload (stdin)

```json
{
  "protocol": "abb_scenario_adapter/1",
  "driver_kind": "thin_chrome_remote_interface",
  "driver_key": "chrome_remote_interface",
  "browser_ws": "ws://127.0.0.1:9224/devtools/browser/<uuid>",
  "cdp_port": 9224,
  "remote_cdp": false,
  "expected_remote_identity": null,
  "expect_product": "Chrome/150.0.7871.49",
  "expect_ua": "Mozilla/5.0 ...",
  "expect_product_live": "Chrome/150.0.7871.49",
  "transport_policy": null,
  "task_url": "http://127.0.0.1:8907/l1/core",
  "steps": [{"op": "new_page"}, {"op": "goto", "url": "{fixture_url}"}],
  "checks": [{"kind": "step_ok", "label": "goto_ok", "step": 1}],
  "connect_timeout_ms": 15000,
  "action_timeout_ms": 8000,
  "task_timeout_ms": 30000,
  "artifact_dir": "/abs/path/to/attempt/artifacts",
  "task_id": "sc_nav_title_read__cri",
  "run_id": "…", "engine": "chrome", "attempt": 1, "seed": "official20260709"
}
```

`binding` is an optional, backward-compatible payload field for adapters in
general and is mandatory for `driver_key: "selenium"`. The runner resolves it
from the Browser × Driver Binding Catalog before starting browser workers; an
adapter must never load the catalog itself or infer a replacement route:

```json
{
  "binding": {
    "binding_id": "chrome__selenium",
    "browser_id": "chrome",
    "driver_id": "selenium",
    "route": {
      "route_id": "chromedriver_cdp",
      "client_protocol": "webdriver_classic",
      "client_endpoint_kind": "chromedriver_http",
      "browser_endpoint_kind": "cdp_http_port",
      "connect_mode": "attach_existing",
      "provider": "chromedriver",
      "ordered_hops": [
        {"from": "scenario_adapter", "to": "selenium_client", "protocol": "selenium_api", "transport": "in_process", "endpoint_kind": "library_api"},
        {"from": "selenium_client", "to": "chromedriver", "protocol": "webdriver_classic", "transport": "http", "endpoint_kind": "chromedriver_http"},
        {"from": "chromedriver", "to": "browser", "protocol": "cdp", "transport": "debugger_address", "endpoint_kind": "cdp_http_port"}
      ],
      "lifecycle": {
        "browser_owner": "runner_browser_manager",
        "bridge_owner": "adapter_per_attempt_child",
        "adapter_owner": "runner_per_attempt_subprocess"
      },
      "discovery": {
        "browser": {"kind": "http_json_version", "endpoint_kind": "cdp_http_discovery", "probe": "GET /json/version", "readiness_owner": "runner_browser_manager"},
        "client": {"kind": "chromedriver_service", "endpoint_kind": "chromedriver_http", "probe": "start Selenium Service on temporary port then POST /session", "readiness_owner": "adapter_per_attempt"}
      },
      "identity": {
        "http_assertions": ["normalized Catalog assertions"],
        "live_transport_assertions": ["normalized Catalog assertions"]
      }
    },
    "pins": {
      "browser": {"ref_id": "browser.chrome", "key": "chrome"},
      "driver": {"ref_id": "driver.selenium", "key": "selenium", "metadata": {"version": "4.46.0", "pip_package": "selenium"}},
      "bridges": [{"ref_id": "bridge.chromedriver", "key": "chromedriver", "metadata": {"version": "…", "binary_path": "…", "sha256_12": "…"}, "executable": "/validated/repo/path"}]
    },
    "fallback_allowed": false
  }
}
```

The exact assertion objects are the Catalog records (mechanism, actual path,
operator, expected ref or literal, and condition); the abbreviated arrays
above only keep the example readable. `transport_policy` remains legacy task
metadata and must not influence Selenium route selection.

`task_timeout_ms` is the runner's hard kill budget for the whole attempt. An
adapter whose per-op waits could stack past it MUST clamp each op's wait to
the remaining budget (minus a small reserve) and fail the op cleanly instead —
being killed mid-run misclassifies an engine-capability fail as infra.

`steps` / `checks` arrive with the runner-side placeholders (`{seed}`,
`{session}`, `{fixture_base_url}`, `{artifact_dir}`) already substituted. The
adapter substitutes the page-level placeholders itself, exactly like
`framework_probe.js`: `{fixture_url}` → `task_url`, `{fixture_origin}` /
`{fixture_host}` → derived from `task_url`, `{artifact_dir}` → `artifact_dir`.

## Result (stdout)

Identical contract to `framework_probe.js`:

```json
{
  "ok": true,
  "answer": "3/3 checks",
  "observations": {
    "checks": [{"name": "driver_connect", "status": "pass", "evidence": "…"}],
    "saved": {"heading": "CDP Core Fixture"},
    "binding": {"verified": true, "http_product": "…", "live_product": "…"},
    "target_cleanup": {"confirmed": true, "same_connection_as_task": true},
    "isolation_restored": true,
    "failure_class": "cdp_semantic"
  },
  "metrics": {"cdp_call_count": 12, "cdp_error_count": 0, "ws_disconnect_count": 0}
}
```

- `ok: false` + `error: {class, message}` is reserved for *harness* problems
  (invalid payload, binding-gate violation, adapter crash): it grades as infra,
  never as an engine result.
- After the mandatory binding gate succeeds, an engine that cannot execute an
  op is a *benchmark result*: report `ok: true` with the relevant checks failed
  and `observations.failure_class` set (default `cdp_semantic`), so the attempt
  grades as a normal fail attributable to the engine. Before a remote binding
  gate succeeds, connect/init failure is not attributable to that product and
  must remain infra; recognized network evidence may still be counted
  separately as a transport outcome.
- `observations.checks` is the grading surface (`grader.kind =
  inline_assertions`): the attempt passes iff every check row has
  `status: "pass"`.
- A remote-CDP adapter must complete owned page/target cleanup before writing
  stdout. A successful result requires `observations.target_cleanup.confirmed
  == true`, `same_connection_as_task == true`, and
  `observations.isolation_restored == true`. A request whose create response
  was lost is ambiguous, not target-free; cleanup failure must turn the result
  into infra so the experiment can stop before another attempt. High-level
  close helpers are not confirmation when they can swallow destruction-event
  deadlines; adapters such as Rod issue `Target.closeTarget` on the exact root
  task connection and require the response's explicit `success=true`.

## Mandatory binding gate (no-fallback rule)

Every adapter MUST verify, per attempt, before running any scenario step:

1. **Endpoint identity**: `GET http://127.0.0.1:{cdp_port}/json/version` must
   report `Browser == expect_product` (and `User-Agent == expect_ua` when
   non-empty), and its `webSocketDebuggerUrl`, when present, must equal
   `browser_ws`.
2. **Live-transport identity**: through the driver's own connected transport,
   CDP-backed adapters use `Browser.getVersion` and require `product ==
   expect_product_live`. A native Selenium route uses the binding's WebDriver
   capability assertions (currently `browserName == "moli"` and
   `browserVersion == expect_product_live`).

For `remote_cdp: true`, the runner additionally supplies a non-empty
`expected_remote_identity` object with `product`, `protocolVersion`, and
`revision`. The adapter must obtain all three through the exact client
connection used for the task and report this evidence as
`observations.binding.expected`, `.actual`, `.compared_fields`,
`.same_connection_as_task`, and `.reconnect_allowed`. Product-only or
reconnected identity evidence is an infra exclusion. The runner independently
enforces this contract before accepting an adapter's `verified: true` claim,
and converts any otherwise gradable remote output without this complete claim
to `binding_unverified` infra while preserving its `connect_error` evidence.

A mismatch is emitted as `ok: false` / `class: "script_error"` — refusing to
run is mandatory; falling back to another endpoint or launching a browser is
forbidden.

## Op & check vocabulary

The op vocabulary and the check evaluator family (`saved_equals`,
`saved_contains`, `saved_not_contains`, `saved_truthy`, `step_ok`,
`step_fails`, `file_nonempty`, `any_of`) are defined by
`runner/scripts/framework_probe.js`. An adapter implements the subset of ops
its bound scenarios use; an unknown op must fail that step with an `unknown op`
error (which surfaces via `step_ok` checks), never silently no-op. Step
results are stringified into `saved` under `save_as` exactly like the probe
(objects JSON-stringified, `undefined` → `"undefined"`, errors →
`"ERROR: <message>"`).

## Artifacts

Adapters append one JSON line per driver-level operation to
`{artifact_dir}/cdp.jsonl` (best effort — trace failures must not fail the
run).
