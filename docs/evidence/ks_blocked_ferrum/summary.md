# Experimental Kitesurf framework-driver probe

- Endpoint: `wss://kitesurf.cloudflare.app/devtools/browser`
- Fixture origin: `https://<dynamic-fixture-origin>`
- Identity: `Chrome/145.0.0.0`, CDP `1.3`, revision `@kitesurf`
- Concurrency: 1; each attempt uses a fresh public browser WebSocket.
- Exploratory only; not formal-score eligible.

## Driver summary

| Driver | Tasks | Attempts | Statuses | Latency |
|---|---:|---:|---|---|
| `ferrum` | 1 | 1 | `{"infra": 1}` | p50=14178 ms; p95=14178 ms; max=14178 ms |

## Non-pass attempts

| Driver | Task | Attempt | Status | Failure |
|---|---|---:|---|---|
| `ferrum` | `sc_agentloop_ax_id_stable_across_mutation__fr` | 1 | `infra` | Ferrum target cleanup was not confirmed: {"backend"=>"ferrum.Target.closeTarget", "required"=>true, "confirmed"=>false, "same_connection_as_task"=>true, "creation_attempts"=>[{"attempt"=>1, "state"=>"ambiguous", "error"=>"Failed to find browser context with id default"}], "attempts"=>[]} |
