# November 2026 benchmark results

Moli 1.1.28 passed **1,928 of 1,928 tasks** on dataset `0.4.2` in realtime layout. Each task passed all three attempts. The November run supplies the Moli column in the README comparisons. The Chrome, Lightpanda and Obscura columns use `four_engine_full_20261016b`. The Kitesurf column uses the August remote evaluation. [October results](https://github.com/lexmount/Lexbench-Headless-Browser/blob/58d88f364483c9990bdc140494237b56dfe9c07f/README.md#results) · [Kitesurf report](https://github.com/lexmount/Lexbench-Headless-Browser/blob/kitesurf-eval/docs/reports/five-engine-report-20260813.md)

| Moli 1.1.28 scope | Tasks passed | Attempts passed |
|:---|---:|---:|
| Full suite | 1,928 / 1,928 | 5,784 / 5,784 |
| L1 protocol and driver compatibility | 1,740 / 1,740 | 5,220 / 5,220 |
| L2 page semantics | 188 / 188 | 564 / 564 |
| Five-engine comparison subset | 1,308 / 1,308 | 3,924 / 3,924 |

The five-engine comparison filters the full run to the same seven subset exclusions as Caliber B in the Kitesurf report. The run's `score_eligible` flag is false because it selected one engine. The table reports task and attempt counts from the result rows. [Chart data and evidence hashes](../evidence/readme-results.json)

## Run details

| Field | Value |
|:---|:---|
| Run id | `moli_1_1_28_realtime_full_k3_20261127` |
| Collection window (UTC) | 2026-11-26 20:07:06 to 20:10:21 |
| Engine | Official Moli `v1.1.28` for Linux x86_64 |
| Binary SHA-256 | `bd31659ed3e49b74f3d7c714a87533ad1ccacedeb84f48026837f07cb84e4ae0` |
| Harness | `0.1.0` at `f4680bd90cc2e43e3f2b2c22ef9f48265104b01f` |
| Runner and fixture tree SHA-256 prefixes | `c599e58a3464` and `6235b86f3a11` |
| Layout and launch flags | `--realtime --debugger` with profile-specific `--resource` and `--enable-automation` |
| Run settings | 16 jobs, 3 attempts per task, seed `official20260709`, independent scoring |
| Host telemetry | 97 samples with `swap_activity` and `swap_in_use` |

Install the [Moli v1.1.28 release](https://github.com/lexmount/moli/releases/tag/v1.1.28) and the pinned harness drivers using the [running guide](../RUNNING.md). Then run the full Moli suite.

```sh
python3 -m runner.run run --engines moli --moli-layout realtime \
  --k 3 --jobs 16 --seed official20260709 --score-mode independent \
  --chrome-baseline best_effort --host-telemetry on --resource-profile off \
  --provenance-level minimal --run-id moli_1_1_28_realtime_full_k3_20261127 \
  --run-id-conflict error
```

## Moli resource sample

The resource run uses `l1.raw_cdp` and `l2.web_platform`, the same 557-task suite as the October resource snapshot. Both rounds ran Moli 1.1.28 in realtime layout with one worker, five attempts per task and seed `official20260709`. The profiled round samples PSS every 250 ms from the process tree and uses cgroup v2 CPU accounting. [Resource measurement protocol](../resource-cost.md)

| Metric per passing attempt | Median | P95 | Samples |
|:---|---:|---:|---:|
| Sampled peak PSS | 112.63 MiB | 165.88 MiB | 2,777 |
| Sampled PSS increase from attempt start | 0.22 MiB | 2.39 MiB | 2,777 |
| Engine CPU time | 30.11 ms | 44.56 ms | 2,777 |
| Peak engine process count | 1 | 1 | 2,777 |
| Fixture response body traffic | 1,598 bytes | 3,291 bytes | 2,777 |

Each round passed 2,777 of 2,785 attempts. The same two IndexedDB checks (`r3_dB_h_idb_upgrade` and `r3_dB_m_idb_getall`) failed on attempts 2 through 5 in the serial runs. The two rounds paired all attempts with zero status differences. The median paired differences were 0.00% for task duration and +1.97% for collection wall time. Both met the 10% calibration threshold.

Host telemetry recorded swap activity and memory pressure during profiling. The single-engine sample has `resource_comparison_eligible: false`. Across the profiled round, 1,803 attempts had PSS samples only at the start and end. Five cold starts had median readiness of 135 ms and median sampled peak PSS of 56.12 MiB. [Recorded statistics and source hashes](../evidence/readme-results.json)

To reproduce the two rounds, run them in the same delegated cgroup v2 scope with the host telemetry on.

```bash
resource_run=(python3 -m runner.run run --engines moli --moli-layout realtime
  --subset l1.raw_cdp --subset l2.web_platform --score-mode independent
  --chrome-baseline best_effort --jobs 1 --k 5 --seed official20260709
  --host-telemetry on --resource-sample-interval-ms 250 --provenance-level full)

"${resource_run[@]}" --resource-profile baseline \
  --run-id moli_1_1_28_realtime_resource_baseline_20261127 --run-id-conflict error

"${resource_run[@]}" --resource-profile engine \
  --resource-calibration-baseline runs/moli_1_1_28_realtime_resource_baseline_20261127 \
  --run-id moli_1_1_28_realtime_resource_engine_20261127 --run-id-conflict error
```

## October resource snapshot

The solid points on the efficiency map use the October 2026 compatibility results and resource measurements for Chrome, Lightpanda and Obscura. The resource sample covers 1,346 shared passing attempts on the 557-task suite. The October host used swap and Obscura's Fetch interception results changed between calibration rounds, so the measurements are indicative. [October resource figures](https://github.com/lexmount/Lexbench-Headless-Browser/blob/58d88f364483c9990bdc140494237b56dfe9c07f/README.md#resource-cost)

| October engine | Median peak PSS | Median CPU time |
|:---|---:|---:|
| Chrome | 734 MiB | 177 ms |
| Moli 1.1.17 (on-demand layout) | 78 MiB | 24 ms |
| Lightpanda | 34 MiB | 7 ms |
| Obscura | 45 MiB | 14 ms |

## Chart regeneration

Regenerate the light and dark charts from the checked-in data with `uv`.

```sh
uv run --no-project --with matplotlib==3.11.2 python tools/render_readme_charts.py
```
