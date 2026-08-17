#!/usr/bin/env python3
"""Compare Kitesurf target creation with its advertised default context ID."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sys
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--expect-product", default=EXPECTED_PRODUCT)
    parser.add_argument(
        "--expect-protocol-version", default=EXPECTED_PROTOCOL_VERSION
    )
    parser.add_argument("--expect-revision", default=EXPECTED_REVISION)
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--delay-ms", type=int, default=1000)
    parser.add_argument("--output", type=pathlib.Path)
    args = parser.parse_args()
    parsed = urllib.parse.urlparse(args.endpoint)
    if parsed.scheme != "wss" or not parsed.hostname:
        parser.error("--endpoint must be an absolute wss:// URL")
    if args.rounds < 1 or args.rounds > 5:
        parser.error("--rounds must be 1 through 5")
    if args.delay_ms < 0:
        parser.error("--delay-ms cannot be negative")
    for option, value in (
        ("--expect-product", args.expect_product),
        ("--expect-protocol-version", args.expect_protocol_version),
        ("--expect-revision", args.expect_revision),
    ):
        if not isinstance(value, str) or not value.strip():
            parser.error(f"{option} must be non-empty")
    return args


def default_output_dir() -> pathlib.Path:
    stamp = dt.datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
    return REPO_ROOT / "runs" / f"experimental_kitesurf_target_context_{stamp}"


def command_outcome(
    client: bench.CDPClient,
    method: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    try:
        return {
            "ok": True,
            "creation_state": "created",
            "result": client.command(method, params),
        }
    except bench.CDPCommandError as exc:
        return {
            "ok": False,
            "creation_state": "rejected",
            "error": str(exc),
            "error_payload": exc.error,
        }
    except Exception as exc:
        return {
            "ok": False,
            "creation_state": "ambiguous",
            "error": f"{type(exc).__name__}: {exc}",
        }


def cleanup_created_target(
    client: bench.CDPClient,
    outcome: dict[str, Any],
) -> dict[str, Any]:
    if outcome.get("ok") is not True:
        ambiguous = outcome.get("creation_state") == "ambiguous"
        return {
            "required": ambiguous,
            "confirmed": not ambiguous,
            "attempts": [],
            "reason": (
                "create outcome was ambiguous and returned no targetId"
                if ambiguous
                else "create command was not attempted or was explicitly rejected"
            ),
        }
    target_id = str((outcome.get("result") or {}).get("targetId") or "")
    if not target_id:
        return {
            "required": True,
            "confirmed": False,
            "target_id": None,
            "attempts": [],
            "error": "successful Target.createTarget response had no targetId",
        }
    attempts: list[dict[str, Any]] = []
    for attempt in range(1, 3):
        try:
            result = client.command(
                "Target.closeTarget", {"targetId": target_id}
            )
            confirmed = result.get("success") is True
            attempts.append(
                {"attempt": attempt, "result": result, "confirmed": confirmed}
            )
            if confirmed:
                return {
                    "required": True,
                    "confirmed": True,
                    "target_id": target_id,
                    "attempts": attempts,
                }
        except Exception as exc:
            attempts.append(
                {
                    "attempt": attempt,
                    "confirmed": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
    return {
        "required": True,
        "confirmed": False,
        "target_id": target_id,
        "attempts": attempts,
        "error": "Target.closeTarget did not confirm cleanup after two attempts",
    }


def run_round(
    args: argparse.Namespace,
    output_dir: pathlib.Path,
    round_number: int,
) -> dict[str, Any]:
    started = time.perf_counter()
    with bench.CDPClient(
        args.endpoint,
        output_dir / "traces" / f"round-{round_number}.cdp.jsonl",
        timeout_s=15.0,
    ) as client:
        identity = client.command("Browser.getVersion", {})
        expected_identity = bench.require_remote_cdp_identity(
            {
                "product": args.expect_product,
                "protocolVersion": args.expect_protocol_version,
                "revision": args.expect_revision,
            },
            label="target-context expected identity",
        )
        actual_identity = bench.require_remote_cdp_identity(
            identity,
            label="target-context task identity",
        )
        if actual_identity != expected_identity:
            raise bench.BenchError(
                "remote identity changed: expected "
                f"{expected_identity!r}, got {actual_identity!r}"
            )
        target_events = [
            event
            for event in client.events
            if event.get("method") == "Target.targetCreated"
        ]
        if not target_events:
            try:
                target_events.append(
                    client.wait_for_event("Target.targetCreated", timeout_s=2.0)
                )
            except bench.CDPTransportTimeout:
                raise
            except TimeoutError:
                pass
        advertised = [
            event.get("params", {}).get("targetInfo", {}).get("browserContextId")
            for event in target_events
        ]
        advertised = [str(value) for value in advertised if value is not None]
        context_id = advertised[0] if advertised else None
        with_context = (
            command_outcome(
                client,
                "Target.createTarget",
                {"url": "about:blank", "browserContextId": context_id},
            )
            if context_id is not None
            else {
                "ok": False,
                "creation_state": "not_attempted",
                "error": "no browserContextId advertised",
            }
        )
        with_context_cleanup = cleanup_created_target(client, with_context)
        if with_context_cleanup["confirmed"]:
            without_context = command_outcome(
                client,
                "Target.createTarget",
                {"url": "about:blank"},
            )
            without_context_cleanup = cleanup_created_target(
                client, without_context
            )
        else:
            without_context = {
                "ok": False,
                "creation_state": "not_attempted",
                "error": "skipped because prior target cleanup was unconfirmed",
            }
            without_context_cleanup = {
                "required": False,
                "confirmed": False,
                "attempts": [],
                "reason": "not attempted after isolation failure",
            }
        isolation_restored = bool(
            with_context_cleanup["confirmed"]
            and without_context_cleanup["confirmed"]
        )
        return {
            "round": round_number,
            "status": "observed" if isolation_restored else "infra",
            "failure": (
                None
                if isolation_restored
                else {
                    "layer": "harness",
                    "code": "target_cleanup_unconfirmed",
                    "detail": "target cleanup was not confirmed; later rounds must stop",
                }
            ),
            "duration_ms": int((time.perf_counter() - started) * 1000),
            "identity": identity,
            "expected_identity": expected_identity,
            "advertised_context_ids": advertised,
            "selected_context_id": context_id,
            "create_with_advertised_context": with_context,
            "cleanup_with_advertised_context": with_context_cleanup,
            "create_without_context": without_context,
            "cleanup_without_context": without_context_cleanup,
            "isolation_restored": isolation_restored,
            "cdp_call_count": client.call_count,
            "cdp_error_count": client.error_count,
            "ws_disconnect_count": client.disconnect_count,
        }


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
    rows: list[dict[str, Any]] = []
    results_path = output_dir / "results.jsonl"
    isolation_abort: str | None = None
    for round_number in range(1, args.rounds + 1):
        row = run_round(args, output_dir, round_number)
        rows.append(row)
        with results_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
        print(
            f"[{round_number}/{args.rounds}] advertised={row['selected_context_id']!r} "
            f"with={row['create_with_advertised_context']['ok']} "
            f"without={row['create_without_context']['ok']}",
            flush=True,
        )
        if row["isolation_restored"] is False:
            isolation_abort = (
                f"target cleanup unconfirmed in round {round_number}; "
                "subsequent rounds aborted"
            )
            print(f"abort: {isolation_abort}", file=sys.stderr, flush=True)
            break
        if round_number < args.rounds and args.delay_ms:
            time.sleep(args.delay_ms / 1000)
    summary = {
        "schema": "experimental.kitesurf_target_context_probe.v2",
        "created_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "source_commit": run_source_commit,
        "source": source,
        "endpoint": args.endpoint,
        "expected_identity": {
            "product": args.expect_product,
            "protocolVersion": args.expect_protocol_version,
            "revision": args.expect_revision,
        },
        "rounds": args.rounds,
        "requested_rounds": args.rounds,
        "completed_rounds": len(rows),
        "aborted": isolation_abort is not None,
        "abort_reason": isolation_abort,
        "formal_score_eligible": False,
        "advertised_context_ids": sorted(
            {value for row in rows for value in row["advertised_context_ids"]}
        ),
        "create_with_advertised_context_successes": sum(
            row["create_with_advertised_context"]["ok"] for row in rows
        ),
        "create_without_context_successes": sum(
            row["create_without_context"]["ok"] for row in rows
        ),
        "total_cdp_calls": sum(row["cdp_call_count"] for row in rows),
        "total_cdp_errors": sum(row["cdp_error_count"] for row in rows),
        "total_disconnects": sum(row["ws_disconnect_count"] for row in rows),
        "status_counts": dict(
            sorted(
                (status, sum(row["status"] == status for row in rows))
                for status in {row["status"] for row in rows}
            )
        ),
    }
    (output_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"output={output_dir}")
    return 2 if isolation_abort is not None else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except bench.BenchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2)
