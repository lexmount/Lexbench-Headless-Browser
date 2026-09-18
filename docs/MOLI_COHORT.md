# Moli 0.1.1 failure cases and versioned follow-up

`benchmarks/moli-0.1.1-failure-cohort.json` defines the failure follow-up cohort. Its initial list contains 370 tasks from the 372 on which Moli 0.1.1 had no passing attempt in the three-attempt, four-engine `four_engine_full_20260812` run. Two diagnostic probes are excluded because Chrome failed all three source-run attempts: `Browser.getBrowserCommandLine` needs an undeclared `--enable-automation` launch flag; `Schema.getDomains` was sent to the browser connection by a harness routing error, although the task declares a page-target command. The routing error is fixed for new runs; the frozen selection still describes the historical source run. The profile binds the original results, exclusions, task list, and benchmark manifest by SHA-256. This is a diagnostic subset, not a fresh random sample or the full 1,928-task benchmark.

The profile JSON records selection provenance and run settings. `benchmarks/moli-0.1.1-failure-task-ids.txt` is the exact, sorted case list; the profile binds its SHA-256. Both files are inputs to the rerun and comparison tools, not measured outputs.

Run on the **same prepared host** for both Moli versions. The runner records host identity, fixture and runner source hashes, adapter hashes, dependency pins, task contracts, seed, repetitions, concurrency, ChromeDriver, and resource settings. The comparator rejects a changed non-Moli condition. It cannot prove that external operating-system load or network conditions were identical; keep those stable and inspect infrastructure failures before interpreting a success-rate difference.

The tested macOS arm64 setup uses Python 3.11, Node 24, the pinned harness drivers, and ChromeDriver 150.0.7871.49. Resource profiling and host telemetry are disabled for functional comparisons. The general resource benchmark still requires Linux with cgroup v2.

To reproduce the all-layout automation cohort, explicitly pass `--moli-layout on`, which starts Moli with `serve --layout`. Real coordinate input and hit testing require that flag in current Moli releases. The runner's general default remains Moli's lightweight mock-layout mode; it deliberately rejects coordinate mouse and touch dispatch. Optional visual/media resource fetching is separately task-scoped through `launch_profile=all_resources`. Compare version candidates with the same launch flags and frozen run profile; a default-mode run and a layout-enabled run test different runtime configurations.

From a Python environment satisfying the repository's dependencies:

```sh
python tools/run_moli_cohort.py /absolute/path/to/moli-v1 moli_v1_qualified370 --moli-layout on
python tools/run_moli_cohort.py /absolute/path/to/moli-v2 moli_v2_qualified370 --moli-layout on
python tools/compare_moli_cohort.py moli_v1_qualified370 moli_v2_qualified370
```

Each run produces 1,110 result rows under the ignored `runs/` directory and a sibling `<run-id>.conditions.json` receipt. The run tool refuses to overwrite an existing run, checks the frozen cohort and manifest before launch, records the Moli and ChromeDriver binary hashes, and verifies completion. The comparator checks every task and attempt plus all recorded non-Moli conditions. Report pass counts from `results.jsonl`, keeping `infra` and `unsupported` separate from task failure. A Moli upgrade may legitimately change task outcomes, timings, and its binary hash and version.

Add `--try-layout` to an off-mode cohort to rerun its failed cases with layout enabled for all three attempts. Only cases passing all three rerun attempts replace their original results. Use the same retry policy for both version cohorts; original and rerun evidence are retained separately.

## Versioned failure lists

The checked-in `moli-0.1.1-failure-task-ids.txt` is the historical 0.1.1 failure cohort. Its profile explicitly records `source_moli_version: 0.1.1`. Passing those cases with another version does not remove them from the historical list.

Every completed run exports a failure list named after the version actually tested, for example `moli-1.1.7-failure-task-ids.txt`. It contains each remaining failed case once, after layout recovery; an empty file means all selected cases passed. The manifest records the version, binary SHA-256, list hash and count. Different runs remain in separate directories, including different builds of the same version.

Each list covers only its run's selected cases. Do not discard untested cases when following up on a subset. Keep failure lists for different Moli versions separate; preserve each run's frozen task scope and results. Version comparisons must use the same input cohort, even when their resulting failure lists differ.
