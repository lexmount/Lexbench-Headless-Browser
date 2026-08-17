# Experimental Kitesurf framework-driver probe

- Endpoint: `wss://kitesurf.cloudflare.app/devtools/browser`
- Fixture origin: `https://<dynamic-fixture-origin>`
- Identity: `Chrome/145.0.0.0`, CDP `1.3`, revision `@kitesurf`
- Concurrency: 1; each attempt uses a fresh public browser WebSocket.
- Exploratory only; not formal-score eligible.

## Driver summary

| Driver | Tasks | Attempts | Statuses | Latency |
|---|---:|---:|---|---|
| `playwright` | 15 | 15 | `{"fail": 9, "pass": 5, "timeout": 1}` | p50=14942 ms; p95=28366 ms; max=30050 ms |

## Non-pass attempts

| Driver | Task | Attempt | Status | Failure |
|---|---|---:|---|---|
| `playwright` | `sc_form_checkbox_check__pw` | 1 | `fail` | answer did not match server-side expectation |
| `playwright` | `sc_form_checkbox_event__pw` | 1 | `fail` | answer did not match server-side expectation |
| `playwright` | `sc_form_fill_overwrites__pw` | 1 | `fail` | answer did not match server-side expectation |
| `playwright` | `sc_form_label_click_toggles__pw` | 1 | `fail` | answer did not match server-side expectation |
| `playwright` | `sc_form_radio_pick__pw` | 1 | `fail` | answer did not match server-side expectation |
| `playwright` | `sc_form_submit_roundtrip__pw` | 1 | `fail` | answer did not match server-side expectation |
| `playwright` | `sc_fr_child_posts_parent__pw` | 1 | `fail` | answer did not match server-side expectation |
| `playwright` | `sc_fr_child_self_navigation__pw` | 1 | `fail` | answer did not match server-side expectation |
| `playwright` | `sc_fr_cross_origin_blocked__pw` | 1 | `fail` | answer did not match server-side expectation |
| `playwright` | `sc_fr_cross_origin_frame_evaluate__pw` | 1 | `timeout` | Command '['node', '<repo>/runner/scripts/framework_probe.js']' timed out after 30.0 seconds |
