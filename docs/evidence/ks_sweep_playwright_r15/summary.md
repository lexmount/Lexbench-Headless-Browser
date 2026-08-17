# Experimental Kitesurf framework-driver probe

- Endpoint: `wss://kitesurf.cloudflare.app/devtools/browser`
- Fixture origin: `https://<dynamic-fixture-origin>`
- Identity: `Chrome/145.0.0.0`, CDP `1.3`, revision `@kitesurf`
- Concurrency: 1; each attempt uses a fresh public browser WebSocket.
- Exploratory only; not formal-score eligible.

## Driver summary

| Driver | Tasks | Attempts | Statuses | Latency |
|---|---:|---:|---|---|
| `playwright` | 6 | 6 | `{"fail": 5, "timeout": 1}` | p50=24794 ms; p95=30050 ms; max=30050 ms |

## Non-pass attempts

| Driver | Task | Attempt | Status | Failure |
|---|---|---:|---|---|
| `playwright` | `sc_dlg_alert_message__pw` | 1 | `fail` | answer did not match server-side expectation |
| `playwright` | `sc_dlg_confirm_accept__pw` | 1 | `fail` | answer did not match server-side expectation |
| `playwright` | `sc_dlg_confirm_dismiss__pw` | 1 | `fail` | answer did not match server-side expectation |
| `playwright` | `sc_dlg_prompt_text__pw` | 1 | `fail` | answer did not match server-side expectation |
| `playwright` | `sc_dlg_sequence_two_dialogs__pw` | 1 | `fail` | answer did not match server-side expectation |
| `playwright` | `sc_dyn_counter_clicks__pw` | 1 | `timeout` | Command '['node', '<repo>/runner/scripts/framework_probe.js']' timed out after 30.0 seconds |
