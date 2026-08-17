# Experimental Kitesurf framework-driver probe

- Endpoint: `wss://kitesurf.cloudflare.app/devtools/browser`
- Fixture origin: `https://<dynamic-fixture-origin>`
- Identity: `Chrome/145.0.0.0`, CDP `1.3`, revision `@kitesurf`
- Concurrency: 1; each attempt uses a fresh public browser WebSocket.
- Exploratory only; not formal-score eligible.

## Driver summary

| Driver | Tasks | Attempts | Statuses | Latency |
|---|---:|---:|---|---|
| `playwright` | 16 | 16 | `{"fail": 6, "pass": 9, "timeout": 1}` | p50=14424 ms; p95=22282 ms; max=30036 ms |

## Non-pass attempts

| Driver | Task | Attempt | Status | Failure |
|---|---|---:|---|---|
| `playwright` | `sc_wait_enabled_then_click__pw` | 1 | `fail` | answer did not match server-side expectation |
| `playwright` | `v3_pw_act_check_select` | 1 | `fail` | script-reported checks failed |
| `playwright` | `v3_pw_act_click_counter` | 1 | `fail` | script-reported checks failed |
| `playwright` | `v3_pw_cal_compute_product` | 1 | `fail` | script-reported checks failed |
| `playwright` | `v3_pw_cal_counter_clicks` | 1 | `fail` | script-reported checks failed |
| `playwright` | `v3_pw_cal_form_checksum` | 1 | `fail` | script-reported checks failed |
| `playwright` | `v3_pw_cal_frame_token` | 1 | `timeout` | Command '['node', '<repo>/runner/scripts/framework_probe.js']' timed out after 30.0 seconds |
