# Reproducing the Moli failure cohort

`benchmarks/moli-chrome-qualified.json` freezes 370 tasks from the 372 on which Moli 0.1.1 had no passing attempt in the three-attempt, four-engine `four_engine_full_20260812` run. Two diagnostic probes are excluded because Chrome itself failed all three attempts under the published contract: `Browser.getBrowserCommandLine` needs an undeclared `--enable-automation` launch flag, and the pinned Chrome protocol no longer provides `Schema.getDomains`. The profile binds the original results, exclusions, task list, and benchmark manifest by SHA-256. This is a diagnostic subset, not a fresh random sample or the full 1,928-task benchmark.

Run on the **same prepared host** for both Moli versions. The runner records host identity, fixture and runner source hashes, adapter hashes, dependency pins, task contracts, seed, repetitions, concurrency, ChromeDriver, and resource settings. The comparator rejects a changed non-Moli condition. It cannot prove that external operating-system load or network conditions were identical; keep those stable and inspect infrastructure failures before interpreting a success-rate difference.

The tested macOS arm64 setup uses Python 3.11, Node 24, the pinned harness drivers, and ChromeDriver 150.0.7871.49. Resource profiling and host telemetry are disabled for functional comparisons. The general resource benchmark still requires Linux with cgroup v2.

From a Python environment satisfying the repository's dependencies:

```sh
python tools/run_moli_cohort.py /absolute/path/to/moli-v1 moli_v1_qualified370
python tools/run_moli_cohort.py /absolute/path/to/moli-v2 moli_v2_qualified370
python tools/compare_moli_cohort.py moli_v1_qualified370 moli_v2_qualified370
```

Each run produces 1,110 result rows under the ignored `runs/` directory and a sibling `<run-id>.conditions.json` receipt. The run tool refuses to overwrite an existing run, checks the frozen cohort and manifest before launch, records the Moli and ChromeDriver binary hashes, and verifies completion. The comparator checks every task and attempt plus all recorded non-Moli conditions. Report pass counts from `results.jsonl`, keeping `infra` and `unsupported` separate from task failure. A Moli upgrade may legitimately change task outcomes, timings, and its binary hash and version.
