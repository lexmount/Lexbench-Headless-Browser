# Experimental Kitesurf framework-driver probe

- Endpoint: `wss://kitesurf.cloudflare.app/devtools/browser`
- Fixture origin: `https://<dynamic-fixture-origin>`
- Identity: `Chrome/145.0.0.0`, CDP `1.3`, revision `@kitesurf`
- Concurrency: 1; each attempt uses a fresh public browser WebSocket.
- Exploratory only; not formal-score eligible.

## Driver summary

| Driver | Tasks | Attempts | Statuses | Latency |
|---|---:|---:|---|---|
| `chromedp` | 1 | 1 | `{"infra": 1}` | p50=18510 ms; p95=18510 ms; max=18510 ms |

## Non-pass attempts

| Driver | Task | Attempt | Status | Failure |
|---|---|---:|---|---|
| `chromedp` | `sc_agentloop_ax_id_stable_across_mutation__chromedp` | 1 | `infra` | chromedp target cleanup was not confirmed: map[attempts:[map[attempt:1 confirmed:false error:invalid context success:false target_id:d4ae80cf970848f896f80ecf65ff98cf] map[attempt:2 confirmed:false error:invalid context success:false target_id:d4ae80cf970848f896f80ecf65ff98cf] map[attempt:1 confirmed:false error:invalid context success:false target_id:36194363e2374e59b0b30f215176c68b] map[attempt:2 |
