#!/usr/bin/env python3
"""Run raw-CDP L1 tasks against a remote Kitesurf endpoint.

This is deliberately an experimental probe, not a fifth engine in the formal
benchmark roster. It reuses the repository's task definitions, raw-CDP driver,
and inline graders. By default it selects only ``scene.kind=about_blank``;
passing ``--fixture-base-url`` also enables the existing self-hosted-fixture
tasks through an explicitly supplied public origin.
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import pathlib
import statistics
import sys
import time
import urllib.parse
from typing import Any


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from runner import run as bench  # noqa: E402
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
RAW_SUBSET = "l1.raw_cdp"
ELIGIBLE_SCENE = "about_blank"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run the fixture-free raw-CDP L1 slice against the public "
            "Kitesurf browser-level WebSocket."
        )
    )
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    add_expected_identity_arguments(parser)
    parser.add_argument(
        "--fixture-base-url",
        help=(
            "public HTTP(S) base URL that exposes the benchmark FixtureServer; "
            "when present, include self_hosted_fixture tasks"
        ),
    )
    parser.add_argument(
        "--fixture-manifest",
        type=pathlib.Path,
        default=DEFAULT_DYNAMIC_FIXTURE_MANIFEST,
        help="committed dynamic FixtureServer response contract",
    )
    parser.add_argument("--task", action="append", help="select one task id; repeatable")
    parser.add_argument("--feature", action="append", help="select one feature; repeatable")
    parser.add_argument("--limit", type=int, help="run only the first N selected tasks")
    parser.add_argument(
        "--delay-ms",
        type=int,
        default=750,
        help="delay between fresh public WebSocket sessions (default: 750)",
    )
    parser.add_argument(
        "--stop-after-transport-errors",
        type=int,
        default=3,
        help="stop after N consecutive transport failures (default: 3)",
    )
    parser.add_argument("--output", type=pathlib.Path, help="new output directory")
    parser.add_argument("--list", action="store_true", help="list selected tasks without connecting")
    args = parser.parse_args()
    if args.limit is not None and args.limit <= 0:
        parser.error("--limit must be positive")
    if args.delay_ms < 0:
        parser.error("--delay-ms cannot be negative")
    if args.stop_after_transport_errors <= 0:
        parser.error("--stop-after-transport-errors must be positive")
    args.expected_identity = bench.require_remote_cdp_identity(
        expected_identity_from_args(args),
        label="raw L1 expected Kitesurf identity",
    )
    if args.fixture_base_url:
        parsed_fixture = urllib.parse.urlparse(args.fixture_base_url)
        if (
            parsed_fixture.scheme not in {"http", "https"}
            or not parsed_fixture.hostname
            or parsed_fixture.username
            or parsed_fixture.password
        ):
            parser.error("--fixture-base-url must be an absolute credential-free HTTP(S) URL")
        args.fixture_base_url = args.fixture_base_url.rstrip("/")
    return args


def validate_endpoint(endpoint: str) -> urllib.parse.ParseResult:
    parsed = urllib.parse.urlparse(endpoint)
    if parsed.scheme not in {"ws", "wss"} or not parsed.hostname:
        raise bench.BenchError("endpoint must be an absolute ws:// or wss:// URL")
    if parsed.username or parsed.password:
        raise bench.BenchError("endpoint credentials are not allowed in the URL")
    return parsed


def load_tasks(args: argparse.Namespace) -> tuple[list[bench.ResolvedTask], int]:
    _, all_raw_tasks = bench.expand_tasks(
        bench.DEFAULT_MANIFEST,
        requested_subsets=[RAW_SUBSET],
        requested_tasks=args.task,
        requested_features=args.feature,
        requested_layers=["L1"],
    )
    eligible_scenes = {ELIGIBLE_SCENE}
    if args.fixture_base_url:
        eligible_scenes.add("self_hosted_fixture")
    eligible = sorted(
        (
            task
            for task in all_raw_tasks
            if task.scene.get("kind") in eligible_scenes
        ),
        key=lambda task: task.task_id,
    )
    excluded = len(all_raw_tasks) - len(eligible)
    if args.limit is not None:
        eligible = eligible[: args.limit]
    if not eligible:
        raise bench.BenchError(
            "selection contains no eligible l1.raw_cdp tasks "
            f"(eligible scene kinds: {sorted(eligible_scenes)})"
        )
    return eligible, excluded


def default_output_dir() -> pathlib.Path:
    stamp = dt.datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
    return REPO_ROOT / "runs" / f"experimental_kitesurf_l1_{stamp}"


def fetch_identity(
    endpoint: str,
    output_dir: pathlib.Path,
    expected_identity: dict[str, str],
) -> dict[str, Any]:
    started = time.perf_counter()
    with bench.CDPClient(
        endpoint,
        output_dir / "identity.cdp.jsonl",
        timeout_s=15.0,
    ) as client:
        identity = client.command("Browser.getVersion")
    bench.require_matching_remote_cdp_identity(
        identity,
        expected_identity,
        label="remote CDP identity preflight",
    )
    identity["probe_duration_ms"] = int((time.perf_counter() - started) * 1000)
    return identity


def remote_browser(endpoint: str, identity: dict[str, Any]) -> bench.BrowserProcess:
    parsed = validate_endpoint(endpoint)
    version_info = dict(identity)
    version_info["webSocketDebuggerUrl"] = endpoint
    version_info["transport"] = "remote_cdp"
    version_info["Browser"] = str(identity.get("product") or "")
    return bench.BrowserProcess(
        engine="kitesurf",
        port=parsed.port or (443 if parsed.scheme == "wss" else 80),
        process=None,  # type: ignore[arg-type]
        version_info=version_info,
        launch_command=("remote_cdp", endpoint),
    )


def task_methods(task: bench.ResolvedTask) -> list[str]:
    methods: set[str] = set()
    for step in task.driver.get("steps", []):
        if isinstance(step, dict):
            method = step.get("method") or step.get("wait_for_event")
            if isinstance(method, str):
                methods.add(method)
    return sorted(methods)


def run_task(
    task: bench.ResolvedTask,
    browser: bench.BrowserProcess,
    artifact_dir: pathlib.Path,
    fixture_base_url: str | None = None,
) -> dict[str, Any]:
    artifact_dir.mkdir(parents=True, exist_ok=False)
    started = time.perf_counter()
    status = "infra"
    failure: dict[str, Any] | None = None
    driver_output: dict[str, Any] | None = None
    caught_exception: BaseException | None = None
    transport_failure = False
    try:
        driver_output = bench.run_raw_cdp_driver(
            task,
            browser,
            artifact_dir,
            fixture_base_url=fixture_base_url,
        )
        status = "pass" if driver_output.get("ok") else "fail"
        failure = (driver_output.get("grader") or {}).get("failure")
    except bench.CDPCommandError as exc:
        caught_exception = exc
        status = "unsupported" if bench.is_unsupported_error(exc) else "fail"
        failure = {
            "class": "engine_unsupported" if status == "unsupported" else "cdp_semantic",
            "detail": str(exc),
        }
    except bench.CDPTransportTimeout as exc:
        # CDPTransportTimeout also subclasses TimeoutError. Keep the public
        # timeout status while recording that this row belongs to the endpoint
        # transport breaker rather than a semantic task deadline.
        caught_exception = exc
        transport_failure = True
        status = "timeout"
        failure = {"class": "infra", "detail": str(exc) or type(exc).__name__}
    except TimeoutError as exc:
        caught_exception = exc
        status = "timeout"
        failure = {"class": "infra", "detail": str(exc) or type(exc).__name__}
    except (ConnectionError, OSError) as exc:
        caught_exception = exc
        transport_failure = True
        status = "transport_error"
        failure = {"class": "infra", "detail": str(exc) or type(exc).__name__}
    except Exception as exc:  # Keep experimental rows complete for diagnosis.
        caught_exception = exc
        status = "infra"
        failure = {"class": "infra", "detail": f"{type(exc).__name__}: {exc}"}
    duration_ms = int((time.perf_counter() - started) * 1000)
    metrics = (driver_output or {}).get("metrics") or getattr(
        caught_exception, "cdp_metrics", {}
    )
    row: dict[str, Any] = {
        "task_id": task.task_id,
        "title": task.task.get("title"),
        "features": task.features,
        "methods": task_methods(task),
        "scene_kind": task.scene.get("kind"),
        "status": status,
        "duration_ms": duration_ms,
        "failure": failure,
        "transport_failure": transport_failure,
        "cdp_call_count": int(metrics.get("cdp_call_count") or 0),
        "cdp_error_count": int(metrics.get("cdp_error_count") or 0),
        "ws_disconnect_count": int(metrics.get("ws_disconnect_count") or 0),
    }
    if driver_output is not None:
        row["answer"] = driver_output.get("answer")
        row["grader"] = driver_output.get("grader")
        row["observations"] = driver_output.get("observations")
        row["identity"] = (driver_output.get("observations") or {}).get(
            "__remote_identity__"
        )
    elif caught_exception is not None:
        observations = getattr(caught_exception, "cdp_observations", None)
        if isinstance(observations, dict):
            row["observations"] = observations
    observations = row.get("observations") or {}
    cleanup = observations.get("target_cleanup")
    row["target_cleanup"] = cleanup
    row["isolation_restored"] = observations.get("isolation_restored")
    return row


def latency_summary(rows: list[dict[str, Any]]) -> dict[str, int] | None:
    values = sorted(int(row["duration_ms"]) for row in rows)
    if not values:
        return None

    def percentile(fraction: float) -> int:
        index = round((len(values) - 1) * fraction)
        return values[index]

    return {
        "min_ms": values[0],
        "p50_ms": int(statistics.median(values)),
        "p95_ms": percentile(0.95),
        "max_ms": values[-1],
    }


def grouped_status(
    rows: list[dict[str, Any]],
    values_for_row: Any,
) -> dict[str, dict[str, int]]:
    grouped: dict[str, collections.Counter[str]] = {}
    for row in rows:
        for value in values_for_row(row):
            grouped.setdefault(str(value), collections.Counter())[str(row["status"])] += 1
    return {
        key: dict(sorted(counter.items()))
        for key, counter in sorted(grouped.items())
    }


def build_summary(
    endpoint: str,
    identity: dict[str, Any],
    expected_identity: dict[str, str],
    run_source_commit: str | None,
    selected_count: int,
    excluded_count: int,
    rows: list[dict[str, Any]],
    stopped_early: bool,
    delay_ms: int,
    fixture_base_url: str | None,
    fixture_verification: dict[str, Any] | None,
    source: dict[str, Any],
) -> dict[str, Any]:
    status_counts = collections.Counter(str(row["status"]) for row in rows)
    return {
        "schema": "experimental.kitesurf_l1_probe.v3",
        "created_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "source_commit": run_source_commit,
        "source": source,
        "endpoint": endpoint,
        "identity": identity,
        "expected_identity": expected_identity,
        "scope": {
            "subset": RAW_SUBSET,
            "scene_kind": ELIGIBLE_SCENE,
            "scene_kinds": (
                [ELIGIBLE_SCENE, "self_hosted_fixture"]
                if fixture_base_url
                else [ELIGIBLE_SCENE]
            ),
            "fixture_base_url": fixture_base_url,
            "fixture_verification": fixture_verification,
            "selected_tasks": selected_count,
            "completed_tasks": len(rows),
            "excluded_selected_self_hosted_tasks": excluded_count,
            "session_strategy": "one fresh public browser WebSocket per task",
            "identity_policy": "Browser.getVersion is checked on that same connection; no reconnect or page-websocket fallback",
            "concurrency": 1,
            "delay_ms": delay_ms,
            "stopped_early": stopped_early,
            "formal_score_eligible": False,
        },
        "status_counts": dict(sorted(status_counts.items())),
        "latency": latency_summary(rows),
        "total_cdp_calls": sum(int(row["cdp_call_count"]) for row in rows),
        "by_cdp_domain": grouped_status(
            rows,
            lambda row: {
                method.split(".", 1)[0]
                for method in row["methods"]
                if "." in method
            },
        ),
        "by_feature": grouped_status(rows, lambda row: row["features"]),
    }


def summary_markdown(summary: dict[str, Any]) -> str:
    scope = summary["scope"]
    latency = summary.get("latency") or {}
    identity = summary["identity"]
    lines = [
        "# Experimental Kitesurf fixture-free L1 probe",
        "",
        f"- Endpoint: `{summary['endpoint']}`",
        f"- Product: `{identity.get('product')}`; protocol `{identity.get('protocolVersion')}`; revision `{identity.get('revision')}`",
        f"- Tasks: {scope['completed_tasks']} completed / {scope['selected_tasks']} selected",
        f"- Statuses: `{json.dumps(summary['status_counts'], sort_keys=True)}`",
        f"- Client-observed task latency: min {latency.get('min_ms')} ms, p50 {latency.get('p50_ms')} ms, p95 {latency.get('p95_ms')} ms, max {latency.get('max_ms')} ms",
        "- Concurrency: 1; each task uses a fresh public WebSocket session.",
        "- This is exploratory evidence only and is not formal-score eligible.",
        "",
        "## CDP domain status counts",
        "",
        "| Domain | Status counts |",
        "|---|---|",
    ]
    for domain, counts in summary["by_cdp_domain"].items():
        lines.append(f"| `{domain}` | `{json.dumps(counts, sort_keys=True)}` |")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    validate_endpoint(args.endpoint)
    tasks, excluded_count = load_tasks(args)
    if args.list:
        for task in tasks:
            print(f"{task.task_id}\t{','.join(task.features)}")
        print(f"selected={len(tasks)} excluded_self_hosted={excluded_count}")
        return 0

    output_dir = (args.output or default_output_dir()).resolve()
    if output_dir.exists():
        raise bench.BenchError(f"output directory already exists: {output_dir}")
    output_dir.mkdir(parents=True)
    results_path = output_dir / "results.jsonl"

    run_source_commit = source_commit()
    source = capture_source_provenance(
        pathlib.Path(__file__),
        extra_paths=(
            REPO_ROOT / "runner/run.py",
            bench.DEFAULT_MANIFEST,
            REPO_ROOT / "tools/kitesurf_dynamic_fixture.py",
            args.fixture_manifest,
        ),
    )
    write_json(output_dir / "provenance.json", source)
    fixture_verification = None
    if args.fixture_base_url:
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
    consecutive_transport_errors = 0
    stopped_early = False
    isolation_abort_reason: str | None = None

    for index, task in enumerate(tasks, start=1):
        row = run_task(
            task,
            browser,
            output_dir / "artifacts" / task.task_id,
            fixture_base_url=args.fixture_base_url,
        )
        row["sequence"] = index
        rows.append(row)
        with results_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
        print(
            f"[{index:02d}/{len(tasks):02d}] {row['status']:<15} "
            f"{row['duration_ms']:>6} ms  {task.task_id}",
            flush=True,
        )
        if row.get("isolation_restored") is not True:
            stopped_early = True
            isolation_abort_reason = (
                "target cleanup was not confirmed; later tasks were skipped "
                "to prevent shared-state contamination"
            )
            print(f"stopping: {isolation_abort_reason}", file=sys.stderr)
            break
        if (
            row["status"] == "transport_error"
            or row.get("transport_failure") is True
        ):
            consecutive_transport_errors += 1
        else:
            consecutive_transport_errors = 0
        if consecutive_transport_errors >= args.stop_after_transport_errors:
            stopped_early = True
            isolation_abort_reason = (
                "stopped after consecutive transport failures; public "
                "endpoint may be throttling"
            )
            print(
                isolation_abort_reason,
                file=sys.stderr,
            )
            break
        if index < len(tasks) and args.delay_ms:
            time.sleep(args.delay_ms / 1000)

    summary = build_summary(
        args.endpoint,
        identity,
        args.expected_identity,
        run_source_commit,
        len(tasks),
        excluded_count,
        rows,
        stopped_early,
        args.delay_ms,
        args.fixture_base_url,
        fixture_verification,
        source,
    )
    summary["scope"]["isolation_abort_reason"] = isolation_abort_reason
    bench.write_json(output_dir / "summary.json", summary)
    (output_dir / "summary.md").write_text(
        summary_markdown(summary),
        encoding="utf-8",
    )
    print(f"output={output_dir}")
    return 2 if stopped_early else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except bench.BenchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2)
