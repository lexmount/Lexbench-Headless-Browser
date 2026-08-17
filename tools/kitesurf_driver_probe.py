#!/usr/bin/env python3
"""Run existing L1 driver tasks against remote Kitesurf.

The probe is intentionally outside the formal engine roster. It preserves the
task definitions, pinned framework adapters, binding checks, and graders while
substituting a public fixture origin and a browser-level remote WSS endpoint.
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import hashlib
import http.server
import json
import pathlib
import statistics
import subprocess
import sys
import time
import threading
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
DRIVER_SUBSETS = {
    "playwright": "l1.playwright",
    "puppeteer": "l1.puppeteer",
    "chrome_remote_interface": "l1.chrome_remote_interface",
    "cdp_use": "l1.cdp_use",
    "pydoll": "l1.pydoll",
    "stagehand": "l1.stagehand",
    "chrome_devtools_mcp": "l1.chrome_devtools_mcp",
    "chromedp": "l1.chromedp",
    "rod": "l1.rod",
    "ferrum": "l1.ferrum",
    "chromiumoxide": "l1.chromiumoxide",
    "agent_browser": "l1.agent_browser_scenarios",
}
FORBIDDEN_TOKENS = {
    "page.capturescreenshot",
    "page.printtopdf",
    "screenshot",
    "pdf",
}


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
    parser.add_argument(
        "--driver",
        action="append",
        choices=sorted(DRIVER_SUBSETS),
        help="driver to run; repeatable (default: playwright and puppeteer)",
    )
    parser.add_argument("--task", action="append", help="task id; repeatable")
    parser.add_argument("--feature", action="append", help="feature id; repeatable")
    parser.add_argument(
        "--scenario",
        action="append",
        help="generated scenario id (without .scenario.json); repeatable",
    )
    parser.add_argument(
        "--scenario-only",
        action="store_true",
        help="select only generated cross-driver scenario tasks",
    )
    parser.add_argument("--limit-per-driver", type=int)
    parser.add_argument("--reruns", type=int, default=1)
    parser.add_argument("--delay-ms", type=int, default=750)
    parser.add_argument("--stop-after-transport-errors", type=int, default=3)
    parser.add_argument("--output", type=pathlib.Path)
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()
    args.driver = args.driver or ["playwright", "puppeteer"]
    if args.limit_per_driver is not None and args.limit_per_driver <= 0:
        parser.error("--limit-per-driver must be positive")
    if args.reruns <= 0:
        parser.error("--reruns must be positive")
    if args.delay_ms < 0:
        parser.error("--delay-ms cannot be negative")
    if args.stop_after_transport_errors <= 0:
        parser.error("--stop-after-transport-errors must be positive")
    args.expected_identity = bench.require_remote_cdp_identity(
        expected_identity_from_args(args),
        label="driver probe expected Kitesurf identity",
    )
    validate_url(args.endpoint, {"ws", "wss"}, "--endpoint")
    validate_url(args.fixture_base_url, {"http", "https"}, "--fixture-base-url")
    args.fixture_base_url = args.fixture_base_url.rstrip("/")
    return args


def validate_url(value: str, schemes: set[str], option: str) -> urllib.parse.ParseResult:
    parsed = urllib.parse.urlparse(value)
    if (
        parsed.scheme not in schemes
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise bench.BenchError(
            f"{option} must be an absolute credential-free URL with scheme "
            f"{sorted(schemes)}"
        )
    return parsed


def task_uses_forbidden_output(task: bench.ResolvedTask) -> bool:
    values = [str(value).lower() for value in task.features]
    values.append(json.dumps(task.driver, sort_keys=True).lower())
    return any(
        token in value
        for value in values
        for token in FORBIDDEN_TOKENS
    )


def load_tasks(args: argparse.Namespace) -> list[bench.ResolvedTask]:
    subsets = [DRIVER_SUBSETS[name] for name in args.driver]
    _, tasks = bench.expand_tasks(
        bench.DEFAULT_MANIFEST,
        requested_subsets=subsets,
        requested_tasks=args.task,
        requested_features=args.feature,
        requested_layers=["L1"],
    )
    selected: list[bench.ResolvedTask] = []
    counts: collections.Counter[str] = collections.Counter()
    for task in sorted(tasks, key=lambda row: (row.subset_id, row.task_id)):
        if args.scenario_only and not task.task.get("_generated_from"):
            continue
        generated_from = str(task.task.get("_generated_from") or "")
        scenario_id = pathlib.Path(generated_from).name.removesuffix(".scenario.json")
        if args.scenario and scenario_id not in set(args.scenario):
            continue
        if task_uses_forbidden_output(task):
            continue
        kind = str(task.driver.get("kind"))
        if kind in bench.FRAMEWORK_DRIVER_KINDS:
            driver = bench.FRAMEWORK_DRIVER_KINDS[kind]
        else:
            driver = (bench.SCENARIO_ADAPTER_KINDS.get(kind) or {}).get("driver_key")
        if driver not in args.driver:
            continue
        if args.limit_per_driver is not None and counts[driver] >= args.limit_per_driver:
            continue
        counts[driver] += 1
        selected.append(task)
    if not selected:
        raise bench.BenchError("selection contains no eligible framework tasks")
    return selected


def default_output_dir() -> pathlib.Path:
    stamp = dt.datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
    return REPO_ROOT / "runs" / f"experimental_kitesurf_drivers_{stamp}"


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
        label="remote driver identity preflight",
    )
    identity["probe_duration_ms"] = int((time.perf_counter() - started) * 1000)
    return identity


def selected_adapter_source_paths(
    tasks: list[bench.ResolvedTask],
) -> tuple[pathlib.Path, ...]:
    """Return hashable source dependencies for every selected driver route."""

    paths = {
        REPO_ROOT / "runner/scripts/adapters/PROTOCOL.md",
        REPO_ROOT / "runner/scripts/lib/remote_identity.js",
        REPO_ROOT / "runner/scripts/lib/remote_cleanup.js",
        REPO_ROOT / "runner/scripts/lib/transport_fault.js",
    }
    source_suffixes = {
        ".js",
        ".py",
        ".rb",
        ".go",
        ".rs",
        ".toml",
        ".mod",
        ".sum",
        ".lock",
    }
    for task in tasks:
        kind = str(task.driver.get("kind"))
        if kind in bench.FRAMEWORK_DRIVER_KINDS:
            paths.add(REPO_ROOT / "runner/scripts/framework_probe.js")
            continue
        spec = bench.SCENARIO_ADAPTER_KINDS.get(kind)
        if spec is None:
            continue
        source = REPO_ROOT / str(spec["script"])
        if source.is_file():
            paths.add(source)
            if str(spec.get("driver_key")) == "stagehand":
                paths.add(
                    REPO_ROOT / "runner/scripts/lib/stagehand_ownership.js"
                )
            if source.suffix == ".py":
                paths.add(REPO_ROOT / "runner/scripts/adapters/remote_identity.py")
                paths.add(REPO_ROOT / "runner/scripts/adapters/remote_cleanup.py")
            continue
        for candidate in source.rglob("*"):
            if (
                candidate.is_file()
                and candidate.suffix.lower() in source_suffixes
                and "target" not in candidate.relative_to(source).parts
            ):
                paths.add(candidate)
    return tuple(sorted(paths))


def selected_adapter_executable_paths(
    tasks: list[bench.ResolvedTask],
) -> tuple[pathlib.Path, ...]:
    """Return every repo-local compiled adapter that will actually run."""

    paths: set[pathlib.Path] = set()
    for task in tasks:
        spec = bench.SCENARIO_ADAPTER_KINDS.get(str(task.driver.get("kind")))
        if spec is None:
            continue
        argv = bench.scenario_adapter_argv(spec)
        if spec["argv"] and "{script}" in str(spec["argv"][0]):
            paths.add(pathlib.Path(argv[0]))
    return tuple(sorted(paths))


class IdentityShim:
    """Local read-only /json/version gate for adapters that connect by WSS."""

    def __init__(self, endpoint: str, identity: dict[str, Any]) -> None:
        payload = {
            "Browser": str(identity.get("product") or ""),
            "Protocol-Version": str(identity.get("protocolVersion") or ""),
            "User-Agent": str(identity.get("userAgent") or ""),
            "webSocketDebuggerUrl": endpoint,
        }
        body = json.dumps(payload, sort_keys=True).encode()

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args: Any) -> None:
                return

            def do_GET(self) -> None:
                if urllib.parse.urlparse(self.path).path != "/json/version":
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return int(self.server.server_address[1])

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def remote_browser(
    endpoint: str,
    identity: dict[str, Any],
    identity_port: int,
) -> bench.BrowserProcess:
    validate_url(endpoint, {"ws", "wss"}, "--endpoint")
    version_info = dict(identity)
    version_info.update(
        {
            "Browser": str(identity.get("product") or ""),
            "webSocketDebuggerUrl": endpoint,
            "transport": "remote_cdp",
        }
    )
    return bench.BrowserProcess(
        engine="kitesurf",
        port=identity_port,
        process=None,  # type: ignore[arg-type]
        version_info=version_info,
        launch_command=("remote_cdp", endpoint),
    )


def seed_for(task_id: str, attempt: int) -> str:
    return hashlib.sha256(
        f"kitesurf-driver-probe:{task_id}:{attempt}".encode()
    ).hexdigest()[:12]


def driver_name(task: bench.ResolvedTask) -> str:
    kind = str(task.driver["kind"])
    if kind in bench.FRAMEWORK_DRIVER_KINDS:
        return str(bench.FRAMEWORK_DRIVER_KINDS[kind])
    adapter = bench.SCENARIO_ADAPTER_KINDS.get(kind)
    if adapter is None:
        raise bench.BenchError(f"unknown driver kind: {kind!r}")
    return str(adapter["driver_key"])


TRANSPORT_ERROR_TOKENS = (
    "econnreset",
    "econnrefused",
    "ehostunreach",
    "enotfound",
    "connection refused",
    "connection reset",
    "reset by peer",
    "connect call failed",
    "connection aborted",
    "broken pipe",
    "no route to host",
    "host is unreachable",
    "network is unreachable",
    "name or service not known",
    "temporary failure in name resolution",
    "nodename nor servname provided",
    "network error",
    "websocket error",
    "websocket connection failed",
    "unexpected server response",
    "connection closed",
    "closed before",
    "socket hang up",
    "http 429",
    "429 too many requests",
    "rate limit",
    "quota exceeded",
    "tls",
    "ssl",
    "certificate",
    "handshake failed",
)
TRANSPORT_TIMEOUT_TOKENS = ("timeout", "timed out", "etimedout")
BINDING_EXCLUSION_ISOLATION_POLICIES = {
    "agent_browser": {
        "phase": "driver_session_closed",
        "cleanup_backend": "agent_browser_named_session_close",
        "cleanup_required": True,
        "same_named_session_required": True,
    },
    "chrome_devtools_mcp": {
        "phase": "before_driver_start",
        "cleanup_backend": "not_started",
        "cleanup_required": False,
        "same_named_session_required": False,
    },
}


def is_transport_connect_failure(
    connect_error: Any,
    _failure: Any,
    observation_failure_class: Any,
) -> bool:
    """Separate connection failures from post-connect CDP semantic failures."""

    if not connect_error:
        return False
    connect_detail = str(connect_error).lower()
    if any(token in connect_detail for token in TRANSPORT_ERROR_TOKENS):
        return True
    if any(
        token in connect_detail for token in TRANSPORT_TIMEOUT_TOKENS
    ):
        return True
    if observation_failure_class == "binding_unverified":
        # A connected client that cannot prove live remote identity is an
        # intentional infra exclusion, not a transport failure. Recognized
        # network errors above still retain their transport classification.
        return False
    return observation_failure_class != "cdp_semantic"


def has_confirmed_binding_exclusion_isolation(
    driver: str,
    observations: dict[str, Any],
) -> bool:
    """Accept only audited pre-task exclusions that cannot leak endpoint state."""

    policy = BINDING_EXCLUSION_ISOLATION_POLICIES.get(driver)
    binding = observations.get("binding")
    evidence = observations.get("binding_exclusion_isolation")
    if (
        policy is None
        or observations.get("failure_class") != "binding_unverified"
        or not isinstance(binding, dict)
        or binding.get("excluded") is not True
        or not isinstance(evidence, dict)
        or evidence.get("schema") != "abb.binding_exclusion_isolation.v1"
        or evidence.get("driver") != driver
        or evidence.get("phase") != policy["phase"]
        or evidence.get("scenario_started") is not False
        or evidence.get("target_creation_requested") is not False
        or observations.get("isolation_restored") is not True
    ):
        return False
    cleanup = evidence.get("cleanup")
    if (
        not isinstance(cleanup, dict)
        or cleanup.get("backend") != policy["cleanup_backend"]
        or cleanup.get("required") is not policy["cleanup_required"]
        or cleanup.get("confirmed") is not True
    ):
        return False
    if policy["same_named_session_required"]:
        return (
            cleanup.get("same_named_session_as_attempt") is True
            and bool(cleanup.get("session"))
        )
    return True


def run_task(
    task: bench.ResolvedTask,
    attempt: int,
    browser: bench.BrowserProcess,
    fixture_base_url: str,
    output_dir: pathlib.Path,
    run_id: str,
) -> dict[str, Any]:
    driver = driver_name(task)
    artifact_dir = output_dir / "artifacts" / driver / task.task_id / str(attempt)
    artifact_dir.mkdir(parents=True, exist_ok=False)
    started = time.perf_counter()
    driver_output: dict[str, Any] | None = None
    driver_observations: dict[str, Any] = {}
    caught_exception: BaseException | None = None
    observation_failure_class: Any = None
    status = "infra"
    failure: dict[str, Any] | None = None
    try:
        if task.driver["kind"] in bench.FRAMEWORK_DRIVER_KINDS:
            driver_output = bench.run_framework_driver(
                task,
                browser,
                artifact_dir,
                fixture_base_url,
                run_id,
                "kitesurf",
                attempt,
                seed_for(task.task_id, attempt),
            )
        else:
            driver_output = bench.run_scenario_adapter_driver(
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
        failure = (
            driver_output.get("failure")
            or (driver_output.get("grader") or {}).get("failure")
            or driver_output.get("error")
        )
        driver_observations = driver_output.get("observations") or {}
        if not isinstance(driver_observations, dict):
            driver_observations = {}
        connect_error = driver_observations.get("connect_error")
        observation_failure_class = driver_observations.get("failure_class")
        if (
            status in {"infra", "fail"}
            and is_transport_connect_failure(
                connect_error,
                failure,
                observation_failure_class,
            )
        ):
            status = "transport_error"
            if connect_error:
                failure = {"class": "infra", "detail": str(connect_error)}
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
    if driver_output is None and caught_exception is not None:
        exception_observations = getattr(caught_exception, "cdp_observations", {})
        if isinstance(exception_observations, dict):
            driver_observations = dict(exception_observations)
            observation_failure_class = driver_observations.get("failure_class")

    target_cleanup = driver_observations.get("target_cleanup")
    target_cleanup_confirmed = (
        isinstance(target_cleanup, dict)
        and target_cleanup.get("confirmed") is True
        and target_cleanup.get("same_connection_as_task") is True
        and driver_observations.get("isolation_restored") is True
    )
    isolated_binding_exclusion = has_confirmed_binding_exclusion_isolation(
        driver,
        driver_observations,
    )
    cleanup_confirmed = target_cleanup_confirmed or isolated_binding_exclusion
    if not cleanup_confirmed:
        original_status = status
        detail = (
            f"remote {driver} attempt lacks confirmed same-connection "
            "target cleanup"
        )
        # No functional or compatibility verdict is eligible when the attempt
        # may have contaminated the next one. Preserve timeout/transport status
        # for accounting while still forcing the isolation breaker below.
        if status in {"pass", "fail", "unsupported"}:
            status = "infra"
            failure = {"class": "infra", "detail": detail}
        driver_observations.update(
            {
                "target_cleanup": target_cleanup,
                "isolation_restored": False,
                "cleanup_contract_error": detail,
                "primary_status": original_status,
            }
        )
        if driver_output is not None:
            driver_output["observations"] = driver_observations
    duration_ms = int((time.perf_counter() - started) * 1000)
    metrics = (driver_output or {}).get("metrics") or getattr(
        caught_exception, "cdp_metrics", {}
    ) or {}
    observations = driver_observations
    return {
        "task_id": task.task_id,
        "title": task.task.get("title"),
        "driver": driver,
        "subset_id": task.subset_id,
        "generated_from": task.task.get("_generated_from"),
        "scenario_id": pathlib.Path(
            str(task.task.get("_generated_from") or "")
        ).name.removesuffix(".scenario.json"),
        "features": task.features,
        "scene_url": task.scene.get("url"),
        "attempt": attempt,
        "status": status,
        "duration_ms": duration_ms,
        "failure": failure,
        "answer": (driver_output or {}).get("answer"),
        "grader": (driver_output or {}).get("grader"),
        "binding": observations.get("binding"),
        "target_cleanup": observations.get("target_cleanup"),
        "isolation_restored": observations.get("isolation_restored"),
        "observations": observations,
        "cdp_call_count": int(metrics.get("cdp_call_count") or 0),
        "cdp_error_count": int(metrics.get("cdp_error_count") or 0),
        "ws_disconnect_count": int(metrics.get("ws_disconnect_count") or 0),
    }


def latency(values: list[int]) -> dict[str, int] | None:
    ordered = sorted(values)
    if not ordered:
        return None
    return {
        "min_ms": ordered[0],
        "p50_ms": int(statistics.median(ordered)),
        "p95_ms": ordered[round((len(ordered) - 1) * 0.95)],
        "max_ms": ordered[-1],
    }


def summarize(
    args: argparse.Namespace,
    identity: dict[str, Any],
    run_source_commit: str | None,
    tasks: list[bench.ResolvedTask],
    rows: list[dict[str, Any]],
    stopped_early: bool,
    source: dict[str, Any],
    fixture_verification: dict[str, Any],
) -> dict[str, Any]:
    per_driver: dict[str, Any] = {}
    for driver in args.driver:
        driver_rows = [row for row in rows if row["driver"] == driver]
        per_driver[driver] = {
            "tasks": len({row["task_id"] for row in driver_rows}),
            "attempts": len(driver_rows),
            "statuses": dict(
                sorted(collections.Counter(row["status"] for row in driver_rows).items())
            ),
            "latency": latency([row["duration_ms"] for row in driver_rows]),
        }
    return {
        "schema": "experimental.kitesurf_driver_probe.v3",
        "created_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "source_commit": run_source_commit,
        "source": source,
        "endpoint": args.endpoint,
        "fixture_base_url": args.fixture_base_url,
        "fixture_verification": fixture_verification,
        "identity": identity,
        "expected_identity": args.expected_identity,
        "scope": {
            "drivers": args.driver,
            "selected_tasks": len(tasks),
            "completed_attempts": len(rows),
            "reruns": args.reruns,
            "scenario_only": bool(args.scenario_only),
            "concurrency": 1,
            "delay_ms": args.delay_ms,
            "session_strategy": "one fresh public browser WebSocket per attempt",
            "stopped_early": stopped_early,
            "formal_score_eligible": False,
        },
        "by_driver": per_driver,
        "total_cdp_calls": sum(row["cdp_call_count"] for row in rows),
    }


def markdown(summary: dict[str, Any], rows: list[dict[str, Any]]) -> str:
    identity = summary["identity"]
    lines = [
        "# Experimental Kitesurf framework-driver probe",
        "",
        f"- Endpoint: `{summary['endpoint']}`",
        f"- Fixture origin: `{summary['fixture_base_url']}`",
        f"- Identity: `{identity.get('product')}`, CDP `{identity.get('protocolVersion')}`, revision `{identity.get('revision')}`",
        "- Concurrency: 1; each attempt uses a fresh public browser WebSocket.",
        "- Exploratory only; not formal-score eligible.",
        "",
        "## Driver summary",
        "",
        "| Driver | Tasks | Attempts | Statuses | Latency |",
        "|---|---:|---:|---|---|",
    ]
    for driver, item in summary["by_driver"].items():
        latency_row = item.get("latency") or {}
        latency_text = (
            f"p50={latency_row.get('p50_ms')} ms; "
            f"p95={latency_row.get('p95_ms')} ms; max={latency_row.get('max_ms')} ms"
        )
        lines.append(
            f"| `{driver}` | {item['tasks']} | {item['attempts']} | "
            f"`{json.dumps(item['statuses'], sort_keys=True)}` | {latency_text} |"
        )
    failures = [row for row in rows if row["status"] != "pass"]
    lines.extend(
        [
            "",
            "## Non-pass attempts",
            "",
            "| Driver | Task | Attempt | Status | Failure |",
            "|---|---|---:|---|---|",
        ]
    )
    for row in failures:
        detail = str((row.get("failure") or {}).get("detail") or "").replace("|", "\\|")
        lines.append(
            f"| `{row['driver']}` | `{row['task_id']}` | {row['attempt']} | "
            f"`{row['status']}` | {detail[:400]} |"
        )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    tasks = load_tasks(args)
    if args.list:
        for task in tasks:
            print(f"{driver_name(task)}\t{task.task_id}\t{task.scene.get('url', '')}")
        print(f"selected={len(tasks)}")
        return 0

    output_dir = (args.output or default_output_dir()).resolve()
    if output_dir.exists():
        raise bench.BenchError(f"output directory already exists: {output_dir}")
    output_dir.mkdir(parents=True)
    run_source_commit = source_commit()
    source = capture_source_provenance(
        pathlib.Path(__file__),
        extra_paths=(
            REPO_ROOT / "runner/run.py",
            bench.DEFAULT_MANIFEST,
            REPO_ROOT / "tools/kitesurf_dynamic_fixture.py",
            args.fixture_manifest,
            *selected_adapter_source_paths(tasks),
        ),
        runtime_executables=selected_adapter_executable_paths(tasks),
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
    identity_shim = IdentityShim(args.endpoint, identity)
    identity_shim.start()
    browser = remote_browser(args.endpoint, identity, identity_shim.port)
    run_id = output_dir.name
    results_path = output_dir / "results.jsonl"
    rows: list[dict[str, Any]] = []
    consecutive_transport_errors = 0
    stopped_early = False
    isolation_abort_reason: str | None = None
    work = [
        (task, attempt)
        for task in tasks
        for attempt in range(1, args.reruns + 1)
    ]
    try:
        for sequence, (task, attempt) in enumerate(work, start=1):
            row = run_task(
                task,
                attempt,
                browser,
                args.fixture_base_url,
                output_dir,
                run_id,
            )
            row["sequence"] = sequence
            rows.append(row)
            with results_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, sort_keys=True) + "\n")
            print(
                f"[{sequence:03d}/{len(work):03d}] {row['status']:<15} "
                f"{row['duration_ms']:>6} ms  {row['driver']:<24} {row['task_id']}",
                flush=True,
            )
            if row["status"] == "transport_error":
                consecutive_transport_errors += 1
            else:
                consecutive_transport_errors = 0
            if row.get("isolation_restored") is not True:
                stopped_early = True
                isolation_abort_reason = (
                    f"sequence {sequence} ({row['driver']}/{row['task_id']}) "
                    "did not confirm target cleanup"
                )
                break
            if consecutive_transport_errors >= args.stop_after_transport_errors:
                stopped_early = True
                break
            if sequence < len(work) and args.delay_ms:
                time.sleep(args.delay_ms / 1000)
    finally:
        identity_shim.stop()

    summary = summarize(
        args,
        identity,
        run_source_commit,
        tasks,
        rows,
        stopped_early,
        source,
        fixture_verification,
    )
    summary["scope"]["isolation_abort_reason"] = isolation_abort_reason
    (output_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (output_dir / "summary.md").write_text(markdown(summary, rows), encoding="utf-8")
    print(f"output={output_dir}")
    return 2 if stopped_early else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except bench.BenchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2)
