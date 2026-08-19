# TESTING — current implementation

This file documents how to test the checked-in runner. Function names, CLI
commands, and truth tables follow `runner/run.py`, which is the source of truth.

## 0. Current-state alignment (facts you must know before testing)

| Item                      | Current state                                                                            | Impact on testing                                       |
| ------------------------- | --------------------------------------------------------------------------------------------------- | ------------------------------------------------------- |
| Structure                 | The main runner is `runner/run.py`; tests live in `test/`, and real-binary smoke tests are skipped by default via an environment switch | Unit tests directly `import runner.run`; `python3 -m pytest test -q`     |
| CLI                       | `python -m runner.run {doctor,validate,list,run,report,inspect}`; `run` adds `--jobs N`           | `--jobs` parallelism (each worker gets its own process/port/profile)      |
| Enabled tasks             | **1928 tasks**, 18 subsets; L1=1740, L2=188, of which 116 scenarios generate 1233 per-driver tasks | All enabled tasks execute and retain evidence; for L2 the capability map determines the headline denominator |
| Driver routing            | raw CDP, Node CDP probe, Playwright/Puppeteer framework probes, plus 11 unified scenario adapter paths | The 4×13 binding catalog must stay consistent with runtime dispatch |
| CDP sessions              | Unified browser-ws + `Target.createTarget/attachToTarget(flatten)` session flow (required for LP/Obscura), with `/json/new` as the fallback | All four engines share the same protocol path; Pydoll+Obscura only add a version-pinned flattened-session bootstrap |
| grader                    | Inline checks (value_equals/value_type/value_truthy/value_contains/eval_no_exception/eval_has_exception/no_error; empty checks fail); server-side `/__grade__/expected_answer` (equals/contains/contains_all) | L1 and L2 grading paths |
| `default_k_runs`          | Both the manifest and the runner fallback are 1                                                                    | Development, smoke, and non-release results uniformly use k=1; only official release evidence uses an explicit `--k 3` |
| Chrome environment        | Launch carries hermetic flags (host-resolver blackhole, background networking and HTTP cache disabled) plus `DBUS_*=/dev/null`; browser logs go to a spool file | Background traffic is controlled and log backpressure cannot freeze the browser process |
| Moli environment          | Resource-frugal by default; only tasks with `launch_profile=all_resources` append `--resource` | Resource dependencies are declared explicitly by the task; a profile switch replaces the current process — two concurrent processes must never pollute memory results; profile and flags must enter provenance |

> **Testing stance**: the framework truth tables (§5/§6/§7) are locked down by `test/`; correctness is anchored to Chrome gold rather than historical run snapshots.

## 1. Mapping the two test categories onto this code


| Category                  | Form it takes in this code               |
| ------------------------- | ---------------------------------------- |
| Framework robustness     | §2 unit tests + §3 engine-less integration + §5/§6/§7 truth-table lockdown |
| Real-engine acceptance   | §4 real-engine smoke + §9 acceptance commands    |


## 2. Unit test targets (real functions → required assertions)

Directly `from runner import run` and assert on pure functions; no browser needed.


| Function                        | Required assertions                                                                              |
| ------------------------------- | ----------------------------------------------------------------------------------------------- |
| `expand_tasks`                  | `task_glob` expansion; `--task/--subset/--feature/--tag` filtering; deduplication by path; unknown subset/task raises `BenchError`           |
| `validate_manifest`             | Missing `bench_id/…/layers` errors; `fallback_allowed` not false errors; an enabled subset that expands to 0 tasks without `allow_empty` errors |
| `validate_task`                 | See the "schema rules" below, item by item                                                                                |
| `resolve_gate_policy`           | Priority `CLI > task > subset > suite > "off"`; internal legacy names still use gate, user-facing copy says Chrome baseline |
| `score_eligible_for_run`        | See the §5 table; each false reason is triggered individually                                                                        |
| `should_include_score`          | See the §5 table                                                                                          |
| `seed_for_attempt`              | Same `(base_seed, task_id, attempt)` → stable 12 digits; different attempt → different; `base_seed=None` → random but recorded for that round          |
| `is_unsupported_error`          | "method not found"/"not found"/"unsupported"/"unknown method" → True; anything else → False                 |
| `serve_engine_launch_command`   | Moli has no extra resource flags by default; only `all_resources` appends `--resource`; Obscura keeps `--allow-private-network`; flags must not leak into other engines |
| `BrowserManager.launch`         | Identical effective args reuse the process; when the task profile changes, the old process is killed before launching, and `processes` holds at most one instance per engine at all times |
| `grade_inline`                  | `value_equals_3` / `result_type_number` hit; unknown check → fail                                      |
| `FixtureServer.expected_count`  | Pure and deterministic: `10 + int(sha256(seed)[:2],16)%90` ∈ [10,99]                                            |
| `FixtureServer.grade_inventory` | pass/fail combinations of the three checks; all pass → `ok:true,failure:null`; otherwise `cdp_semantic`                             |
| `summarize_results`             | Keeps task-row pass_rate/legacy pairwise; adds two axes, L1 compatibility and L2 capability semantic correctness; failure_classes/failure_origins and Chrome baseline counts |
| `validate_capability_map`       | Every active L2 task is covered exactly once; role/tag alignment; semantic/cross-check must use a server-side grader; cross-check must have a canonical semantic probe |
| `summarize_semantic_results`    | Multiple semantic probes of the same capability are combined all-pass into one `(capability,engine,attempt)` verdict; cross-check/diagnostic do not enter the denominator; a partial capability is recorded as missing |
| `process_diagnostic`            | running/exited/timed_out for engine/driver processes, returncode, signal/name, and the core-dump bit when obtainable; unknown evidence must be null |
| `artifact_paths`                | Path = `artifacts/<layer>/<subset>/<task>/<engine>/<attempt>`; tmp and final are separated                      |
| `write_json` / `append_jsonl`   | Atomic rename; JSONL is one object per line; `read_jsonl` raises a `BenchError` carrying the line number on a bad line                                        |


**`validate_task` schema rules (assert each item, with one bad sample per rule):**


| Rule                                                                    | The bad sample should report                             |
| ---------------------------------------------------------------------- | ------------------------------------------------------- |
| All required fields present                                             | Missing `grader` → `missing required field grader`            |
| `layer` == the subset's layer                                           | An L2 task placed in an L1 subset → reports layer mismatch                 |
| `subset_id` == subset                                                   | Mismatch → reports subset_id mismatch                               |
| `description` is a non-empty string                                     | Empty string / non-string → reports description must be non-empty           |
| `features` is a list of normalized capability-tag strings               | Non-string list → reports features must be a string list              |
| `tags` is a list of selection/management metadata strings               | Non-string list → reports tags must be a string list                  |
| Obsolete scoring and curation fields are forbidden                       | `score_lane`, `score_policy`, `status`, `source_reference`, `migration_notes`, or `expected_status_claim` → reports obsolete |
| `chrome_gate` parses to a valid policy; defaults to `off` when unset    | Invalid policy → reports invalid chrome_gate / valid Chrome baseline policy |
| `driver.kind ∈ {raw_cdp,node_cdp_probe,framework_playwright,framework_puppeteer}` and == subset.driver | `puppeteer_cdp` → reports unsupported driver.kind             |
| `node_cdp_probe` must have an existing `driver.script`                  | Points at a missing file → reports script not found                            |
| `scene.kind ∈ {about_blank,self_hosted_fixture}`                        | Anything else → reports unsupported scene                              |
| `self_hosted_fixture.url` starts with `/`                               | `http://…` → reports must be absolute fixture path            |
| `grader.kind ∈ {inline_assertions,server_side}`                         | Anything else → reports unsupported grader                               |
| `inline_assertions` has non-empty `grader.checks` or `driver.checks`    | Only a success return envelope with no verifiable check → reports return envelope alone is not verifiable |
| `server_side.endpoint` starts with `/`                                  | Mismatch → reports an error                                                  |
| `artifact_profile ∈ ARTIFACT_PROFILES`                                  | Anything else → reports an error                                                  |
| Optional `launch_profile ∈ {default,all_resources}`                     | Anything else → reports an error; unset resolves to `default`                                |

`config/l2_semantic_capabilities.json` is additionally constrained by these global rules: every active L2 task
maps to exactly one primary capability; `purpose.diagnostic` may only map to `diagnostic`;
`semantic_probe`/`driver_cross_check` must go through the `server_side` grader. Both full and partial
`validate` runs validate the entire active L2 map, so that a narrow selection cannot hide unmapped new tasks.

`validate` also checks expected-answer registry coverage: any task whose `grader.endpoint` is
`/__grade__/expected_answer` must have an entry in `fixtures/expected_answers.json`
(including all `expected_answers.fragment.json`); otherwise it errors instead of waiting until runtime
to record infra. Server-side grading of L2 rows also merges driver step checks: if any check fails,
the whole row fails (L1 scenario rows keep the historical server-grade-only convention).

The inline grader has two additional identity/representation-agnostic primitives: `array_match_contains`
uniquely locates an array element by its match field and then asserts on its nested array (insensitive to
positional reordering; multiple matches are judged fail); `saved_body_text_equals` decodes the body envelope
per `base64Encoded` and compares the content. On the scoring side, tasks tagged with the
`purpose.diagnostic` tag get `score_included=false` for all engines — a task the oracle cannot verify
must not be scored for candidate engines only.


## 3. Engine-less integration (fake CDP / fake server)

Verifies the CLI and run loop without starting a real browser.


| Test                    | Command / technique                                             | Expectation                                          |
| ----------------------- | --------------------------------------------------------------- | ---------------------------------------------------- |
| validate passes         | `python -m runner.run validate`                                 | `OK … tasks=2 subsets=…`, exit 0                        |
| validate catches a bad task | Temporarily insert a task JSON with a missing field         | One `ERROR:` per issue to stderr, exit 1                            |
| list tasks --json       | `list tasks --json`                                             | Stable JSON array with all fields present                                       |
| dry-run                 | `run --dry-run`                                                 | Prints resolved tasks + the Chrome baseline policy; does not create a run directory                |
| fake CDP drives raw_cdp end to end | Start a fake `/json/version`+WS, replay `Runtime.evaluate→{value:3}`         | status=`pass`                                        |
| fake CDP injects unsupported | The WS replies `{"error":{"message":"'Foo.bar' wasn't found"}}` to some method | status=`unsupported`,class=`engine_unsupported`      |
| fake CDP injects a semantic error | Replies `{value:2}`                                     | status=`fail`,class=`cdp_semantic`                   |
| fake CDP task/socket timeout | The command wait raises `socket.timeout`                       | status=`timeout`,origin=`task_timeout`; the engine process is still running |
| fake CDP disconnect     | Close immediately after the handshake                           | status=`crash`,class=`infra`,origin=`client_transport`; must not be called an engine crash |
| fake CDP disconnect with engine exit | At disconnect the fake engine returncode is a signal | status=`crash`,origin=`engine_process`, with signal/core evidence recorded |
| Plain OSError           | FileNotFound/generic I/O/URLError                               | status=`infra`, no origin/process; must not be called client transport |
| scripted dirty stdout   | Make the script print non-JSON                                  | status=`infra`,class=`script_error`                  |
| scripted non-zero exit  | Script `process.exit(1)`                                        | status=`infra`,class=`script_error`,origin=`driver_process` |
| driver failure + engine exit | The adapter exits non-zero or returns a structured `ok:false` while the engine exits on a signal | engine_process is primary; driver diagnostics are kept in secondary_failure/process |
| driver pass + engine exit   | The adapter returns pass, but the reusable engine has already exited on a signal | status=`crash`,origin=`engine_process`; the original pass is kept as secondary_status |
| Pydoll protocol error    | The private `_execute_command` returns `{"error":...}`         | click step `ok=false`, the error is retained, `cdp_error_count` increments |


> Minimal fake CDP surface: `/json/version` returns `webSocketDebuggerUrl`; the WS accepts `Page.enable/Runtime.enable/Page.navigate/Runtime.evaluate` and supports injecting errors/disconnects/timeouts. The same fake also serves the §5 truth tables.

## 4. Real-engine smoke (exact commands)

Expensive; manual / nightly. Binaries are pinned by this repository's `build_artifacts/` (`doctor` verifies the sha12).


| smoke           | Command                                                        | Pass condition                                                |
| --------------- | -------------------------------------------------------------- | ------------------------------------------------------------- |
| doctor          | `python -m runner.run doctor`                                  | All four engines report `ready:<product>`, Obscura version/SHA exact, node present, final line `OK` |
| L1 chrome       | `run --task runtime_evaluate_basic_001 --engines chrome --k 1` | chrome attempt `pass` (`1+2==3`)                               |
| Moli Selenium   | `run --task sc_cs_url_surface__se --engines moli --k 1 --chrome-baseline off --score-mode independent` | attempt `pass`, and the binding transport is `native_webdriver` |
| L1 three engines | `run --subset l1.raw_cdp --tag purpose.smoke --engines chrome,moli,lightpanda --k 1` | A complete run directory is produced; Chrome/Moli/Lightpanda each emit a status |
| L2 three-engine smoke | `run --layer L2 --engines chrome,moli,lightpanda --k 1` | 188×3 rows of evidence land completely on disk; the report gives full capability verdicts and separate cross-check and diagnostic sections |
| report          | `run` auto-reports by default; `report --run runs/<run_id>` can backfill an old run          | Writes `scores.json`/`scorecard.md`               |
| inspect         | `inspect --run runs/<id> --task … --engine moli --attempt 1`   | Prints status/failure/grader/the last CDP error                         |


## 5. Truth table A: Chrome baseline / status / score (core architectural invariants — must be locked)

**5.1 Chrome baseline branches (`run_attempts`)**


| Resolved baseline    | Does Chrome run | When Chrome does not pass          | Candidate engines |
| -------------------- | ---------- | ------------------------------ | ---------------- |
| `required`           | Runs first         | Candidates are written `chrome_gate_fail` (legacy status, unscored) and skipped | Run only if Chrome passes |
| `best_effort`        | Runs but does not block      | Recorded, not skipped                         | Run regardless               |
| `off`                | Does not run         | ——                             | Run directly              |


> Current `manifest.json`: L1 subsets default to `best_effort`, and L2 `l2.web_platform` defaults to `required`; when an override is needed, prefer an explicit `--chrome-baseline off|best_effort|required`. `--chrome-gate` is a hidden alias kept for compatibility with old scripts.

**5.2 Exception → status / failure.class (`run_driver_attempt`)**


| Trigger                                    | status             | failure.class        | failure.origin | kernel_workitem |
| ------------------------------------------ | ------------------ | -------------------- | -------------- | --------------- |
| All grading passes                         | `pass`             | null                 | — | —               |
| Raw grading fails                          | `fail`             | `cdp_semantic`       | — | —               |
| `CDPCommandError` with unsupported wording | `unsupported`      | `engine_unsupported` | — | false           |
| `CDPCommandError`, not unsupported         | `fail`             | `cdp_semantic`       | — | **true for moli** |
| task/subprocess `TimeoutExpired` with the engine still alive | `timeout`       | `infra`              | `task_timeout` | false           |
| socket/`TimeoutError` with the engine still alive | `timeout`          | `infra`              | `task_timeout` | false           |
| Client transport disconnects but the engine is still alive | `crash` (compatibility status) | `infra`              | `client_transport` | false       |
| Engine exit/signal/core already observed   | `crash`            | `infra`              | `engine_process` | false          |
| FileNotFound/plain I/O/URLError            | `infra`            | `infra`              | — | false           |
| Other exceptions                           | `infra`            | `infra`              | — | false           |
| scripted non-zero exit                     | `infra`            | `script_error`       | `driver_process` | false          |
| scripted dirty stdout                      | `infra`            | `script_error`       | — | false           |
| scripted `ok:false` + `engine_unsupported` | `unsupported`      | `engine_unsupported` | — | false           |
| scripted `ok:false`, anything else         | `infra`            | `script_error`       | — | false           |
| Candidate skipped because the Chrome baseline did not pass | `chrome_gate_fail` | `infra`              | — | false           |

`status=crash` is a coarse-grained status kept for compatibility with existing consumers and cannot be read
directly as a browser crash. Only when `failure.origin=engine_process` and `failure.process.state=exited`,
or an old artifact provides equivalent exit/signal/core evidence, may a report state engine/browser crash.
Whenever the reusable engine is observed to have exited by the time the attempt completes, the engine is
primary; even if the driver already returned pass, the attempt is promoted to crash and the pass is kept in
`secondary_status`. If there was already a driver failure/process, they are kept in `secondary_failure`
and `secondary_process` respectively.


**5.3 Every reason for `score_eligible_for_run` → false**


| Reason                  | Trigger condition                                     |
| ----------------------- | ---------------------------------------------------- |
| suite fallback          | `fallback_allowed` not false                           |
| debug                   | `--debug`                                            |
| partial engine set      | Some task exists and the run is neither the full four-engine set nor a baseline-checked run with "the complete three candidate engines + all Chrome gates explicitly off" |


**5.4 `should_include_score` (whether a single attempt is scored)**

(Lanes have been removed; all tasks within a subset are scored uniformly. Which engines are scored depends on `--score-mode`.)

- `baseline_checked` (default): scored ⇔ `score_eligible` and engine∈{moli,lightpanda,obscura} (Chrome is unscored; it only runs as a robustness baseline when the baseline is explicitly required/best_effort) and status∉{chrome_gate_fail,infra} and not fallback. The complete candidate roster combined with `--chrome-baseline off` allows official scoring without rerunning Chrome.
- `independent`: scored ⇔ `score_eligible` and engine∈{chrome,moli,lightpanda,obscura} (all four scored independently, no gate) and status∉{infra} and not fallback.

(That is: genuine `fail/unsupported/timeout/crash` **count toward the denominator**; only Chrome-baseline skips and infra do not.)

## 6. Truth table B: artifact write invariants


| Invariant                                       | Code basis                                      | How to test                                |
| ----------------------------------------------- | ----------------------------------------------- | ------------------------------------------ |
| Write to a tmp directory first, then atomically rename to final                     | `artifact_paths` + `tmp_dir.replace(final_dir)` | After the run, final exists and no `.tmp-`* residue remains                |
| Refuse to overwrite an existing attempt/run directory                          | `if final_dir.exists(): raise` / `reserve_run_dir(..., error)` | An explicit `--run-id` is used verbatim (no timestamp suffix); the default run id is a UTC timestamp; conflicts auto-append `_002`, and only an explicit `--run-id-conflict error` refuses |
| Every attempt has `run.json`+`grader.json`           | `write_json` writes unconditionally                               | Failed attempts are present too                              |
| All files declared by the profile exist (may be empty)                           | `ensure_profile_files`                          | l1/l2_standard each have all 5 files (run/cdp/grader/stdout/stderr) |
| `results.jsonl` has one attempt per line and is **written after the artifact** | The append happens after `tmp_dir.replace`                   | Line count == attempt count; every line parses via `read_jsonl`       |
| `run.json` matches the corresponding `results.jsonl` line              | The same `result` dict                                | Spot-check fields                                     |


## 7. Truth table C: fixture + server grader (the anti-cheating core)

`FixtureServer` (`/storage/indexeddb_inventory`):


| Assertion       | Explanation                                                                          |
| --------------- | ----------------------------------------------------------------------------------- |
| The answer is verified against the server-side seed rule | `expected_count(seed)` is the grader's ground truth; the page carries the target count to complete the IDB write/read, but that cannot substitute for server-side grading |
| The seed determines the answer, deterministically    | Same seed → same count; different seed → almost certainly different                                                    |
| Server-side observation traces | The page reports `idb_write`/`idb_read` via `fetch(/__event__)`, and the grader inspects `events[session]` — not pure self-attestation    |
| The three checks align with the semantics   | `answer_matches_seed` (answer correct) + `indexeddb_write_observed` + `indexeddb_read_observed` |
| Only all-green passes        | Any fail → `ok:false` + `cdp_semantic`                                               |


> Anti-cheating test technique: construct a payload where "the answer is correct but no write event was reported" → the grader must judge `indexeddb_write_observed:fail` (proving it does not merely look at the answer).

## 8. Known gaps

| gap                       | Current state                                       | To do                                                                       |
| ------------------------- | -------------------------------------------------- | -------------------------------------------------------------------------- |
| **observability class**   | Present in the enum, but no automated path produces it                                     | Needs an adjudication source for "the state exists but the snapshot cannot see it"                                                      |
| **L1 ab dialog** | A few AB confirm-dialog cases behave differently from Chrome under headless on this machine | Attributed to tooling/environment differences, not the kernel; entering the leaderboard would require separate anchoring |


## 9. Acceptance criteria (the concrete commands this version of the code must pass)

**It runs (framework baseline):**

1. `python -m runner.run validate` → `OK`, exit 0.
2. `python -m runner.run list tasks --json` → stable JSON.
3. `python -m runner.run run --task runtime_evaluate_basic_001 --engines chrome --k 1` → produces a complete run directory and report files.
4. `python -m runner.run report --run runs/<id>` → can regenerate `scores.json` / `scorecard.md` from an old `results.jsonl`.
5. `python -m runner.run inspect …` → prints the key information of a failed attempt.
6. A normal `run` → produces `host_telemetry.jsonl` / `host_summary.json`; collection anomalies do not change functional status.
7. `--resource-profile engine --jobs 1 --k 5 --score-mode independent` together with a profiler-off
   baseline → produces `resource_summary.json` / `resource-card.md`; `resource_comparison_eligible=true`
   is set only when the all-pass intersection, host pollution, and observer-effect quality gates all pass.

**Robust (architectural invariants):**

1. Every exception mapping in §5 is reproduced with fake CDP; status/failure.class all match.
2. All §6 write invariants are green (atomicity, refusal to overwrite, matching line counts).
3. §7 fixture anti-cheating: a forged payload is correctly rejected by the server grader.
4. Reproducible: two runs with a fixed `--seed`, after normalization (stripping timestamps/paths/durations), yield identical `scores.json`.

**Acceptance (data side, Chrome gold convention):**

1. Chrome baseline check: on required tasks Chrome must fully pass, otherwise candidate engines skip that task (not counted as a candidate fail); it expresses whether the case holds on the gold baseline.
2. L1 concordance for `purpose.smoke` / `coverage.core` is ≥95% with explainable deviations; the L2 "Moli passes / LP collapses" direction holds.

## 10. The actual test/ directory (landed)

```text
Lexbench-Headless-Browser/
  test/
    conftest.py                  # sys.path bootstrap + fake_cdp / fixture_server / bench factories
    _fakes.py                    # FakeCDP (scripted HTTP+WS) / StubProc / task & manifest factories
    _stub_scripts/stub.js        # mode-switched node stub for node_cdp_probe (no real CDP connection)
    test_unit_validate.py        # §2: good+bad for every validate rule
    test_unit_versioning.py      # dataset (bench_version) and harness version: shapes, single source, run_manifest
    test_unit_gate_score.py      # §5: Chrome baseline priority / 5 score_eligible reasons / should_include_score
    test_unit_checks.py          # §2: all grade_inline kinds / seed determinism / parse_engines / placeholder substitution
    test_fixture_server.py       # §7: static serving / routing / directory-traversal blocking / expected_answer / anti-cheating
    test_integration_fake_cdp.py # §5.2 exception→status truth table + §6 artifact invariants
    test_scripted_driver.py      # node stub: dirty stdout / non-zero exit / unsupported / server+inline grader / env templates
    test_framework_driver.py     # framework_playwright/pp: steps/scene validation / env assembly / binding validation / node probe failure classification
    test_resources.py            # host/cgroup/PSS/CPU/HTTP+redirect+SSE+WS traffic, A/B summary, target cleanup
    test_smoke_engines.py        # §4: the real four engines, @skipif not ABB_ENGINE_TESTS
```

How to run: `cd Lexbench-Headless-Browser && python3 -m pytest test -q`; the all-green result at the current commit is authoritative —
do not freeze drift-prone test counts into documentation.

Five `run.py` defects found and fixed while writing tests: `is_unsupported_error` missed Chrome's `'X' wasn't found`; `read_fixture_file` had prefix-based directory traversal (switched to `is_relative_to`); an empty checks list passed vacuously (changed to inject a `checks_declared` failure); the other 2 items (the grade_inventory anti-cheating OR logic, and the loss of `ws_disconnect_count` on crash) are honestly flagged in the tests as remaining behavior.
