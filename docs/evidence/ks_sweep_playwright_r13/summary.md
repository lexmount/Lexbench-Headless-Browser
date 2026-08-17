# Experimental Kitesurf framework-driver probe

- Endpoint: `wss://kitesurf.cloudflare.app/devtools/browser`
- Fixture origin: `https://<dynamic-fixture-origin>`
- Identity: `Chrome/145.0.0.0`, CDP `1.3`, revision `@kitesurf`
- Concurrency: 1; each attempt uses a fresh public browser WebSocket.
- Exploratory only; not formal-score eligible.

## Driver summary

| Driver | Tasks | Attempts | Statuses | Latency |
|---|---:|---:|---|---|
| `playwright` | 1 | 1 | `{"infra": 1}` | p50=12853 ms; p95=12853 ms; max=12853 ms |

## Non-pass attempts

| Driver | Task | Attempt | Status | Failure |
|---|---|---:|---|---|
| `playwright` | `sc_computed_style_breadth__pw` | 1 | `infra` | framework target cleanup was not confirmed: {"backend":"framework_page.close","required":true,"confirmed":false,"same_connection_as_task":true,"error":"async crash prevented confirmed target cleanup"} |
