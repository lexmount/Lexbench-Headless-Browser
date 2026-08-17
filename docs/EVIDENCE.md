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

Attached to [v0.5.0](https://github.com/lexmount/Lexbench-Headless-Browser/releases/tag/v0.5.0).
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

The Kitesurf lane ships two more assets on the same release, indexed by
[`docs/EVIDENCE.md` on the `kitesurf-eval` branch](../../../tree/kitesurf-eval/docs/EVIDENCE.md).

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
came out byte-identical to the committed copies. The five-engine report is
generated on the `kitesurf-eval` branch, where its inputs live.

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
