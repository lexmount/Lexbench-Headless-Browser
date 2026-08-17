# Experimental Kitesurf framework-driver probe

- Endpoint: `wss://kitesurf.cloudflare.app/devtools/browser`
- Fixture origin: `https://<dynamic-fixture-origin>`
- Identity: `Chrome/145.0.0.0`, CDP `1.3`, revision `@kitesurf`
- Concurrency: 1; each attempt uses a fresh public browser WebSocket.
- Exploratory only; not formal-score eligible.

## Driver summary

| Driver | Tasks | Attempts | Statuses | Latency |
|---|---:|---:|---|---|
| `playwright` | 12 | 12 | `{"fail": 9, "infra": 1, "pass": 2}` | p50=23845 ms; p95=24308 ms; max=25279 ms |

## Non-pass attempts

| Driver | Task | Attempt | Status | Failure |
|---|---|---:|---|---|
| `playwright` | `sc_net_auth_header_roundtrip__pw` | 1 | `fail` | answer did not match server-side expectation |
| `playwright` | `sc_net_extra_headers_echo__pw` | 1 | `fail` | answer did not match server-side expectation |
| `playwright` | `sc_net_mock_only_matching__pw` | 1 | `fail` | answer did not match server-side expectation |
| `playwright` | `sc_net_request_recorded__pw` | 1 | `fail` | answer did not match server-side expectation |
| `playwright` | `sc_net_route_abort_handled__pw` | 1 | `fail` | answer did not match server-side expectation |
| `playwright` | `sc_net_route_content_type__pw` | 1 | `fail` | answer did not match server-side expectation |
| `playwright` | `sc_net_route_mock_json__pw` | 1 | `fail` | answer did not match server-side expectation |
| `playwright` | `sc_net_route_passthrough__pw` | 1 | `fail` | answer did not match server-side expectation |
| `playwright` | `sc_net_route_rewrite_status__pw` | 1 | `fail` | answer did not match server-side expectation |
| `playwright` | `sc_obs_ax_snapshot_heading__pw` | 1 | `infra` | framework target cleanup was not confirmed: {"backend":"framework_page.close","required":true,"confirmed":false,"same_connection_as_task":true,"error":"async crash prevented confirmed target cleanup"} |
