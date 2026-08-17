# Experimental Kitesurf fixture-free L1 probe

- Endpoint: `wss://kitesurf.cloudflare.app/devtools/browser`
- Product: `Chrome/145.0.0.0`; protocol `1.3`; revision `@kitesurf`
- Tasks: 375 completed / 375 selected
- Statuses: `{"fail": 129, "pass": 246}`
- Client-observed task latency: min 1996 ms, p50 5724 ms, p95 11357 ms, max 14486 ms
- Concurrency: 1; each task uses a fresh public WebSocket session.
- This is exploratory evidence only and is not formal-score eligible.

## CDP domain status counts

| Domain | Status counts |
|---|---|
| `Accessibility` | `{"fail": 2, "pass": 18}` |
| `Audits` | `{"fail": 3}` |
| `Autofill` | `{"pass": 1}` |
| `Browser` | `{"fail": 7, "pass": 4}` |
| `CSS` | `{"pass": 1}` |
| `DOM` | `{"fail": 20, "pass": 78}` |
| `DOMDebugger` | `{"fail": 1, "pass": 6}` |
| `DOMSnapshot` | `{"fail": 2}` |
| `DOMStorage` | `{"fail": 1}` |
| `Debugger` | `{"fail": 20, "pass": 7}` |
| `Emulation` | `{"fail": 2, "pass": 17}` |
| `Fetch` | `{"fail": 25}` |
| `IO` | `{"fail": 3}` |
| `Input` | `{"fail": 4, "pass": 11}` |
| `Inspector` | `{"pass": 1}` |
| `Log` | `{"fail": 4, "pass": 6}` |
| `Moli` | `{"pass": 1}` |
| `Network` | `{"fail": 14, "pass": 18}` |
| `Overlay` | `{"pass": 2}` |
| `Page` | `{"fail": 23, "pass": 81}` |
| `Performance` | `{"fail": 4, "pass": 2}` |
| `Profiler` | `{"fail": 7, "pass": 4}` |
| `Runtime` | `{"fail": 96, "pass": 160}` |
| `Schema` | `{"pass": 1}` |
| `Security` | `{"pass": 4}` |
| `Storage` | `{"pass": 1}` |
| `Target` | `{"fail": 9, "pass": 12}` |
| `Tracing` | `{"fail": 1, "pass": 2}` |
| `WebMCP` | `{"fail": 1}` |
