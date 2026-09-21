# Moli 0.1.1 failure cases and versioned follow-up

`benchmarks/moli-0.1.1-failure-cohort.json` defines all 372 tasks that did not pass all three Moli 0.1.1 attempts in the four-engine `four_engine_full_20260812` run. Chrome outcomes do not filter this Moli follow-up cohort, and the follow-up runner disables Chrome baseline checks. The profile binds the original results, task list, and benchmark manifest by SHA-256. This is a historical failure subset, not a fresh random sample or the full 1,928-task benchmark. Both `Browser.getBrowserCommandLine` and `Schema.getDomains` remain included; the latter uses the corrected page-target routing in the current harness.

The profile JSON records selection provenance and run settings. `benchmarks/moli-0.1.1-failure-task-ids.txt` is the exact, sorted case list; the profile binds its SHA-256. Both files are inputs to the rerun and comparison tools, not measured outputs.

Run on the **same prepared host** for both Moli versions. The runner records host identity, fixture and runner source hashes, adapter hashes, dependency pins, task contracts, seed, repetitions, concurrency, ChromeDriver, and resource settings. The comparator rejects a changed non-Moli condition. It cannot prove that external operating-system load or network conditions were identical; keep those stable and inspect infrastructure failures before interpreting a success-rate difference.

The tested macOS arm64 setup uses Python 3.11, Node 24, the pinned harness drivers, and ChromeDriver 150.0.7871.49. Resource profiling and host telemetry are disabled for functional comparisons. The general resource benchmark still requires Linux with cgroup v2.

To reproduce the all-layout automation cohort, explicitly pass `--moli-layout on`, which starts Moli with `serve --layout`. Real coordinate input and hit testing require that flag in current Moli releases. The runner's general default remains Moli's lightweight mock-layout mode; it deliberately rejects coordinate mouse and touch dispatch. Optional visual/media resource fetching is separately task-scoped through `launch_profile=all_resources`. Compare version candidates with the same launch flags and frozen run profile; a default-mode run and a layout-enabled run test different runtime configurations.

From a Python environment satisfying the repository's dependencies:

```sh
python tools/run_moli_cohort.py /absolute/path/to/moli-v1 moli_v1_failures372 --moli-layout on
python tools/run_moli_cohort.py /absolute/path/to/moli-v2 moli_v2_failures372 --moli-layout on
python tools/compare_moli_cohort.py moli_v1_failures372 moli_v2_failures372
```

Each run produces 1,110 result rows under the ignored `runs/` directory and a sibling `<run-id>.conditions.json` receipt. The run tool refuses to overwrite an existing run, checks the frozen cohort and manifest before launch, records the Moli and ChromeDriver binary hashes, and verifies completion. The comparator checks every task and attempt plus all recorded non-Moli conditions. Report pass counts from `results.jsonl`, keeping `infra` and `unsupported` separate from task failure. A Moli upgrade may legitimately change task outcomes, timings, and its binary hash and version.


## Input cohort and run results

`moli-0.1.1-failure-task-ids.txt` is the 372-case input cohort derived from Moli 0.1.1 failures. Its profile records `source_moli_version: 0.1.1`. The same input cohort can test later versions; passing with another version does not rewrite this historical input list.

Each run's final verdicts remain in `results.jsonl`, with the tested version, binary identity and selected task scope in `run_manifest.json`. A partial regression run is not a new version-wide failure cohort.

If a future version needs a dedicated input cohort, record that source version and selection evidence in a separate profile and task list. Compare versions on the same input cohort and keep historical runs unchanged.
