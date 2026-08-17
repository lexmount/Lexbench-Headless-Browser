"""TESTING.md §4 — real-engine smoke (opt-in, needs pinned binaries).

Skipped unless ABB_ENGINE_TESTS is set: these launch the pinned Chrome/Moli/
Lightpanda/Obscura binaries from build_artifacts/. Run manually / nightly:

    ABB_ENGINE_TESTS=1 python3 -m pytest test/test_smoke_engines.py -q
"""
from __future__ import annotations

import json
import os

import pytest

from runner import run as runner_run

pytestmark = pytest.mark.skipif(
    not os.environ.get("ABB_ENGINE_TESTS"),
    reason="needs pinned engine binaries; set ABB_ENGINE_TESTS=1 to run",
)


def test_doctor_ok(capsys):
    rc = runner_run.main(["doctor"])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert out.rstrip().endswith("OK")


def test_tiny_l1_chrome_run_and_report(tmp_path, capsys):
    run_id = "pytest_smoke_l1_chrome"
    rc = runner_run.main(
        [
            "run",
            "--task",
            "runtime_evaluate_basic_001",
            "--engines",
            "chrome",
            "--k",
            "1",
            "--out",
            str(tmp_path),
            "--run-id",
            run_id,
        ]
    )
    assert rc == 0
    run_dirs = list(tmp_path.glob(f"{run_id}_*"))
    assert len(run_dirs) == 1
    run_dir = run_dirs[0]
    rows = runner_run.read_jsonl(run_dir / "results.jsonl")
    chrome_rows = [row for row in rows if row["engine"] == "chrome"]
    assert chrome_rows and chrome_rows[0]["status"] == "pass"  # 1 + 2 == 3
    assert chrome_rows[0]["answer"] == 3

    capsys.readouterr()
    assert runner_run.main(["report", "--run", str(run_dir)]) == 0
    scores = json.loads((run_dir / "scores.json").read_text())
    assert scores["run_id"] == run_dir.name


def test_tiny_l1_obscura_run_records_launch_provenance(tmp_path):
    run_id = "pytest_smoke_l1_obscura"
    rc = runner_run.main(
        [
            "run",
            "--task",
            "pw_raw_browser_getversion",
            "--engines",
            "obscura",
            "--k",
            "1",
            "--chrome-baseline",
            "off",
            "--host-telemetry",
            "off",
            "--out",
            str(tmp_path),
            "--run-id",
            run_id,
        ]
    )
    assert rc == 0
    run_dir = next(tmp_path.glob(f"{run_id}*"))
    row = runner_run.read_jsonl(run_dir / "results.jsonl")[0]
    assert row["engine"] == "obscura"
    assert row["status"] == "pass"
    assert row["fallback_used"] is False
    provenance = row["engine_provenance"]
    assert (
        provenance["binary_sha256"]
        == "42c7eac0f635959f09a7d32adfdd3a9bb5c852c65532630308fb93aee483f96f"
    )
    assert provenance["launch_command"][-1] == "--allow-private-network"
    assert provenance["http_endpoint"].startswith("http://127.0.0.1:")
    assert provenance["browser_ws"].startswith("ws://127.0.0.1:")


def test_relaunch_is_visible_in_attempt_provenance(tmp_path):
    """A replaced engine process must be distinguishable in the result rows.

    Moli's all-resources tasks force the worker to replace its process, which is
    the same mechanism a crash-and-relaunch takes. Without the generation and
    pid on the row there is no way to tell that two attempts ran on different
    processes, which is what makes a load-only non-pass unreplayable (#128).
    """
    run_id = "pytest_smoke_relaunch_provenance"
    rc = runner_run.main(
        [
            "run",
            "--engines",
            "moli",
            "--task",
            "pw_raw_browser_getversion",
            "--task",
            "v2_t2_misc_log_entryadded",
            "--k",
            "1",
            "--jobs",
            "1",
            "--chrome-baseline",
            "off",
            "--score-mode",
            "independent",
            "--host-telemetry",
            "off",
            "--out",
            str(tmp_path),
            "--run-id",
            run_id,
        ]
    )
    assert rc == 0
    rows = runner_run.read_jsonl(next(tmp_path.glob(f"{run_id}*")) / "results.jsonl")
    assert len(rows) == 2
    contexts = {row["task_id"]: row["run_context"] for row in rows}
    assert {ctx["worker_slot"] for ctx in contexts.values()} == {1}
    # The profile switch replaced the process, so the two attempts must not
    # claim the same generation.
    assert len({ctx["browser_generation"] for ctx in contexts.values()}) == 2
    assert len({ctx["browser_pid"] for ctx in contexts.values()}) == 2
    # The predecessor is same-process context, so a replaced process starts a
    # fresh chain rather than naming a task that ran on the dead one.
    assert contexts["pw_raw_browser_getversion"]["prev_task_id"] is None
    assert contexts["v2_t2_misc_log_entryadded"]["prev_task_id"] is None
    assert all(ctx["started_monotonic_ms"] > 0 for ctx in contexts.values())
    # start and duration must share a clock, or `start + duration` is not an end
    # time and the two sequential attempts appear to overlap.
    first, second = (
        next(row for row in rows if row["task_id"] == task_id)
        for task_id in ("pw_raw_browser_getversion", "v2_t2_misc_log_entryadded")
    )
    first_end = first["run_context"]["started_monotonic_ms"] + first["duration_ms"]
    assert first_end <= second["run_context"]["started_monotonic_ms"]


def test_framework_transport_crash_is_compatibility_evidence(tmp_path):
    """A framework killed by the engine after connect must grade, not go infra.

    Lightpanda answers `Browser.getVersion` without echoing the `sessionId` it
    was called with, which trips an assertion inside playwright-core and kills
    the transport from an event-loop callback. That is the engine failing to
    drive, so it belongs in the score as a fail; routing it to infra would drop
    a whole driver column out of the denominator. The assertion is
    on the boundary rather than on the defect, so it still holds once the engine
    stops violating the protocol.
    """
    run_id = "pytest_smoke_fw_transport"
    rc = runner_run.main(
        [
            "run",
            "--task",
            "sc_cs_url_surface__pw",
            "--engines",
            "lightpanda",
            "--k",
            "1",
            "--score-mode",
            "independent",
            "--host-telemetry",
            "off",
            "--out",
            str(tmp_path),
            "--run-id",
            run_id,
        ]
    )
    assert rc == 0
    row = runner_run.read_jsonl(next(tmp_path.glob(f"{run_id}*")) / "results.jsonl")[0]
    assert row["status"] in {"pass", "fail"}, row
    assert (row.get("failure") or {}).get("class") != "script_error", row


def test_moli_native_webdriver_selenium(tmp_path):
    run_id = "pytest_smoke_moli_native_selenium"
    rc = runner_run.main(
        [
            "run",
            "--task",
            "sc_cs_url_surface__se",
            "--engines",
            "moli",
            "--k",
            "1",
            "--chrome-baseline",
            "off",
            "--score-mode",
            "independent",
            "--out",
            str(tmp_path),
            "--run-id",
            run_id,
        ]
    )
    assert rc == 0
    run_dirs = list(tmp_path.glob(f"{run_id}*"))
    assert len(run_dirs) == 1
    run_dir = run_dirs[0]
    rows = runner_run.read_jsonl(run_dir / "results.jsonl")
    assert len(rows) == 1
    assert rows[0]["engine"] == "moli"
    assert rows[0]["status"] == "pass"
    assert rows[0]["launch_profile"] == "default"
    assert "--resource" not in rows[0]["engine_provenance"]["launch_command"]

    manifest = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
    assert (
        manifest["engines"]["moli"]["resource_fetch_policy"]
        == "task_scoped_launch_profile"
    )
    assert manifest["engines"]["moli"]["launch_profile_args"] == {
        "all_resources": ["--resource"]
    }
    assert manifest["resolved_tasks"][0]["launch_profile"] == "default"

    stdout_path = run_dir / rows[0]["artifact_dir"] / "stdout.log"
    adapter_result = json.loads(stdout_path.read_text(encoding="utf-8"))
    binding = adapter_result["observations"]["binding"]
    assert binding["binding_id"] == "moli__selenium"
    assert binding["browser_id"] == "moli"
    assert binding["driver_id"] == "selenium"
    assert binding["route_id"] == "native_webdriver"
    assert binding["transport"] == "native_webdriver"
    assert binding["transport_policy"] == "engine_native"
    assert binding["fallback_allowed"] is False
    assert binding["live_browser_name"] == "moli"
    assert all(
        row["status"] in {"verified", "not_applicable"}
        for row in binding["identity"]["http"]
    )
    assert all(
        row["status"] == "verified"
        for row in binding["identity"]["live"]
    )
    assert binding["verified"] is True


def test_moli_resource_task_uses_all_resources_profile(tmp_path):
    run_id = "pytest_smoke_moli_resource_profile"
    rc = runner_run.main(
        [
            "run",
            "--task",
            "v2_t2_misc_log_entryadded",
            "--engines",
            "moli",
            "--k",
            "1",
            "--chrome-baseline",
            "off",
            "--score-mode",
            "independent",
            "--out",
            str(tmp_path),
            "--run-id",
            run_id,
        ]
    )
    assert rc == 0
    run_dir = next(tmp_path.glob(f"{run_id}*"))
    row = runner_run.read_jsonl(run_dir / "results.jsonl")[0]
    assert row["engine"] == "moli"
    assert row["status"] == "pass"
    assert row["launch_profile"] == "all_resources"
    assert row["engine_provenance"]["serve_args"] == [
        "--resource"
    ]
    assert row["engine_provenance"]["launch_command"][-1] == (
        "--resource"
    )
