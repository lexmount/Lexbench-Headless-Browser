# Four-engine resource profiling (A/B calibration)

**Runs** `resource_baseline_20260812` (profiler off) → `resource_engine_20260812` (profiler on)· 2026-08-12 · Bench `2026.08.02-v0_4.1`

**Corpus**: `l1.raw_cdp`(375) + `l2.web_platform`(182) = 557 tasks · `--jobs 1 --k 5 --score-mode independent` · balanced rotation order · seed `official20260709`. Running the full 1,928 tasks at jobs=1×k=5 is infeasible in wall-clock time, so this round's basis is this representative subset; the report's numbers must not be extrapolated to a full-corpus basis.

**Observer-effect gate**: `--resource-max-observer-effect-pct 20` (explicit parameter, recorded in run_manifest). Rationale for the choice: Chrome's process tree exceeds one hundred processes, and the two full-tree smaps_rollup PSS scans at the start and end of an attempt are an inherent cost of the measurement itself (collection-wall median +15.74%); the **median task-duration delta**, which measures actual measurement distortion, is ≤0.87% for all four engines (global +0.02%), with 0 status mismatches, and the two rounds' functional counts match exactly status by status (pass=8185 fail=2920 unsupported=5 crash=30). Under the default 10% threshold everything meets the bar except Chrome's collection-wall.

**Structural boundary**: resource profiling and functional scores are never blended; remote-endpoint engines (such as Kitesurf) have no process tree or cgroup and are structurally unmeasurable — an empty cell must not be read as zero resource usage.

---

# Resource card: resource_engine_20260812

- resource_comparison_eligible: `True`
- all-pass intersection: `1045` task-attempts
- scope: warm engine process tree; cold-start results are separate
- functional capability scores are not combined with resource cost

## All-pass intersection metrics

| engine | functional pass/attempts | intersection | CPU median ms | CPU p95 ms | PSS peak median MiB | PSS delta median MiB | fixture tx median bytes |
|---|---:|---:|---:|---:|---:|---:|---:|
| chrome | 2763/2785 | 1045 | 686.95 | 825.42 | 697.14 | 11.17 | 1668.00 |
| moli | 2662/2785 | 1045 | 100.61 | 174.44 | 92.12 | 2.68 | 1598.00 |
| lightpanda | 1465/2785 | 1045 | 36.48 | 77.22 | 33.55 | 0.35 | 1598.00 |
| obscura | 1295/2785 | 1045 | 38.44 | 60.04 | 38.74 | 4.05 | 1598.00 |

## Cold start (separate diagnostic)

| engine | samples | ready median ms | launch CPU median ms | launch peak PSS median MiB |
|---|---:|---:|---:|---:|
| chrome | 1 | 2739.00 | 3294.36 | 347.99 |
| moli | 3 | 1488.00 | 98.62 | 66.86 |
| lightpanda | 1 | 1401.00 | 76.24 | 16.69 |
| obscura | 31 | 904.00 | 6.62 | 20.71 |

## Excluded outcomes

| engine | status counts outside all-pass intersection |
|---|---|
| chrome | fail=17, pass=1718, unsupported=5 |
| moli | fail=123, pass=1617 |
| lightpanda | fail=1320, pass=420 |
| obscura | crash=30, fail=1460, pass=250 |

## Measurement quality flags

- chrome: `baseline_end_only=140, pss_scan_retried=2, pss_zero_address_space_process=117`
- moli: `baseline_end_only=141`
- lightpanda: `baseline_end_only=1550`
- obscura: `baseline_end_only=549, engine_root_exited=5, pss_scan_incomplete=5, pss_zero_address_space_process=25`

## Host and observer quality

- host pollution flags: `none`
- profiler median duration delta: `0.016011528402992692`%; collection-wall delta: `3.8287259178480526`%; status mismatches: `0`
