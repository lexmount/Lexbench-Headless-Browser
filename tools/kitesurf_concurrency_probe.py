#!/usr/bin/env python3
"""Run a bounded public-WSS concurrency smoke test for issue #136."""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import json
import pathlib
import statistics
import sys
import threading
import time
import urllib.parse
from typing import Any


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from runner import run as bench  # noqa: E402
from tools.kitesurf_common import (  # noqa: E402
    capture_source_provenance,
    source_commit,
    write_json,
)


DEFAULT_ENDPOINT = "wss://kitesurf.cloudflare.app/devtools/browser"
EXPECTED_PRODUCT = "Chrome/145.0.0.0"
EXPECTED_PROTOCOL_VERSION = "1.3"
EXPECTED_REVISION = "@kitesurf"
RESULT_STATUSES = ("pass", "fail", "timeout", "transport_error", "infra")
INCOMPLETE_STATUSES = {"timeout", "transport_error", "infra"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--level", action="append", type=int)
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--cooldown-seconds", type=float, default=5.0)
    parser.add_argument("--expect-product", default=EXPECTED_PRODUCT)
    parser.add_argument(
        "--expect-protocol-version", default=EXPECTED_PROTOCOL_VERSION
    )
    parser.add_argument("--expect-revision", default=EXPECTED_REVISION)
    parser.add_argument("--output", type=pathlib.Path)
    args = parser.parse_args()
    args.level = args.level or [1, 3, 5, 10]
    parsed = urllib.parse.urlparse(args.endpoint)
    if parsed.scheme != "wss" or not parsed.hostname:
        parser.error("--endpoint must be an absolute wss:// URL")
    if not args.level or any(level < 1 or level > 10 for level in args.level):
        parser.error("each --level must be from 1 through 10")
    if len(set(args.level)) != len(args.level):
        parser.error("--level values must be unique")
    if args.rounds < 1 or args.rounds > 3:
        parser.error("--rounds must be from 1 through 3")
    if args.cooldown_seconds < 0 or args.cooldown_seconds > 30:
        parser.error("--cooldown-seconds must be from 0 through 30")
    args.expected_identity = bench.require_remote_cdp_identity(
        {
            "product": args.expect_product,
            "protocolVersion": args.expect_protocol_version,
            "revision": args.expect_revision,
        },
        label="bounded-concurrency expected identity",
    )
    return args


def default_output_dir() -> pathlib.Path:
    stamp = dt.datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
    return REPO_ROOT / "runs" / f"experimental_kitesurf_concurrency_{stamp}"


def run_worker(
    endpoint: str,
    expected_identity: dict[str, str],
    output_dir: pathlib.Path,
    level: int,
    round_number: int,
    worker: int,
    barrier: threading.Barrier,
) -> dict[str, Any]:
    started = time.perf_counter()
    target_id: str | None = None
    client: bench.CDPClient | None = None
    transport_connected = False
    target_state = "not_started"
    status = "infra"
    failure_class: str | None = "infra"
    phase = "barrier"
    binding_verified = False
    error_type: str | None = None
    error: str | None = None
    identity: Any = {}
    actual_identity: dict[str, str] | None = None
    value: Any = None
    cleanup_attempted = False
    isolation_restored = True
    cleanup_error: str | None = None
    try:
        barrier.wait(timeout=10)
        phase = "connect"
        client = bench.CDPClient(
            endpoint,
            output_dir / "traces" / f"l{level}-r{round_number}-w{worker}.cdp.jsonl",
            timeout_s=15.0,
        )
        client.connect()
        transport_connected = True
        phase = "binding"
        identity = client.command("Browser.getVersion")
        actual_identity = bench.require_remote_cdp_identity(
            identity,
            label="bounded-concurrency task identity",
        )
        if actual_identity != expected_identity:
            raise bench.BenchError(
                "remote identity changed: expected "
                f"{expected_identity!r}, got {actual_identity!r}"
            )
        binding_verified = True
        phase = "functional"
        target_state = "requesting"
        try:
            created = client.command(
                "Target.createTarget", {"url": "about:blank"}
            )
        except bench.CDPCommandError:
            target_state = "rejected"
            raise
        except Exception:
            target_state = "ambiguous"
            raise
        if not isinstance(created, dict):
            target_state = "ambiguous"
            raise bench.BenchError(
                "Target.createTarget returned a non-object success response"
            )
        target_id = str(created.get("targetId") or "")
        if not target_id:
            target_state = "ambiguous"
            raise bench.BenchError(
                "successful Target.createTarget response had no targetId"
            )
        target_state = "tracked"
        attached = client.command(
            "Target.attachToTarget",
            {"targetId": target_id, "flatten": True},
        )
        session_id = str(
            attached.get("sessionId") if isinstance(attached, dict) else ""
        )
        if not session_id:
            raise bench.BenchError("Target.attachToTarget returned no sessionId")
        evaluated = client.command(
            "Runtime.evaluate",
            {"expression": "6 * 7", "returnByValue": True},
            session_id=session_id,
        )
        if not isinstance(evaluated, dict):
            raise bench.BenchError(
                "Runtime.evaluate returned a non-object success response"
            )
        value = (evaluated.get("result") or {}).get("value")
        if value != 42:
            raise bench.BenchError(f"evaluation returned {value!r}, expected 42")
        close_result = client.command(
            "Target.closeTarget", {"targetId": target_id}
        )
        if (
            not isinstance(close_result, dict)
            or close_result.get("success") is not True
        ):
            raise bench.BenchError(
                "Target.closeTarget did not confirm closure: "
                + json.dumps(close_result, sort_keys=True)
            )
        target_id = None
        target_state = "closed"
        status = "pass"
        failure_class = None
    except bench.CDPTransportTimeout as exc:
        status = "timeout"
        failure_class = "transport_timeout"
        error_type = type(exc).__name__
        error = str(exc)[:1000]
    except TimeoutError as exc:
        status = "timeout"
        failure_class = "timeout"
        error_type = type(exc).__name__
        error = str(exc)[:1000]
    except (bench.CDPTransportError, ConnectionError, OSError) as exc:
        status = "transport_error"
        failure_class = "transport_error"
        error_type = type(exc).__name__
        error = str(exc)[:1000]
    except bench.CDPCommandError as exc:
        status = "infra" if phase == "binding" else "fail"
        failure_class = (
            "binding_unverified" if phase == "binding" else "cdp_semantic"
        )
        error_type = type(exc).__name__
        error = str(exc)[:1000]
    except bench.BenchError as exc:
        if phase == "binding":
            status = "infra"
            failure_class = "binding_unverified"
        elif binding_verified:
            status = "fail"
            failure_class = "cdp_semantic"
        else:
            status = "infra"
            failure_class = "infra"
        error_type = type(exc).__name__
        error = str(exc)[:1000]
    except Exception as exc:
        status = "infra"
        failure_class = (
            "binding_unverified" if phase == "binding" else "infra"
        )
        error_type = type(exc).__name__
        error = str(exc)[:1000]
    finally:
        if client is not None:
            if target_id is not None:
                cleanup_attempted = True
                try:
                    cleanup_result = client.command(
                        "Target.closeTarget", {"targetId": target_id}
                    )
                    if cleanup_result.get("success") is True:
                        target_id = None
                        target_state = "closed"
                    else:
                        isolation_restored = False
                        cleanup_error = (
                            "Target.closeTarget cleanup did not confirm closure: "
                            + json.dumps(cleanup_result, sort_keys=True)
                        )
                except Exception as exc:
                    isolation_restored = False
                    cleanup_error = f"{type(exc).__name__}: {exc}"
            elif target_state == "ambiguous":
                isolation_restored = False
                cleanup_error = (
                    "Target.createTarget outcome was ambiguous and returned no "
                    "targetId; cleanup could not be confirmed"
                )
            client.close()
    row = {
        "level": level,
        "round": round_number,
        "worker": worker,
        "status": status,
        "failure_class": failure_class,
        "phase": phase,
        "duration_ms": int((time.perf_counter() - started) * 1000),
        "product": identity.get("product") if isinstance(identity, dict) else None,
        "identity": identity,
        "expected_identity": expected_identity,
        "binding": {
            "expected": expected_identity,
            "actual": actual_identity if actual_identity is not None else identity,
            "verified": binding_verified,
            "same_connection_as_task": transport_connected,
        },
        "value": value,
        "cleanup_attempted": cleanup_attempted,
        "target_state": target_state,
        "isolation_restored": isolation_restored,
        "cdp_call_count": client.call_count if client else 0,
        "cdp_error_count": client.error_count if client else 0,
        "ws_disconnect_count": client.disconnect_count if client else 0,
    }
    if error_type is not None:
        row["error_type"] = error_type
        row["error"] = error
    if cleanup_error is not None:
        row["cleanup_error"] = cleanup_error[:1000]
    return row


def percentile(values: list[int], fraction: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[round((len(ordered) - 1) * fraction)]


def main() -> int:
    args = parse_args()
    output_dir = (args.output or default_output_dir()).resolve()
    if output_dir.exists():
        raise bench.BenchError(f"output directory already exists: {output_dir}")
    output_dir.mkdir(parents=True)
    run_source_commit = source_commit()
    source = capture_source_provenance(
        pathlib.Path(__file__),
        extra_paths=(REPO_ROOT / "runner/run.py",),
    )
    write_json(output_dir / "provenance.json", source)
    results_path = output_dir / "results.jsonl"
    rows: list[dict[str, Any]] = []
    abort_kind: str | None = None
    abort_reason: str | None = None

    work = [(level, round_number) for level in args.level for round_number in range(1, args.rounds + 1)]
    for index, (level, round_number) in enumerate(work, start=1):
        barrier = threading.Barrier(level)
        with concurrent.futures.ThreadPoolExecutor(max_workers=level) as pool:
            futures = [
                pool.submit(
                    run_worker,
                    args.endpoint,
                    args.expected_identity,
                    output_dir,
                    level,
                    round_number,
                    worker,
                    barrier,
                )
                for worker in range(1, level + 1)
            ]
            batch = [future.result() for future in futures]
        rows.extend(batch)
        with results_path.open("a", encoding="utf-8") as handle:
            for row in batch:
                handle.write(json.dumps(row, sort_keys=True) + "\n")
        passed = sum(row["status"] == "pass" for row in batch)
        failed = sum(row["status"] == "fail" for row in batch)
        incomplete = sum(
            row["status"] in INCOMPLETE_STATUSES for row in batch
        )
        durations = [
            int(row["duration_ms"])
            for row in batch
            if row["status"] in {"pass", "fail"}
        ]
        latency = (
            f"p50={int(statistics.median(durations))}ms max={max(durations)}ms"
            if durations
            else "p50=n/a max=n/a"
        )
        print(
            f"[{index:02d}/{len(work):02d}] level={level:>2} round={round_number} "
            f"pass={passed}/{level} fail={failed} incomplete={incomplete} "
            f"{latency}",
            flush=True,
        )
        unclean = [
            row for row in batch if row.get("isolation_restored") is False
        ]
        if unclean:
            workers = ", ".join(str(row["worker"]) for row in unclean)
            abort_kind = "isolation"
            abort_reason = (
                f"target cleanup was not confirmed at level={level}, "
                f"round={round_number}, worker(s)={workers}"
            )
            print(f"abort: {abort_reason}", file=sys.stderr, flush=True)
            break
        binding_failures = [
            row
            for row in batch
            if row.get("failure_class") == "binding_unverified"
        ]
        if binding_failures:
            workers = ", ".join(
                str(row["worker"]) for row in binding_failures
            )
            abort_kind = "binding"
            abort_reason = (
                f"remote identity was not verified at level={level}, "
                f"round={round_number}, worker(s)={workers}"
            )
            print(f"abort: {abort_reason}", file=sys.stderr, flush=True)
            break
        if index < len(work) and args.cooldown_seconds:
            time.sleep(args.cooldown_seconds)

    by_level: dict[str, Any] = {}
    for level in args.level:
        selected = [row for row in rows if row["level"] == level]
        if not selected:
            by_level[str(level)] = {
                "attempts": 0,
                "functional_attempts": 0,
                "pass": 0,
                "fail": 0,
                "timeout": 0,
                "transport_error": 0,
                "infra": 0,
                "p50_ms": None,
                "p95_ms": None,
                "max_ms": None,
            }
            continue
        functional = [
            row for row in selected if row["status"] in {"pass", "fail"}
        ]
        durations = [int(row["duration_ms"]) for row in functional]
        by_level[str(level)] = {
            "attempts": len(selected),
            "functional_attempts": len(functional),
            "pass": sum(row["status"] == "pass" for row in selected),
            "fail": sum(row["status"] == "fail" for row in selected),
            "timeout": sum(row["status"] == "timeout" for row in selected),
            "transport_error": sum(
                row["status"] == "transport_error" for row in selected
            ),
            "infra": sum(row["status"] == "infra" for row in selected),
            "p50_ms": (
                int(statistics.median(durations)) if durations else None
            ),
            "p95_ms": percentile(durations, 0.95),
            "max_ms": max(durations) if durations else None,
        }
    status_counts = {
        status: sum(row["status"] == status for row in rows)
        for status in RESULT_STATUSES
    }
    requested_sessions = sum(args.level) * args.rounds
    evidence_complete = (
        abort_reason is None
        and len(rows) == requested_sessions
        and not any(status_counts[status] for status in INCOMPLETE_STATUSES)
    )
    summary = {
        "schema": "experimental.kitesurf_concurrency_probe.v3",
        "created_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "source_commit": run_source_commit,
        "source": source,
        "endpoint": args.endpoint,
        "expected_identity": args.expected_identity,
        "levels": args.level,
        "rounds": args.rounds,
        "cooldown_seconds": args.cooldown_seconds,
        "bounded_max_concurrency": 10,
        "formal_score_eligible": False,
        "aborted": abort_reason is not None,
        "abort_kind": abort_kind,
        "abort_reason": abort_reason,
        "evidence_complete": evidence_complete,
        "requested_sessions": requested_sessions,
        "completed_sessions": len(rows),
        "by_level": by_level,
        "status_counts": status_counts,
        "qualified_functional_sessions": (
            status_counts["pass"] + status_counts["fail"]
        ),
        "total_sessions": len(rows),
        "total_cdp_calls": sum(int(row["cdp_call_count"]) for row in rows),
        "total_cdp_errors": sum(int(row["cdp_error_count"]) for row in rows),
        "total_disconnects": sum(int(row["ws_disconnect_count"]) for row in rows),
    }
    (output_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"output={output_dir}")
    return 0 if evidence_complete else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except bench.BenchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2)
