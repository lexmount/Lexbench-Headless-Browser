# Resource cost and the fair-comparison contract

[English](resource-cost.md) · [中文](resource-cost.zh.md)

Resource telemetry is an independent observation dimension separate from the functional score. It
answers "how much CPU, memory, and fixture application traffic did the same batch of successful
tasks consume"; it does not change a task's pass/fail, is not folded into the native capability
score, and does not produce a blended "resource total score".

Currently supported: `chrome`, `moli`, `lightpanda`, `obscura`; the aggregation dimension is
resolved from `run_manifest.selected_engines` and no longer hardcodes three engines.

## Two run modes

A normal `run` enables low-frequency host telemetry by default:

```bash
python3 -m runner.run run \
  --engines chrome,moli,lightpanda,obscura \
  --host-telemetry on \
  --host-sample-interval-s 2
```

It records load, MemAvailable, swap, CPU/memory/IO PSI, the count of benchmark descendant
processes, kernel, CPU governor, and cgroup version. The pollution gate flags swap activity,
low available memory, sustained PSI, or abnormal process growth, but it does not rewrite
functional results. When genuinely needed, it can be disabled with `--host-telemetry off`.

Engine profiling is an explicit opt-in. A formal resource comparison should first run a
profiler-off baseline on the same corpus, seed, k, and machine, and then run profiler-on:

```bash
# A: balance the engine order, but do not enable the attempt profiler
python3 -m runner.run run \
  --subset l1.raw_cdp \
  --tag purpose.smoke \
  --engines chrome,moli,lightpanda,obscura \
  --score-mode independent \
  --jobs 1 \
  --k 5 \
  --seed resource-calibration-v1 \
  --resource-profile baseline \
  --run-id resource_ab_off

# B: the same matrix with CPU/PSS/fixture traffic enabled
python3 -m runner.run run \
  --subset l1.raw_cdp \
  --tag purpose.smoke \
  --engines chrome,moli,lightpanda,obscura \
  --score-mode independent \
  --jobs 1 \
  --k 5 \
  --seed resource-calibration-v1 \
  --resource-profile engine \
  --resource-sample-interval-ms 500 \
  --resource-calibration-baseline runs/<resource_ab_off_run> \
  --resource-max-observer-effect-pct 10 \
  --run-id resource_ab_on
```

Both `baseline` and `engine` do a strict round-robin rotation over consecutive task-attempts;
within any N attempts, the number of times each engine occupies each ordinal position differs
by at most 1, and the seed only determines the offset of the first round. On short tasks,
scanning the full Chrome process tree can itself be expensive, so 500ms is a conservative
starting point; adjust it based on A/B results rather than mechanically chasing a higher
sampling frequency.

## Scopes and metrics

The three scopes are never mixed:

- `engine_scope`: the engine root plus all descendants — the primary comparison basis for the selected engine.
- `harness_scope`: control costs such as the driver, adapters, and grader; not counted in the primary engine basis.
- `host_scope`: whole-machine environment and pollution evidence, used only for comparability and attribution.

A warm attempt records at least:

- CPU: `cpu_total_ms`, `cpu_user_ms`, `cpu_system_ms`, `avg_cores`.
- PSS: `pss_baseline_bytes`, `pss_peak_bytes`, `pss_end_bytes`,
  `pss_peak_delta_bytes`.
- Processes: baseline/peak/end process count, plus the run-level PSS/process leak slope.
- cgroup memory: current/peak as supplementary accounting; it never masquerades as PSS.
- Traffic: fixture application request/response headers and body bytes.

Cold start is written separately to `cold_start.jsonl`, recording `ready_ms`, `launch_cpu_ms`, and
`launch_peak_pss_bytes`; it does not enter the warm aggregation.

## Backends and failure semantics

CPU preferentially uses a dedicated cgroup v2 `cpu.stat` per worker×engine. Without delegation
permission, the fallback is a full-process-tree accumulation from `/proc/PID/stat`, and
`proc_tree_child_exit_loss_risk` is written.

PSS uses each process's `/proc/PID/smaps_rollup`, summed over the full descendant tree. `/proc`
cannot provide an atomic tree+PSS snapshot, so the scan performs bounded retries around transient
child-process exits. Confirmed zombies still count toward the process count, but their released
address space is treated as 0 live PSS, and `pss_zero_address_space_process` is written.
Permission errors or unconfirmable read failures write `unavailable.pss` and are never written as
0. With a dedicated cgroup, the complete set is enumerated from `cgroup.procs`; without one,
`/proc/PID/task/TID/children` is walked. PSS files are read in parallel to lower the observer
wall overhead, while each reader thread's CPU is counted into `sampler_cpu_ms`.
`pss_peak_bytes` is the maximum over all PSS samples of the attempt; if a short task has only the
two baseline/end points, `baseline_end_only` is written explicitly — it must not be interpreted as
an instantaneous peak from continuous sampling.

Resource-collection anomalies are isolated inside the profiler: the task's functional status is
still decided by the original driver/grader.

## Fixture traffic accounting

Phase 1's portable ground truth comes from the self-hosted fixture server:

- `fixture_app_rx_body_bytes`: HTTP bodies received by the server plus WebSocket client payloads.
- `fixture_app_tx_body_bytes`: HTTP/SSE bodies sent by the server plus WebSocket server
  payloads.
- `fixture_app_rx_header_bytes` / `fixture_app_tx_header_bytes`: HTTP application-layer headers.
- Redirects, HTTP uploads/downloads, SSE, and WebSocket all go through the same counter.
- `/__grade__/` and `/__event__/` go into a separate harness counter and are not mixed into the fixture app numbers.

With `jobs=1` the active attempt is unique and attribution is unambiguous. Concurrent diagnostic
runs attempt attribution via session/referrer/cookie/body; when unique attribution is impossible,
the value is marked unavailable and never faked as 0. The portable backend does not yet implement
control-plane bytes or wire bytes; the report carries an explicit reason.

## Comparison eligibility

`resource_comparison_eligible=true` requires all of the following:

1. At least two engines are selected, and every result row's engine belongs to the same
   `selected_engines`;
2. `--resource-profile engine`, `--jobs 1`, `--k >= 5`;
3. `--score-mode independent`, using the balanced rotation order;
4. The intersection where all selected engines pass on the same `task_id × attempt` is non-empty;
5. The CPU/PSS/traffic metrics required for the intersection have no gaps;
6. The host pollution gate passes;
7. The profiler on/off A/B is fully paired, with corpus/seed/k/jobs/engine+harness pin/host
   provenance exactly identical, a status mismatch rate not exceeding 1%, and both the median
   task duration and the collection-wall overhead (including the baseline/end scans) within the
   configured thresholds overall and per engine.

The report aggregates only the all-pass intersection. fail/unsupported/timeout still keep their
raw resource data and are listed in `excluded.status_by_engine`, so that fail-fast is not
mistaken for an efficiency advantage. Each metric reports its sample count, median, p95, and a
bootstrap median 95% CI.

## Artifacts

A normal run adds:

```text
host_telemetry.jsonl
host_summary.json
```

`--resource-profile engine` additionally adds:

```text
cold_start.jsonl
resource_summary.json
resource-card.md
artifacts/<layer>/<subset>/<task>/<engine>/<attempt>/resource.jsonl
```

The attempt summary lives at `results.jsonl[].resource`. Backend, sampling interval, sample
counts, sampler CPU, quality flags, host/kernel/governor, jobs, reuse, engine SHA, and harness
pin all enter run provenance; `runner.source.tree_sha256` hashes the actual content of the
effective runner and adapter source files, including uncommitted modifications. The current
schemas are:

- `abb_host_telemetry/1`
- `abb_engine_resources/1`
- `abb_fixture_traffic/1`
- `abb_resource_summary/1`
- `abb_runner_source/1`

