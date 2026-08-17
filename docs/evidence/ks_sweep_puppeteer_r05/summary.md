# Experimental Kitesurf framework-driver probe

- Endpoint: `wss://kitesurf.cloudflare.app/devtools/browser`
- Fixture origin: `https://<dynamic-fixture-origin>`
- Identity: `Chrome/145.0.0.0`, CDP `1.3`, revision `@kitesurf`
- Concurrency: 1; each attempt uses a fresh public browser WebSocket.
- Exploratory only; not formal-score eligible.

## Driver summary

| Driver | Tasks | Attempts | Statuses | Latency |
|---|---:|---:|---|---|
| `puppeteer` | 5 | 5 | `{"fail": 2, "pass": 2, "timeout": 1}` | p50=26258 ms; p95=30041 ms; max=30041 ms |

## Non-pass attempts

| Driver | Task | Attempt | Status | Failure |
|---|---|---:|---|---|
| `puppeteer` | `sc_app_login_greets__pp` | 1 | `fail` | answer did not match server-side expectation |
| `puppeteer` | `sc_app_logout_locks__pp` | 1 | `fail` | answer did not match server-side expectation |
| `puppeteer` | `sc_app_remove_item__pp` | 1 | `timeout` | Command '['node', '<repo>/runner/scripts/framework_probe.js']' timed out after 30.0 seconds |
