# Reproducing the published runs

[English](REPRODUCE.md) · [中文](REPRODUCE.zh.md)

Every number in the reports traces back to one of three runs. This page records their exact parameters, how a run turns into a report, and what to check when your rerun disagrees.

## The three published runs

| Run id | Purpose | Task set | k | jobs | Profiler |
|:---|:---|:---|---:|---:|:---|
| `four_engine_full_20260812` | Functional scores | full corpus, 1,928 tasks | 3 | 16 | off |
| `resource_baseline_20260812` | Resource round A | `l1.raw_cdp` + `l2.web_platform`, 557 tasks | 5 | 1 | off |
| `resource_engine_20260812` | Resource round B | same 557 tasks | 5 | 1 | on |

Run parameters: bench `2026.08.02-v0_4.1`, seed `official20260709`, `--score-mode independent --chrome-baseline best_effort`, engines `chrome,moli,lightpanda,obscura`. Engine pins (version and sha256) are listed in each report's provenance table and enforced by `doctor`; where to get each binary — [Moli](https://github.com/lexmount/moli), [Chrome for Testing](https://googlechromelabs.github.io/chrome-for-testing/), [Lightpanda](https://github.com/lightpanda-io/browser), [Obscura](https://github.com/h4ckf0r0day/obscura) — and where to place it are covered in [RUNNING.md](RUNNING.md). The commands below are taken verbatim from each run's recorded `argv` in its `run_manifest.json`.

The functional run:

```bash
python3 -m runner.run run \
  --engines chrome,moli,lightpanda,obscura \
  --chrome-baseline best_effort --score-mode independent \
  --seed official20260709 --k 3 --jobs 16 --host-telemetry on \
  --run-id four_engine_full_20260812 --provenance-level minimal
```

The resource runs, in order (round B must reference round A):

```bash
python3 -m runner.run run \
  --subset l1.raw_cdp --subset l2.web_platform \
  --engines chrome,moli,lightpanda,obscura \
  --chrome-baseline best_effort --score-mode independent \
  --seed official20260709 --k 5 --jobs 1 --host-telemetry on \
  --resource-profile baseline \
  --run-id resource_baseline_20260812 --provenance-level full

python3 -m runner.run run \
  --subset l1.raw_cdp --subset l2.web_platform \
  --engines chrome,moli,lightpanda,obscura \
  --chrome-baseline best_effort --score-mode independent \
  --seed official20260709 --k 5 --jobs 1 --host-telemetry on \
  --resource-profile engine \
  --resource-calibration-baseline runs/resource_baseline_20260812 \
  --resource-max-observer-effect-pct 20 \
  --run-id resource_engine_20260812 --provenance-level full
```

The observer-effect threshold is an explicit `20` rather than the default 10 because Chrome's hundred-plus-process tree makes the two full-tree PSS scans an inherent cost of measuring at all; the report documents that choice and shows the actual task-duration disturbance stayed at or under 0.87%.

Both resource rounds must run on the same machine with nothing else competing for it. The A/B comparison only means something if the machine state held still between the rounds.

## From a run to a report

Reports are generated, never written:

```bash
python3 tools/report_four_engine.py runs/four_engine_full_20260812 \
  -o docs/reports/four-engine-report-20260812.md
```

The generator reads `results.jsonl` and recomputes everything. Running it twice on the same run directory produces byte-identical output, which is the check that a published report has not been hand-edited: regenerate and `diff`.

## Why a rerun is comparable at all

- `--run-id` names the run directory verbatim, and all recorded timestamps are UTC.
- The per-attempt seed is derived as `sha256(seed:task_id:attempt)`, independent of engine order or wall clock.
- `run_manifest.json` digests the runner source tree, the fixture tree and the compiled adapter binaries. Before comparing two runs, compare these digests: if they differ, the harnesses differ.
- Result rows contain no absolute host paths; per-launch temp directories are recorded as `<ephemeral>`.
- `--provenance-level minimal` keeps hardware and kernel facts and drops deployment fingerprints such as cgroup paths and CPU affinity. The resource pair ran at `full` because calibration auditing needs the deployment detail; the functional run published at `minimal`.

## Where the evidence lives

Run directories are not committed (`runs/` is ignored; the three published ones total over a gigabyte uncompressed). The published evidence ships as compressed bundles attached to the repository's GitHub Releases, each containing `results.jsonl`, `run_manifest.json` and the summary files, with sha256 checksums listed alongside. Clone the repository for the harness and reports; fetch the bundles when you want to audit or regenerate.

## When your numbers differ

Work down this ladder before concluding anything about a browser:

1. Compare `run_manifest.json` digests and engine sha256 values against the published ones. Different pins mean you measured different software, which is an answer, not an error.
2. Check `doctor` was green and no rows are `infra` or `chrome_gate_fail` in unexpected volume. Identity failures are environment problems.
3. For functional scores, small flake-level differences show up as tasks passing k=2 of 3; the all-attempts-pass rule makes the headline number sensitive to real instability, which is intentional.
4. For resource numbers, confirm `resource_comparison_eligible: true` in your own round B. Numbers from an ineligible round are not comparable with anyone's, including ours. Resource figures also never extrapolate beyond the 557-task set.
