# Running the bench

[English](RUNNING.md) · [中文](RUNNING.zh.md)

This page walks you through completing a full run on your own machine. If your goal is to reproduce the published numbers exactly, read [REPRODUCE.md](REPRODUCE.md) after this one.

## The browsers under test

| Engine | Role | Where the binary comes from |
|:---|:---|:---|
| [Moli](https://github.com/lexmount/moli) | candidate | repository releases, or build from source per its docs |
| [Chrome for Testing](https://googlechromelabs.github.io/chrome-for-testing/) | reference column | official availability page (chromedriver ships on the same page) |
| [Lightpanda](https://github.com/lightpanda-io/browser) | candidate | repository releases, or build from source per its docs |
| [Obscura](https://github.com/h4ckf0r0day/obscura) | candidate | repository releases, or build from source per its docs |

`--engines` accepts any comma-separated subset of these four names and defaults to all of them. chromedriver is not under test; it is the bridge the Selenium route needs. The remote engine, Kitesurf, does not go through `--engines`; its lane is driven by the recipe machinery documented in [kitesurf-deployment.md](kitesurf-deployment.md).

## Shortest path: one engine first

To see a single engine run as quickly as possible, you need neither four binaries nor the Go/Rust/Ruby toolchains (those only serve the compiled-adapter subsets). Place one engine's binary, install the Node dependencies, and smoke the raw-CDP subset:

```bash
npm ci
python3 -m runner.run run --subset l1.raw_cdp --tag purpose.smoke \
  --engines chrome --score-mode independent --seed smoke
```

Swap `chrome` for `moli`, `lightpanda` or `obscura` for another engine. One boundary to know: a single-engine run writes its result rows to `results.jsonl` as usual (per-task pass/fail is all there), but formal scoring requires the full roster, so a partial-roster run is always `score_included: false`. Use it to verify the environment and observe one engine, not to report numbers.

## Prerequisites

Linux, with cgroup v2 enabled (resource telemetry reads the cgroup and process tree); other platforms are not supported yet. You need Python 3.11 or newer and Node 20. Go, Rust and Ruby are only needed for the compiled adapters: `chromedp` and `rod` (Go), `chromiumoxide` (Rust), `ferrum` (Ruby).

## 1. Engine binaries

Engine binaries are not committed; get them from the table above, and when reproducing published numbers the versions must match the pins (version plus sha256) in the report's provenance table. Place a pinned set under `build_artifacts/sets/<name>/` with a `set.json` manifest, then activate it:

```bash
tools/select_engine_set.sh <name>
```

Conventional paths after activation:

```
build_artifacts/chrome-for-testing/bin/chrome
build_artifacts/moli/bin/moli
build_artifacts/lightpanda/zig-out/bin/lightpanda
build_artifacts/obscura/bin/obscura          # standard build, not stealth
build_artifacts/chromedriver/bin/chromedriver
```

`ENGINE_DEFS` in `runner/run.py` records the evidence pins (version plus sha256); `build_artifacts/active-set.json` overrides them per machine. `doctor` verifies every binary against the pins before any run, so a wrong or tampered binary fails loudly instead of producing quiet numbers.

## 2. Driver dependencies

```bash
npm ci                              # Node drivers, pinned by package-lock.json
pip install -e '.[drivers,dev]'     # selenium + pydoll pins, pytest
gem install ferrum -v 0.17.2
go build -C runner/scripts/adapters/chromedp_adapter -o chromedp_adapter .
go build -C runner/scripts/adapters/rod_adapter -o rod_adapter .
cargo build --manifest-path runner/scripts/adapters/chromiumoxide_adapter/Cargo.toml
```

All driver versions are pinned in `harness_pins.json`, and `doctor` cross-checks what is installed against those pins.

## 3. Inspection

```bash
python3 -m runner.run doctor      # engines launch, identity checks, pins, adapters
python3 -m runner.run validate    # dataset integrity: 1,928 tasks, 18 subsets
python3 -m pytest test -q         # harness unit tests, no engine binaries needed
```

What a failure at each gate means: `doctor` red is an environment problem (missing binary, pin mismatch, adapter not built); `validate` red means the task set on disk does not match the manifest; test failures mean the harness itself is broken. None of these should ever be interpreted as a browser result.

## 4. Run

A smoke run takes a few minutes:

```bash
python3 -m runner.run run --subset l1.raw_cdp --tag purpose.smoke \
  --engines chrome,moli,lightpanda,obscura \
  --score-mode independent --chrome-baseline best_effort --seed smoke
```

A full formal run, in the published configuration:

```bash
python3 -m runner.run run \
  --engines chrome,moli,lightpanda,obscura \
  --score-mode independent --chrome-baseline best_effort \
  --seed official20260709 --k 3 --jobs 16 --host-telemetry on \
  --run-id <explicit-name> --provenance-level minimal
```

## 5. What a run produces

Everything lands in `runs/<run-id>/`:

| File | Contents |
|:---|:---|
| `run_manifest.json` | Full provenance: engine identities, harness pins, digests of the runner source tree, the fixture tree and the compiled adapters |
| `results.jsonl` | One row per task × engine × attempt, with status and failure attribution |
| `scores.json` | Aggregated scores per evaluation axis |
| `scorecard.md` | Human-readable summary |

## 6. Scoring modes

`--score-mode independent` scores every selected engine on its own attempts. This is the published configuration; Chrome participates as a reference column, not a gate. The default `baseline_checked` applies Chrome baseline policies and scores only candidate engines.

`--chrome-baseline best_effort` rather than `required`: under `required`, tasks Chrome fails are removed from every engine's denominator, which forces Chrome's own column toward 100% by construction and makes it useless as a comparison column.

## 7. Resource profiling

Functional scores and resource measurements do not share a run. The A/B protocol is two runs on the same machine, same task set, same seed:

```bash
# round A: baseline, profiler off
python3 -m runner.run run ... --resource-profile baseline --jobs 1 --k 5 --score-mode independent

# round B: engine, profiler on, calibrated against round A
python3 -m runner.run run ... --resource-profile engine --jobs 1 --k 5 --score-mode independent \
  --resource-calibration-baseline runs/<baseline-run>
```

Round B compares its task-duration distribution against round A to quantify how much the profiler itself disturbed the engines. CPU, PSS, process counts and fixture traffic are reported only when that disturbance clears the gate (`resource_comparison_eligible: true`). The full contract is in [resource-cost.md](resource-cost.md).

## Common failures

- `doctor` reports a pin mismatch: the binary under `build_artifacts/` is not the pinned build. Activate the right set or update `active-set.json` deliberately.
- Rows come back as `infra`: the identity gate failed, meaning the client did not reach the engine it was supposed to reach. This is an environment or routing problem, never a compatibility score.
- A compiled adapter is missing: rebuild with the Go/Rust commands above; `doctor` prints the exact command it expects.
