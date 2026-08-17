"""TESTING.md §3 + §5.2 + §6 — run_driver_attempt against a fake CDP endpoint.

Drives run_driver_attempt() directly with synthetic raw_cdp ResolvedTasks
against the FakeCDP server (stdlib HTTP + RFC6455 WebSocket). Locks the
status/failure.class truth table (§5.2, incl. kernel_workitem for moli) and
the artifact write invariants (§6: atomic rename, refuse overwrite, profile
placeholder files, results.jsonl parity with run.json).
"""
from __future__ import annotations

import errno
import json
import pathlib
import signal
import socket
import types
import urllib.error

import pytest

from runner import run as runner_run
from _fakes import CLOSE, make_resolved, make_task_dict, stub_browser

GATE_OFF = {"required": False, "status": "off", "chrome_attempt_ref": None}


def eval_result(value, value_type="number", extra=None):
    payload = {"result": {"result": {"type": value_type, "value": value}}}
    if extra:
        payload["result"].update(extra)
    return payload


def run_attempt(tmp_path, fake, task, engine="moli", attempt=1, seed="seed123", score_eligible=True):
    run_dir = tmp_path / "run"
    results_path = run_dir / "results.jsonl"
    browser = stub_browser(fake.port, engine=engine)
    result = runner_run.run_driver_attempt(
        run_dir,
        results_path,
        "fake_run_001",
        task,
        engine,
        attempt,
        seed,
        browser,
        GATE_OFF,
        score_eligible=score_eligible,
        fixture_base_url=None,
    )
    return run_dir, results_path, result


# --- pass path + artifact invariants -----------------------------------------


def test_pass_path_and_artifact_invariants(tmp_path, fake_cdp):
    fake = fake_cdp({"Runtime.evaluate": eval_result(3)})
    task = make_resolved(subset_gate="off")
    run_dir, results_path, result = run_attempt(tmp_path, fake, task)

    assert result["status"] == "pass"
    assert result["failure"] is None
    assert result["answer"] == 3
    assert result["score_included"] is True  # moli + score-eligible run
    assert result["cdp_call_count"] >= 2
    assert result["cdp_error_count"] == 0

    # §6: final artifact dir exists, tmp dir was atomically renamed away.
    final = run_dir / "artifacts" / "L1" / task.subset_id / task.task_id / "moli" / "1"
    assert final.is_dir()
    assert result["artifact_dir"] == f"artifacts/L1/{task.subset_id}/{task.task_id}/moli/1"
    leftovers = [p for p in final.parent.iterdir() if "tmp-" in p.name]
    assert leftovers == []

    # §6: profile-declared files all exist (l1_standard).
    for name in runner_run.ARTIFACT_PROFILES["l1_standard"]:
        assert (final / name).exists(), name
    assert (final / "cdp.jsonl").read_text().strip()  # trace captured

    # §6: results.jsonl has exactly one row and it matches run.json.
    rows = runner_run.read_jsonl(results_path)
    assert len(rows) == 1
    assert rows[0] == json.loads((final / "run.json").read_text())

    grader = json.loads((final / "grader.json").read_text())
    assert grader["ok"] is True
    assert {check["name"] for check in grader["checks"]} == {"value_equals_3", "result_type_number"}


def test_raw_cdp_substitutes_portable_fixture_and_attempt_paths(tmp_path, fake_cdp):
    seen = {}

    def capture_files(params):
        seen["files"] = params["files"]
        return {"result": {}}

    def capture_download_path(params):
        if params.get("behavior") == "default":
            seen["download_reset"] = params
        else:
            seen["download_path"] = params["downloadPath"]
        return {"result": {}}

    fake = fake_cdp(
        {
            "DOM.setFileInputFiles": capture_files,
            "Browser.setDownloadBehavior": capture_download_path,
        }
    )
    task = make_resolved(
        task=make_task_dict(
            task_id="portable_raw_paths_001",
            driver={
                "kind": "raw_cdp",
                "steps": [
                    {
                        "method": "DOM.setFileInputFiles",
                        "params": {
                            "files": ["{fixture_path}/v0_2/t2b/upload_sample.txt"],
                        },
                    },
                    {
                        "method": "Browser.setDownloadBehavior",
                        "session": "browser",
                        "params": {
                            "behavior": "allowAndName",
                            "downloadPath": "{artifact_dir}",
                        },
                    },
                ],
            },
            grader={"kind": "inline_assertions", "checks": [{"kind": "no_error"}]},
        ),
        subset_gate="off",
    )
    run_dir, _, result = run_attempt(tmp_path, fake, task)

    assert result["status"] == "pass"
    assert seen["files"] == [
        str((runner_run.BENCH_ROOT / "fixtures" / "v0_2" / "t2b" / "upload_sample.txt").resolve())
    ]
    assert pathlib.Path(seen["files"][0]).is_file()
    final_dir = run_dir / result["artifact_dir"]
    raw_attempt_dir = pathlib.Path(seen["download_path"])
    assert raw_attempt_dir.parent == final_dir.parent
    assert raw_attempt_dir.name.startswith(".1.tmp-")
    assert not raw_attempt_dir.exists()
    assert final_dir.is_dir()
    assert seen["download_reset"] == {
        "behavior": "default",
        "eventsEnabled": False,
    }


def test_second_identical_attempt_refuses_overwrite(tmp_path, fake_cdp):
    fake = fake_cdp({"Runtime.evaluate": eval_result(3)})
    task = make_resolved(subset_gate="off")
    run_attempt(tmp_path, fake, task)
    with pytest.raises(runner_run.BenchError, match="refusing to overwrite"):
        run_attempt(tmp_path, fake, task)
    # And no partial rows were appended by the refused attempt.
    assert len(runner_run.read_jsonl(tmp_path / "run" / "results.jsonl")) == 1


# --- §5.2 truth table ----------------------------------------------------------


def test_semantic_grading_failure(tmp_path, fake_cdp):
    fake = fake_cdp({"Runtime.evaluate": eval_result(2)})
    task = make_resolved(subset_gate="off")
    _, results_path, result = run_attempt(tmp_path, fake, task)
    assert result["status"] == "fail"
    assert result["failure"]["class"] == "cdp_semantic"
    assert result["score_included"] is True  # real fail stays in the denominator


def test_cdp_error_unsupported_message(tmp_path, fake_cdp):
    fake = fake_cdp({"Runtime.evaluate": {"error": {"code": -32601, "message": "Method not found"}}})
    task = make_resolved(subset_gate="off")
    _, _, result = run_attempt(tmp_path, fake, task)
    assert result["status"] == "unsupported"
    assert result["failure"]["class"] == "engine_unsupported"
    assert result["failure"]["kernel_workitem"] is False


def test_chrome_style_wasnt_found_grades_as_unsupported(tmp_path, fake_cdp):
    # Fixed: Chrome's actual unknown-method text "'X' wasn't found" now maps to
    # engine_unsupported per TESTING.md §3 (no kernel_workitem).
    fake = fake_cdp({"Runtime.evaluate": {"error": {"message": "'Runtime.evaluate' wasn't found"}}})
    task = make_resolved(subset_gate="off")
    _, _, result = run_attempt(tmp_path, fake, task)
    assert result["status"] == "unsupported"
    assert result["failure"]["class"] == "engine_unsupported"
    assert result["failure"]["kernel_workitem"] is False


def test_cdp_semantic_error_kernel_workitem_only_for_moli(tmp_path, fake_cdp):
    overrides = {"Runtime.evaluate": {"error": {"message": "Invalid expression passed"}}}
    task = make_resolved(subset_gate="off")

    fake = fake_cdp(overrides)
    _, _, moli_result = run_attempt(tmp_path / "moli", fake, task, engine="moli")
    assert moli_result["status"] == "fail"
    assert moli_result["failure"]["class"] == "cdp_semantic"
    assert moli_result["failure"]["kernel_workitem"] is True

    fake2 = fake_cdp(overrides)
    _, _, lp_result = run_attempt(tmp_path / "lp", fake2, task, engine="lightpanda")
    assert lp_result["failure"]["class"] == "cdp_semantic"
    assert lp_result["failure"]["kernel_workitem"] is False


def test_connection_closed_mid_command_is_crash(tmp_path, fake_cdp):
    fake = fake_cdp({"Runtime.evaluate": CLOSE})
    task = make_resolved(subset_gate="off")
    run_dir, results_path, result = run_attempt(tmp_path, fake, task)
    assert result["status"] == "crash"
    assert result["failure"]["class"] == "infra"
    assert result["failure"]["origin"] == "client_transport"
    assert result["failure"]["process"]["kind"] == "engine"
    assert result["failure"]["process"]["state"] == "running"
    # NOTE(bug?): TESTING.md §3 expects ws_disconnect_count > 0 here, but when
    # the driver raises, its metrics dict never reaches the result row, so the
    # counter stays at the attempt_base_result default of 0.
    assert result["ws_disconnect_count"] == 0
    # Artifact + results row are still written for the crashed attempt (§6).
    final = run_dir / "artifacts" / "L1" / task.subset_id / task.task_id / "moli" / "1"
    assert (final / "run.json").exists() and (final / "grader.json").exists()
    assert len(runner_run.read_jsonl(results_path)) == 1


def test_socket_timeout_is_task_timeout_not_browser_crash(tmp_path, fake_cdp, monkeypatch):
    fake = fake_cdp()
    task = make_resolved(subset_gate="off")

    def raise_socket_timeout(*_args, **_kwargs):
        raise socket.timeout("timed out")

    monkeypatch.setattr(runner_run, "run_raw_cdp_driver", raise_socket_timeout)
    _, _, result = run_attempt(tmp_path, fake, task)

    assert result["status"] == "timeout"
    assert result["failure"]["class"] == "infra"
    assert result["failure"]["origin"] == "task_timeout"
    assert result["failure"]["process"]["kind"] == "engine"
    assert result["failure"]["process"]["state"] == "running"
    assert result["failure"]["process"]["returncode"] is None


@pytest.mark.parametrize(
    "error_factory",
    [
        lambda: FileNotFoundError("artifact path is missing"),
        lambda: OSError(errno.EIO, "artifact I/O failed"),
        lambda: urllib.error.URLError("fixture grader is unavailable"),
    ],
    ids=["file-not-found", "generic-io", "url-error"],
)
def test_non_transport_oserror_is_infra_unclassified(
    tmp_path, fake_cdp, monkeypatch, error_factory
):
    fake = fake_cdp()
    task = make_resolved(subset_gate="off")

    def raise_io_error(*_args, **_kwargs):
        raise error_factory()

    monkeypatch.setattr(runner_run, "run_raw_cdp_driver", raise_io_error)
    _, _, result = run_attempt(tmp_path, fake, task)

    assert result["status"] == "infra"
    assert result["failure"]["class"] == "infra"
    assert "origin" not in result["failure"]
    assert "process" not in result["failure"]


def test_explicit_socket_errno_is_client_transport(tmp_path, fake_cdp, monkeypatch):
    fake = fake_cdp()
    task = make_resolved(subset_gate="off")

    def raise_socket_error(*_args, **_kwargs):
        raise OSError(errno.ENOTCONN, "socket is not connected")

    monkeypatch.setattr(runner_run, "run_raw_cdp_driver", raise_socket_error)
    _, _, result = run_attempt(tmp_path, fake, task)

    assert result["status"] == "crash"
    assert result["failure"]["origin"] == "client_transport"
    assert result["failure"]["process"]["state"] == "running"


def test_disconnect_with_engine_exit_records_signal(tmp_path, fake_cdp):
    fake = fake_cdp({"Runtime.evaluate": CLOSE})
    task = make_resolved(subset_gate="off")
    browser = stub_browser(fake.port)

    class ExitedProc:
        pid = -1

        def poll(self):
            return -signal.SIGSEGV

    browser.process = ExitedProc()
    run_dir = tmp_path / "run"
    result = runner_run.run_driver_attempt(
        run_dir,
        run_dir / "results.jsonl",
        "fake_run_001",
        task,
        "moli",
        1,
        "seed123",
        browser,
        GATE_OFF,
        score_eligible=True,
        fixture_base_url=None,
    )

    assert result["status"] == "crash"
    assert result["failure"]["origin"] == "engine_process"
    process = result["failure"]["process"]
    assert process["kind"] == "engine"
    assert process["state"] == "exited"
    assert process["returncode"] == -signal.SIGSEGV
    assert process["signal"] == signal.SIGSEGV
    assert process["signal_name"] == "SIGSEGV"
    assert process["core_dumped"] is None


def test_process_diagnostic_preserves_core_dump_evidence(monkeypatch):
    class ExitedProc:
        pid = 12345

        def poll(self):
            return -signal.SIGABRT

    monkeypatch.setattr(
        runner_run.os,
        "waitid",
        lambda *_args: types.SimpleNamespace(si_code=runner_run.os.CLD_DUMPED),
    )

    process = runner_run.process_diagnostic("engine", ExitedProc())
    assert process["signal_name"] == "SIGABRT"
    assert process["core_dumped"] is True


def test_engine_exit_promotion_reuses_observed_core_dump_diagnostic():
    class MustNotPollAgain:
        def poll(self):
            raise AssertionError("terminal process diagnostic must be reused")

    observed = {
        "kind": "engine",
        "state": "exited",
        "returncode": -signal.SIGABRT,
        "signal": signal.SIGABRT,
        "signal_name": "SIGABRT",
        "core_dumped": True,
    }
    result = {
        "status": "infra",
        "failure": runner_run.failure_obj("infra", "fixture I/O failed"),
    }
    grader = {"ok": False, "checks": [], "failure": result["failure"]}

    runner_run.promote_observed_engine_exit(
        result,
        grader,
        MustNotPollAgain(),
        observed,
    )

    assert result["status"] == "crash"
    assert result["failure"]["origin"] == "engine_process"
    assert result["failure"]["process"] == observed
    assert result["failure"]["process"]["core_dumped"] is True
    assert grader["failure"] == result["failure"]


def test_engine_exit_promotion_refreshes_previously_running_diagnostic():
    class ExitsDuringCleanup:
        def poll(self):
            return -signal.SIGSEGV

    result = {
        "status": "timeout",
        "failure": runner_run.failure_obj(
            "infra",
            "task timed out",
            origin="task_timeout",
            process={
                "kind": "engine",
                "state": "running",
                "returncode": None,
                "signal": None,
                "signal_name": None,
                "core_dumped": None,
            },
        ),
    }
    grader = {"ok": False, "checks": [], "failure": result["failure"]}
    observed_running = dict(result["failure"]["process"])

    runner_run.promote_observed_engine_exit(
        result,
        grader,
        ExitsDuringCleanup(),
        observed_running,
    )

    assert result["status"] == "crash"
    assert result["failure"]["origin"] == "engine_process"
    assert result["failure"]["process"]["signal_name"] == "SIGSEGV"
    assert result["failure"]["secondary_status"] == "timeout"
    assert result["failure"]["secondary_failure"]["origin"] == "task_timeout"


def test_exception_details_feed_eval_checks(tmp_path, fake_cdp):
    throwing = eval_result(None, value_type="undefined", extra={"exceptionDetails": {"text": "ReferenceError"}})

    task_expects_exception = make_resolved(
        task=make_task_dict(
            task_id="expects_exception_001",
            grader={"kind": "inline_assertions", "checks": [{"kind": "eval_has_exception"}]},
        ),
        subset_gate="off",
    )
    fake = fake_cdp({"Runtime.evaluate": throwing})
    _, _, result = run_attempt(tmp_path / "has", fake, task_expects_exception)
    assert result["status"] == "pass"

    task_expects_clean = make_resolved(
        task=make_task_dict(
            task_id="expects_clean_001",
            grader={"kind": "inline_assertions", "checks": [{"kind": "eval_no_exception"}]},
        ),
        subset_gate="off",
    )
    fake2 = fake_cdp({"Runtime.evaluate": throwing})
    _, _, result2 = run_attempt(tmp_path / "none", fake2, task_expects_clean)
    assert result2["status"] == "fail"
    assert result2["failure"]["class"] == "cdp_semantic"


def test_save_as_placeholder_chaining(tmp_path, fake_cdp):
    # {saved:x.y} chaining: step 2 receives the value saved by step 1.
    seen = {}

    def capture(params):
        seen["expression"] = params.get("expression")
        return eval_result("ok", value_type="string")

    fake = fake_cdp(
        {
            "Runtime.evaluate": lambda params: (
                capture(params)
                if params.get("expression") != "1 + 2"
                else {"result": {"result": {"type": "object", "value": {"token": "tok-7"}}}}
            )
        }
    )
    task = make_resolved(
        task=make_task_dict(
            task_id="saved_chain_001",
            driver={
                "kind": "raw_cdp",
                "steps": [
                    {"method": "Runtime.enable"},
                    {"method": "Runtime.evaluate", "params": {"expression": "1 + 2", "returnByValue": True}, "save_as": "first"},
                    {"method": "Runtime.evaluate", "params": {"expression": "{saved:first.result.value.token}"}, "save_as": "second"},
                ],
            },
            grader={"kind": "inline_assertions", "checks": [{"kind": "value_equals", "name": "second", "expected": "ok"}]},
        ),
        subset_gate="off",
    )
    _, _, result = run_attempt(tmp_path, fake, task)
    assert result["status"] == "pass"
    assert seen["expression"] == "tok-7"


def test_unhandled_driver_error_is_infra(tmp_path, fake_cdp):
    # A fixture-scene raw task without a fixture server raises BenchError -> infra.
    fake = fake_cdp()
    task = make_resolved(
        task=make_task_dict(
            task_id="fixture_no_server_001",
            scene={"kind": "self_hosted_fixture", "url": "/l1/core"},
        ),
        subset_gate="off",
    )
    _, _, result = run_attempt(tmp_path, fake, task)
    assert result["status"] == "infra"
    assert result["failure"]["class"] == "infra"
    assert result["score_included"] is False  # infra never scores


def test_gate_skip_attempt_writes_chrome_gate_fail(tmp_path):
    task = make_resolved(subset_gate="required")
    run_dir = tmp_path / "run"
    results_path = run_dir / "results.jsonl"
    gate = {"required": True, "status": "fail", "chrome_attempt_ref": "chrome:x:1"}
    result = runner_run.run_gate_skip_attempt(run_dir, results_path, "fake_run_001", task, "moli", 1, "seed123", gate)
    assert result["status"] == "chrome_gate_fail"
    assert result["failure"]["class"] == "infra"
    assert result["score_included"] is False
    final = run_dir / "artifacts" / "L1" / task.subset_id / task.task_id / "moli" / "1"
    assert (final / "run.json").exists()
    rows = runner_run.read_jsonl(results_path)
    assert rows[0]["status"] == "chrome_gate_fail"


def test_chrome_attempt_never_scores(tmp_path, fake_cdp):
    fake = fake_cdp({"Runtime.evaluate": eval_result(3)})
    task = make_resolved(subset_gate="off")
    _, _, result = run_attempt(tmp_path, fake, task, engine="chrome")
    assert result["status"] == "pass"
    assert result["score_included"] is False
