"""An async crash is charged to the engine only with protocol evidence.

`framework_probe.js` installs global uncaughtException / unhandledRejection
handlers because Playwright and Puppeteer throw from event-loop callbacks that
no try/catch in the probe can reach. Deciding who caused such a crash from its
timing alone ("it happened after connect") also charges the engine for bugs in
the probe's own callbacks. The classifier requires a frame showing the framework
crashed while decoding a message the engine sent.

Both cases below are deterministic: they run the classifier against captured
stacks, so neither needs a browser and neither changes meaning when an engine is
fixed.
"""
from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from runner import run as runner_run

MODULE = runner_run.BENCH_ROOT / "runner" / "scripts" / "lib" / "transport_fault.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")

# Captured from the Lightpanda investigation in #135: its Browser.getVersion
# reply omits the sessionId it was called with, so playwright-core routes the
# reply to the root session, misses the callback and trips an assertion inside
# the transport.
MALFORMED_SESSION_RESPONSE_STACK = "\n".join(
    [
        "Error: Assertion error",
        "    at assert (/repo/node_modules/playwright-core/lib/coreBundle.js:663:11)",
        "    at _CRSession._onMessage (/repo/node_modules/playwright-core/lib/coreBundle.js:34597:11)",
        "    at CRConnection._onMessage (/repo/node_modules/playwright-core/lib/coreBundle.js:34535:20)",
        "    at Immediate.<anonymous> (/repo/node_modules/playwright-core/lib/coreBundle.js:38922:32)",
        "    at process.processImmediate (node:internal/timers:483:21)",
    ]
)

# A rejection with nothing to do with the engine: raised from the probe's own
# timer callback, after the transport is up.
UNRELATED_REJECTION_STACK = "\n".join(
    [
        "Error: unrelated",
        "    at Timeout._onTimeout (/repo/runner/scripts/framework_probe.js:900:11)",
        "    at listOnTimeout (node:internal/timers:594:17)",
        "    at process.processTimers (node:internal/timers:469:7)",
    ]
)


def classify(stack: str) -> dict | None:
    script = (
        f"const {{transportFaultSignature}} = require({json.dumps(str(MODULE))});"
        f"process.stdout.write(JSON.stringify(transportFaultSignature({json.dumps(stack)})));"
    )
    proc = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=30
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_malformed_session_response_is_engine_evidence():
    signature = classify(MALFORMED_SESSION_RESPONSE_STACK)
    assert signature is not None
    assert signature["basis"] == "transport_decode"
    assert "onMessage" in signature["frame"]


def test_malformed_event_is_engine_evidence_despite_deferred_dispatch():
    """Playwright dispatches CDP events through `Promise.resolve().then(...)`.

    That microtask drops the transport frames, so an engine that sends a
    malformed *event* crashes in a framework handler with no `_onMessage` on the
    stack. Keying only on the decode frame would file a real engine defect as
    infra and drop the task out of scoring.
    """
    stack = "\n".join(
        [
            "TypeError: Cannot read properties of undefined (reading 'id')",
            "    at _FrameSession._onExecutionContextCreated (/repo/node_modules/playwright-core/lib/coreBundle.js:41022:33)",
            "    at /repo/node_modules/playwright-core/lib/coreBundle.js:34599:15",
            "    at process.processTicksAndRejections (node:internal/process/task_queues:105:5)",
        ]
    )
    signature = classify(stack)
    assert signature is not None
    assert signature["basis"] == "framework_internal"


def test_unrelated_rejection_after_connect_is_not_engine_evidence():
    assert classify(UNRELATED_REJECTION_STACK) is None


def test_a_stack_outside_the_frameworks_is_not_engine_evidence():
    """Naming a transport method is not enough; it has to be the framework's."""
    stack = "\n".join(
        [
            "Error: boom",
            "    at CRConnection._onMessage (/repo/runner/scripts/framework_probe.js:12:3)",
        ]
    )
    assert classify(stack) is None


def test_a_missing_stack_is_not_engine_evidence():
    assert classify("") is None


def test_a_probe_listener_that_throws_is_not_engine_evidence():
    """The framework invokes the probe's route/dialog/console listeners.

    A crash in one of those has framework frames below it, so anything that
    keyed on the framework merely appearing on the stack would charge the engine
    for the probe's own bug — the misattribution this module exists to stop.
    """
    stack = "\n".join(
        [
            "TypeError: cannot read x of undefined",
            "    at /repo/runner/scripts/framework_probe.js:512:44",
            "    at Route._handle (/repo/node_modules/playwright-core/lib/coreBundle.js:31004:7)",
            "    at CRNetworkManager._onRequest (/repo/node_modules/playwright-core/lib/coreBundle.js:39887:12)",
            "    at process.processTicksAndRejections (node:internal/process/task_queues:105:5)",
        ]
    )
    assert classify(stack) is None


def test_node_internal_frames_do_not_hide_the_throw_site():
    """A rejection surfacing through node's timers still originates in the probe."""
    stack = "\n".join(
        [
            "Error: unrelated",
            "    at node:internal/process/task_queues:95:5",
            "    at Timeout._onTimeout (/repo/runner/scripts/framework_probe.js:900:11)",
            "    at CRPage._onLifecycleEvent (/repo/node_modules/playwright-core/lib/coreBundle.js:40001:9)",
        ]
    )
    assert classify(stack) is None


def test_a_class_merely_ending_in_connection_is_not_the_decode_path():
    """`WebSocketConnection` is not the CDP transport.

    It is still framework code, so it remains engine-attributable — but on the
    weaker basis, which is what keeps the direct protocol case auditable apart
    from everything else the framework does.
    """
    stack = "\n".join(
        [
            "Error: boom",
            "    at WebSocketConnection.onMessage (/repo/node_modules/playwright-core/lib/x.js:9:1)",
        ]
    )
    signature = classify(stack)
    assert signature is not None
    assert signature["basis"] == "framework_internal"
