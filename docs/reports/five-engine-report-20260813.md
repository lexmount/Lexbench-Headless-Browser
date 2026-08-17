# Agent Browser Bench Five-Engine Evaluation (4 Local Engines + Kitesurf Remote Endpoint)

**Four-engine run** `four_engine_full_20260812` · 2026-08-12 · Bench `2026.08.02-v0_4.1` · Kitesurf lane: k=1 + B-class adjudication

Kitesurf is a remote endpoint: no binary fingerprint, no resource measurement, shared infrastructure, public-network latency counted against task budgets, `formal_score_eligible: false`. Its column is **not in the same evidence class** as the local engines and is compared only within the two explicit calibers below.

## Caliber definitions (computed by this script, not transcribed)

- Full corpus: 1,928 tasks.
- **† Uniform exclusion** of 257 tasks (excluded for all five engines alike, keeping the columns comparable): `l1.agent_browser_scenarios`, `l1.agent_browser_tool`, `l1.chrome_devtools_mcp` — against a remote endpoint these driver stacks own too much of the failure surface themselves for failures to be attributable.
- **Caliber A** (attributable surface): 1,671 tasks.
- **Caliber B** (after excluding systematic blockages): 1,308 tasks; additionally excludes 363 tasks: `l1.chromedp`, `l1.ferrum`, `l1.chromiumoxide`, `l1.selenium` — whole-column zeroes, each with a single systematic root cause (see §Defects).

## Overview (task level; local engines = all k attempts pass; Kitesurf = after adjudication)

| Engine | Caliber A pass | Caliber A rate | Caliber B pass | Caliber B rate |
|---|---:|---:|---:|---:|
| chrome | 1,669 / 1,671 | 99.88% | 1,306 / 1,308 | 99.85% |
| moli | 1,359 / 1,671 | 81.33% | 1,071 / 1,308 | 81.88% |
| lightpanda | 697 / 1,671 | 41.71% | 697 / 1,308 | 53.29% |
| obscura | 660 / 1,671 | 39.50% | 587 / 1,308 | 44.88% |
| kitesurf | 812 / 1,671 | 48.59% | 812 / 1,308 | 62.08% |

The Kitesurf lane covered 1,305/1,671 caliber-A tasks and 1,302/1,308 caliber-B tasks; uncovered tasks count as not passed.

## Per-subset breakdown (within caliber A)

| Subset | Tasks | chrome | moli | lightpanda | obscura | kitesurf |
|---|---:|---:|---:|---:|---:|---:|
| `l1.cdp_use` | 92 | 92 | 69 | 76 | 50 | 73 |
| `l1.chrome_remote_interface` | 92 | 92 | 69 | 76 | 52 | 74 |
| `l1.chromedp` | 92 | 92 | 65 | 0 | 26 | 0† |
| `l1.chromiumoxide` | 92 | 92 | 68 | 0 | 0 | 0† |
| `l1.ferrum` | 92 | 92 | 69 | 0 | 47 | 0† |
| `l1.playwright` | 142 | 142 | 96 | 0 | 55 | 69 |
| `l1.puppeteer` | 143 | 143 | 96 | 114 | 73 | 96 |
| `l1.pydoll` | 92 | 92 | 69 | 0 | 29 | 40 |
| `l1.raw_cdp` | 375 | 373 | 355 | 160 | 167 | 246 |
| `l1.rod` | 92 | 92 | 69 | 59 | 46 | 55 |
| `l1.selenium` | 87 | 87 | 86 | 0 | 0 | 0† |
| `l1.stagehand` | 92 | 92 | 69 | 76 | 21 | 37 |
| `l2.playwright` | 3 | 3 | 0 | 0 | 0 | 0 |
| `l2.puppeteer` | 3 | 3 | 0 | 3 | 2 | 0 |
| `l2.web_platform` | 182 | 182 | 179 | 133 | 92 | 122 |

†: systematically blocked subsets (excluded from caliber B); the values are actual observations, not capability ceilings.

## B-class adjudication

| Stage | Count |
|---|---:|
| Passed on the primary round | 811 |
| fail → rerun pass (ruled a flake, counted as pass) | 1 |
| fail → rerun still fail (escalated to A-class, counted as fail) | 454 |
| fail with no rerun evidence (counted as fail) | 0 |
| Other statuses (infra/timeout/unsupported, etc.) | 39 |

Adjudication rule: a primary-round fail counts as a failure only if the rerun evidence still fails (or no rerun evidence exists); a rerun pass is ruled a flake and the pass stands.

## Validity boundaries

- The Kitesurf column cannot be compared with local engines on resources: a remote endpoint is structurally unmeasurable (no process tree, no cgroup). **An empty cell must not be read as zero resource usage.**
- The remote service may change over time; this report's Kitesurf readings are a snapshot at collection time, a different reproducibility class from the four-engine columns.
- The local four-engine readings are authoritative in the four-engine report; this report does not rerun the local columns.

## Run notes for this round (method and observations; all numbers come from the script-generated sections above)

- **Kitesurf lane collection**: 2026-08-13, via `tools/kitesurf_l1_probe.py` / `kitesurf_driver_probe.py` / `kitesurf_l2_sample_probe.py`, k=1 serial with 750ms spacing; every failure entered one rerun adjudication round (`runs/ks_adj_*`). When a task could not confirm target cleanup, the probe tripped its circuit breaker as a whole to protect the shared endpoint, and the outer wrapper restarted a continuation over the remaining tasks (playwright ended up split into 25 continuation segments this way).
- **Identity verification**: the live-transport three-field identity (`Browser.getVersion`) matched the previously reported pin exactly: `Chrome/145.0.0.0` / `1.3` / `@kitesurf`. **Server-side drift**: the `Browser` header of HTTP discovery (`/json/version`) now self-reports `Kitesurf/0.0.1` (at the time of the prior report it was consistent with the WS side's Chrome-style value).
- **playwright intermittent crashes**: the endpoint intermittently emits duplicate `Target.targetCreated` announcements, which playwright's strict target registry treats as fatal ("Duplicate target"); this round it manifested as deterministic triggering on some tasks (the same evidence seen during the prior report's active period, not a new regression). Affected rows count as infra, never as pass.
- **Two-origin fixtures**: the static origin was verified against its content contract 19/19 (the URL is a run parameter); the dynamic origin was a local FixtureServer behind a cloudflared quick tunnel, with pre-run contract verification fully green on 127 static routes + 28 dynamic probes. Mid-run, the WS handshake case-sensitive header comparison defect described in the prior report's §8 was fixed and committed (RFC 9110 token semantics).
- **Comparison with the prior report (2026-08-11)**: caliber A 812 vs 817 (−0.30pp), caliber B 812 vs 817 (−0.38pp), within reproduction tolerance for a k=1 remote lane; ordering unchanged; the three systematic blockage subsets reproduced as whole-column zeroes (root causes unchanged: parameterless events missing the `params` member / announced browserContextId not accepted / TLS-only discovery surface).
