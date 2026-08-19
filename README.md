<p align="center">
  <img src="docs/assets/lexbench-hero.png" alt="Lexbench-Headless-Browser — Benchmarking headless browsers as agent runtimes" width="100%">
</p>

<h1 align="center">Lexbench-Headless-Browser</h1>

<p align="center">
  <a href="https://x.com/LexmountAI"><img src="https://img.shields.io/badge/X-%40LexmountAI-000000?style=flat&logo=x&logoColor=white" alt="Follow @LexmountAI on X"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-Apache--2.0-2F80ED.svg" alt="License: Apache-2.0"></a>
</p>

<p align="center">
  <a href="README.md">English</a> · <a href="README.zh.md">中文</a>
</p>

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)"
            srcset="https://img.shields.io/badge/The%20next%20users%20of%20the%20headless%20browser%20are%20agents.-3D74D9?style=for-the-badge">
    <img src="https://img.shields.io/badge/The%20next%20users%20of%20the%20headless%20browser%20are%20agents.-2050B3?style=for-the-badge"
         alt="The next users of the headless browser are agents." />
  </picture>
</p>

You are on `kitesurf-eval`, the evaluation lane for [Kitesurf](https://blog.cloudflare.com/kitesurf/), the agent-first cloud browser Cloudflare just launched. The [`main`](https://github.com/lexmount/Lexbench-Headless-Browser) branch carries the full story: the benchmark's motivation, the four-engine leaderboard over locally pinned binaries, and the resource measurements. Kitesurf exists only behind a remote endpoint, with no binary digest and no process tree to measure, so its evidence class differs from a local binary (`formal_score_eligible: false`). This branch adds the fifth column, the machinery that makes a remote engine measurable at all, and reports the comparison on two explicitly defined task subsets. Everything else, the 1,928-task set, the drivers, the graders and the methodology, is identical to `main`.

## Results

<div align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/five-engine-caliber-b-dark.png">
    <img alt="Task success rate of five headless browsers over 1,308 comparable tasks: Chrome 99.8%, Moli 81.9%, Kitesurf 62.1%, Lightpanda 53.3%, Obscura 44.9%" src="docs/assets/five-engine-caliber-b-light.png" width="100%">
  </picture>
</div>

<div align="center">

| Engine | Task subset A (1,671 tasks) | Task subset B (1,308 tasks) |
|:---|---:|---:|
| [Chrome for Testing](https://googlechromelabs.github.io/chrome-for-testing/) | 1,669 / 1,671 · **99.88%** | 1,306 / 1,308 · **99.85%** |
| [Moli](https://github.com/lexmount/moli) | 1,359 / 1,671 · **81.33%** | 1,071 / 1,308 · **81.88%** |
| [Kitesurf](https://blog.cloudflare.com/kitesurf/) (remote) | 812 / 1,671 · **48.59%** | 812 / 1,308 · **62.08%** |
| [Lightpanda](https://github.com/lightpanda-io/browser) | 697 / 1,671 · **41.71%** | 697 / 1,308 · **53.29%** |
| [Obscura](https://github.com/h4ckf0r0day/obscura) | 660 / 1,671 · **39.50%** | 587 / 1,308 · **44.88%** |

</div>

Both task subsets start from the same 1,928-task corpus and drop tasks for all five engines alike. Task subset A removes 257 tasks in three subsets where a remote endpoint makes failures unattributable, leaving the attributable surface. Task subset B removes a further 363 tasks in four subsets that come back as whole-column zeroes with a single systematic root cause each, leaving the set where the five columns are directly comparable. The Kitesurf lane runs at k=1 with B-class adjudication and covered 1,305 tasks of subset A and 1,302 of subset B; whatever it did not cover counts as not passed. Full report: [docs/reports/five-engine-report-20260813.md](docs/reports/five-engine-report-20260813.md) (the report labels the two task subsets Caliber A and Caliber B).

The four-engine leaderboard over the full corpus, where every engine is a pinned local binary and nothing needs to be dropped, is on [`main`](https://github.com/lexmount/Lexbench-Headless-Browser#results).

## Resource Cost

Resource figures cover the four local engines only. Kitesurf runs on shared infrastructure that this harness does not own, so its CPU, memory and process counts are unmeasurable rather than zero, and an empty cell never means cheap.

<div align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/efficiency-map-dark.png">
    <img alt="Task success rate plotted against median peak memory per task for the four local engines: Chrome at 99.9% and 697 MiB, Moli at 80.7% and 92 MiB, Lightpanda at 43.8% and 34 MiB, Obscura at 39.5% and 39 MiB" src="docs/assets/efficiency-map-light.png" width="100%">
  </picture>
</div>

Median peak process-tree memory per task: Lightpanda 34 MiB, Obscura 39 MiB, Moli 92 MiB, Chrome 697 MiB. Median engine CPU per task on the same set: 36 ms, 38 ms, 101 ms, 687 ms. Details and the calibration record are in the [resource card](docs/reports/resource-card-20260812.md).

## Documentation

<div align="center">

| Document | What it answers |
|:---|:---|
| [docs/kitesurf-deployment.md](docs/kitesurf-deployment.md) | Deploying the fixtures and running the Kitesurf lane |
| [docs/EVIDENCE.md](docs/EVIDENCE.md) | The evidence chain: run fingerprints, release archives, and the commands that regenerate the reports |
| [docs/RUNNING.md](docs/RUNNING.md) | Installing the engines and drivers, and running the bench |
| [docs/REPRODUCE.md](docs/REPRODUCE.md) | Reproducing the published runs and regenerating their reports |
| [docs/RESULTS.md](docs/RESULTS.md) | Reading the results: scoring boundaries and limits |
| [docs/reports/](docs/reports/) | The reports themselves, generated from run artifacts |

</div>

## What this branch adds

- The generic `remote_cdp` identity contract in the runner and the adapters, with three-field same-connection verification, so a remote endpoint passes the same anti-substitution gate a local binary does. See [runner/scripts/adapters/PROTOCOL.md](runner/scripts/adapters/PROTOCOL.md).
- The recipe machinery: `tools/kitesurf_experiments.py {check,list,render,run}` over [config/kitesurf_experiments.json](config/kitesurf_experiments.json).
- A two-origin fixture contract: static fixtures served from this repository's GitHub Pages (`pages/`, pinned by [config/kitesurf_static_fixture.json](config/kitesurf_static_fixture.json)) and a self-deployed dynamic origin (`python3 -m runner.run fixture-serve` behind an HTTPS tunnel, contract-verified by [config/kitesurf_dynamic_fixture.json](config/kitesurf_dynamic_fixture.json)).
- Five-engine aggregation: `tools/report_five_engine.py` computes the published denominators, the task-subset split and the B-class adjudication instead of narrating them.

Start with [docs/kitesurf-deployment.md](docs/kitesurf-deployment.md) for the fixture resolution rules and the four-command flow. Once Kitesurf ships a local binary it joins the main roster and this branch retires.

## License

Apache-2.0. See [LICENSE](LICENSE). Upstream task and fixture attributions are retained in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
