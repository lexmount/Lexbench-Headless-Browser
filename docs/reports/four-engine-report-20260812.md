# Agent Browser Bench Four-Engine Evaluation (L1 + L2)

**Run** `four_engine_full_20260812` · 2026-08-12 · Bench `2026.08.02-v0_4.1`

Head-to-head comparison of Chrome 151, Moli 0.1.1, Lightpanda, Obscura on 1,928 tasks. k=3, 23,136 result rows, wall time 1:34:36, `score_eligible: true`, no fallback.

This report covers local pinned-binary engines only. Remote endpoints (such as Kitesurf) sit in a different evidence class; see the five-engine report.

---

## 1. Overview

| Engine | pass | Pass rate |
|---|---:|---:|
| Chrome 151 | 1,926 / 1,928 | **99.90%** |
| Moli 0.1.1 | 1,556 / 1,928 | **80.71%** |
| Lightpanda | 845 / 1,928 | **43.83%** |
| Obscura | 762 / 1,928 | **39.52%** |

### By evaluation axis

| Axis | Units | Chrome 151 | Moli 0.1.1 | Lightpanda | Obscura |
|---|---:|---:|---:|---:|---:|
| L1 protocol/driver compatibility | 5,220 | 5214/5220 (99.89%) | 4131/5220 (79.14%) | 2129/5220 (40.79%) | 2007/5220 (38.45%) |
| L2 semantic capability | 192 | 192/192 (100.00%) | 183/192 (95.31%) | 132/192 (68.75%) | 84/192 (43.75%) |

---

## 2. Artifact provenance

| Engine | Version | SHA-256 prefix |
|---|---|---|
| Chrome | 151.0.7922.47 | `3b0be9872ea9` |
| Moli | moli 0.1.1 | `74e08f8d3eb6` |
| Lightpanda | 1.0.0-dev.321+b04c99a9 | `70f5ab69b0ce` |
| Obscura | obscura 0.1.11 | `42c7eac0f635` |

**Run parameters**: `--engines chrome,moli,lightpanda,obscura --chrome-baseline best_effort --score-mode independent --seed official20260709 --k 3 --jobs 16 --host-telemetry on`

`best_effort` rather than `required`: Chrome is scored as a comparable reference column, not used as a gate. Under `required`, tasks Chrome fails would be removed from every engine's scoring, and Chrome's own pass rate would approach 100% by construction, making that column meaningless.

**Harness fingerprints**: runner source tree `385ecc482171` (31 files) · fixtures `7687c7f261fa` (269 files)

---

## 3. Per-subset breakdown

| Subset | Tasks | Chrome 151 | Moli 0.1.1 | Lightpanda | Obscura |
|---|---:|---:|---:|---:|---:|
| `l1.agent_browser_scenarios` | 89 | 89 | 66 | 80 | 48 |
| `l1.agent_browser_tool` | 78 | 78 | 64 | 68 | 54 |
| `l1.cdp_use` | 92 | 92 | 69 | 76 | 50 |
| `l1.chrome_devtools_mcp` | 90 | 90 | 67 | 0 | 0 |
| `l1.chrome_remote_interface` | 92 | 92 | 69 | 76 | 52 |
| `l1.chromedp` | 92 | 92 | 65 | 0 | 26 |
| `l1.chromiumoxide` | 92 | 92 | 68 | 0 | 0 |
| `l1.ferrum` | 92 | 92 | 69 | 0 | 47 |
| `l1.playwright` | 142 | 142 | 96 | 0 | 55 |
| `l1.puppeteer` | 143 | 143 | 96 | 114 | 73 |
| `l1.pydoll` | 92 | 92 | 69 | 0 | 29 |
| `l1.raw_cdp` | 375 | 373 | 355 | 160 | 167 |
| `l1.rod` | 92 | 92 | 69 | 59 | 46 |
| `l1.selenium` | 87 | 87 | 86 | 0 | 0 |
| `l1.stagehand` | 92 | 92 | 69 | 76 | 21 |
| `l2.playwright` | 3 | 3 | 0 | 0 | 0 |
| `l2.puppeteer` | 3 | 3 | 0 | 3 | 2 |
| `l2.web_platform` | 182 | 182 | 179 | 133 | 92 |

---

## 4. Stability

| Engine | Mixed-status groups / total | Groups with infra/crash/timeout | Of which k/k consistent |
|---|---:|---:|---:|
| Chrome 151 | 0 / 1,928 | 0 | 0 |
| Moli 0.1.1 | 0 / 1,928 | 1 | 1 |
| Lightpanda | 1 / 1,928 | 1 | 1 |
| Obscura | 2 / 1,928 | 89 | 89 |

Across 7,712 `task × engine` groups, 3 groups had inconsistent statuses (0.04%).

### Status distribution

| Engine | crash | fail | infra | pass | timeout | unsupported |
|---|---:|---:|---:|---:|---:|---:|
| Chrome 151 | 0 | 3 | 0 | 5778 | 0 | 3 |
| Moli 0.1.1 | 0 | 1113 | 3 | 4668 | 0 | 0 |
| Lightpanda | 3 | 3244 | 0 | 2537 | 0 | 0 |
| Obscura | 18 | 2697 | 6 | 2289 | 243 | 531 |

---

## 5. Validity boundaries

- **No resource profile was collected in this run.** `--resource-profile` was off; resource comparison requires separate `baseline` and `engine` rounds under the A/B protocol in `docs/resource-cost.md`.

- **L3 is out of scope.** Real-site chain results are not part of this report.

- **Chrome is a reference column, not a gold standard.** `--score-mode independent`; each engine is scored independently.

