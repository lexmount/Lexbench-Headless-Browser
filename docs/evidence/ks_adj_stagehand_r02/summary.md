# Experimental Kitesurf framework-driver probe

- Endpoint: `wss://kitesurf.cloudflare.app/devtools/browser`
- Fixture origin: `https://<dynamic-fixture-origin>`
- Identity: `Chrome/145.0.0.0`, CDP `1.3`, revision `@kitesurf`
- Concurrency: 1; each attempt uses a fresh public browser WebSocket.
- Exploratory only; not formal-score eligible.

## Driver summary

| Driver | Tasks | Attempts | Statuses | Latency |
|---|---:|---:|---|---|
| `stagehand` | 9 | 9 | `{"fail": 9}` | p50=20551 ms; p95=24041 ms; max=24041 ms |

## Non-pass attempts

| Driver | Task | Attempt | Status | Failure |
|---|---|---:|---|---|
| `stagehand` | `sc_st_local_survives_reload__sh` | 1 | `fail` | answer did not match server-side expectation |
| `stagehand` | `sc_wait_attribute_set__sh` | 1 | `fail` | answer did not match server-side expectation |
| `stagehand` | `sc_wait_element_hidden__sh` | 1 | `fail` | answer did not match server-side expectation |
| `stagehand` | `sc_wait_enabled_then_click__sh` | 1 | `fail` | answer did not match server-side expectation |
| `stagehand` | `sc_wait_late_item__sh` | 1 | `fail` | answer did not match server-side expectation |
| `stagehand` | `sc_wait_list_progressive_fill__sh` | 1 | `fail` | answer did not match server-side expectation |
| `stagehand` | `sc_wait_slow_badge__sh` | 1 | `fail` | answer did not match server-side expectation |
| `stagehand` | `sc_wait_spinner_replaced__sh` | 1 | `fail` | answer did not match server-side expectation |
| `stagehand` | `sc_wait_two_phase_settle__sh` | 1 | `fail` | answer did not match server-side expectation |
