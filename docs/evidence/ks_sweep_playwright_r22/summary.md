# Experimental Kitesurf framework-driver probe

- Endpoint: `wss://kitesurf.cloudflare.app/devtools/browser`
- Fixture origin: `https://<dynamic-fixture-origin>`
- Identity: `Chrome/145.0.0.0`, CDP `1.3`, revision `@kitesurf`
- Concurrency: 1; each attempt uses a fresh public browser WebSocket.
- Exploratory only; not formal-score eligible.

## Driver summary

| Driver | Tasks | Attempts | Statuses | Latency |
|---|---:|---:|---|---|
| `playwright` | 15 | 15 | `{"fail": 5, "pass": 9, "timeout": 1}` | p50=11539 ms; p95=15853 ms; max=30030 ms |

## Non-pass attempts

| Driver | Task | Attempt | Status | Failure |
|---|---|---:|---|---|
| `playwright` | `sc_obs_console_error_captured__pw` | 1 | `fail` | answer did not match server-side expectation |
| `playwright` | `sc_obs_console_log_captured__pw` | 1 | `fail` | answer did not match server-side expectation |
| `playwright` | `sc_st_local_shared_same_origin__pw` | 1 | `fail` | answer did not match server-side expectation |
| `playwright` | `sc_st_local_survives_reload__pw` | 1 | `fail` | answer did not match server-side expectation |
| `playwright` | `sc_up_upload_filereader__pw` | 1 | `fail` | answer did not match server-side expectation |
| `playwright` | `sc_up_upload_server_checksum__pw` | 1 | `timeout` | Command '['node', '<repo>/runner/scripts/framework_probe.js']' timed out after 30.0 seconds |
