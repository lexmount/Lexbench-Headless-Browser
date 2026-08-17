# Experimental Kitesurf framework-driver probe

- Endpoint: `wss://kitesurf.cloudflare.app/devtools/browser`
- Fixture origin: `https://<dynamic-fixture-origin>`
- Identity: `Chrome/145.0.0.0`, CDP `1.3`, revision `@kitesurf`
- Concurrency: 1; each attempt uses a fresh public browser WebSocket.
- Exploratory only; not formal-score eligible.

## Driver summary

| Driver | Tasks | Attempts | Statuses | Latency |
|---|---:|---:|---|---|
| `puppeteer` | 4 | 4 | `{"pass": 3, "timeout": 1}` | p50=21719 ms; p95=30040 ms; max=30040 ms |

## Non-pass attempts

| Driver | Task | Attempt | Status | Failure |
|---|---|---:|---|---|
| `puppeteer` | `sc_app_cart_persists_reload__pp` | 1 | `timeout` | Command '['node', '<repo>/runner/scripts/framework_probe.js']' timed out after 30.0 seconds |
