# Experimental Kitesurf framework-driver probe

- Endpoint: `wss://kitesurf.cloudflare.app/devtools/browser`
- Fixture origin: `https://<dynamic-fixture-origin>`
- Identity: `Chrome/145.0.0.0`, CDP `1.3`, revision `@kitesurf`
- Concurrency: 1; each attempt uses a fresh public browser WebSocket.
- Exploratory only; not formal-score eligible.

## Driver summary

| Driver | Tasks | Attempts | Statuses | Latency |
|---|---:|---:|---|---|
| `chromiumoxide` | 1 | 1 | `{"infra": 1}` | p50=19835 ms; p95=19835 ms; max=19835 ms |

## Non-pass attempts

| Driver | Task | Attempt | Status | Failure |
|---|---|---:|---|---|
| `chromiumoxide` | `sc_agentloop_ax_id_stable_across_mutation__oxide` | 1 | `infra` | chromiumoxide target cleanup was not confirmed: {"attempts":[],"backend":"chromiumoxide.Target.closeTarget","confirmed":false,"creation_attempts":[{"attempt":1,"error":"timeout after 8000ms creating page","state":"ambiguous"}],"required":true,"same_connection_as_task":true} |
