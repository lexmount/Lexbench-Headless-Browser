# Experimental Kitesurf framework-driver probe

- Endpoint: `wss://kitesurf.cloudflare.app/devtools/browser`
- Fixture origin: `https://<dynamic-fixture-origin>`
- Identity: `Chrome/145.0.0.0`, CDP `1.3`, revision `@kitesurf`
- Concurrency: 1; each attempt uses a fresh public browser WebSocket.
- Exploratory only; not formal-score eligible.

## Driver summary

| Driver | Tasks | Attempts | Statuses | Latency |
|---|---:|---:|---|---|
| `playwright` | 9 | 9 | `{"fail": 1, "infra": 1, "pass": 7}` | p50=11780 ms; p95=15042 ms; max=15042 ms |

## Non-pass attempts

| Driver | Task | Attempt | Status | Failure |
|---|---|---:|---|---|
| `playwright` | `sc_cs_ua_surface__pw` | 1 | `fail` | answer did not match server-side expectation |
| `playwright` | `sc_dl_download_checksum__pw` | 1 | `infra` | script exited with code 1 |
