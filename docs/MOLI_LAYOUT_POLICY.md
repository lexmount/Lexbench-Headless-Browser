# Moli layout rerun

Default runs keep layout off and execute every case for the configured `k` attempts (normally three). `--moli-layout on` enables layout from the start.

Add `--try-layout` to rerun failed cases after the complete normal run:

1. Run every case with layout off for all `k` attempts. All attempts must pass for a case to pass.
2. For each failed Moli case, enable layout and run all `k` attempts again. Do not stop early.
3. Only if all `k` rerun attempts pass, replace that case's original results with the complete rerun. Otherwise keep the original failed case unchanged. Never combine successful attempts from the two batches.

Successful original cases are not rerun. `--try-layout` has no extra effect with `--moli-layout on`. A mandatory Chrome baseline rejection does not trigger a Moli rerun. There is no `auto` mode or preliminary classification.

Each rerun uses a fresh browser process and a separate fixture session, with the same task, attempt ordinals and seeds. The final `results.jsonl` has the original matrix size, one authoritative row per engine/task/attempt. Pass rates count cases, not physical executions.

The original matrix is retained as `initial_results.jsonl`; the full rerun is retained as `layout_retry_results.jsonl`. Both artifact sets and their hashes remain available. Only after all reruns finish is the final matrix replaced atomically. An interrupted run remains incomplete and does not yield a publishable final score.

Primary duration and resource fields describe the selected final executions. Failed recovery batches remain in the retry matrix, and the manifest retains total extra execution count and duration. `layout_retry.total_execution_duration_ms` on each replaced row includes its original and rerun durations; resource evidence for both remains in the artifacts. The manifest declares `failed_case_layout_rerun_v1`, records retried/recovered cases and hashes of all three matrices. Reports must disclose this recovery policy separately from fixed-layout runs.

Each completed run also exports a versioned failure list, such as `moli-1.1.7-failure-task-ids.txt`, from the selected final results. Recovered cases are omitted and unsuccessful reruns remain. The manifest binds the version and binary identity; historical lists from other versions stay unchanged.
