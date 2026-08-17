from __future__ import annotations

import asyncio
import sys
import types

from runner.scripts.adapters import pydoll_adapter
from runner.scripts.adapters.pydoll_adapter import (
    Adapter,
    protocol_error_message,
    setup_obscura_connection,
)


class _InputCommands:
    @staticmethod
    def dispatch_mouse_event(**params):
        return {"method": "Input.dispatchMouseEvent", "params": params}


class _MouseButton:
    LEFT = "left"


class _MouseEventType:
    MOUSE_PRESSED = "mousePressed"
    MOUSE_RELEASED = "mouseReleased"


class _ProtocolErrorTab:
    async def query(self, _selector, raise_exc=False):
        return object()

    async def execute_script(self, _expression):
        return {"result": {"result": {"value": "10:20:30:40"}}}

    async def _execute_command(self, _command):
        return {
            "id": 7,
            "error": {
                "code": -32000,
                "message": (
                    "Input.dispatchMouseEvent is not supported: "
                    "coordinate-based mouse input requires layout hit testing"
                ),
            },
        }


def _install_fake_pydoll_input_modules(monkeypatch):
    modules = {
        "pydoll": types.ModuleType("pydoll"),
        "pydoll.commands": types.ModuleType("pydoll.commands"),
        "pydoll.protocol": types.ModuleType("pydoll.protocol"),
        "pydoll.protocol.input": types.ModuleType("pydoll.protocol.input"),
        "pydoll.protocol.input.types": types.ModuleType("pydoll.protocol.input.types"),
    }
    modules["pydoll.commands"].InputCommands = _InputCommands
    modules["pydoll.protocol.input.types"].MouseButton = _MouseButton
    modules["pydoll.protocol.input.types"].MouseEventType = _MouseEventType
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)


def test_protocol_error_message_preserves_protocol_diagnostic():
    message = protocol_error_message(
        {"error": {"code": -32601, "message": "method is not supported", "data": "Input"}}
    )
    assert message == "CDP command failed: method is not supported; code=-32601; data=Input"


def test_click_protocol_error_records_failed_step(monkeypatch, tmp_path):
    _install_fake_pydoll_input_modules(monkeypatch)
    adapter = Adapter(
        {
            "task_url": "http://127.0.0.1:8000/l1/core",
            "artifact_dir": str(tmp_path),
            "task_timeout_ms": 30000,
        }
    )
    adapter.tab = _ProtocolErrorTab()

    result = asyncio.run(adapter.execute_step({"op": "click", "selector": "#go"}))

    assert result["ok"] is False
    assert result["unsupported"] is True
    assert "Input.dispatchMouseEvent is not supported" in result["error"]
    assert adapter.step_results == [result]
    assert adapter.op_calls == 1
    assert adapter.op_errors == 1


class _TargetCommands:
    @staticmethod
    def create_target(browser_context_id=None):
        return {
            "method": "Target.createTarget",
            "params": {"browserContextId": browser_context_id},
        }

    @staticmethod
    def attach_to_target(target_id, flatten=False):
        return {
            "method": "Target.attachToTarget",
            "params": {"targetId": target_id, "flatten": flatten},
        }

    @staticmethod
    def close_target(target_id):
        return {
            "method": "Target.closeTarget",
            "params": {"targetId": target_id},
        }


class _Tab:
    def __init__(
        self,
        browser,
        *,
        target_id,
        browser_context_id,
        connection_handler,
    ):
        self.browser = browser
        self.target_id = target_id
        self.browser_context_id = browser_context_id
        self.connection_handler = connection_handler
        self._routing_session_handler = None
        self._routing_session_id = None


class _ObscuraBrowser:
    def __init__(self, *, attach_error=False):
        self._connection_handler = object()
        self._tabs_opened = {}
        self.commands = []
        self.attach_error = attach_error

    async def _execute_command(self, command):
        self.commands.append(command)
        if command["method"] == "Target.createTarget":
            return {"result": {"targetId": "target-1"}}
        if (
            command["method"] == "Target.attachToTarget"
            and self.attach_error
        ):
            return {"error": {"message": "attach rejected"}}
        if command["method"] == "Target.closeTarget":
            return {"result": {"success": True}}
        return {"result": {"sessionId": "session-1"}}

    async def new_tab(self, *_args, **_kwargs):
        raise AssertionError("stock Pydoll new_tab must not be used for Obscura")


def _install_fake_pydoll_target_modules(monkeypatch):
    modules = {
        "pydoll": types.ModuleType("pydoll"),
        "pydoll.browser": types.ModuleType("pydoll.browser"),
        "pydoll.browser.tab": types.ModuleType("pydoll.browser.tab"),
        "pydoll.commands": types.ModuleType("pydoll.commands"),
    }
    modules["pydoll.browser.tab"].Tab = _Tab
    modules["pydoll.commands"].TargetCommands = _TargetCommands
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)


def test_obscura_bootstrap_uses_pydoll_flattened_session(monkeypatch, tmp_path):
    _install_fake_pydoll_target_modules(monkeypatch)
    monkeypatch.setattr(pydoll_adapter, "CLIENT_VERSION", "2.23.1")
    adapter = Adapter(
        {
            "engine": "obscura",
            "task_url": "http://127.0.0.1:8000/l1/core",
            "artifact_dir": str(tmp_path),
            "task_timeout_ms": 30000,
        }
    )
    browser = _ObscuraBrowser()
    adapter.browser = browser

    tab = asyncio.run(adapter.new_tab())

    assert [row["method"] for row in browser.commands] == [
        "Target.createTarget",
        "Target.attachToTarget",
    ]
    assert browser.commands[1]["params"]["flatten"] is True
    assert tab._routing_session_handler is browser._connection_handler
    assert tab._routing_session_id == "session-1"
    assert browser._tabs_opened == {"target-1": tab}
    assert (
        adapter.binding["compatibility_bootstrap"]
        == "obscura_pydoll_flattened_session_v1"
    )


def test_obscura_bootstrap_fails_closed_on_unpinned_pydoll(
    monkeypatch, tmp_path
):
    _install_fake_pydoll_target_modules(monkeypatch)
    monkeypatch.setattr(pydoll_adapter, "CLIENT_VERSION", "2.24.0")
    adapter = Adapter(
        {
            "engine": "obscura",
            "task_url": "http://127.0.0.1:8000/l1/core",
            "artifact_dir": str(tmp_path),
            "task_timeout_ms": 30000,
        }
    )
    browser = _ObscuraBrowser()
    adapter.browser = browser

    try:
        asyncio.run(adapter.new_tab())
    except RuntimeError as exc:
        assert "pinned to pydoll-python 2.23.1" in str(exc)
    else:
        raise AssertionError("unpinned Pydoll version must fail closed")
    assert browser.commands == []


def test_obscura_bootstrap_closes_target_when_attach_fails(
    monkeypatch, tmp_path
):
    _install_fake_pydoll_target_modules(monkeypatch)
    monkeypatch.setattr(pydoll_adapter, "CLIENT_VERSION", "2.23.1")
    adapter = Adapter(
        {
            "engine": "obscura",
            "task_url": "http://127.0.0.1:8000/l1/core",
            "artifact_dir": str(tmp_path),
            "task_timeout_ms": 30000,
        }
    )
    browser = _ObscuraBrowser(attach_error=True)
    adapter.browser = browser

    try:
        asyncio.run(adapter.new_tab())
    except RuntimeError as exc:
        assert "attach rejected" in str(exc)
    else:
        raise AssertionError("attach failure must fail the bootstrap")

    assert [row["method"] for row in browser.commands] == [
        "Target.createTarget",
        "Target.attachToTarget",
        "Target.closeTarget",
    ]
    assert browser._tabs_opened == {}


def test_obscura_bootstrap_reports_private_tab_api_drift_and_closes_target(
    monkeypatch, tmp_path
):
    _install_fake_pydoll_target_modules(monkeypatch)
    monkeypatch.setattr(pydoll_adapter, "CLIENT_VERSION", "2.23.1")

    class BrokenTab:
        def __init__(self, *_args, **_kwargs):
            raise TypeError("unexpected keyword argument")

    sys.modules["pydoll.browser.tab"].Tab = BrokenTab
    adapter = Adapter(
        {
            "engine": "obscura",
            "task_url": "http://127.0.0.1:8000/l1/core",
            "artifact_dir": str(tmp_path),
            "task_timeout_ms": 30000,
        }
    )
    browser = _ObscuraBrowser()
    adapter.browser = browser

    try:
        asyncio.run(adapter.new_tab())
    except RuntimeError as exc:
        assert "audited private API surface" in str(exc)
        assert "Tab/session routing internals" in str(exc)
    else:
        raise AssertionError("private API drift must fail closed")

    assert browser.commands[-1]["method"] == "Target.closeTarget"
    assert browser._tabs_opened == {}


def test_obscura_connection_reports_missing_private_setup_api(monkeypatch):
    monkeypatch.setattr(pydoll_adapter, "CLIENT_VERSION", "2.23.1")

    try:
        asyncio.run(
            setup_obscura_connection(
                object(),
                "ws://127.0.0.1:9225/devtools/browser",
                1.0,
            )
        )
    except RuntimeError as exc:
        assert "audited private API surface" in str(exc)
        assert "_setup_ws_address is unavailable" in str(exc)
    else:
        raise AssertionError("missing private setup API must fail closed")
