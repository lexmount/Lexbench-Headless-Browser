#!/usr/bin/env python3
"""Run a small, non-formal L2 semantic sample through a public fixture URL."""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import hashlib
import json
import pathlib
import statistics
import subprocess
import sys
import time
import urllib.parse
from typing import Any


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from runner import run as bench  # noqa: E402
from tools.kitesurf_l1_probe import (  # noqa: E402
    fetch_identity,
    remote_browser,
    validate_endpoint,
)
from tools.kitesurf_common import (  # noqa: E402
    add_expected_identity_arguments,
    capture_source_provenance,
    expected_identity_from_args,
    source_commit,
    write_json,
)
from tools.kitesurf_dynamic_fixture import (  # noqa: E402
    DEFAULT_MANIFEST as DEFAULT_DYNAMIC_FIXTURE_MANIFEST,
    DynamicFixtureError,
    compact_verification,
    verify_dynamic_fixture,
)


DEFAULT_ENDPOINT = "wss://kitesurf.cloudflare.app/devtools/browser"
DEFAULT_TASKS = [
    "v2_leg_probe_c5_customelements",
    "v2_wpt_crypto_sha256_abc",
    "r4_q08_sale_has",
    "v2_wpt_dom_event_order",
    "v2_wpt_dom_mo_attributes",
    "v2_wpt_dom_domparser",
    "v2_wpt_enc_fatal_throws",
    "v2_leg_probe_c13_intl",
    "v2_wpt_store_cookie_readwrite",
    "storage_indexeddb_inventory_001",
    "v2_wpt_store_local_setget",
    "v2_wpt_url_sp_sort",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    add_expected_identity_arguments(parser)
    parser.add_argument("--fixture-base-url", required=True)
    parser.add_argument(
        "--fixture-manifest",
        type=pathlib.Path,
        default=DEFAULT_DYNAMIC_FIXTURE_MANIFEST,
        help="committed dynamic FixtureServer response contract",
    )
    parser.add_argument("--task", action="append")
    parser.add_argument("--reruns", type=int, default=2)
    parser.add_argument("--delay-ms", type=int, default=750)
    parser.add_argument("--output", type=pathlib.Path)
    args = parser.parse_args()
    validate_endpoint(args.endpoint)
    parsed = urllib.parse.urlparse(args.fixture_base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        parser.error("--fixture-base-url must be an absolute HTTP(S) URL")
    if args.reruns < 1 or args.reruns > 3:
        parser.error("--reruns must be 1 through 3")
    if args.delay_ms < 0:
        parser.error("--delay-ms cannot be negative")
    args.expected_identity = bench.require_remote_cdp_identity(
        expected_identity_from_args(args),
        label="L2 sample expected Kitesurf identity",
    )
    args.fixture_base_url = args.fixture_base_url.rstrip("/")
    args.task = args.task or list(DEFAULT_TASKS)
    return args


def default_output_dir() -> pathlib.Path:
    stamp = dt.datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
    return REPO_ROOT / "runs" / f"experimental_kitesurf_l2_sample_{stamp}"


def load_capability_map() -> dict[str, dict[str, str]]:
    payload = json.loads(
        (REPO_ROOT / "config/l2_semantic_capabilities.json").read_text(
            encoding="utf-8"
        )
    )
    result: dict[str, dict[str, str]] = {}
    for capability in payload["capabilities"]:
        for probe in capability["probes"]:
            result[str(probe["task_id"])] = {
                "capability_id": str(capability["capability_id"]),
                "category": str(capability["category"]),
                "observable": str(capability["observable"]),
                "role": str(probe["role"]),
            }
    return result


def load_tasks(args: argparse.Namespace) -> list[bench.ResolvedTask]:
    _, tasks = bench.expand_tasks(
        bench.DEFAULT_MANIFEST,
        requested_subsets=["l2.web_platform"],
        requested_tasks=args.task,
        requested_layers=["L2"],
    )
    selected = sorted(tasks, key=lambda task: task.task_id)
    invalid = [
        task.task_id
        for task in selected
        if task.driver.get("kind") != "node_cdp_probe"
        or task.scene.get("kind") != "self_hosted_fixture"
    ]
    if invalid:
        raise bench.BenchError(
            "L2 sample requires node_cdp_probe/self_hosted_fixture tasks: "
            + ", ".join(invalid)
        )
    found = {task.task_id for task in selected}
    missing = sorted(set(args.task) - found)
    if missing:
        raise bench.BenchError("selected task IDs not found: " + ", ".join(missing))
    return selected


def seed_for(task_id: str, attempt: int) -> str:
    return hashlib.sha256(
        f"kitesurf-l2-sample:{task_id}:{attempt}".encode()
    ).hexdigest()[:12]


def run_task(
    task: bench.ResolvedTask,
    attempt: int,
    browser: bench.BrowserProcess,
    fixture_base_url: str,
    output_dir: pathlib.Path,
    run_id: str,
    capability: dict[str, str],
) -> dict[str, Any]:
    artifact_dir = output_dir / "artifacts" / task.task_id / str(attempt)
    artifact_dir.mkdir(parents=True, exist_ok=False)
    started = time.perf_counter()
    driver_output: dict[str, Any] | None = None
    caught_exception: BaseException | None = None
    status = "infra"
    failure: dict[str, Any] | None = None
    try:
        driver_output = bench.run_node_cdp_probe_driver(
            task,
            browser,
            artifact_dir,
            fixture_base_url,
            run_id,
            "kitesurf",
            attempt,
            seed_for(task.task_id, attempt),
        )
        status = str(
            driver_output.get("status")
            or ("pass" if driver_output.get("ok") else "fail")
        )
        failure = driver_output.get("failure") or (
            (driver_output.get("grader") or {}).get("failure")
        )
    except subprocess.TimeoutExpired as exc:
        caught_exception = exc
        status = "timeout"
        failure = {"class": "infra", "detail": str(exc)}
    except TimeoutError as exc:
        caught_exception = exc
        status = "timeout"
        failure = {"class": "infra", "detail": str(exc) or type(exc).__name__}
    except (ConnectionError, OSError) as exc:
        caught_exception = exc
        status = "transport_error"
        failure = {"class": "infra", "detail": str(exc) or type(exc).__name__}
    except Exception as exc:
        caught_exception = exc
        status = "infra"
        failure = {"class": "infra", "detail": f"{type(exc).__name__}: {exc}"}
    metrics = (driver_output or {}).get("metrics") or {}
    observations = (driver_output or {}).get("observations") or getattr(
        caught_exception, "cdp_observations", {}
    )
    if not isinstance(observations, dict):
        observations = {}
    target_cleanup = observations.get("target_cleanup") or observations.get(
        "outer_target_cleanup"
    )
    isolation_restored = observations.get("isolation_restored")
    if isolation_restored is None and isinstance(target_cleanup, dict):
        isolation_restored = target_cleanup.get("confirmed") is True
    return {
        "task_id": task.task_id,
        "task_version": task.task.get("task_version"),
        "attempt": attempt,
        "status": status,
        "duration_ms": int((time.perf_counter() - started) * 1000),
        "answer": (driver_output or {}).get("answer"),
        "failure": failure,
        "grader": (driver_output or {}).get("grader"),
        "binding": observations.get("binding"),
        "observations": observations,
        "target_cleanup": target_cleanup,
        "isolation_restored": isolation_restored,
        "capability": capability,
        "cdp_call_count": int(metrics.get("cdp_call_count") or 0),
        "cdp_error_count": int(metrics.get("cdp_error_count") or 0),
        "ws_disconnect_count": int(metrics.get("ws_disconnect_count") or 0),
    }


def main() -> int:
    args = parse_args()
    tasks = load_tasks(args)
    capability_map = load_capability_map()
    missing_capabilities = [
        task.task_id for task in tasks if task.task_id not in capability_map
    ]
    if missing_capabilities:
        raise bench.BenchError(
            "tasks not in capability map: " + ", ".join(missing_capabilities)
        )
    output_dir = (args.output or default_output_dir()).resolve()
    if output_dir.exists():
        raise bench.BenchError(f"output directory already exists: {output_dir}")
    output_dir.mkdir(parents=True)
    run_source_commit = source_commit()
    source = capture_source_provenance(
        pathlib.Path(__file__),
        extra_paths=(
            REPO_ROOT / "tools/kitesurf_l1_probe.py",
            REPO_ROOT / "runner/run.py",
            REPO_ROOT / "runner/scripts/l2_fixture_probe.js",
            REPO_ROOT / "runner/scripts/storage_indexeddb_inventory_001.js",
            REPO_ROOT / "runner/scripts/lib/rawws.js",
            REPO_ROOT / "runner/scripts/lib/remote_identity.js",
            REPO_ROOT / "tools/kitesurf_dynamic_fixture.py",
            args.fixture_manifest,
            bench.DEFAULT_MANIFEST,
            REPO_ROOT / "config/l2_semantic_capabilities.json",
        ),
    )
    write_json(output_dir / "provenance.json", source)
    fixture_report_path = output_dir / "fixture_verification.json"
    try:
        fixture_report = verify_dynamic_fixture(
            args.fixture_base_url,
            args.fixture_manifest,
            report_path=fixture_report_path,
        )
    except DynamicFixtureError as exc:
        raise bench.BenchError(str(exc)) from exc
    fixture_verification = compact_verification(
        fixture_report,
        fixture_report_path,
    )
    identity = fetch_identity(
        args.endpoint,
        output_dir,
        args.expected_identity,
    )
    browser = remote_browser(args.endpoint, identity)
    rows: list[dict[str, Any]] = []
    aborted = False
    abort_reason: str | None = None
    results_path = output_dir / "results.jsonl"
    work = [
        (task, attempt)
        for task in tasks
        for attempt in range(1, args.reruns + 1)
    ]
    for sequence, (task, attempt) in enumerate(work, start=1):
        row = run_task(
            task,
            attempt,
            browser,
            args.fixture_base_url,
            output_dir,
            output_dir.name,
            capability_map[task.task_id],
        )
        row["sequence"] = sequence
        rows.append(row)
        with results_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
        print(
            f"[{sequence:02d}/{len(work):02d}] {row['status']:<15} "
            f"{row['duration_ms']:>6} ms  {task.task_id}",
            flush=True,
        )
        if row.get("isolation_restored") is not True:
            aborted = True
            abort_reason = (
                "target cleanup was not confirmed; later attempts were skipped "
                "to prevent shared-state contamination"
            )
            print(f"aborting: {abort_reason}", file=sys.stderr, flush=True)
            break
        if sequence < len(work) and args.delay_ms:
            time.sleep(args.delay_ms / 1000)
    durations = sorted(row["duration_ms"] for row in rows)
    by_category: dict[str, dict[str, int]] = {}
    for category in sorted({row["capability"]["category"] for row in rows}):
        selected = [row for row in rows if row["capability"]["category"] == category]
        by_category[category] = dict(
            sorted(collections.Counter(row["status"] for row in selected).items())
        )
    summary = {
        "schema": "experimental.kitesurf_l2_sample_probe.v3",
        "created_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "source_commit": run_source_commit,
        "source": source,
        "endpoint": args.endpoint,
        "fixture_base_url": args.fixture_base_url,
        "fixture_verification": fixture_verification,
        "identity": identity,
        "expected_identity": args.expected_identity,
        "tasks": len(tasks),
        "attempts": len(rows),
        "requested_attempts": len(work),
        "aborted": aborted,
        "abort_reason": abort_reason,
        "reruns": args.reruns,
        "concurrency": 1,
        "formal_score_eligible": False,
        "binding_verified_attempts": sum(
            1
            for row in rows
            if (row.get("binding") or {}).get("verified") is True
        ),
        "binding_unverified_attempts": sum(
            1
            for row in rows
            if (row.get("binding") or {}).get("verified") is not True
        ),
        "status_counts": dict(
            sorted(collections.Counter(row["status"] for row in rows).items())
        ),
        "by_category": by_category,
        "latency": {
            "min_ms": min(durations),
            "p50_ms": int(statistics.median(durations)),
            "p95_ms": durations[round((len(durations) - 1) * 0.95)],
            "max_ms": max(durations),
        },
        "total_cdp_calls": sum(row["cdp_call_count"] for row in rows),
        "total_cdp_errors": sum(row["cdp_error_count"] for row in rows),
        "total_disconnects": sum(row["ws_disconnect_count"] for row in rows),
    }
    (output_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"output={output_dir}")
    return 2 if aborted else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except bench.BenchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2)
