#!/usr/bin/env python3
"""Generate the four-engine evaluation report from run products.

Deterministic: every number is computed from results.jsonl / scores.json /
run_manifest.json in the given run directory, ordering is fixed, and no
generation-time timestamp is embedded. Rerunning on the same data yields a
byte-identical report.

Usage:
    python3 tools/report_four_engine.py runs/<run-id> [-o docs/reports/foo.md]
"""

from __future__ import annotations

import argparse
import collections
import json
import pathlib
import sys

ENGINE_ORDER = ("chrome", "moli", "lightpanda", "obscura")

ENGINE_LABELS = {
    "chrome": "Chrome",
    "moli": "Moli",
    "lightpanda": "Lightpanda",
    "obscura": "Obscura",
}

STATUS_ORDER = ("crash", "fail", "infra", "pass", "timeout", "unsupported")


def load_run(run_dir: pathlib.Path) -> tuple[dict, list[dict], dict]:
    manifest = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
    scores = json.loads((run_dir / "scores.json").read_text(encoding="utf-8"))
    rows = [
        json.loads(line)
        for line in (run_dir / "results.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return manifest, rows, scores


def engine_label(manifest: dict, engine: str) -> str:
    version = str(((manifest.get("engines") or {}).get(engine) or {}).get("version") or "")
    label = ENGINE_LABELS.get(engine, engine)
    if engine == "chrome":
        major = version.split(".", 1)[0]
        return f"{label} {major}" if major.isdigit() else label
    if engine == "moli":
        tail = version.removeprefix("moli").strip()
        return f"{label} {tail}" if tail else label
    return label


def pct(numerator: int, denominator: int) -> str:
    if not denominator:
        return "n/a"
    return f"{100.0 * numerator / denominator:.2f}%"


def group_rows(rows: list[dict]) -> dict[tuple[str, str], list[dict]]:
    groups: dict[tuple[str, str], list[dict]] = collections.defaultdict(list)
    for row in rows:
        groups[(row["engine"], row["task_id"])].append(row)
    return groups


def task_level_pass(groups: dict[tuple[str, str], list[dict]]) -> dict[str, int]:
    """A task counts as pass for an engine only if every attempt passed."""
    passed: dict[str, int] = collections.defaultdict(int)
    for (engine, _), attempts in groups.items():
        if all(row["status"] == "pass" for row in attempts):
            passed[engine] += 1
    return dict(passed)


def fmt_duration(manifest: dict) -> str | None:
    started = manifest.get("started_at")
    completed = manifest.get("completed_at")
    if not (started and completed):
        return None
    import datetime as dt

    try:
        delta = dt.datetime.fromisoformat(completed) - dt.datetime.fromisoformat(started)
    except ValueError:
        return None
    total = int(delta.total_seconds())
    return f"{total // 3600}:{total % 3600 // 60:02d}:{total % 60:02d}"


def build_report(manifest: dict, rows: list[dict], scores: dict) -> str:
    engines = [e for e in ENGINE_ORDER if e in (manifest.get("selected_engines") or [])]
    groups = group_rows(rows)
    task_pass = task_level_pass(groups)
    task_ids = sorted({task for (_, task) in groups})
    task_total = len(task_ids)
    k = int(manifest.get("k_runs") or 1)
    run_date = str(manifest.get("started_at") or "")[:10]

    axes = scores.get("evaluation_axes") or {}
    l1_axis = (axes.get("protocol_driver_compatibility") or {}).get("by_engine") or {}
    l2_axis = (axes.get("web_platform_workflow_semantic_correctness") or {}).get("by_engine") or {}

    subset_tasks: dict[str, set[str]] = collections.defaultdict(set)
    subset_pass: dict[tuple[str, str], int] = collections.defaultdict(int)
    for (engine, task), attempts in groups.items():
        subset = attempts[0]["subset_id"]
        subset_tasks[subset].add(task)
        if all(row["status"] == "pass" for row in attempts):
            subset_pass[(subset, engine)] += 1

    status_counts: dict[str, collections.Counter] = {e: collections.Counter() for e in engines}
    mixed: dict[str, int] = collections.defaultdict(int)
    hard_fail_groups: dict[str, int] = collections.defaultdict(int)
    hard_fail_consistent: dict[str, int] = collections.defaultdict(int)
    for (engine, _), attempts in groups.items():
        statuses = [row["status"] for row in attempts]
        for status in statuses:
            status_counts[engine][status] += 1
        if len(set(statuses)) > 1:
            mixed[engine] += 1
        if any(s in ("infra", "crash", "timeout") for s in statuses):
            hard_fail_groups[engine] += 1
            if len(set(statuses)) == 1:
                hard_fail_consistent[engine] += 1

    row_total = len(rows)
    group_total = len(groups)
    mixed_total = sum(mixed.values())

    engine_meta = manifest.get("engines") or {}
    seed = manifest.get("seed")
    score_mode = manifest.get("score_mode")
    baseline_policy = manifest.get("chrome_baseline_policy")
    if isinstance(baseline_policy, dict):
        baseline_policy = baseline_policy.get("requested") or baseline_policy.get("resolved_default")
    jobs = (manifest.get("runner") or {}).get("jobs")
    duration = fmt_duration(manifest)
    resource_mode = ((manifest.get("resource_profile") or {}).get("mode")) or "off"

    lines: list[str] = []
    out = lines.append
    out(f"# Agent Browser Bench Four-Engine Evaluation (L1 + L2)")
    out("")
    header = f"**Run** `{manifest.get('run_id')}` · {run_date} · Bench `{manifest.get('bench_version')}`"
    # Runs recorded before the harness was versioned carry no harness_version;
    # their reports keep regenerating byte-identically without one.
    if manifest.get("harness_version"):
        header += f" · Harness `{manifest['harness_version']}`"
    out(header)
    out("")
    engine_names = ", ".join(engine_label(manifest, e) for e in engines)
    duration_part = f", wall time {duration}" if duration else ""
    out(
        f"Head-to-head comparison of {engine_names} on {task_total:,} tasks. k={k}, "
        f"{row_total:,} result rows{duration_part}, "
        f"`score_eligible: {str(bool(manifest.get('score_eligible'))).lower()}`, no fallback."
    )
    out("")
    out(
        "This report covers local pinned-binary engines only. Remote endpoints "
        "(such as Kitesurf) sit in a different evidence class; see the five-engine report."
    )
    out("")
    out("---")
    out("")
    out("## 1. Overview")
    out("")
    out("| Engine | pass | Pass rate |")
    out("|---|---:|---:|")
    for engine in engines:
        passed = task_pass.get(engine, 0)
        out(f"| {engine_label(manifest, engine)} | {passed:,} / {task_total:,} | **{pct(passed, task_total)}** |")
    out("")
    out("### By evaluation axis")
    out("")
    out("| Axis | Units | " + " | ".join(engine_label(manifest, e) for e in engines) + " |")
    out("|---|---:|" + "---:|" * len(engines))
    for axis_label, axis in (
        ("L1 protocol/driver compatibility", l1_axis),
        ("L2 semantic capability", l2_axis),
    ):
        total = int((axis.get(engines[0]) or {}).get("total") or 0)
        if not total:
            continue
        cells = []
        for engine in engines:
            stats = axis.get(engine) or {}
            cells.append(
                f"{stats.get('pass', 0)}/{stats.get('total', 0)} "
                f"({pct(int(stats.get('pass', 0)), int(stats.get('total', 0) or 1))})"
            )
        out(f"| {axis_label} | {total:,} | " + " | ".join(cells) + " |")
    out("")
    out("---")
    out("")
    out("## 2. Artifact provenance")
    out("")
    out("| Engine | Version | SHA-256 prefix |")
    out("|---|---|---|")
    for engine in engines:
        meta = engine_meta.get(engine) or {}
        out(f"| {ENGINE_LABELS.get(engine, engine)} | {meta.get('version')} | `{meta.get('sha256_12')}` |")
    out("")
    out(
        f"**Run parameters**: `--engines {','.join(engines)} --chrome-baseline {baseline_policy} "
        f"--score-mode {score_mode} --seed {seed} --k {k} --jobs {jobs} --host-telemetry on`"
    )
    out("")
    if baseline_policy == "best_effort":
        out(
            "`best_effort` rather than `required`: Chrome is scored as a comparable "
            "reference column, not used as a gate. Under `required`, tasks Chrome fails "
            "would be removed from every engine's scoring, and Chrome's own pass rate "
            "would approach 100% by construction, making that column meaningless."
        )
        out("")
    runner_meta = manifest.get("runner") or {}
    source = runner_meta.get("source") or {}
    fixtures = runner_meta.get("fixtures") or {}
    parts = []
    if source.get("tree_sha256"):
        parts.append(
            f"runner source tree `{str(source['tree_sha256'])[:12]}` ({source.get('file_count')} files)"
        )
    if fixtures.get("tree_sha256"):
        parts.append(
            f"fixtures `{str(fixtures['tree_sha256'])[:12]}` ({fixtures.get('file_count')} files)"
        )
    if parts:
        out("**Harness fingerprints**: " + " · ".join(parts))
        out("")
    out("---")
    out("")
    out("## 3. Per-subset breakdown")
    out("")
    out("| Subset | Tasks | " + " | ".join(engine_label(manifest, e) for e in engines) + " |")
    out("|---|---:|" + "---:|" * len(engines))
    for subset in sorted(subset_tasks):
        total = len(subset_tasks[subset])
        cells = [str(subset_pass.get((subset, engine), 0)) for engine in engines]
        out(f"| `{subset}` | {total} | " + " | ".join(cells) + " |")
    out("")
    out("---")
    out("")
    out("## 4. Stability")
    out("")
    out("| Engine | Mixed-status groups / total | Groups with infra/crash/timeout | Of which k/k consistent |")
    out("|---|---:|---:|---:|")
    for engine in engines:
        out(
            f"| {engine_label(manifest, engine)} | {mixed.get(engine, 0)} / {task_total:,} "
            f"| {hard_fail_groups.get(engine, 0)} | {hard_fail_consistent.get(engine, 0)} |"
        )
    out("")
    out(
        f"Across {group_total:,} `task × engine` groups, {mixed_total} groups had "
        f"inconsistent statuses ({100.0 * mixed_total / group_total:.2f}%)."
    )
    out("")
    out("### Status distribution")
    out("")
    statuses = [s for s in STATUS_ORDER if any(status_counts[e].get(s) for e in engines)]
    out("| Engine | " + " | ".join(statuses) + " |")
    out("|---|" + "---:|" * len(statuses))
    for engine in engines:
        cells = [str(status_counts[engine].get(s, 0)) for s in statuses]
        out(f"| {engine_label(manifest, engine)} | " + " | ".join(cells) + " |")
    out("")
    out("---")
    out("")
    out("## 5. Validity boundaries")
    out("")
    if resource_mode == "off":
        out(
            "- **No resource profile was collected in this run.** `--resource-profile` was off; "
            "resource comparison requires separate `baseline` and `engine` rounds under the "
            "A/B protocol in `docs/resource-cost.md`."
        )
    else:
        out(
            f"- This run used `--resource-profile {resource_mode}`; resource readings are in "
            "the run products and the resource card."
        )
    out("")
    out("- **L3 is out of scope.** Real-site chain results are not part of this report.")
    out("")
    if score_mode == "independent":
        out(
            "- **Chrome is a reference column, not a gold standard.** `--score-mode independent`; "
            "each engine is scored independently."
        )
        out("")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=pathlib.Path)
    parser.add_argument("-o", "--output", type=pathlib.Path, help="write here instead of stdout")
    args = parser.parse_args()

    manifest, rows, scores = load_run(args.run_dir)
    report = build_report(manifest, rows, scores)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(report, encoding="utf-8")
        print(f"wrote {args.output}", file=sys.stderr)
    else:
        sys.stdout.write(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
