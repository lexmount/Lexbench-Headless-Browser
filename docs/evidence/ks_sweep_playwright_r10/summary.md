# Experimental Kitesurf framework-driver probe

- Endpoint: `wss://kitesurf.cloudflare.app/devtools/browser`
- Fixture origin: `https://<dynamic-fixture-origin>`
- Identity: `Chrome/145.0.0.0`, CDP `1.3`, revision `@kitesurf`
- Concurrency: 1; each attempt uses a fresh public browser WebSocket.
- Exploratory only; not formal-score eligible.

## Driver summary

| Driver | Tasks | Attempts | Statuses | Latency |
|---|---:|---:|---|---|
| `playwright` | 3 | 3 | `{"fail": 1, "pass": 1, "timeout": 1}` | p50=29473 ms; p95=30031 ms; max=30031 ms |

## Non-pass attempts

| Driver | Task | Attempt | Status | Failure |
|---|---|---:|---|---|
| `playwright` | `sc_app_login_greets__pw` | 1 | `fail` | answer did not match server-side expectation |
| `playwright` | `sc_app_logout_locks__pw` | 1 | `timeout` | Command '['node', '<repo>/runner/scripts/framework_probe.js']' timed out after 30.0 seconds |
