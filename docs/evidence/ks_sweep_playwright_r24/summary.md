# Experimental Kitesurf framework-driver probe

- Endpoint: `wss://kitesurf.cloudflare.app/devtools/browser`
- Fixture origin: `https://<dynamic-fixture-origin>`
- Identity: `Chrome/145.0.0.0`, CDP `1.3`, revision `@kitesurf`
- Concurrency: 1; each attempt uses a fresh public browser WebSocket.
- Exploratory only; not formal-score eligible.

## Driver summary

| Driver | Tasks | Attempts | Statuses | Latency |
|---|---:|---:|---|---|
| `playwright` | 18 | 18 | `{"fail": 2, "infra": 1, "pass": 15}` | p50=8220 ms; p95=11885 ms; max=14630 ms |

## Non-pass attempts

| Driver | Task | Attempt | Status | Failure |
|---|---|---:|---|---|
| `playwright` | `v3_pw_cookie_roundtrip` | 1 | `fail` | script-reported checks failed |
| `playwright` | `v3_pw_eval_content_html` | 1 | `fail` | script-reported checks failed |
| `playwright` | `v3_pw_obs_ax_snapshot` | 1 | `infra` | framework target cleanup was not confirmed: {"backend":"framework_page.close","required":true,"confirmed":false,"same_connection_as_task":true,"error":"async crash prevented confirmed target cleanup"} |
