"""TESTING.md §3 (scripted rows) + §5.2 — run_node_cdp_probe_driver via a stub script.

Uses test/_stub_scripts/stub.js (resolved relative to the real BENCH_ROOT, as
run.py does) so no real driver script runs. Covers: dirty stdout -> infra/
script_error, nonzero exit -> infra, ok:false engine_unsupported ->
unsupported, script timeout -> timeout/infra, server_side grading against a
live FixtureServer, the newer inline_assertions script-graded path
(observations.checks), and driver.env templating ({seed}/{session}/
{fixture_base_url}/{artifact_dir}) plus the CDP_PORT/BROWSER_WS env plumbing.

Requires node (guarded); everything else stays on 127.0.0.1.
"""
from __future__ import annotations

import json
import signal
import shutil

import pytest

from runner import run as runner_run
from _fakes import make_l2_task_dict, make_resolved, stub_browser

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is required for node_cdp_probe stub tests")

STUB_SCRIPT = "test/_stub_scripts/stub.js"
GATE = {"required": True, "status": "pass", "chrome_attempt_ref": "chrome:x:1"}


def scripted_task(mode, grader=None, env_extra=None, task_ms=15000, task_id=None):
    driver_env = {"STUB_MODE": mode}
    if env_extra:
        driver_env.update(env_extra)
    obj = make_l2_task_dict(
        task_id=task_id or f"stub_{mode}_001",
        scene={"kind": "self_hosted_fixture", "url": "/fixtures/stub.html?seed={seed}&session={session}"},
        driver={"kind": "node_cdp_probe", "script": STUB_SCRIPT, "env": driver_env},
        grader=grader or {"kind": "inline_assertions", "checks": []},
        timeouts={"task_ms": task_ms, "hard_kill_ms": task_ms * 2},
    )
    return make_resolved(task=obj, subset_gate="required")


def run_attempt(
    tmp_path,
    fake,
    task,
    fixture_base_url,
    engine="moli",
    seed="sd123",
    browser_process=None,
):
    run_dir = tmp_path / "run"
    results_path = run_dir / "results.jsonl"
    browser = stub_browser(fake.port, engine=engine)
    if browser_process is not None:
        browser.process = browser_process
    result = runner_run.run_driver_attempt(
        run_dir,
        results_path,
        "stub_run_001",
        task,
        engine,
        1,
        seed,
        browser,
        GATE,
        score_eligible=True,
        fixture_base_url=fixture_base_url,
    )
    return run_dir, result


class ExitedEngineProcess:
    pid = -1

    def poll(self):
        return -signal.SIGSEGV


@pytest.fixture
def base_url(fixture_server):
    return fixture_server().base_url


# --- failure mappings (§5.2 scripted rows) -----------------------------------


def test_dirty_stdout_is_infra_script_error(tmp_path, fake_cdp, base_url):
    run_dir, result = run_attempt(tmp_path, fake_cdp(), scripted_task("not_json"), base_url)
    assert result["status"] == "infra"
    assert result["failure"]["class"] == "script_error"
    final = run_dir / result["artifact_dir"]
    assert "not json" in (final / "stdout.log").read_text()


def test_nonzero_exit_is_infra_script_error(tmp_path, fake_cdp, base_url):
    _, result = run_attempt(tmp_path, fake_cdp(), scripted_task("exit_nonzero"), base_url)
    assert result["status"] == "infra"
    assert result["failure"]["class"] == "script_error"
    assert "exited with code 3" in result["failure"]["detail"]
    assert result["failure"]["origin"] == "driver_process"
    assert result["failure"]["process"] == {
        "kind": "driver",
        "state": "exited",
        "returncode": 3,
        "signal": None,
        "signal_name": None,
        "core_dumped": None,
    }


def test_engine_exit_overrides_driver_nonzero_and_preserves_driver_process(
    tmp_path, fake_cdp, base_url
):
    _, result = run_attempt(
        tmp_path,
        fake_cdp(),
        scripted_task("exit_nonzero"),
        base_url,
        browser_process=ExitedEngineProcess(),
    )

    assert result["status"] == "crash"
    failure = result["failure"]
    assert failure["origin"] == "engine_process"
    assert failure["process"]["signal_name"] == "SIGSEGV"
    assert failure["secondary_status"] == "infra"
    assert failure["secondary_failure"]["origin"] == "driver_process"
    assert failure["secondary_failure"]["detail"] == "script exited with code 3"
    assert failure["secondary_process"] == failure["secondary_failure"]["process"]
    assert failure["secondary_process"]["kind"] == "driver"
    assert failure["secondary_process"]["returncode"] == 3


def test_engine_exit_overrides_structured_driver_failure_and_preserves_detail(
    tmp_path, fake_cdp, base_url
):
    run_dir, result = run_attempt(
        tmp_path,
        fake_cdp(),
        scripted_task("script_fail"),
        base_url,
        browser_process=ExitedEngineProcess(),
    )

    assert result["status"] == "crash"
    failure = result["failure"]
    assert failure["origin"] == "engine_process"
    assert failure["secondary_status"] == "infra"
    assert failure["secondary_failure"]["class"] == "script_error"
    assert "stub exploded" in failure["secondary_failure"]["detail"]
    grader = json.loads((run_dir / result["artifact_dir"] / "grader.json").read_text())
    assert grader["failure"] == failure


def test_ok_false_engine_unsupported(tmp_path, fake_cdp, base_url):
    _, result = run_attempt(tmp_path, fake_cdp(), scripted_task("unsupported"), base_url)
    assert result["status"] == "unsupported"
    assert result["failure"]["class"] == "engine_unsupported"
    assert "IndexedDB is not implemented" in result["failure"]["detail"]
    assert result["score_included"] is True  # unsupported stays in the denominator


def test_ok_false_generic_is_infra_script_error(tmp_path, fake_cdp, base_url):
    _, result = run_attempt(tmp_path, fake_cdp(), scripted_task("script_fail"), base_url)
    assert result["status"] == "infra"
    assert result["failure"]["class"] == "script_error"
    assert "stub exploded" in result["failure"]["detail"]


def test_script_timeout_is_timeout_infra(tmp_path, fake_cdp, base_url):
    _, result = run_attempt(tmp_path, fake_cdp(), scripted_task("sleep", task_ms=1200), base_url)
    assert result["status"] == "timeout"
    assert result["failure"]["class"] == "infra"
    assert result["failure"]["origin"] == "task_timeout"
    assert result["failure"]["process"]["kind"] == "driver"
    assert result["failure"]["process"]["state"] == "timed_out"
    assert result["duration_ms"] >= 1200


# --- inline_assertions (script-graded) path ------------------------------------


def test_inline_checks_pass(tmp_path, fake_cdp, base_url):
    run_dir, result = run_attempt(tmp_path, fake_cdp(), scripted_task("ok_inline"), base_url)
    assert result["status"] == "pass"
    assert result["failure"] is None
    assert result["answer"] == "42"
    final = run_dir / result["artifact_dir"]
    grader = json.loads((final / "grader.json").read_text())
    assert grader["ok"] is True
    assert grader["checks"][0]["name"] == "stub_check"
    # l2_standard placeholder files (console/network/pss may be empty).
    for name in runner_run.ARTIFACT_PROFILES["l2_standard"]:
        assert (final / name).exists(), name


def test_success_shaped_driver_result_does_not_hide_engine_exit(
    tmp_path, fake_cdp, base_url
):
    run_dir, result = run_attempt(
        tmp_path,
        fake_cdp(),
        scripted_task("ok_inline"),
        base_url,
        browser_process=ExitedEngineProcess(),
    )

    assert result["status"] == "crash"
    failure = result["failure"]
    assert failure["origin"] == "engine_process"
    assert failure["process"]["signal_name"] == "SIGSEGV"
    assert failure["secondary_status"] == "pass"
    assert "secondary_failure" not in failure
    grader = json.loads((run_dir / result["artifact_dir"] / "grader.json").read_text())
    assert grader["ok"] is False
    assert grader["failure"] == failure


def test_inline_checks_fail_is_cdp_semantic(tmp_path, fake_cdp, base_url):
    _, result = run_attempt(tmp_path, fake_cdp(), scripted_task("ok_inline_fail"), base_url)
    assert result["status"] == "fail"
    assert result["failure"]["class"] == "cdp_semantic"
    assert "script-reported checks failed" in result["failure"]["detail"]


def test_inline_without_checks_fails(tmp_path, fake_cdp, base_url):
    _, result = run_attempt(tmp_path, fake_cdp(), scripted_task("ok_no_checks"), base_url)
    assert result["status"] == "fail"
    assert result["failure"]["class"] == "cdp_semantic"
    assert "script reported no checks" in result["failure"]["detail"]


# --- server_side grading against a live FixtureServer ---------------------------


def test_server_side_grader_pass(tmp_path, fake_cdp, fixture_server):
    task = scripted_task(
        "ok_inline",
        grader={"kind": "server_side", "endpoint": "/__grade__/expected_answer"},
        task_id="stub_server_pass_001",
    )
    server = fixture_server(expected_answers={"stub_server_pass_001": {"mode": "equals", "expected": "42"}})
    _, result = run_attempt(tmp_path, fake_cdp(), task, server.base_url)
    assert result["status"] == "pass"
    assert result["failure"] is None


def test_server_side_grader_wrong_answer_fails(tmp_path, fake_cdp, fixture_server):
    task = scripted_task(
        "ok_inline",
        grader={"kind": "server_side", "endpoint": "/__grade__/expected_answer"},
        env_extra={"STUB_ANSWER": "41"},
        task_id="stub_server_fail_001",
    )
    server = fixture_server(expected_answers={"stub_server_fail_001": {"mode": "equals", "expected": "42"}})
    run_dir, result = run_attempt(tmp_path, fake_cdp(), task, server.base_url)
    assert result["status"] == "fail"
    assert result["failure"]["class"] == "cdp_semantic"
    grader = json.loads((run_dir / result["artifact_dir"] / "grader.json").read_text())
    assert grader["checks"][0]["name"] == "answer_equals_expected"
    assert grader["checks"][0]["status"] == "fail"


# --- driver.env templating + CDP plumbing (run_node_cdp_probe_driver direct) --------


def test_driver_env_templating_and_cdp_env(tmp_path, fake_cdp, base_url, monkeypatch):
    monkeypatch.delenv("AGENT_BROWSER_SOCKET_DIR", raising=False)
    fake = fake_cdp()
    seed = "sd456"
    task = scripted_task(
        "ok_inline",
        env_extra={
            "MY_SEED": "{seed}",
            "MY_SESSION": "{session}",
            "MY_BASE": "{fixture_base_url}",
            "MY_ART": "{artifact_dir}",
            "CFG_JSON": {"a": "{seed}", "n": 1},  # dict values are JSON-encoded then templated
        },
        task_id="stub_env_001",
    )
    artifact_dir = tmp_path / "artifact"
    artifact_dir.mkdir()
    browser = stub_browser(fake.port)
    driver_out = runner_run.run_node_cdp_probe_driver(
        task, browser, artifact_dir, base_url, "stub_run_env", "moli", 2, seed
    )
    assert driver_out["ok"] is True
    env = driver_out["observations"]["env"]
    session = f"stub_run_env-{task.task_id}-moli-2-{seed}"
    assert env["MY_SEED"] == seed
    assert env["MY_SESSION"] == session
    assert env["MY_BASE"] == base_url
    assert env["MY_ART"] == str(artifact_dir)
    assert json.loads(env["CFG_JSON"]) == {"a": seed, "n": 1}
    # Runner-provided plumbing:
    assert env["CDP_PORT"] == str(fake.port)
    assert env["BROWSER_WS"].startswith("ws://127.0.0.1:")
    assert env["SEED"] == seed
    assert env["ATTEMPT"] == "2"
    assert env["ARTIFACT_DIR"] == str(artifact_dir)
    assert env["AGENT_BROWSER_SOCKET_DIR"] == str(runner_run.DEFAULT_AGENT_BROWSER_SOCKET_DIR)
    assert env["AGENT_BROWSER_NAMESPACE"].startswith("a")
    assert len(env["AGENT_BROWSER_NAMESPACE"]) == 13
    assert env["AGENT_BROWSER_IDLE_TIMEOUT_MS"] == "15000"
    assert env["TASK_URL"] == f"{base_url}/fixtures/stub.html?seed={seed}&session={session}"
    # stdout/stderr logs land in the artifact dir.
    assert (artifact_dir / "stdout.log").exists()
    assert (artifact_dir / "stderr.log").exists()


def test_scripted_without_fixture_server_is_infra(tmp_path, fake_cdp):
    # run_driver_attempt refuses node_cdp_probe without a fixture server.
    _, result = run_attempt(tmp_path, fake_cdp(), scripted_task("ok_inline", task_id="stub_nofix_001"), None)
    assert result["status"] == "infra"
    assert result["failure"]["class"] == "infra"
    assert "fixture server is not running" in result["failure"]["detail"]
