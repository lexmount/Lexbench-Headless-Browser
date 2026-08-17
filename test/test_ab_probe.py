"""l1_ab_probe.js subprocess-boundary regression coverage.

Runs the real probe script as a node subprocess against a stub AB_BIN that
records every argv it receives and answers the connect/binding handshake.
No browser and no real agent-browser binary are involved; the assertion is
that placeholders like {fixture_origin} are substituted before crossing the
subprocess boundary (version 1 sent the literal placeholder text).
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess

import pytest

from runner import run as runner_run

BENCH_ROOT = runner_run.BENCH_ROOT
HAVE_NODE = shutil.which("node") is not None

STUB_AB = """#!/usr/bin/env node
const fs = require("fs");
const argv = process.argv.slice(2);
fs.appendFileSync(process.env.AB_CAPTURE, JSON.stringify(argv) + "\\n");
const cmd = argv.slice(argv.indexOf("--json") + 1);
const out = { success: true, data: {} };
if (cmd[0] === "get" && cmd[1] === "cdp-url") out.data.cdpUrl = process.env.BROWSER_WS;
if (cmd[0] === "eval") out.data.result = true;
process.stdout.write(JSON.stringify(out));
"""


@pytest.mark.skipif(not HAVE_NODE, reason="node is required for the ab probe script")
def test_eval_steps_substitute_fixture_placeholders(tmp_path: pathlib.Path) -> None:
    task = json.loads(
        (BENCH_ROOT / "tasks/L1/agent_browser/ab_probe_eval_fetch.json").read_text(
            encoding="utf-8"
        )
    )
    stub = tmp_path / "stub_ab.js"
    stub.write_text(STUB_AB, encoding="utf-8")
    stub.chmod(0o755)
    capture = tmp_path / "ab_argv.jsonl"
    capture.write_text("", encoding="utf-8")

    env = dict(os.environ)
    env.update(
        {
            "AB_BIN": str(stub),
            "AB_CAPTURE": str(capture),
            "CDP_PORT": "9222",
            "BROWSER_WS": "ws://127.0.0.1:9222/devtools/browser/test-identity",
            "TASK_URL": "http://127.0.0.1:8123/l1/probe",
            "ARTIFACT_DIR": str(tmp_path),
            "AB_STEPS": json.dumps(task["driver"]["env"]["AB_STEPS"]),
            "AB_CHECKS": json.dumps(task["driver"]["env"]["AB_CHECKS"]),
        }
    )
    proc = subprocess.run(
        ["node", str(BENCH_ROOT / "runner/scripts/l1_ab_probe.js")],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    output = json.loads(proc.stdout)
    assert output["ok"] is True, output
    check_rows = output["observations"]["checks"]
    assert all(row["status"] == "pass" for row in check_rows), check_rows

    calls = [json.loads(line) for line in capture.read_text().splitlines()]
    eval_calls = [argv for argv in calls if "eval" in argv]
    assert eval_calls, calls
    joined = " ".join(eval_calls[0])
    assert "{fixture_origin}" not in joined
    assert "http://127.0.0.1:8123/page2" in joined
    # No argv anywhere may leak an unexpanded placeholder.
    assert not any("{fixture_" in part for argv in calls for part in argv)
