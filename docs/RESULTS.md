# Reading the results

[English](RESULTS.md) · [中文](RESULTS.zh.md)

The reports print the numbers. This page defines what the numbers mean, where their boundaries sit, and what they cannot support.

## What the headline number is

The headline pass rate counts a task as passed only when all k attempts pass (k=3 in the published run). A task that passes twice and times out once counts as failed. This makes the headline sensitive to instability on purpose: an agent retrying a flaky operation burns its budget just as surely as it does on a hard failure.

Every engine is scored on its own attempts (`--score-mode independent`). Chrome is a reference column, not a gate. The alternative, `--chrome-baseline required`, would remove Chrome-failing tasks from every engine's denominator and push Chrome's own column toward 100% by construction, which is why the published runs use `best_effort`: Chrome's 99.90% is a measured value, and the two tasks it fails stay in everyone's denominator.

## Status taxonomy

Every result row carries exactly one status:

| Status | Meaning | Counts against the engine? |
|:---|:---|:---|
| `pass` | Operation completed and every check passed | counts for |
| `fail` | Connected and ran, but the operation or its check failed | yes |
| `unsupported` | The engine reports the capability as not implemented | yes, and it is the honest failure mode |
| `timeout` | Task budget exhausted | yes |
| `crash` | Engine process died | yes |
| `infra` | Identity gate or environment failed; the engine was never validly reached | no; the row is excluded as harness territory |

Failing rows additionally carry `failure.class` and `failure.origin`, so a report can attribute a miss to the protocol surface, the page semantics, the driver stack, or the harness, instead of leaving a bare count.

## The L1 axis: one behavior, thirteen ecosystems

L1 deliberately repeats behavior across driver stacks, because driver compatibility is the dimension under measurement. 1,233 of the 1,740 L1 tasks are expanded from 116 driver-independent scenario specs, so when an engine passes a scenario under Puppeteer and fails it under Selenium, that difference is the finding.

Read a whole-column zero as a bootstrap failure, not as 92 separate misses: the stack cannot complete its session handshake against that engine, and every task behind the handshake is unreachable. The reports attribute each zero column to its root cause.

The published run also shows the direction of the signal is mixed, which is worth reading as evidence about the bench itself: Lightpanda beats Moli in six subsets (`l1.puppeteer` 114:96, `l1.agent_browser_scenarios` 80:66, `l1.cdp_use`, `l1.chrome_remote_interface` and `l1.stagehand` 76:69, `l1.agent_browser_tool` 68:64) while Moli leads overall. A task set tuned to favour one engine would not produce that pattern.

## The L2 axis: capabilities, not task counts

L2 grades semantics by behavior. Fixtures run on the harness's own server, and a server-side grader checks the observable outcome (DOM state, storage state, network server state, or a workflow result) rather than trusting whatever the protocol echoed back.

Raw task rows are not the denominator. The 188 L2 tasks map through [`config/l2_semantic_capabilities.json`](../config/l2_semantic_capabilities.json) onto 72 capabilities under three roles:

- a `semantic_probe` counts toward its capability's verdict, and a capability passes only when every scored probe assigned to it passes;
- a `driver_cross_check` is reported separately and adds no denominator unit;
- a `diagnostic` provides failure-localization evidence and adds no denominator unit.

The conjunction is strict but its weight is one. A six-vector WebCrypto digest family can localize an algorithm-specific bug without turning one implementation gap into six headline failures; conversely a capability does not pass on a lucky subset of its probes. In the published run this yields the 192 scoring units in the report's L2 axis.

Two guardrails keep this stable. A partial run that selects only some probes of a capability marks that capability `missing` instead of silently computing a score from half the evidence. And the run manifest snapshots the capability map (path and sha256), so a run remains interpretable after the map evolves.

For multi-step workflow tasks the grading is two-gated: the driver's step checks must complete and the fixture server must accept the extracted final answer against its expected-answer registry. Driver-side equality checks alone never decide a workflow.

## Reading the resource numbers

Resource figures come from the separate A/B-calibrated round documented in [resource-cost.md](resource-cost.md) and [REPRODUCE.md](REPRODUCE.md). When reading them:

- They are computed on the 1,045 task-attempt intersection that all four engines passed. A failed task never enters an engine's account as a cheap failure.
- They describe the 557-task resource set and do not extrapolate to the full corpus.
- Filled values are medians; the resource card also carries p95 values, PSS deltas, process counts and fixture traffic.
- A remote engine has no process tree on the measuring machine. An empty cell means unmeasurable, never zero.

## The five-engine comparison

The main branch reports four locally pinned binaries. The [`kitesurf-eval`](https://github.com/lexmount/Lexbench-Headless-Browser/blob/kitesurf-eval/README.md) branch adds Kitesurf, a remote endpoint, and publishes its comparison on explicitly defined task subsets: first dropping the subsets whose failures cannot be attributed across a remote boundary, then further dropping subsets that are systematically blocked end to end. The subset definitions, the adjudication rule and the five-engine table live in that branch's report; the short version is that a remote endpoint is only comparable inside an explicitly stated denominator.

## Why these mechanisms

A benchmark published by an engine's own team invites one question first: were the tasks picked to flatter that engine? The task set's coverage came from surveying what pinned versions of Playwright, Puppeteer, agent-browser and the other stacks actually call, then filling the paths real frameworks hit; every task must pass on Chrome to be admitted at all. The mixed subset signal above is the observable consequence.

The L2 mechanisms are the ones production pages lean on, with third-party usage data:

- Modern selectors are everyday load-bearing surface. Chrome's use counter has pages using `:has()` in 51.7% of page loads as of June 2026, up from 40.6% a year earlier ([chromestatus, bucket 4743](https://chromestatus.com/metrics/css/timeline/popularity/4743)). Project Wallace's 2026 crawl of top-site homepage CSS finds `:has()` in 41.3% of stylesheets, `:nth-child` in 76.8%, `:nth-of-type` in 53.2% ([The CSS Selection 2026](https://www.projectwallace.com/the-css-selection/2026)). A selector evaluated wrongly is a content or visibility state computed wrongly, not a cosmetic blemish.
- Client-side storage is silent infrastructure. Chrome's last published counters had IndexedDB reads on 19.3% and writes on 16.6% of page loads ([bucket 3023](https://chromestatus.com/metrics/feature/timeline/popularity/3023), [bucket 3024](https://chromestatus.com/metrics/feature/timeline/popularity/3024)); Firestore's web offline persistence is IndexedDB ([docs](https://firebase.google.com/docs/firestore/manage-data/enable-offline)). The agent-relevant failure mode motivated these tasks: an engine that degrades a missing storage API gracefully renders the page anyway with empty data, and the agent confidently reports a wrong answer instead of an error. Only a storage-semantics task exposes that.
- Driver interaction primitives consume layout and computed style directly. Playwright's actionability rules require a non-empty bounding box, computed visibility and hit-target checks before every click or fill ([playwright.dev/docs/actionability](https://playwright.dev/docs/actionability)). An engine with incomplete style or layout does not lose one task; it loses the framework's waiting and clicking substrate. Lightpanda's own documentation states its Web API coverage is incomplete ([lightpanda.io/docs](https://lightpanda.io/docs/)), consistent with where its L2 misses cluster.

## Boundaries

- Screenshot, PDF and raster output are outside the current measurement scope. This is a deferred boundary, not a permanent verdict: it predates candidate engines growing paint pipelines and is tracked for re-evaluation. Nothing here measures pixel correctness.
- Functional results come from one pinned engine set on one machine class. Different builds are different software; compare manifests before comparing numbers.
- Chrome's 99.90% is measured, not axiomatic. The two tasks it fails are visible in the report and stay in every denominator.
