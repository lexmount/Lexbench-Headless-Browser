"""v0.3 framework driver (framework_playwright / framework_puppeteer) coverage.

Five layers, mirroring how the driver is wired in runner/run.py:

  A. validate_task / validate_framework_steps schema rules (pure unit).
  B. run_framework_driver env construction (run_node_driver_process faked).
  C. browser_cdp_product per-process caching (CDPClient faked).
  D. check_harness_pins / installed_npm_version / harness_pins_summary
     against a tmp harness_pins.json (installed_npm_version faked).
  E. framework_probe.js env/binding-gate failure protocol: node subprocess,
     no browser, stdout must be a single JSON object (gated on node +
     repo-local playwright-core, like the scripted-driver stub tests).
  F. End-to-end against the pinned Chrome via BrowserManager + FixtureServer
     (gated on the chrome binary + node + repo-local frameworks, in the
     spirit of test_smoke_engines.py).
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import signal
import socket
import subprocess
import sys
import time

import pytest

from runner import run as runner_run
from _fakes import StubProc, make_resolved

BENCH_ROOT = runner_run.BENCH_ROOT
HAVE_NODE = shutil.which("node") is not None
HAVE_PW_CORE = (BENCH_ROOT / "node_modules" / "playwright-core").exists()
HAVE_PP_CORE = (BENCH_ROOT / "node_modules" / "puppeteer-core").exists()
CHROME_BINARY = runner_run.ENGINE_DEFS["chrome"]["binary"]

probe_gate = pytest.mark.skipif(
    not HAVE_NODE or not HAVE_PW_CORE,
    reason="needs node and repo-local playwright-core (npm ci)",
)
engine_gate = pytest.mark.skipif(
    not (HAVE_NODE and HAVE_PW_CORE and HAVE_PP_CORE and CHROME_BINARY.exists()),
    reason="needs node, repo-local playwright-core/puppeteer-core (npm ci) and the pinned Chrome binary",
)


# --- shared factories ---------------------------------------------------------


def test_runner_direct_script_help_remains_supported():
    proc = subprocess.run(
        [sys.executable, str(BENCH_ROOT / "runner" / "run.py"), "--help"],
        cwd=BENCH_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    assert "Agent Browser Bench CLI" in proc.stdout


def make_framework_task_dict(kind: str = "framework_playwright", **over):
    """A well-formed framework driver task dict (overridable per test)."""
    base = {
        "task_id": f"unit_{kind}_001",
        "task_version": 1,
        "layer": "L1",
        "subset_id": "l1.framework",
        "description": "Drives the engine with a pinned real framework over CDP for unit tests.",
        "features": ["framework.connect", "framework.evaluate"],
        "tags": ["purpose.framework", "version.v0_3"],
        "scene": {"kind": "self_hosted_fixture", "url": "/l1/core?seed={seed}&session={session}"},
        "driver": {
            "kind": kind,
            "steps": [
                {"op": "new_page"},
                {"op": "goto", "url": "{fixture_url}"},
                {"op": "evaluate", "expression": "6 * 7", "save_as": "answer"},
            ],
            "checks": [{"kind": "saved_equals", "name": "answer", "expected": "42"}],
        },
        "grader": {"kind": "inline_assertions", "checks": []},
        "timeouts": {"task_ms": 45000, "hard_kill_ms": 90000},
        "artifact_profile": "l1_standard",
    }
    base.update(over)
    return base


def framework_subset(kind: str = "framework_playwright"):
    return {"_layer_id": "L1", "subset_id": "l1.framework", "driver": kind, "chrome_gate": "off"}


def validate_errors(task_dict):
    kind = task_dict["driver"].get("kind", "framework_playwright")
    return runner_run.validate_task(make_resolved(task=task_dict), framework_subset(kind))


def assert_error(errors, substring):
    assert any(substring in error for error in errors), f"expected {substring!r} in {errors}"


def framework_browser(version_info=None, cdp_product="MoliLive/0.1"):
    info = {
        "Browser": "Moli/0.1.0",
        "User-Agent": "MoliUA/1.0",
        "webSocketDebuggerUrl": "ws://127.0.0.1:9333/devtools/browser/abc",
    }
    if version_info is not None:
        info = version_info
    return runner_run.BrowserProcess(
        engine="moli",
        port=9333,
        process=StubProc(),  # type: ignore[arg-type]
        version_info=info,
        cdp_product=cdp_product,
    )


# --- A. validate layer ---------------------------------------------------------


def test_validate_framework_pw_task_ok():
    assert validate_errors(make_framework_task_dict("framework_playwright")) == []


def test_validate_framework_pp_task_ok():
    assert validate_errors(make_framework_task_dict("framework_puppeteer")) == []


def test_validate_steps_empty_list():
    task = make_framework_task_dict(driver={"kind": "framework_playwright", "steps": []})
    assert_error(validate_errors(task), "non-empty driver.steps")


def test_validate_steps_missing():
    task = make_framework_task_dict(driver={"kind": "framework_playwright"})
    assert_error(validate_errors(task), "non-empty driver.steps")


def test_validate_step_must_be_object():
    task = make_framework_task_dict(driver={"kind": "framework_playwright", "steps": ["goto"]})
    assert_error(validate_errors(task), "driver.steps[0] must be an object")


def test_validate_step_missing_op():
    task = make_framework_task_dict(driver={"kind": "framework_playwright", "steps": [{"selector": "#x"}]})
    assert_error(validate_errors(task), "must declare a string `op`")


def test_validate_timeout_ms_must_be_number():
    task = make_framework_task_dict(
        driver={"kind": "framework_playwright", "steps": [{"op": "click", "selector": "#x", "timeout_ms": "fast"}]}
    )
    assert_error(validate_errors(task), "timeout_ms must be a number")


def test_validate_save_as_must_be_string():
    task = make_framework_task_dict(
        driver={"kind": "framework_playwright", "steps": [{"op": "evaluate", "expression": "1", "save_as": 7}]}
    )
    assert_error(validate_errors(task), "save_as must be a string")


def test_validate_screenshot_and_pdf_ops_are_outside_target():
    for op in ("screenshot", "pdf"):
        task = make_framework_task_dict(driver={"kind": "framework_playwright", "steps": [{"op": op}]})
        assert_error(validate_errors(task), f"op `{op}` is outside the benchmark target")


def test_validate_checks_must_be_list():
    task = make_framework_task_dict(
        driver={"kind": "framework_puppeteer", "steps": [{"op": "new_page"}], "checks": {"kind": "saved_truthy"}}
    )
    assert_error(validate_errors(task), "driver.checks must be a list")


def test_validate_connect_options_must_be_dict():
    task = make_framework_task_dict(
        driver={"kind": "framework_puppeteer", "steps": [{"op": "new_page"}], "connect_options": ["slowMo"]}
    )
    assert_error(validate_errors(task), "driver.connect_options must be an object")


def test_validate_scene_must_be_self_hosted_fixture():
    task = make_framework_task_dict(scene={"kind": "about_blank"})
    assert_error(validate_errors(task), "must use a self_hosted_fixture scene")


def test_validate_probe_script_must_exist(monkeypatch):
    monkeypatch.setattr(runner_run, "FRAMEWORK_PROBE_SCRIPT", pathlib.Path("/nonexistent/framework_probe.js"))
    assert_error(validate_errors(make_framework_task_dict()), "probe/adapter script missing")


# --- B. run_framework_driver env construction -----------------------------------


def capture_node_process(captured):
    def fake(task, script, env, artifact_dir, fixture_base_url, run_id, engine, attempt, seed, session):
        captured.update(
            script=script,
            env=env,
            artifact_dir=artifact_dir,
            fixture_base_url=fixture_base_url,
            session=session,
        )
        return {
            "ok": True,
            "answer": "stub",
            "observations": {},
            "grader": {"ok": True, "checks": [], "failure": None},
            "metrics": {},
        }

    return fake


def test_run_framework_driver_env_playwright(tmp_path, monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(runner_run, "run_node_driver_process", capture_node_process(captured))
    task = make_resolved(
        task=make_framework_task_dict(
            "framework_playwright",
            driver={
                "kind": "framework_playwright",
                "steps": [
                    {"op": "goto", "url": "{fixture_base_url}/l1/core"},
                    {"op": "fill", "selector": "#seed", "value": "{seed}"},
                    {"op": "evaluate", "expression": "'{session}'", "save_as": "answer"},
                ],
                "checks": [{"kind": "saved_equals", "name": "answer", "expected": "{seed}"}],
                "connect_options": {"slowMo": 5},
            },
        )
    )
    browser = framework_browser()
    base = "http://127.0.0.1:18080"
    out = runner_run.run_framework_driver(task, browser, tmp_path, base, "run1", "moli", 2, "seed9")
    assert out["ok"] is True and out["answer"] == "stub"

    session = f"run1-{task.task_id}-moli-2-seed9"
    assert captured["script"] == runner_run.FRAMEWORK_PROBE_SCRIPT
    assert captured["session"] == session
    env = captured["env"]
    assert env["FRAMEWORK"] == "playwright"
    assert env["BROWSER_WS"] == "ws://127.0.0.1:9333/devtools/browser/abc"
    assert env["CDP_PORT"] == "9333"
    assert env["EXPECT_PRODUCT"] == "Moli/0.1.0"
    assert env["EXPECT_UA"] == "MoliUA/1.0"
    assert env["EXPECT_PRODUCT_LIVE"] == "MoliLive/0.1"
    # {seed}/{session}/{fixture_base_url} templating inside FW_STEPS/FW_CHECKS.
    steps = json.loads(env["FW_STEPS"])
    assert steps[0]["url"] == f"{base}/l1/core"
    assert steps[1]["value"] == "seed9"
    assert steps[2]["expression"] == f"'{session}'"
    checks = json.loads(env["FW_CHECKS"])
    assert checks[0]["expected"] == "seed9"
    assert json.loads(env["FW_CONNECT_OPTIONS"]) == {"slowMo": 5}
    assert env["TASK_URL"] == f"{base}/l1/core?seed=seed9&session={session}"
    assert env["TASK_ID"] == task.task_id
    assert env["RUN_ID"] == "run1"
    assert env["ENGINE"] == "moli"
    assert env["ATTEMPT"] == "2"
    assert env["SEED"] == "seed9"
    assert env["ARTIFACT_DIR"] == str(tmp_path)


def test_run_framework_driver_env_puppeteer(tmp_path, monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(runner_run, "run_node_driver_process", capture_node_process(captured))
    task = make_resolved(task=make_framework_task_dict("framework_puppeteer"))
    runner_run.run_framework_driver(task, framework_browser(), tmp_path, "http://127.0.0.1:18080", "run1", "moli", 1, "s1")
    env = captured["env"]
    assert env["FRAMEWORK"] == "puppeteer"
    # Empty connect_options serializes as an empty JSON object, never "null".
    assert json.loads(env["FW_CONNECT_OPTIONS"]) == {}


def test_run_framework_driver_requires_browser_ws(tmp_path, monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(runner_run, "run_node_driver_process", capture_node_process(captured))
    task = make_resolved(task=make_framework_task_dict())
    browser = framework_browser(version_info={"Browser": "Moli/0.1.0"}, cdp_product="")
    with pytest.raises(runner_run.BenchError, match="no browser websocket"):
        runner_run.run_framework_driver(task, browser, tmp_path, "http://127.0.0.1:18080", "run1", "moli", 1, "s1")
    assert "env" not in captured  # never reached the node process


# --- C. browser_cdp_product caching ---------------------------------------------


def make_fake_cdp_client(result=None, exc=None):
    class FakeCDPClient:
        connects = 0
        commands = 0

        def __init__(self, *args, **kwargs):
            type(self).connects += 1
            if exc is not None:
                raise exc

        def __enter__(self):
            return self

        def __exit__(self, *excinfo):
            return False

        def command(self, method, params=None):
            type(self).commands += 1
            return dict(result or {})

    return FakeCDPClient


def test_browser_cdp_product_connects_once(monkeypatch):
    fake = make_fake_cdp_client(result={"product": "X/1.0"})
    monkeypatch.setattr(runner_run, "CDPClient", fake)
    browser = framework_browser(cdp_product=None)
    assert runner_run.browser_cdp_product(browser) == "X/1.0"
    assert runner_run.browser_cdp_product(browser) == "X/1.0"
    assert fake.connects == 1
    assert fake.commands == 1
    assert browser.cdp_product == "X/1.0"


def test_browser_cdp_product_connect_error_retries_next_attempt(monkeypatch):
    fake = make_fake_cdp_client(exc=ConnectionError("boom"))
    monkeypatch.setattr(runner_run, "CDPClient", fake)
    browser = framework_browser(cdp_product=None)
    assert runner_run.browser_cdp_product(browser) == ""
    assert runner_run.browser_cdp_product(browser) == ""
    # A transient capture failure must not be cached: the next attempt retries
    # instead of poisoning every later binding gate on this browser process.
    assert fake.connects == 2
    assert browser.cdp_product is None


def test_browser_cdp_product_without_ws_url_is_empty(monkeypatch):
    fake = make_fake_cdp_client(result={"product": "X/1.0"})
    monkeypatch.setattr(runner_run, "CDPClient", fake)
    browser = framework_browser(version_info={"Browser": "Moli/0.1.0"}, cdp_product=None)
    assert runner_run.browser_cdp_product(browser) == ""
    assert fake.connects == 0


# --- D. harness pins ------------------------------------------------------------


def write_pins(tmp_path, drivers):
    path = tmp_path / "harness_pins.json"
    path.write_text(json.dumps({"drivers": drivers}), encoding="utf-8")
    return path


def test_check_harness_pins_ok(tmp_path, monkeypatch):
    path = write_pins(tmp_path, {"pw": {"version": "1.61.1", "npm_package": "playwright-core"}})
    monkeypatch.setattr(runner_run, "HARNESS_PINS_PATH", path)
    seen: list[str] = []

    def fake_installed(package):
        seen.append(package)
        return "1.61.1"

    monkeypatch.setattr(runner_run, "installed_npm_version", fake_installed)
    assert runner_run.check_harness_pins() is True
    assert seen == ["playwright-core"]  # npm_package overrides the driver name


def test_check_harness_pins_missing_package(tmp_path, monkeypatch):
    path = write_pins(tmp_path, {"playwright-core": {"version": "1.61.1"}})
    monkeypatch.setattr(runner_run, "HARNESS_PINS_PATH", path)
    monkeypatch.setattr(runner_run, "installed_npm_version", lambda package: None)
    assert runner_run.check_harness_pins() is False


def test_check_harness_pins_version_mismatch(tmp_path, monkeypatch):
    path = write_pins(tmp_path, {"playwright-core": {"version": "1.61.1"}})
    monkeypatch.setattr(runner_run, "HARNESS_PINS_PATH", path)
    monkeypatch.setattr(runner_run, "installed_npm_version", lambda package: "9.9.9")
    assert runner_run.check_harness_pins() is False


def test_check_harness_pins_manifest_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(runner_run, "HARNESS_PINS_PATH", tmp_path / "absent.json")
    assert runner_run.check_harness_pins() is False


def test_check_harness_pins_manifest_invalid_json(tmp_path, monkeypatch):
    path = tmp_path / "harness_pins.json"
    path.write_text("not json", encoding="utf-8")
    monkeypatch.setattr(runner_run, "HARNESS_PINS_PATH", path)
    assert runner_run.check_harness_pins() is False


def test_installed_npm_version_reads_package_json(tmp_path, monkeypatch):
    monkeypatch.setattr(runner_run, "BENCH_ROOT", tmp_path)
    pkg_dir = tmp_path / "node_modules" / "leftpad"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "package.json").write_text(json.dumps({"version": "1.2.3"}), encoding="utf-8")
    assert runner_run.installed_npm_version("leftpad") == "1.2.3"
    assert runner_run.installed_npm_version("ghost") is None


def test_harness_pins_summary(tmp_path, monkeypatch):
    path = write_pins(tmp_path, {"playwright-core": {"version": "1.61.1"}})
    monkeypatch.setattr(runner_run, "HARNESS_PINS_PATH", path)
    monkeypatch.setattr(runner_run, "installed_npm_version", lambda package: "1.60.0")
    summary = runner_run.harness_pins_summary()
    assert summary["drivers"]["playwright-core"] == {"pinned": "1.61.1", "installed": "1.60.0"}


def test_harness_pins_summary_without_manifest(tmp_path, monkeypatch):
    monkeypatch.setattr(runner_run, "HARNESS_PINS_PATH", tmp_path / "absent.json")
    assert runner_run.harness_pins_summary() == {"manifest": None}


def test_check_harness_pins_pip_entry_dispatch(tmp_path, monkeypatch):
    # A driver entry with pip_package is verified against the runner's own
    # interpreter (importlib.metadata), not node_modules.
    path = write_pins(tmp_path, {"cdp-use": {"version": "1.4.5", "pip_package": "cdp-use"}})
    monkeypatch.setattr(runner_run, "HARNESS_PINS_PATH", path)
    monkeypatch.setattr(runner_run, "installed_pip_version", lambda package: "1.4.5")
    monkeypatch.setattr(
        runner_run, "installed_npm_version", lambda package: pytest.fail("pip entry must not consult node_modules")
    )
    assert runner_run.check_harness_pins() is True


def test_check_harness_pins_pip_entry_missing(tmp_path, monkeypatch):
    path = write_pins(tmp_path, {"cdp-use": {"version": "1.4.5", "pip_package": "cdp-use"}})
    monkeypatch.setattr(runner_run, "HARNESS_PINS_PATH", path)
    monkeypatch.setattr(runner_run, "installed_pip_version", lambda package: None)
    assert runner_run.check_harness_pins() is False


def test_installed_pip_version_real_lookup():
    # pytest itself is always importable in the test venv; a bogus name is not.
    assert runner_run.installed_pip_version("pytest")
    assert runner_run.installed_pip_version("definitely-not-a-real-package-xyz") is None


def test_installed_go_module_version_parses_go_mod(tmp_path, monkeypatch):
    monkeypatch.setattr(runner_run, "BENCH_ROOT", tmp_path)
    (tmp_path / "go.mod").write_text(
        "module example.com/adapter\n\ngo 1.26\n\nrequire (\n"
        "\tgithub.com/chromedp/cdproto v0.0.0-20260714\n"
        "\tgithub.com/chromedp/chromedp v0.16.0\n)\n",
        encoding="utf-8",
    )
    assert runner_run.installed_go_module_version("go.mod", "github.com/chromedp/chromedp") == "v0.16.0"
    assert runner_run.installed_go_module_version("go.mod", "github.com/absent/module") is None
    assert runner_run.installed_go_module_version("missing/go.mod", "x") is None


def test_check_harness_pins_go_entry_dispatch(tmp_path, monkeypatch):
    path = write_pins(
        tmp_path,
        {"chromedp": {"version": "v0.16.0", "go_module": "github.com/chromedp/chromedp", "go_mod_file": "go.mod"}},
    )
    monkeypatch.setattr(runner_run, "HARNESS_PINS_PATH", path)
    monkeypatch.setattr(runner_run, "installed_go_module_version", lambda f, m: "v0.16.0")
    assert runner_run.check_harness_pins() is True
    monkeypatch.setattr(runner_run, "installed_go_module_version", lambda f, m: "v0.15.0")
    assert runner_run.check_harness_pins() is False


# --- E. framework_probe.js protocol (node subprocess, no browser) ----------------

PROBE_ENV_KEYS = (
    "FRAMEWORK",
    "BROWSER_WS",
    "CDP_PORT",
    "EXPECT_PRODUCT",
    "EXPECT_UA",
    "EXPECT_PRODUCT_LIVE",
    "FW_STEPS",
    "FW_CHECKS",
    "FW_CONNECT_OPTIONS",
    "TASK_URL",
    "ARTIFACT_DIR",
)


def run_probe(tmp_path, extra_env):
    env = {key: value for key, value in os.environ.items() if key not in PROBE_ENV_KEYS}
    artifact_dir = tmp_path / "artifact"
    artifact_dir.mkdir(exist_ok=True)
    env["ARTIFACT_DIR"] = str(artifact_dir)
    env.update({key: str(value) for key, value in extra_env.items()})
    proc = subprocess.run(
        ["node", str(runner_run.FRAMEWORK_PROBE_SCRIPT)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        env=env,
    )
    assert proc.returncode == 0, f"probe exited {proc.returncode}: {proc.stderr}"
    # The runner contract: stdout is exactly one JSON object.
    return json.loads(proc.stdout)


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@probe_gate
def test_probe_missing_required_env(tmp_path):
    out = run_probe(tmp_path, {})
    assert out["ok"] is False
    assert out["error"]["class"] == "script_error"
    assert "FRAMEWORK, BROWSER_WS and TASK_URL are required" in out["error"]["message"]


@probe_gate
def test_probe_unknown_framework(tmp_path):
    out = run_probe(
        tmp_path,
        {
            "FRAMEWORK": "selenium",
            "BROWSER_WS": "ws://127.0.0.1:1/devtools/browser/x",
            "TASK_URL": "http://127.0.0.1:1/l1/core",
        },
    )
    assert out["ok"] is False
    assert out["error"]["class"] == "script_error"
    assert "unknown FRAMEWORK selenium" in out["error"]["message"]


@probe_gate
def test_probe_invalid_fw_steps_json(tmp_path):
    out = run_probe(
        tmp_path,
        {
            "FRAMEWORK": "playwright",
            "BROWSER_WS": "ws://127.0.0.1:1/devtools/browser/x",
            "TASK_URL": "http://127.0.0.1:1/l1/core",
            "FW_STEPS": "{not json",
        },
    )
    assert out["ok"] is False
    assert out["error"]["class"] == "script_error"
    assert "invalid JSON" in out["error"]["message"]


@probe_gate
def test_probe_refuses_to_run_unverified(tmp_path):
    # No CDP_PORT/EXPECT_PRODUCT -> the binding gate refuses outright.
    out = run_probe(
        tmp_path,
        {
            "FRAMEWORK": "playwright",
            "BROWSER_WS": "ws://127.0.0.1:1/devtools/browser/x",
            "TASK_URL": "http://127.0.0.1:1/l1/core",
            "FW_STEPS": "[]",
        },
    )
    assert out["ok"] is False
    assert out["error"]["class"] == "script_error"
    assert "refusing to run unverified" in out["error"]["message"]
    assert out["observations"]["binding"]["verified"] is False


@probe_gate
def test_probe_unreachable_cdp_port_fails_binding_gate(tmp_path):
    port = free_port()  # nobody is listening here
    out = run_probe(
        tmp_path,
        {
            "FRAMEWORK": "playwright",
            "BROWSER_WS": f"ws://127.0.0.1:{port}/devtools/browser/x",
            "TASK_URL": f"http://127.0.0.1:{port}/l1/core",
            "CDP_PORT": port,
            "EXPECT_PRODUCT": "Chrome/1.0",
            "FW_STEPS": "[]",
        },
    )
    assert out["ok"] is False
    assert out["error"]["class"] == "script_error"
    assert f"/json/version unreachable on port {port}" in out["error"]["message"]
    assert out["observations"]["binding"]["verified"] is False


# --- per-worker attempt context --------------------------------------------------


def test_note_task_reports_the_previous_task_per_engine():
    manager = runner_run.BrowserManager()
    assert manager.note_task("chrome", "task_a") is None
    assert manager.note_task("chrome", "task_b") == "task_a"
    # Engines reuse separate processes, so their task chains are independent.
    assert manager.note_task("moli", "task_c") is None
    assert manager.note_task("chrome", "task_d") == "task_b"
    assert manager.note_task("moli", "task_e") == "task_c"


def test_worker_slot_defaults_and_is_carried_by_the_manager():
    assert runner_run.BrowserManager().worker_slot == 0
    assert runner_run.BrowserManager(worker_slot=7).worker_slot == 7


# --- F. end-to-end against the pinned Chrome -------------------------------------


@pytest.fixture(scope="module")
def chrome_and_fixtures():
    manager = runner_run.BrowserManager(dynamic_ports=True)
    server = runner_run.FixtureServer()  # real fixtures dir: serves /l1/core
    try:
        server.start()
        browser = manager.launch("chrome")
        yield browser, server.base_url
    finally:
        manager.close_all()
        server.stop()


@engine_gate
@pytest.mark.parametrize("kind", sorted(runner_run.FRAMEWORK_DRIVER_KINDS))
def test_framework_driver_end_to_end_chrome(chrome_and_fixtures, tmp_path, kind):
    browser, base_url = chrome_and_fixtures
    task = make_resolved(
        task=make_framework_task_dict(
            kind,
            task_id=f"it_{kind}_eval_001",
            driver={
                "kind": kind,
                "steps": [
                    {"op": "new_page"},
                    {"op": "goto", "url": "{fixture_url}"},
                    {"op": "evaluate", "expression": "6 * 7", "save_as": "answer"},
                ],
                "checks": [{"kind": "saved_equals", "name": "answer", "expected": "42"}],
            },
        )
    )
    artifact_dir = tmp_path / kind
    artifact_dir.mkdir()
    out = runner_run.run_framework_driver(task, browser, artifact_dir, base_url, "pytest_fw", "chrome", 1, "s1")
    assert out["ok"] is True, out
    binding = out["observations"]["binding"]
    assert binding["verified"] is True
    assert binding["framework"] == runner_run.FRAMEWORK_DRIVER_KINDS[kind]
    assert out["answer"] == "42"
    assert out["observations"]["saved"]["answer"] == "42"
    assert out["grader"]["ok"] is True
    check_names = {check["name"]: check["status"] for check in out["grader"]["checks"]}
    assert check_names["framework_connect"] == "pass"
    assert check_names["saved_equals"] == "pass"
    # The probe traces its ops into cdp.jsonl inside the artifact dir.
    assert (artifact_dir / "cdp.jsonl").read_text(encoding="utf-8").strip()


# --- D. run_scenario_adapter_driver payload construction -------------------------


def capture_adapter_subprocess(captured):
    def fake(task, argv, env, artifact_dir, fixture_base_url, run_id, engine, attempt, seed, session, stdin_text=None):
        captured.update(argv=argv, session=session, payload=json.loads(stdin_text or "{}"))
        return {
            "ok": True,
            "answer": "stub",
            "observations": {},
            "grader": {"ok": True, "checks": [], "failure": None},
            "metrics": {},
        }

    return fake


def test_run_scenario_adapter_driver_payload(tmp_path, monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(runner_run, "run_driver_subprocess", capture_adapter_subprocess(captured))
    task = make_resolved(
        task=make_framework_task_dict(
            "thin_chrome_remote_interface",
            driver={
                "kind": "thin_chrome_remote_interface",
                "steps": [
                    {"op": "goto", "url": "{fixture_base_url}/l1/core"},
                    {"op": "fill", "selector": "#seed", "value": "{seed}"},
                ],
                "checks": [{"kind": "saved_equals", "name": "answer", "expected": "{seed}"}],
            },
        )
    )
    browser = framework_browser()
    base = "http://127.0.0.1:18080"
    out = runner_run.run_scenario_adapter_driver(task, browser, tmp_path, base, "run1", "moli", 2, "seed9")
    assert out["ok"] is True and out["answer"] == "stub"

    session = f"run1-{task.task_id}-moli-2-seed9"
    assert captured["session"] == session
    argv = captured["argv"]
    spec = runner_run.SCENARIO_ADAPTER_KINDS["thin_chrome_remote_interface"]
    assert argv[:-1] == spec["argv"]
    assert argv[-1].endswith(spec["script"])
    payload = captured["payload"]
    assert payload["protocol"] == "abb_scenario_adapter/1"
    assert payload["driver_key"] == "chrome_remote_interface"
    assert payload["browser_ws"] == "ws://127.0.0.1:9333/devtools/browser/abc"
    assert payload["cdp_port"] == 9333
    assert payload["expect_product"] == "Moli/0.1.0"
    assert payload["expect_ua"] == "MoliUA/1.0"
    assert payload["expect_product_live"] == "MoliLive/0.1"
    assert payload["engine"] == "moli"
    assert payload["transport_policy"] is None
    # {seed}/{fixture_base_url} templating happens runner-side, in the payload.
    assert payload["steps"][0]["url"] == f"{base}/l1/core"
    assert payload["steps"][1]["value"] == "seed9"
    assert payload["checks"][0]["expected"] == "seed9"
    assert payload["task_url"] == f"{base}/l1/core?seed=seed9&session={session}"
    assert payload["artifact_dir"] == str(tmp_path)
    assert "binding" not in payload


def selenium_task():
    return make_resolved(
        task=make_framework_task_dict(
            "webdriver_selenium",
            driver={
                "kind": "webdriver_selenium",
                "transport_policy": "engine_native",
                "steps": [{"op": "title", "save_as": "answer"}],
                "checks": [
                    {"kind": "saved_truthy", "name": "answer"},
                ],
            },
        )
    )


def test_resolve_selenium_binding_normalizes_frozen_catalog_api():
    resolved = runner_run.resolve_selenium_runtime_bindings(
        [selenium_task()],
        ["moli"],
    )

    binding = resolved["moli"]
    assert binding["binding_id"] == "moli__selenium"
    assert binding["browser_id"] == "moli"
    assert binding["driver_id"] == "selenium"
    assert binding["route"]["route_id"] == "native_webdriver"
    assert binding["route"]["client_protocol"] == "webdriver_classic"
    assert binding["route"]["ordered_hops"][-1]["to"] == "browser"
    assert binding["route"]["lifecycle"] == {
        "browser_owner": "runner_browser_manager",
        "bridge_owner": "none",
        "adapter_owner": "runner_per_attempt_subprocess",
    }
    assert binding["route"]["identity"]["http_assertions"]
    assert binding["route"]["identity"]["live_transport_assertions"]
    assert binding["pins"]["driver"]["metadata"]["pip_package"] == "selenium"
    assert binding["pins"]["bridges"] == []
    assert binding["fallback_allowed"] is False


def test_run_scenario_adapter_driver_includes_only_preresolved_selenium_binding(
    tmp_path,
    monkeypatch,
):
    captured: dict = {}
    monkeypatch.setattr(
        runner_run,
        "run_driver_subprocess",
        capture_adapter_subprocess(captured),
    )
    task = selenium_task()
    binding = runner_run.resolve_selenium_runtime_bindings(
        [task],
        ["moli"],
    )["moli"]
    monkeypatch.setattr(
        runner_run,
        "browser_cdp_product",
        lambda browser: pytest.fail(
            "native WebDriver route must not call the CDP-only identity API"
        ),
    )

    runner_run.run_scenario_adapter_driver(
        task,
        framework_browser(),
        tmp_path,
        "http://127.0.0.1:18080",
        "run1",
        "moli",
        1,
        "seed1",
        runtime_binding=binding,
    )

    payload = captured["payload"]
    assert payload["binding"] == binding
    assert payload["binding"]["binding_id"] == "moli__selenium"
    assert payload["transport_policy"] == "engine_native"
    assert payload["expect_product_live"] == "Moli/0.1.0"


def test_run_scenario_adapter_driver_rejects_missing_selenium_binding_before_subprocess(
    tmp_path,
    monkeypatch,
):
    captured: dict = {}
    monkeypatch.setattr(
        runner_run,
        "run_driver_subprocess",
        capture_adapter_subprocess(captured),
    )
    monkeypatch.setattr(
        runner_run,
        "browser_cdp_product",
        lambda browser: pytest.fail(
            "missing binding must fail before the CDP-only identity API"
        ),
    )
    with pytest.raises(
        runner_run.BenchError,
        match=r"missing pre-resolved Selenium binding for \(moli, selenium\)",
    ):
        runner_run.run_scenario_adapter_driver(
            selenium_task(),
            framework_browser(),
            tmp_path,
            "http://127.0.0.1:18080",
            "run1",
            "moli",
            1,
            "seed1",
        )
    assert "payload" not in captured


def test_run_scenario_adapter_driver_rejects_unknown_binding_route_before_cdp(
    tmp_path,
    monkeypatch,
):
    task = selenium_task()
    binding = runner_run.resolve_selenium_runtime_bindings(
        [task],
        ["moli"],
    )["moli"]
    binding["route"]["route_id"] = "unknown_route"
    monkeypatch.setattr(
        runner_run,
        "browser_cdp_product",
        lambda browser: pytest.fail("unknown route must fail before CDP"),
    )
    monkeypatch.setattr(
        runner_run,
        "run_driver_subprocess",
        lambda *args, **kwargs: pytest.fail("unknown route must fail before subprocess"),
    )

    with pytest.raises(
        runner_run.BenchError,
        match="unknown pre-resolved Selenium route `unknown_route`",
    ):
        runner_run.run_scenario_adapter_driver(
            task,
            framework_browser(),
            tmp_path,
            "http://127.0.0.1:18080",
            "run1",
            "moli",
            1,
            "seed1",
            runtime_binding=binding,
        )


def test_native_selenium_product_fallback_avoids_cdp_identity(
    tmp_path,
    monkeypatch,
):
    captured: dict = {}
    monkeypatch.setattr(
        runner_run,
        "run_driver_subprocess",
        capture_adapter_subprocess(captured),
    )
    monkeypatch.setattr(
        runner_run,
        "browser_cdp_product",
        lambda browser: pytest.fail("native route must not query CDP identity"),
    )
    task = selenium_task()
    binding = runner_run.resolve_selenium_runtime_bindings(
        [task],
        ["moli"],
    )["moli"]
    browser = framework_browser(
        version_info={
            "Product": "MoliProduct/1.0",
            "User-Agent": "MoliUA/1.0",
            "webSocketDebuggerUrl": "ws://127.0.0.1:9333/devtools/browser/abc",
        },
    )

    runner_run.run_scenario_adapter_driver(
        task,
        browser,
        tmp_path,
        "http://127.0.0.1:18080",
        "run1",
        "moli",
        1,
        "seed1",
        runtime_binding=binding,
    )

    assert captured["payload"]["expect_product"] == "MoliProduct/1.0"
    assert captured["payload"]["expect_product_live"] == "MoliProduct/1.0"


def test_bridge_selenium_live_product_falls_back_to_http_identity(
    tmp_path,
    monkeypatch,
):
    captured: dict = {}
    monkeypatch.setattr(
        runner_run,
        "run_driver_subprocess",
        capture_adapter_subprocess(captured),
    )
    monkeypatch.setattr(runner_run, "browser_cdp_product", lambda browser: "")
    browser = framework_browser(
        version_info={
            "Browser": "Chrome/150.0.0.0",
            "User-Agent": "ChromeUA/150",
            "webSocketDebuggerUrl": "ws://127.0.0.1:9333/devtools/browser/abc",
        },
    )
    binding = {
        "binding_id": "chrome__selenium",
        "route": {"route_id": "chromedriver_cdp"},
    }

    runner_run.run_scenario_adapter_driver(
        selenium_task(),
        browser,
        tmp_path,
        "http://127.0.0.1:18080",
        "run1",
        "chrome",
        1,
        "seed1",
        runtime_binding=binding,
    )

    assert captured["payload"]["expect_product"] == "Chrome/150.0.0.0"
    assert captured["payload"]["expect_product_live"] == "Chrome/150.0.0.0"


def test_non_selenium_product_only_keeps_legacy_browser_identity(
    tmp_path,
    monkeypatch,
):
    captured: dict = {}
    monkeypatch.setattr(
        runner_run,
        "run_driver_subprocess",
        capture_adapter_subprocess(captured),
    )
    monkeypatch.setattr(
        runner_run,
        "browser_cdp_product",
        lambda browser: "MoliLive/1.0",
    )
    task = make_resolved(
        task=make_framework_task_dict(
            "thin_chrome_remote_interface",
            driver={
                "kind": "thin_chrome_remote_interface",
                "steps": [{"op": "title", "save_as": "answer"}],
                "checks": [{"kind": "saved_truthy", "name": "answer"}],
            },
        )
    )
    browser = framework_browser(
        version_info={
            "Product": "MoliProduct/1.0",
            "User-Agent": "MoliUA/1.0",
            "webSocketDebuggerUrl": "ws://127.0.0.1:9333/devtools/browser/abc",
        },
    )

    runner_run.run_scenario_adapter_driver(
        task,
        browser,
        tmp_path,
        "http://127.0.0.1:18080",
        "run1",
        "moli",
        1,
        "seed1",
    )

    assert captured["payload"]["expect_product"] == ""
    assert captured["payload"]["expect_product_live"] == "MoliLive/1.0"
    assert "binding" not in captured["payload"]


def test_non_selenium_resolution_does_not_load_catalog(monkeypatch):
    task = make_resolved(task=make_framework_task_dict("framework_playwright"))
    monkeypatch.setattr(
        runner_run.binding_catalog,
        "load_catalog",
        lambda: pytest.fail("non-Selenium runs must not load the binding catalog"),
    )
    assert runner_run.resolve_selenium_runtime_bindings([task], ["chrome"]) == {}


def test_selenium_resolution_rejects_non_object_harness_pins(
    tmp_path,
    monkeypatch,
):
    pins_path = tmp_path / "harness_pins.json"
    pins_path.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(runner_run, "HARNESS_PINS_PATH", pins_path)

    with pytest.raises(
        runner_run.BenchError,
        match="harness_pins.json root must be an object",
    ):
        runner_run.resolve_selenium_runtime_bindings(
            [selenium_task()],
            ["moli"],
        )


@pytest.mark.parametrize(
    ("installed_version", "message"),
    [
        (None, "pinned Selenium client is not installed"),
        (
            "4.45.0",
            "installed Selenium client version does not match harness pin",
        ),
    ],
)
def test_selenium_client_pin_preflight_fails_before_run_or_browser_side_effects(
    monkeypatch,
    installed_version,
    message,
):
    monkeypatch.setattr(
        runner_run,
        "installed_pip_version",
        lambda package: installed_version,
    )
    monkeypatch.setattr(
        runner_run,
        "reserve_run_dir",
        lambda *args, **kwargs: pytest.fail(
            "client preflight must fail before reserving a run directory"
        ),
    )
    monkeypatch.setattr(
        runner_run,
        "FixtureServer",
        lambda *args, **kwargs: pytest.fail(
            "client preflight must fail before starting fixtures"
        ),
    )
    monkeypatch.setattr(
        runner_run,
        "BrowserManager",
        lambda *args, **kwargs: pytest.fail(
            "client preflight must fail before starting a browser"
        ),
    )
    args = argparse.Namespace(
        engines="moli",
        jobs=1,
        score_mode="independent",
        chrome_gate="off",
    )

    with pytest.raises(runner_run.BenchError, match=message):
        runner_run.run_attempts(args, {}, [selenium_task()])


def test_missing_chromedriver_fails_before_run_or_browser_side_effects(
    tmp_path,
    monkeypatch,
):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    monkeypatch.setattr(runner_run, "BENCH_ROOT", repo_root)
    pins_path = write_pins(
        tmp_path,
        {
            "selenium": {
                "version": "4.46.0",
                "pip_package": "selenium",
            },
            "chromedriver": {
                "version": "150.0.0.0",
                "binary_path": "build_artifacts/chromedriver/bin/chromedriver",
            },
        },
    )
    monkeypatch.setattr(runner_run, "HARNESS_PINS_PATH", pins_path)
    monkeypatch.setattr(
        runner_run,
        "reserve_run_dir",
        lambda *args, **kwargs: pytest.fail("must fail before reserving a run directory"),
    )
    monkeypatch.setattr(
        runner_run,
        "FixtureServer",
        lambda *args, **kwargs: pytest.fail("must fail before starting fixtures"),
    )
    monkeypatch.setattr(
        runner_run,
        "BrowserManager",
        lambda *args, **kwargs: pytest.fail("must fail before starting a browser"),
    )
    args = argparse.Namespace(
        engines="chrome",
        jobs=1,
        score_mode="independent",
        chrome_gate="off",
    )

    with pytest.raises(runner_run.BenchError, match="executable is missing"):
        runner_run.run_attempts(args, {}, [selenium_task()])


def test_missing_catalog_binding_fails_before_run_or_browser_side_effects(
    monkeypatch,
):
    monkeypatch.setattr(
        runner_run.binding_catalog,
        "load_catalog",
        lambda: (_ for _ in ()).throw(
            runner_run.binding_catalog.CatalogError("missing Selenium pair")
        ),
    )
    monkeypatch.setattr(
        runner_run,
        "reserve_run_dir",
        lambda *args, **kwargs: pytest.fail("must fail before reserving a run directory"),
    )
    monkeypatch.setattr(
        runner_run,
        "FixtureServer",
        lambda *args, **kwargs: pytest.fail("must fail before starting fixtures"),
    )
    monkeypatch.setattr(
        runner_run,
        "BrowserManager",
        lambda *args, **kwargs: pytest.fail("must fail before starting a browser"),
    )
    args = argparse.Namespace(
        engines="moli",
        jobs=1,
        score_mode="independent",
        chrome_gate="off",
    )

    with pytest.raises(
        runner_run.BenchError,
        match="selenium binding configuration error: missing Selenium pair",
    ):
        runner_run.run_attempts(args, {}, [selenium_task()])


@pytest.mark.parametrize(
    "mode",
    ["not_file", "not_executable", "symlink_escape", "digest_mismatch"],
)
def test_chromedriver_pin_must_resolve_to_safe_executable(
    tmp_path,
    monkeypatch,
    mode,
):
    root = tmp_path / "repo"
    root.mkdir()
    relative = pathlib.Path("build_artifacts/chromedriver/bin/chromedriver")
    candidate = root / relative
    candidate.parent.mkdir(parents=True)
    if mode == "not_file":
        candidate.mkdir()
        expected = "not an executable file"
    elif mode == "not_executable":
        candidate.write_text("driver", encoding="utf-8")
        candidate.chmod(0o644)
        expected = "not an executable file"
    elif mode == "symlink_escape":
        outside = tmp_path / "outside-driver"
        outside.write_text("#!/bin/sh\n", encoding="utf-8")
        outside.chmod(0o755)
        candidate.symlink_to(outside)
        expected = "escapes the repository"
    else:
        candidate.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        candidate.chmod(0o755)
        expected = "sha256 mismatch"
    monkeypatch.setattr(runner_run, "BENCH_ROOT", root)
    binding = runner_run.binding_catalog.load_catalog().require_binding(
        "chrome",
        "selenium",
    )
    pins = {
        "selenium": {
            "version": "4.46.0",
            "pip_package": "selenium",
        },
        "chromedriver": {
            "version": "150.0.0.0",
            "binary_path": relative.as_posix(),
            "sha256_12": "000000000000",
        },
    }

    with pytest.raises(runner_run.BenchError, match=expected):
        runner_run.normalize_selenium_binding(binding, pins)


@pytest.mark.parametrize(
    ("pin_name", "field", "value", "message"),
    [
        ("selenium", "version", "", "non-empty version"),
        ("selenium", "pip_package", "other", "pip_package `selenium`"),
        ("chromedriver", "version", "", "non-empty version"),
        ("chromedriver", "sha256_12", "not-a-digest", "sha256_12"),
    ],
)
def test_selenium_runtime_pin_metadata_fails_closed_in_preflight(
    tmp_path,
    monkeypatch,
    pin_name,
    field,
    value,
    message,
):
    root = tmp_path / "repo"
    executable = root / "build_artifacts/chromedriver/bin/chromedriver"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    monkeypatch.setattr(runner_run, "BENCH_ROOT", root)
    binding = runner_run.binding_catalog.load_catalog().require_binding(
        "chrome",
        "selenium",
    )
    pins = {
        "selenium": {
            "version": "4.46.0",
            "pip_package": "selenium",
        },
        "chromedriver": {
            "version": "150.0.0.0",
            "binary_path": "build_artifacts/chromedriver/bin/chromedriver",
            "sha256_12": runner_run.sha256_file(executable)[:12],
        },
    }
    pins[pin_name][field] = value

    with pytest.raises(runner_run.BenchError, match=message):
        runner_run.normalize_selenium_binding(binding, pins)


def test_required_gate_threads_preresolved_selenium_binding(
    tmp_path,
    monkeypatch,
):
    captured: dict = {}

    def fake_run_driver_attempt(*args, **kwargs):
        captured["scenario_binding"] = kwargs.get("scenario_binding")
        return {"status": "pass"}

    monkeypatch.setattr(runner_run, "run_driver_attempt", fake_run_driver_attempt)
    binding = {"binding_id": "chrome__selenium"}
    gate, row = runner_run.run_required_gate(
        tmp_path,
        tmp_path / "results.jsonl",
        "run1",
        selenium_task(),
        1,
        "seed1",
        {"chrome": object()},
        False,
        "http://127.0.0.1:18080",
        scenario_binding=binding,
    )

    assert row["status"] == "pass"
    assert gate["status"] == "pass"
    assert captured["scenario_binding"] is binding


def test_catalog_unavailable_attempt_short_circuits_and_persists_binding(
    tmp_path,
    monkeypatch,
):
    task = make_resolved(
        task=make_framework_task_dict("webdriver_selenium"),
        subset_gate="off",
    )
    binding = runner_run.binding_catalog.load_catalog().require_binding(
        "obscura",
        "selenium",
    )
    unavailable = runner_run.unavailable_binding_payload(binding)
    monkeypatch.setattr(
        runner_run,
        "run_scenario_adapter_driver",
        lambda *args, **kwargs: pytest.fail(
            "catalog-unavailable binding must not launch the driver adapter"
        ),
    )
    browser = framework_browser()
    browser.engine = "obscura"

    result = runner_run.run_driver_attempt(
        tmp_path / "run",
        tmp_path / "run" / "results.jsonl",
        "unavailable-binding",
        task,
        "obscura",
        1,
        "seed",
        browser,
        {"required": False, "status": "off", "chrome_attempt_ref": None},
        score_eligible=True,
        fixture_base_url=None,
        scenario_binding=unavailable,
    )

    assert result["status"] == "unsupported"
    assert result["failure"]["class"] == "engine_unsupported"
    assert result["failure"]["origin"] == "binding_catalog"
    assert result["binding"]["binding_id"] == "obscura__selenium"
    assert result["binding"]["fallback_allowed"] is False
    assert result["cdp_call_count"] == 0
    assert result["score_included"] is True
    artifact_dir = tmp_path / "run" / result["artifact_dir"]
    stdout = json.loads((artifact_dir / "stdout.log").read_text())
    assert stdout["observations"]["binding"] == result["binding"]


def test_run_scenario_adapter_driver_requires_browser_ws(tmp_path, monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(runner_run, "run_driver_subprocess", capture_adapter_subprocess(captured))
    task = make_resolved(task=make_framework_task_dict("thin_chrome_remote_interface"))
    browser = framework_browser(version_info={"Browser": "Moli/0.1.0"}, cdp_product="")
    with pytest.raises(runner_run.BenchError, match="no browser websocket"):
        runner_run.run_scenario_adapter_driver(task, browser, tmp_path, "http://127.0.0.1:18080", "run1", "moli", 1, "s1")
    assert "payload" not in captured


def wait_for_pid_absent(pid: int, timeout_s: float = 3.0):
    stat_path = pathlib.Path(f"/proc/{pid}/stat")
    deadline = time.monotonic() + timeout_s
    while stat_path.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not stat_path.exists(), f"process {pid} remains, including as a zombie"


def wait_for_port_state(port: int, *, open_: bool, timeout_s: float = 3.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.05):
                actual = True
        except OSError:
            actual = False
        if actual is open_:
            return
        time.sleep(0.02)
    assert actual is open_, f"port {port} open={actual}, expected {open_}"


def force_cleanup_selenium_test_processes(state_path: pathlib.Path):
    """Test-only finalizer which never substitutes for cleanup assertions."""
    if not state_path.exists():
        return
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    group_id = int(state["adapter_pid"])
    for key in ("adapter_pid", "bridge_pid", "descendant_pid"):
        raw_pid = state.get(key)
        if raw_pid is None:
            continue
        pid = int(raw_pid)
        try:
            if os.getsid(pid) == group_id:
                os.killpg(group_id, signal.SIGKILL)
                break
        except OSError:
            continue
    for key in ("adapter_pid", "bridge_pid", "descendant_pid"):
        raw_pid = state.get(key)
        if raw_pid is None:
            continue
        try:
            os.kill(int(raw_pid), signal.SIGKILL)
        except OSError:
            pass


def leaking_selenium_adapter(
    root: pathlib.Path,
    *,
    mode: str,
    ignore_sigterm: bool = False,
) -> tuple[pathlib.Path, pathlib.Path, int, list[str]]:
    bridge_script = root / "fake_bridge.py"
    adapter_script = root / "abrupt_adapter.py"
    state_path = root / "bridge_state.json"
    bridge_port = free_port()
    bridge_script.write_text(
        "import json, os, pathlib, signal, socket, subprocess, sys, time\n"
        "port = int(sys.argv[1])\n"
        "state_path = pathlib.Path(sys.argv[2])\n"
        "stubborn = sys.argv[3] == '1'\n"
        "child_ready = state_path.with_suffix('.child')\n"
        "if stubborn:\n"
        "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "listener = socket.socket()\n"
        "listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
        "listener.bind(('127.0.0.1', port))\n"
        "listener.listen()\n"
        "listener.settimeout(0.1)\n"
        "child = None\n"
        "if stubborn:\n"
        "    child = subprocess.Popen([\n"
        "        sys.executable,\n"
        "        '-c',\n"
        "        'import os,pathlib,signal,sys,time; '\n"
        "        'signal.signal(signal.SIGTERM, signal.SIG_IGN); '\n"
        "        'pathlib.Path(sys.argv[1]).write_text(str(os.getpid()), encoding=\"utf-8\"); '\n"
        "        'time.sleep(60)',\n"
        "        str(child_ready),\n"
        "    ], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        "    deadline = time.monotonic() + 2\n"
        "    while not child_ready.exists() and time.monotonic() < deadline:\n"
        "        time.sleep(0.01)\n"
        "    if not child_ready.exists():\n"
        "        raise RuntimeError('stubborn descendant did not start')\n"
        "state_path.write_text(json.dumps({\n"
        "    'adapter_pid': os.getppid(),\n"
        "    'bridge_pid': os.getpid(),\n"
        "    'descendant_pid': child.pid if child is not None else None,\n"
        "    'port': port,\n"
        "}), encoding='utf-8')\n"
        "while True:\n"
        "    try:\n"
        "        connection, _ = listener.accept()\n"
        "        connection.close()\n"
        "    except socket.timeout:\n"
        "        pass\n",
        encoding="utf-8",
    )
    adapter_script.write_text(
        "import json, os, pathlib, signal, subprocess, sys, time\n"
        "bridge = subprocess.Popen([\n"
        "    sys.executable, sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[5]\n"
        "], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        "state_path = pathlib.Path(sys.argv[3])\n"
        "deadline = time.monotonic() + 2\n"
        "while not state_path.exists() and time.monotonic() < deadline:\n"
        "    time.sleep(0.01)\n"
        "if not state_path.exists():\n"
        "    raise RuntimeError('fake bridge did not start')\n"
        "mode = sys.argv[4]\n"
        "stubborn = sys.argv[5] == '1'\n"
        "if stubborn:\n"
        "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "valid = json.dumps({\n"
        "    'ok': True,\n"
        "    'answer': 'cleanup-ok',\n"
        "    'observations': {'checks': [{'name': 'cleanup', 'status': 'pass'}]},\n"
        "    'metrics': {},\n"
        "})\n"
        "if mode == 'success':\n"
        "    print(valid, flush=True)\n"
        "elif mode == 'nonzero':\n"
        "    print(valid, flush=True)\n"
        "    raise SystemExit(7)\n"
        "elif mode == 'missing':\n"
        "    pass\n"
        "elif mode == 'malformed':\n"
        "    print('not json', flush=True)\n"
        "elif mode == 'malformed_nonzero':\n"
        "    print('not json', flush=True)\n"
        "    raise SystemExit(7)\n"
        "elif mode == 'scalar':\n"
        "    print('[]', flush=True)\n"
        "elif mode == 'crash':\n"
        "    print('crash stdout marker', flush=True)\n"
        "    print('crash stderr marker', file=sys.stderr, flush=True)\n"
        "    raise RuntimeError('adapter crash marker')\n"
        "elif mode == 'abrupt':\n"
        "    os.kill(os.getpid(), signal.SIGKILL)\n"
        "elif mode == 'hang':\n"
        "    time.sleep(60)\n"
        "else:\n"
        "    raise RuntimeError(f'unknown mode {mode}')\n",
        encoding="utf-8",
    )
    argv = [
        sys.executable,
        str(adapter_script),
        str(bridge_script),
        str(bridge_port),
        str(state_path),
        mode,
        "1" if ignore_sigterm else "0",
    ]
    return adapter_script, state_path, bridge_port, argv


def run_leaking_selenium_adapter(
    tmp_path: pathlib.Path,
    *,
    mode: str,
    ignore_sigterm: bool = False,
):
    _, state_path, bridge_port, argv = leaking_selenium_adapter(
        tmp_path,
        mode=mode,
        ignore_sigterm=ignore_sigterm,
    )
    artifact_dir = tmp_path / "artifact"
    artifact_dir.mkdir()
    task = make_resolved(
        task=make_framework_task_dict(
            "webdriver_selenium",
            timeouts={"task_ms": 5000, "hard_kill_ms": 7000},
        )
    )
    try:
        result = runner_run.run_driver_subprocess(
            task,
            argv,
            dict(os.environ),
            artifact_dir,
            "http://127.0.0.1:1",
            "run1",
            "moli",
            1,
            "seed1",
            "session1",
        )
    except BaseException:
        force_cleanup_selenium_test_processes(state_path)
        raise
    state = json.loads(state_path.read_text(encoding="utf-8"))
    try:
        wait_for_pid_absent(state["adapter_pid"])
        wait_for_pid_absent(state["bridge_pid"])
        if state["descendant_pid"] is not None:
            wait_for_pid_absent(state["descendant_pid"])
        wait_for_port_state(bridge_port, open_=False)
    finally:
        force_cleanup_selenium_test_processes(state_path)
    return result, artifact_dir, state


@pytest.mark.parametrize(
    ("mode", "expected_detail", "expected_stdout"),
    [
        pytest.param("success", None, '"cleanup-ok"', id="valid-success"),
        pytest.param("nonzero", "exited with code 7", '"cleanup-ok"', id="nonzero"),
        pytest.param("missing", "script reported failure", "", id="missing-output"),
        pytest.param("malformed", "not a single JSON object", "not json", id="malformed-output"),
        pytest.param("malformed_nonzero", "exited with code 7", "not json", id="malformed-nonzero"),
        pytest.param("scalar", "stdout JSON must be an object", "[]", id="non-object-output"),
        pytest.param("crash", "exited with code 1", "crash stdout marker", id="unhandled-crash"),
        pytest.param("abrupt", "exited with code -9", "", id="abrupt-exit"),
    ],
)
def test_selenium_ordinary_terminal_paths_reap_bridge_group(
    tmp_path,
    mode,
    expected_detail,
    expected_stdout,
):
    result, artifact_dir, _ = run_leaking_selenium_adapter(
        tmp_path,
        mode=mode,
    )

    if expected_detail is None:
        assert result["ok"] is True
    else:
        assert expected_detail in result["failure"]["detail"]
    stdout = (artifact_dir / "stdout.log").read_text(encoding="utf-8")
    assert expected_stdout in stdout


def test_selenium_unhandled_crash_preserves_stderr_traceback(tmp_path):
    result, artifact_dir, _ = run_leaking_selenium_adapter(
        tmp_path,
        mode="crash",
    )

    assert "exited with code 1" in result["failure"]["detail"]
    assert "crash stdout marker" in (
        artifact_dir / "stdout.log"
    ).read_text(encoding="utf-8")
    stderr = (artifact_dir / "stderr.log").read_text(encoding="utf-8")
    assert "crash stderr marker" in stderr
    assert "RuntimeError: adapter crash marker" in stderr


def test_selenium_hard_kill_deadline_bounds_stubborn_group_cleanup(tmp_path):
    _, state_path, bridge_port, argv = leaking_selenium_adapter(
        tmp_path,
        mode="hang",
        ignore_sigterm=True,
    )
    artifact_dir = tmp_path / "artifact"
    artifact_dir.mkdir()
    task = make_resolved(
        task=make_framework_task_dict(
            "webdriver_selenium",
            timeouts={"task_ms": 500, "hard_kill_ms": 1200},
        )
    )
    started = time.monotonic()
    try:
        with pytest.raises(subprocess.TimeoutExpired) as raised:
            runner_run.run_driver_subprocess(
                task,
                argv,
                dict(os.environ),
                artifact_dir,
                "http://127.0.0.1:1",
                "run1",
                "moli",
                1,
                "seed1",
                "session1",
            )
        elapsed = time.monotonic() - started
        assert elapsed <= 1.5
        assert raised.value.timeout == pytest.approx(0.5)

        state = json.loads(state_path.read_text(encoding="utf-8"))
        wait_for_pid_absent(state["adapter_pid"])
        wait_for_pid_absent(state["bridge_pid"])
        wait_for_pid_absent(state["descendant_pid"])
        wait_for_port_state(bridge_port, open_=False)
        assert (artifact_dir / "stdout.log").read_text(encoding="utf-8") == ""
        assert (artifact_dir / "stderr.log").read_text(encoding="utf-8") == ""
    finally:
        force_cleanup_selenium_test_processes(state_path)


def test_selenium_identity_capture_failure_aborts_and_cleans_within_hard_deadline(
    tmp_path,
    monkeypatch,
):
    _, state_path, bridge_port, argv = leaking_selenium_adapter(
        tmp_path,
        mode="hang",
        ignore_sigterm=True,
    )
    artifact_dir = tmp_path / "artifact"
    artifact_dir.mkdir()
    task = make_resolved(
        task=make_framework_task_dict(
            "webdriver_selenium",
            timeouts={"task_ms": 5000, "hard_kill_ms": 1200},
        )
    )

    def lose_identity(proc):
        state_deadline = time.monotonic() + 0.8
        while not state_path.exists() and time.monotonic() < state_deadline:
            time.sleep(0.01)
        assert state_path.exists(), "fake bridge did not start"
        return None

    monkeypatch.setattr(
        runner_run,
        "_capture_driver_process_identity",
        lose_identity,
    )
    started = time.monotonic()
    try:
        result = runner_run.run_driver_subprocess(
            task,
            argv,
            dict(os.environ),
            artifact_dir,
            "http://127.0.0.1:1",
            "run1",
            "moli",
            1,
            "seed1",
            "session1",
        )

        assert time.monotonic() - started <= 1.5
        assert result["ok"] is False
        assert "identity could not be captured" in result["failure"]["detail"]
        state = json.loads(state_path.read_text(encoding="utf-8"))
        wait_for_pid_absent(state["adapter_pid"])
        wait_for_pid_absent(state["bridge_pid"])
        wait_for_pid_absent(state["descendant_pid"])
        wait_for_port_state(bridge_port, open_=False)
    finally:
        force_cleanup_selenium_test_processes(state_path)


def test_selenium_session_signal_has_no_numeric_pid_fallback(
    monkeypatch,
):
    identity = runner_run._DriverProcessIdentity(
        leader_pid=100,
        pgid=100,
        session=100,
        leader_start_ticks=1,
    )
    moved_member = runner_run._DriverProcStat(
        pid=200,
        state="S",
        pgrp=200,
        session=100,
        start_ticks=2,
    )
    numeric_signals: list[tuple[int, int]] = []

    monkeypatch.setattr(
        runner_run,
        "_driver_session_members",
        lambda current: (moved_member,),
    )

    def no_pidfd(pid):
        raise OSError("pidfd unavailable")

    monkeypatch.setattr(runner_run.os, "pidfd_open", no_pidfd)
    monkeypatch.setattr(
        runner_run.os,
        "kill",
        lambda pid, sig: numeric_signals.append((pid, sig)),
    )
    monkeypatch.setattr(
        runner_run.os,
        "killpg",
        lambda pgid, sig: numeric_signals.append((pgid, sig)),
    )

    assert runner_run._signal_driver_session(
        identity,
        signal.SIGKILL,
    )
    assert numeric_signals == []


def test_selenium_nonzero_returncode_survives_cleanup_diagnostic(
    tmp_path,
    monkeypatch,
):
    real_cleanup = runner_run._cleanup_selenium_process_group
    cleanup_calls = 0

    def fail_first_cleanup(proc, identity, hard_deadline, grace_s=2.0):
        nonlocal cleanup_calls
        cleanup_calls += 1
        if cleanup_calls == 1:
            raise RuntimeError("cleanup diagnostic")
        return real_cleanup(proc, identity, hard_deadline, grace_s)

    monkeypatch.setattr(
        runner_run,
        "_cleanup_selenium_process_group",
        fail_first_cleanup,
    )
    result, _, _ = run_leaking_selenium_adapter(
        tmp_path,
        mode="nonzero",
    )

    assert cleanup_calls == 2
    assert "exited with code 7" in result["failure"]["detail"]
    assert result["observations"]["selenium_cleanup"] == {
        "confirmed": True,
        "diagnostic": "cleanup diagnostic",
    }


def test_selenium_malformed_stdout_survives_cleanup_diagnostic(
    tmp_path,
    monkeypatch,
):
    real_cleanup = runner_run._cleanup_selenium_process_group
    cleanup_calls = 0

    def fail_first_cleanup(proc, identity, hard_deadline, grace_s=2.0):
        nonlocal cleanup_calls
        cleanup_calls += 1
        if cleanup_calls == 1:
            raise RuntimeError("cleanup diagnostic")
        return real_cleanup(proc, identity, hard_deadline, grace_s)

    monkeypatch.setattr(
        runner_run,
        "_cleanup_selenium_process_group",
        fail_first_cleanup,
    )
    result, _, _ = run_leaking_selenium_adapter(
        tmp_path,
        mode="malformed",
    )

    assert cleanup_calls == 2
    assert "not a single JSON object" in result["failure"]["detail"]
    assert result["observations"]["selenium_cleanup"] == {
        "confirmed": True,
        "diagnostic": "cleanup diagnostic",
    }


def test_selenium_cleanup_is_not_retried_after_hard_deadline(
    tmp_path,
    monkeypatch,
):
    real_cleanup = runner_run._cleanup_selenium_process_group
    cleanup_calls = 0

    def expire_after_cleanup(proc, identity, hard_deadline, grace_s=2.0):
        nonlocal cleanup_calls
        cleanup_calls += 1
        real_cleanup(proc, identity, hard_deadline, grace_s)
        monkeypatch.setattr(
            runner_run.time,
            "monotonic",
            lambda: hard_deadline,
        )
        raise RuntimeError("cleanup deadline exhausted")

    monkeypatch.setattr(
        runner_run,
        "_cleanup_selenium_process_group",
        expire_after_cleanup,
    )
    result, _, _ = run_leaking_selenium_adapter(
        tmp_path,
        mode="nonzero",
    )

    assert cleanup_calls == 1
    assert "exited with code 7" in result["failure"]["detail"]
    assert result["observations"]["selenium_cleanup"] == {
        "confirmed": False,
        "diagnostic": "cleanup deadline exhausted",
    }


def test_selenium_unconfirmed_cleanup_is_not_reported_as_code_none(
    tmp_path,
    monkeypatch,
):
    real_cleanup = runner_run._cleanup_selenium_process_group

    def report_unconfirmed(proc, identity, hard_deadline, grace_s=2.0):
        real_cleanup(proc, identity, hard_deadline, grace_s)
        return False

    monkeypatch.setattr(
        runner_run,
        "_cleanup_selenium_process_group",
        report_unconfirmed,
    )
    result, _, _ = run_leaking_selenium_adapter(
        tmp_path,
        mode="success",
    )

    assert result["ok"] is False
    assert "cleanup was not confirmed" in result["failure"]["detail"]
    assert "code None" not in result["failure"]["detail"]


def test_selenium_cleanup_interrupt_does_not_replace_original_exception(
    tmp_path,
    monkeypatch,
):
    _, state_path, bridge_port, argv = leaking_selenium_adapter(
        tmp_path,
        mode="hang",
    )
    artifact_dir = tmp_path / "artifact"
    artifact_dir.mkdir()
    task = make_resolved(
        task=make_framework_task_dict(
            "webdriver_selenium",
            timeouts={"task_ms": 5000, "hard_kill_ms": 7000},
        )
    )
    original = RuntimeError("primary wait failure")
    cleanup_interrupt = KeyboardInterrupt()
    real_cleanup = runner_run._cleanup_selenium_process_group
    cleanup_calls = 0

    def fail_wait(identity, deadline):
        state_deadline = time.monotonic() + 2
        while not state_path.exists() and time.monotonic() < state_deadline:
            time.sleep(0.01)
        assert state_path.exists(), "fake bridge did not start"
        raise original

    def interrupt_cleanup(proc, identity, hard_deadline, grace_s=2.0):
        nonlocal cleanup_calls
        cleanup_calls += 1
        if cleanup_calls == 1:
            raise cleanup_interrupt
        return real_cleanup(proc, identity, hard_deadline, grace_s)

    monkeypatch.setattr(runner_run, "_wait_driver_without_reaping", fail_wait)
    monkeypatch.setattr(
        runner_run,
        "_cleanup_selenium_process_group",
        interrupt_cleanup,
    )
    try:
        with pytest.raises(RuntimeError) as raised:
            runner_run.run_driver_subprocess(
                task,
                argv,
                dict(os.environ),
                artifact_dir,
                "http://127.0.0.1:1",
                "run1",
                "moli",
                1,
                "seed1",
                "session1",
            )

        assert raised.value is original
        assert cleanup_calls == 2
        state = json.loads(state_path.read_text(encoding="utf-8"))
        wait_for_pid_absent(state["adapter_pid"])
        wait_for_pid_absent(state["bridge_pid"])
        wait_for_port_state(bridge_port, open_=False)
        assert (artifact_dir / "stdout.log").exists()
        assert (artifact_dir / "stderr.log").exists()
    finally:
        force_cleanup_selenium_test_processes(state_path)


def test_selenium_cleanup_escalates_stubborn_bridge_and_preserves_browser_owner(
    tmp_path,
):
    browser_port = free_port()
    browser_process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "http.server",
            str(browser_port),
            "--bind",
            "127.0.0.1",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    manager = runner_run.BrowserManager()
    manager.processes["chrome"] = runner_run.BrowserProcess(
        engine="chrome",
        port=browser_port,
        process=browser_process,
        version_info={"Browser": "FakeBrowser/1.0"},
    )
    try:
        wait_for_port_state(browser_port, open_=True)
        result, _, state = run_leaking_selenium_adapter(
            tmp_path,
            mode="abrupt",
            ignore_sigterm=True,
        )

        assert "exited with code -9" in result["failure"]["detail"]
        assert state["descendant_pid"] is not None
        assert browser_process.poll() is None
        wait_for_port_state(browser_port, open_=True)
    finally:
        manager.close_all()

    assert browser_process.poll() is not None
    wait_for_pid_absent(browser_process.pid)
    wait_for_port_state(browser_port, open_=False)


def test_driver_subprocess_timeout_kills_descendant_process_group(tmp_path):
    script = tmp_path / "spawn_child.py"
    script.write_text(
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "print(child.pid, flush=True)\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    artifact_dir = tmp_path / "artifact"
    artifact_dir.mkdir()
    task_dict = make_framework_task_dict(
        "webdriver_selenium",
        timeouts={"task_ms": 300, "hard_kill_ms": 1000},
    )
    task = make_resolved(task=task_dict)

    with pytest.raises(subprocess.TimeoutExpired) as raised:
        runner_run.run_driver_subprocess(
            task,
            [sys.executable, str(script)],
            dict(os.environ),
            artifact_dir,
            "http://127.0.0.1:1",
            "run1",
            "moli",
            1,
            "seed1",
            "session1",
        )

    output = raised.value.stdout or ""
    if isinstance(output, bytes):
        output = output.decode("utf-8", errors="replace")
    child_pid = int(output.strip().splitlines()[0])
    wait_for_pid_absent(child_pid)


@pytest.mark.parametrize(
    "injected",
    [
        pytest.param(KeyboardInterrupt(), id="keyboard-interrupt"),
        pytest.param(RuntimeError("communicate failed"), id="unexpected-runtime-error"),
    ],
)
def test_driver_subprocess_unexpected_communicate_error_cleans_process_group(
    tmp_path,
    monkeypatch,
    injected,
):
    child_pid_path = tmp_path / "child.pid"
    script = tmp_path / "spawn_child_then_wait.py"
    script.write_text(
        "import pathlib, subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid), encoding='utf-8')\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    artifact_dir = tmp_path / "artifact"
    artifact_dir.mkdir()
    task = make_resolved(
        task=make_framework_task_dict(
            "webdriver_selenium",
            timeouts={"task_ms": 30_000, "hard_kill_ms": 35_000},
        )
    )
    real_wait = runner_run._wait_driver_without_reaping

    def interrupting_wait(identity, deadline):
        child_deadline = time.monotonic() + 2
        while (
            not child_pid_path.exists()
            and time.monotonic() < child_deadline
        ):
            time.sleep(0.01)
        assert child_pid_path.exists(), "descendant did not start"
        # Prove the real child is still live before injecting the caller-side
        # interruption/error which the cleanup path must preserve.
        assert real_wait(identity, time.monotonic() + 0.01) is False
        raise injected

    monkeypatch.setattr(
        runner_run,
        "_wait_driver_without_reaping",
        interrupting_wait,
    )

    with pytest.raises(type(injected)) as raised:
        runner_run.run_driver_subprocess(
            task,
            [sys.executable, str(script), str(child_pid_path)],
            dict(os.environ),
            artifact_dir,
            "http://127.0.0.1:1",
            "run1",
            "moli",
            1,
            "seed1",
            "session1",
        )

    assert raised.value is injected
    child_pid = int(child_pid_path.read_text(encoding="utf-8"))
    wait_for_pid_absent(child_pid)


def test_adapter_registry_scripts_exist():
    # Every registered scenario adapter must ship its executable; a registry
    # entry pointing at a missing script would fail every run at dispatch time.
    for kind, spec in runner_run.SCENARIO_ADAPTER_KINDS.items():
        script = runner_run.BENCH_ROOT / spec["script"]
        assert script.exists(), f"{kind}: adapter script missing: {spec['script']}"
        assert spec["argv"], f"{kind}: adapter argv must be non-empty"


# --- G. server_side grading: L2 driver checks gate the verdict -----------------


def _run_server_side_grading_stub(tmp_path, monkeypatch, *, layer, check_status):
    envelope = {
        "ok": True,
        "answer": "ERIC-57",
        "observations": {
            "checks": [
                {"name": "goto_ok", "status": check_status, "evidence": "step 1"},
            ],
            "failure_class": "cdp_semantic",
        },
        "metrics": {"cdp_call_count": 1, "cdp_error_count": 0, "ws_disconnect_count": 0},
    }
    stub = tmp_path / "stub_server_side.py"
    stub.write_text(
        "import json\nprint(json.dumps(" + repr(envelope) + "))\n",
        encoding="utf-8",
    )
    artifact_dir = tmp_path / f"artifact_{layer}_{check_status}"
    artifact_dir.mkdir()
    task = make_resolved(
        task=make_framework_task_dict(
            layer=layer,
            grader={"kind": "server_side", "endpoint": "/__grade__/expected_answer"},
            timeouts={"task_ms": 5000, "hard_kill_ms": 10000},
        )
    )
    server_verdict = {
        "ok": True,
        "checks": [
            {"name": "answer_equals_expected", "status": "pass", "evidence": "match"}
        ],
        "failure": None,
    }
    monkeypatch.setattr(runner_run, "http_json", lambda *_a, **_k: dict(server_verdict))
    return runner_run.run_driver_subprocess(
        task,
        [sys.executable, str(stub)],
        dict(os.environ),
        artifact_dir,
        "http://127.0.0.1:1",
        "run1",
        "moli",
        1,
        "seed1",
        "session1",
    )


def test_l2_server_side_verdict_requires_driver_checks(tmp_path, monkeypatch):
    result = _run_server_side_grading_stub(
        tmp_path, monkeypatch, layer="L2", check_status="fail"
    )
    assert result["ok"] is False
    assert result["grader"]["ok"] is False
    names = [check["name"] for check in result["grader"]["checks"]]
    assert names.index("answer_equals_expected") < names.index("goto_ok")
    assert result["grader"]["failure"]["class"] == "cdp_semantic"


def test_l2_server_side_verdict_passes_when_both_gates_pass(tmp_path, monkeypatch):
    result = _run_server_side_grading_stub(
        tmp_path, monkeypatch, layer="L2", check_status="pass"
    )
    assert result["ok"] is True
    names = [check["name"] for check in result["grader"]["checks"]]
    assert "goto_ok" in names and "answer_equals_expected" in names


def test_l1_server_side_verdict_keeps_server_grade_only(tmp_path, monkeypatch):
    result = _run_server_side_grading_stub(
        tmp_path, monkeypatch, layer="L1", check_status="fail"
    )
    assert result["ok"] is True
    assert all(check["name"] != "goto_ok" for check in result["grader"]["checks"])
