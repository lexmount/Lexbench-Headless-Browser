# Evidence

[English](EVIDENCE.md) · [中文](EVIDENCE.zh.md)

Every number in `docs/reports/` comes from a run's own products. This page is
the index between the two: which files fix a run's identity, which archive
holds its raw rows, and what each archive's sha256 should be.

The chain runs report → this index → release asset. The small files travel with
a clone, the bulk products stay out of git history.

## What ships in the repository

`docs/evidence/<run_id>/` carries the files that fingerprint a run. About
4.7 MB in total, enough to check what produced a published number without
downloading anything.

| File | What it fixes |
|:---|:---|
| `run_manifest.json` | Engine versions and sha256, driver pins per ecosystem, digests of the runner source tree, the fixture tree and every compiled adapter, the seed, and the full argv |
| `scores.json` | Per-axis and per-subset counts, `score_eligible`, failure-class and failure-origin tallies |
| `host_summary.json` | Host telemetry window, sample count, and whether the machine was contended (`polluted`) |
| `resource_summary.json` | Calibrated resource distributions, the observer-effect A/B comparison and `resource_comparison_eligible` (profiled round only) |
| `cold_start.jsonl` | Cold-start diagnostics, kept separate from the warm profile (profiled round only) |

Two of the report's tables can be checked against these files directly. The
four-engine axis table reads 5214/5220 for Chrome on L1 and 192/192 on L2,
which is what `scores.json` holds; the resource card's 687 ms median CPU for
Chrome is `by_engine.chrome.metrics.cpu_total_ms.median` at 686.948.

The headline task-level rates need the result rows themselves, since a task
passes only when all of its attempts pass. Those rows are in the archives
below.

## Release assets

Attached to [evidence-20260812](https://github.com/lexmount/Lexbench-Headless-Browser/releases/tag/evidence-20260812),
a tag that names when the runs were collected rather than the state of the
code: the runs date from 2026-08-12 and 2026-08-13, while the tree the tag
points at is later than that.

Each `evidence-*` archive holds one run's `results.jsonl`,
`host_telemetry.jsonl` and `scorecard.md`; each `artifacts-*` archive holds
that run's per-attempt protocol logs, which is what lets a third party audit an
individual failure rather than only the result row.

| Asset | Contents | Download | Expanded |
|:---|:---|---:|---:|
| `evidence-four_engine_full_20260812.tar.gz` | 23,136 result rows, host telemetry, scorecard | 2.1 MiB | 46 MiB |
| `evidence-resource_baseline_20260812.tar.gz` | 11,140 rows, profiler off | 610 KiB | 24 MiB |
| `evidence-resource_engine_20260812.tar.gz` | 11,140 rows, profiler on, plus the generated resource card | 1.6 MiB | 41 MiB |
| `artifacts-four_engine_full_20260812.tar.gz` | 133,882 per-attempt artifact files | 15.1 MiB | 173 MiB |
| `artifacts-resource_baseline_20260812.tar.gz` | 59,366 files | 7.1 MiB | 111 MiB |
| `artifacts-resource_engine_20260812.tar.gz` | 70,505 files | 13.1 MiB | 171 MiB |

The Kitesurf lane ships two more assets on the same release, indexed in
[the Kitesurf lane section](#the-kitesurf-lane) below.

### sha256

```
3052461b458581c8da620d55c3741d18dc50693d78c7a6ebeffb2241251f12f9  evidence-four_engine_full_20260812.tar.gz
55d4f98f4f386ec2f7856d0b96b2c8f9e9838d872f00f03f1589e6e8a94f51a5  evidence-resource_baseline_20260812.tar.gz
935c6232e5598cddd25cebc11ed2c35e03e43b0d3f77c1bc011787f07c0b8162  evidence-resource_engine_20260812.tar.gz
c6680f481c8d928373cb627534d6cec02754b2126ead81e2eded5ef3ab2e53cc  artifacts-four_engine_full_20260812.tar.gz
dbf474352f8db054cf61870bc8b0792f8b1d48aa38b8e4ee2a31b337e8bd0ab9  artifacts-resource_baseline_20260812.tar.gz
6c67855929d185d1a41fc59920d32c62e459a15fd2719c86f611582f8b83b349  artifacts-resource_engine_20260812.tar.gz
```

Check a download against this list:

```bash
sha256sum -c <<'EOF'
3052461b458581c8da620d55c3741d18dc50693d78c7a6ebeffb2241251f12f9  evidence-four_engine_full_20260812.tar.gz
EOF
```

The archives are built with fixed member order, fixed ownership and a fixed
mtime, so rebuilding one from the same run reproduces its sha256.

## Regenerating a report

Expand a run's `evidence-*` archive next to its `docs/evidence/<run_id>/`
directory so the generator sees both the rows and the manifest, then:

```bash
python3 tools/report_four_engine.py runs/four_engine_full_20260812 \
    -o docs/reports/four-engine-report-20260812.md
```

Both published reports were regenerated this way from the release archives and
came out byte-identical to the committed copies. The five-engine report's own
regeneration command is in the Kitesurf lane section below.

## What was removed before publication

Run products record the paths and origins a round actually used, which is a
local audit trail rather than evidence. `tools/scrub_release_paths.py` rewrites
three kinds of detail on the way into an archive:

| Detail | Becomes |
|:---|:---|
| The absolute path of the checkout that produced the run | `<repo>` |
| The host a fixture round was served from | `<static-fixture-origin>` or `<dynamic-fixture-origin>` |
| An ISO-8601 timestamp carrying a non-UTC offset | The same instant at `+00:00` |

Fixture origins are discovered from the products themselves, from the
`base_url` a verification report records and the `scope.fixture_base_url` a run
summary records, so the script names no host of its own.

Nothing in the fingerprint chain is touched: engine and adapter sha256 values
stay as recorded, and only the path that led to a binary is treated as a local
detail. The four-engine run needed 5,028 path rewrites across 1,338 files and
no origin rewrites at all, since its fixtures are served from `127.0.0.1`. Both
reports still regenerate byte-identically from the scrubbed trees, which is how
the rewrite is verified to have left the evidence alone.

Re-check a tree at any time:

```bash
python3 tools/scrub_release_paths.py runs/ --check
python3 tools/scrub_release_paths.py build/release-runs/ --check --origins-from runs/
```

The second form is how an already-scrubbed tree is checked. Origins are
discovered from the products, and a scrubbed product no longer names one, so
the check needs the original tree to know what to look for. Asked to check a
scrubbed tree without it, the script refuses rather than reporting a clean
result it never tested.

## The Kitesurf lane

The five-engine report draws on two sources: the four local engines come from
the four-engine run indexed above, and the Kitesurf column comes from the lane
collected on this branch.

### What ships in the repository

`docs/evidence/<run_id>/` carries the per-round fingerprints for all 49 rounds
of the lane, about 1.2 MB in total.

| File | What it fixes |
|:---|:---|
| `provenance.json` | Branch, HEAD, tree digest, worktree state, and the sha256 of every compiled adapter the round invoked |
| `summary.json` | The endpoint, the expected and observed identity, status tallies, latency, and the fixture-verification result the round ran on |
| `summary.md` | The same round in readable form |
| `identity.cdp.jsonl` | The live-transport identity exchange, one row per connection |

Two lane-level contract reports sit alongside them:
`ks_static_verification.json` (19 static fixture files verified against their
content contract) and `ks_dynamic_verification.json` (127 static routes plus 28
dynamic probes verified before the round started).

Every round records the same source identity, so all 49 came from one
unchanged working state. That identity is a commit and tree from before this
repository's history was rebuilt for publication, which means the hashes in
`provenance.json` no longer resolve here; they remain useful as an internal
consistency record rather than as something to check out.

### Release assets

Attached to [evidence-20260812](https://github.com/lexmount/Lexbench-Headless-Browser/releases/tag/evidence-20260812),
alongside the four-engine archives.

| Asset | Contents | Download | Expanded |
|:---|:---|---:|---:|
| `evidence-kitesurf_lane_20260813.tar.gz` | All 49 rounds: `results.jsonl`, per-round `fixture_verification.json`, and the fingerprint files above | 1.2 MiB | 13 MiB |
| `artifacts-kitesurf_lane_20260813.tar.gz` | 4,989 per-attempt protocol artifacts | 888 KiB | 12 MiB |

```
003b4af6eb7ccecfd13e7bf09cfb7d00db95c0e5d40338629b8663efa4fb5fd8  evidence-kitesurf_lane_20260813.tar.gz
71b55f610b5b2f40742c43297a6f4e44a6baceec70f228ec7556eec3175a4a75  artifacts-kitesurf_lane_20260813.tar.gz
```

The four-engine archives, and the scrubbing rules that apply to this lane as
well, are indexed in the sections above.

### Regenerating the five-engine report

The lane is 49 rounds rather than one run, because the probe trips a circuit
breaker whenever a task cannot confirm target cleanup and the wrapper restarts
a continuation over the remaining tasks. Playwright alone ended up in 25
segments. Every segment is an input, and every failure's rerun evidence is a
second input:

```bash
python3 tools/report_five_engine.py \
    --four-engine-run ../main/runs/four_engine_full_20260812 \
    --kitesurf-results runs/ks_raw_full/results.jsonl \
                       runs/ks_driver_full/results.jsonl \
                       runs/ks_l2_full/results.jsonl \
                       runs/ks_blocked_*/results.jsonl \
                       runs/ks_sweep_*/results.jsonl \
    --kitesurf-rerun runs/ks_adj_*/results.jsonl \
                     runs/ks_l2_full/results.jsonl \
    -o docs/reports/five-engine-report-20260813.md
```

`ks_l2_full` appears on both sides because that round carries two rows per
task, its own primary attempt and its own rerun.

Running this reproduces every generated section of the published report
byte-for-byte, from the scrubbed archives as well as from the original tree.
The report's closing "Run notes" section is prose written by hand after
generation; it describes method and observations and cites only numbers the
generated sections above it already computed.

### What the Kitesurf column is not

A remote endpoint carries no binary digest, no process tree and no cgroup, so
`formal_score_eligible` is false for this lane and no resource figure exists
for it. An empty resource cell means unmeasurable, never zero. The readings are
a snapshot of a service that can change under us, which is a weaker
reproducibility class than the four pinned binaries above.
