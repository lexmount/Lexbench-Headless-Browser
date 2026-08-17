# Experimental Kitesurf framework-driver probe

- Endpoint: `wss://kitesurf.cloudflare.app/devtools/browser`
- Fixture origin: `https://<dynamic-fixture-origin>`
- Identity: `Chrome/145.0.0.0`, CDP `1.3`, revision `@kitesurf`
- Concurrency: 1; each attempt uses a fresh public browser WebSocket.
- Exploratory only; not formal-score eligible.

## Driver summary

| Driver | Tasks | Attempts | Statuses | Latency |
|---|---:|---:|---|---|
| `playwright` | 28 | 28 | `{"fail": 7, "pass": 20, "timeout": 1}` | p50=11745 ms; p95=23957 ms; max=30048 ms |

## Non-pass attempts

| Driver | Task | Attempt | Status | Failure |
|---|---|---:|---|---|
| `playwright` | `sc_fr_sibling_isolation__pw` | 1 | `fail` | answer did not match server-side expectation |
| `playwright` | `sc_fr_static_child_read__pw` | 1 | `fail` | answer did not match server-side expectation |
| `playwright` | `sc_loc_attribute_selector_click__pw` | 1 | `fail` | answer did not match server-side expectation |
| `playwright` | `sc_loc_iframe_same_origin_read__pw` | 1 | `fail` | answer did not match server-side expectation |
| `playwright` | `sc_multi_tab_collect__pw` | 1 | `fail` | answer did not match server-side expectation |
| `playwright` | `sc_nav_push_state_back__pw` | 1 | `fail` | answer did not match server-side expectation |
| `playwright` | `sc_net_abort_subresource_image__pw` | 1 | `fail` | answer did not match server-side expectation |
| `playwright` | `sc_net_auth_challenge_then_pass__pw` | 1 | `timeout` | Command '['node', '<repo>/runner/scripts/framework_probe.js']' timed out after 30.0 seconds |
