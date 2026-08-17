#!/usr/bin/env python3
"""Aggregate the five-engine (4 local + Kitesurf) evaluation deterministically.

This encodes the denominator rules and the B-class adjudication flow that the
five-engine report narrates, so the published numbers are computed, not
transcribed:

- Full corpus: every enabled task (1,928 at bench 2026.08.02-v0_4.1).
- Caliber A: the full corpus minus the unattributable subsets (†) — driver
  stacks whose failures against a remote endpoint cannot be attributed
  between the engine and the stack itself. Excluded uniformly for ALL
  engines so the columns stay comparable.
- Caliber B: caliber A minus the subsets systematically blocked by defects of
  the remote endpoint (whole-column zeroes with a single root cause). This
  answers "how does Kitesurf do where its transport works at all".
- B-class adjudication: a primary Kitesurf failure is only a scored failure
  if it survives rerun evidence. fail -> rerun pass => dismissed as flake
  (the pass stands); fail -> rerun fail (or no rerun) => A-class, scored
  as fail.

Local engines come from a normal harness run directory (results.jsonl with
k attempts; a task passes only if every attempt passed). Kitesurf comes from
one or more recipe results.jsonl files (k=1 lane) plus optional rerun files.

Usage:
    python3 tools/report_five_engine.py \
        --four-engine-run runs/<run-id> \
        --kitesurf-results runs/ks_raw_full/results.jsonl [...] \
        --kitesurf-rerun runs/ks_rerun/results.jsonl [...] \
        -o docs/reports/five-engine.md
"""

from __future__ import annotations

import argparse
import collections
import json
import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

LOCAL_ENGINE_ORDER = ("chrome", "moli", "lightpanda", "obscura")

# † unattributable against a remote endpoint: the driver stack itself owns
# too much of the failure surface (local daemon/socket architecture or MCP
# server process) to attribute a failure to the engine.
UNATTRIBUTABLE_SUBSETS = (
    "l1.agent_browser_scenarios",
    "l1.agent_browser_tool",
    "l1.chrome_devtools_mcp",
)

# Whole-column blockages with a single systematic root cause at the remote
# endpoint (parameterless events without `params`; announced-but-rejected
# browserContextId; TLS-only discovery surface).
SYSTEMICALLY_BLOCKED_SUBSETS = (
    "l1.chromedp",
    "l1.ferrum",
    "l1.chromiumoxide",
    "l1.selenium",
)


def load_task_subsets() -> dict[str, str]:
    subsets: dict[str, str] = {}
    for path in sorted((REPO_ROOT / "tasks").rglob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        task_id = payload.get("task_id")
        subset = payload.get("subset_id")
        if isinstance(task_id, str) and isinstance(subset, str):
            subsets[task_id] = subset
    return subsets


def load_jsonl(path: pathlib.Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def local_task_pass(run_dir: pathlib.Path) -> tuple[dict[str, dict[str, bool]], dict]:
    """Per-engine task pass map (all-k attempts pass) plus the run manifest."""
    manifest = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
    groups: dict[tuple[str, str], list[str]] = collections.defaultdict(list)
    for row in load_jsonl(run_dir / "results.jsonl"):
        groups[(row["engine"], row["task_id"])].append(row["status"])
    passed: dict[str, dict[str, bool]] = collections.defaultdict(dict)
    for (engine, task_id), statuses in groups.items():
        passed[engine][task_id] = all(status == "pass" for status in statuses)
    return passed, manifest


def kitesurf_adjudicated(
    primary_paths: list[pathlib.Path],
    rerun_paths: list[pathlib.Path],
) -> tuple[dict[str, str], dict[str, int]]:
    """Final per-task Kitesurf status after B-class adjudication.

    Later primary files override earlier ones for the same task (targeted
    rerun recipes re-emit the tasks they re-ran). Rerun evidence then
    adjudicates the failures.
    """
    primary: dict[str, str] = {}
    for path in primary_paths:
        for row in load_jsonl(path):
            primary[str(row["task_id"])] = str(row["status"])

    rerun_pass: set[str] = set()
    rerun_seen: set[str] = set()
    for path in rerun_paths:
        for row in load_jsonl(path):
            task_id = str(row["task_id"])
            rerun_seen.add(task_id)
            if str(row["status"]) == "pass":
                rerun_pass.add(task_id)

    final: dict[str, str] = {}
    tally = {
        "primary_pass": 0,
        "flake_dismissed": 0,
        "a_class_confirmed": 0,
        "unadjudicated_fail": 0,
        "non_fail_other": 0,
    }
    for task_id, status in primary.items():
        if status == "pass":
            final[task_id] = "pass"
            tally["primary_pass"] += 1
        elif status == "fail":
            if task_id in rerun_pass:
                final[task_id] = "pass"
                tally["flake_dismissed"] += 1
            elif task_id in rerun_seen:
                final[task_id] = "fail"
                tally["a_class_confirmed"] += 1
            else:
                final[task_id] = "fail"
                tally["unadjudicated_fail"] += 1
        else:
            final[task_id] = status
            tally["non_fail_other"] += 1
    return final, tally


def pct(numerator: int, denominator: int) -> str:
    if not denominator:
        return "n/a"
    return f"{100.0 * numerator / denominator:.2f}%"


def build_report(
    four_engine_run: pathlib.Path,
    kitesurf_primary: list[pathlib.Path],
    kitesurf_rerun: list[pathlib.Path],
) -> str:
    task_subset = load_task_subsets()
    local_pass, manifest = local_task_pass(four_engine_run)
    engines = [e for e in LOCAL_ENGINE_ORDER if e in local_pass]

    corpus = sorted(
        {task for tasks in local_pass.values() for task in tasks}
    )
    subset_of = {task: task_subset.get(task, "unknown") for task in corpus}

    excluded_dagger = {t for t in corpus if subset_of[t] in UNATTRIBUTABLE_SUBSETS}
    caliber_a_tasks = [t for t in corpus if t not in excluded_dagger]
    blocked = {t for t in caliber_a_tasks if subset_of[t] in SYSTEMICALLY_BLOCKED_SUBSETS}
    caliber_b_tasks = [t for t in caliber_a_tasks if t not in blocked]

    ks_status, ks_tally = kitesurf_adjudicated(kitesurf_primary, kitesurf_rerun)

    def local_count(engine: str, tasks: list[str]) -> int:
        return sum(1 for t in tasks if local_pass[engine].get(t))

    def ks_count(tasks: list[str]) -> int:
        return sum(1 for t in tasks if ks_status.get(t) == "pass")

    ks_covered_a = sum(1 for t in caliber_a_tasks if t in ks_status)
    ks_covered_b = sum(1 for t in caliber_b_tasks if t in ks_status)

    lines: list[str] = []
    out = lines.append
    run_date = str(manifest.get("started_at") or "")[:10]
    out("# Agent Browser Bench Five-Engine Evaluation (4 Local Engines + Kitesurf Remote Endpoint)")
    out("")
    out(
        f"**Four-engine run** `{manifest.get('run_id')}` · {run_date} · "
        f"Bench `{manifest.get('bench_version')}` · Kitesurf lane: k=1 + B-class adjudication"
    )
    out("")
    out(
        "Kitesurf is a remote endpoint: no binary fingerprint, no resource measurement, "
        "shared infrastructure, public-network latency counted against task budgets, "
        "`formal_score_eligible: false`. Its column is **not in the same evidence class** "
        "as the local engines and is compared only within the two explicit calibers below."
    )
    out("")
    out("## Caliber definitions (computed by this script, not transcribed)")
    out("")
    out(f"- Full corpus: {len(corpus):,} tasks.")
    out(
        f"- **† Uniform exclusion** of {len(excluded_dagger)} tasks (excluded for all five "
        "engines alike, keeping the columns comparable): "
        + ", ".join(f"`{s}`" for s in UNATTRIBUTABLE_SUBSETS)
        + " — against a remote endpoint these driver stacks own too much of the failure "
        "surface themselves for failures to be attributable."
    )
    out(f"- **Caliber A** (attributable surface): {len(caliber_a_tasks):,} tasks.")
    out(
        f"- **Caliber B** (after excluding systematic blockages): {len(caliber_b_tasks):,} tasks; "
        f"additionally excludes {len(blocked)} tasks: "
        + ", ".join(f"`{s}`" for s in SYSTEMICALLY_BLOCKED_SUBSETS)
        + " — whole-column zeroes, each with a single systematic root cause (see §Defects)."
    )
    out("")
    out("## Overview (task level; local engines = all k attempts pass; Kitesurf = after adjudication)")
    out("")
    out("| Engine | Caliber A pass | Caliber A rate | Caliber B pass | Caliber B rate |")
    out("|---|---:|---:|---:|---:|")
    for engine in engines:
        a = local_count(engine, caliber_a_tasks)
        b = local_count(engine, caliber_b_tasks)
        out(
            f"| {engine} | {a:,} / {len(caliber_a_tasks):,} | {pct(a, len(caliber_a_tasks))} "
            f"| {b:,} / {len(caliber_b_tasks):,} | {pct(b, len(caliber_b_tasks))} |"
        )
    ka = ks_count(caliber_a_tasks)
    kb = ks_count(caliber_b_tasks)
    out(
        f"| kitesurf | {ka:,} / {len(caliber_a_tasks):,} | {pct(ka, len(caliber_a_tasks))} "
        f"| {kb:,} / {len(caliber_b_tasks):,} | {pct(kb, len(caliber_b_tasks))} |"
    )
    out("")
    if ks_covered_a < len(caliber_a_tasks):
        out(
            f"The Kitesurf lane covered {ks_covered_a:,}/{len(caliber_a_tasks):,} caliber-A tasks "
            f"and {ks_covered_b:,}/{len(caliber_b_tasks):,} caliber-B tasks; uncovered tasks "
            "count as not passed."
        )
        out("")
    out("## Per-subset breakdown (within caliber A)")
    out("")
    out("| Subset | Tasks | " + " | ".join(engines) + " | kitesurf |")
    out("|---|---:|" + "---:|" * (len(engines) + 1))
    by_subset: dict[str, list[str]] = collections.defaultdict(list)
    for task in caliber_a_tasks:
        by_subset[subset_of[task]].append(task)
    for subset in sorted(by_subset):
        tasks = by_subset[subset]
        cells = [str(local_count(engine, tasks)) for engine in engines]
        ks_cell = str(ks_count(tasks))
        if subset in SYSTEMICALLY_BLOCKED_SUBSETS:
            ks_cell += "†"
        out(f"| `{subset}` | {len(tasks)} | " + " | ".join(cells) + f" | {ks_cell} |")
    out("")
    out(
        "†: systematically blocked subsets (excluded from caliber B); the values are actual "
        "observations, not capability ceilings."
    )
    out("")
    out("## B-class adjudication")
    out("")
    out("| Stage | Count |")
    out("|---|---:|")
    out(f"| Passed on the primary round | {ks_tally['primary_pass']:,} |")
    out(f"| fail → rerun pass (ruled a flake, counted as pass) | {ks_tally['flake_dismissed']:,} |")
    out(f"| fail → rerun still fail (escalated to A-class, counted as fail) | {ks_tally['a_class_confirmed']:,} |")
    out(f"| fail with no rerun evidence (counted as fail) | {ks_tally['unadjudicated_fail']:,} |")
    out(f"| Other statuses (infra/timeout/unsupported, etc.) | {ks_tally['non_fail_other']:,} |")
    out("")
    out(
        "Adjudication rule: a primary-round fail counts as a failure only if the rerun "
        "evidence still fails (or no rerun evidence exists); a rerun pass is ruled a "
        "flake and the pass stands."
    )
    out("")
    out("## Validity boundaries")
    out("")
    out(
        "- The Kitesurf column cannot be compared with local engines on resources: a remote "
        "endpoint is structurally unmeasurable (no process tree, no cgroup). "
        "**An empty cell must not be read as zero resource usage.**"
    )
    out(
        "- The remote service may change over time; this report's Kitesurf readings are a "
        "snapshot at collection time, a different reproducibility class from the "
        "four-engine columns."
    )
    out(
        "- The local four-engine readings are authoritative in the four-engine report; "
        "this report does not rerun the local columns."
    )
    out("")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--four-engine-run", type=pathlib.Path, required=True)
    parser.add_argument(
        "--kitesurf-results", type=pathlib.Path, nargs="+", required=True,
        help="primary Kitesurf lane results.jsonl files (later files override earlier per task)",
    )
    parser.add_argument(
        "--kitesurf-rerun", type=pathlib.Path, nargs="*", default=[],
        help="rerun/confirm results.jsonl files used for B-class adjudication",
    )
    parser.add_argument("-o", "--output", type=pathlib.Path)
    args = parser.parse_args()

    report = build_report(args.four_engine_run, args.kitesurf_results, args.kitesurf_rerun)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(report, encoding="utf-8")
        print(f"wrote {args.output}", file=sys.stderr)
    else:
        sys.stdout.write(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
