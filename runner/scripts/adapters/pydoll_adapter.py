#!/usr/bin/env python3
"""Pydoll scenario adapter.

Drives the engine under test with the pinned `pydoll-python` PyPI package — a
zero-webdriver, protocol-first Python automation library. Speaks
the abb_scenario_adapter/1 contract (see PROTOCOL.md in this directory).

Unlike the thin-client adapters (chrome-remote-interface / cdp-use), pydoll
ships its own high-level surface — `Tab.go_to`, `Tab.refresh`,
`WebElement.click` / `insert_text` / `text` — so this adapter exercises those
APIs directly instead of re-implementing ops over raw CDP. That is the point:
the column measures whether pydoll's abstractions hold up on each engine.
"""
from __future__ import annotations

import asyncio
import json
import pathlib
import sys
import time
import urllib.parse
import urllib.request
from typing import Any

try:
    from .remote_identity import compare_remote_identity, require_remote_identity
    from .remote_cleanup import apply_cleanup_contract
except ImportError:  # Executed as a standalone adapter script.
    from remote_identity import compare_remote_identity, require_remote_identity
    from remote_cleanup import apply_cleanup_contract

try:
    from importlib.metadata import version as _pkg_version

    CLIENT_VERSION = _pkg_version("pydoll-python")
except Exception:  # pragma: no cover - metadata lookup is best effort
    CLIENT_VERSION = "unknown"

UNSUPPORTED_MARKERS = ("not found", "wasn't found", "unsupported", "unknown method", "not implemented", "not supported")
OBSCURA_COMPAT_CLIENT_VERSION = "2.23.1"


def require_obscura_compat_client() -> None:
    if CLIENT_VERSION != OBSCURA_COMPAT_CLIENT_VERSION:
        raise RuntimeError(
            "Obscura Pydoll bootstrap is pinned to pydoll-python "
            f"{OBSCURA_COMPAT_CLIENT_VERSION}; installed {CLIENT_VERSION}"
        )


def obscura_private_api_error(detail: str) -> RuntimeError:
    return RuntimeError(
        "Obscura Pydoll bootstrap requires the audited private API surface "
        f"from pydoll-python {OBSCURA_COMPAT_CLIENT_VERSION}; {detail}"
    )


class TabBootstrapFailure(RuntimeError):
    """A failed page bootstrap with explicit target-lifecycle evidence."""

    def __init__(
        self,
        message: str,
        *,
        target_id: str | None,
        creation_state: str,
        cleanup: dict[str, Any],
    ) -> None:
        super().__init__(message)
        self.target_id = target_id
        self.creation_state = creation_state
        self.cleanup = cleanup


async def setup_obscura_connection(
    browser: Any,
    browser_ws: str,
    timeout_s: float,
) -> None:
    """Bind Pydoll's handler without assuming an initial target exists."""
    require_obscura_compat_client()
    setup = getattr(browser, "_setup_ws_address", None)
    if not callable(setup):
        raise obscura_private_api_error("Chrome._setup_ws_address is unavailable")
    try:
        setup_awaitable = setup(browser_ws)
    except TypeError as exc:
        raise obscura_private_api_error(
            "Chrome._setup_ws_address has an unexpected call signature"
        ) from exc
    await asyncio.wait_for(setup_awaitable, timeout=timeout_s)


def emit(obj: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj))


def to_saved_string(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        try:
            return json.dumps(value, ensure_ascii=False)
        except Exception:
            return str(value)
    return str(value)


def is_unsupported_message(msg: str) -> bool:
    lowered = str(msg or "").lower()
    return any(marker in lowered for marker in UNSUPPORTED_MARKERS)


def protocol_error_message(response: Any) -> str | None:
    """Return a stable diagnostic for a CDP error-shaped response.

    Pydoll's private ``_execute_command`` returns the raw protocol envelope.
    Unlike most of its public helpers, it does not raise when that envelope
    contains ``error``.  Callers must therefore reject the response explicitly
    or an engine-side command error becomes a success-shaped no-op.
    """
    if not isinstance(response, dict) or not response.get("error"):
        return None
    error = response["error"]
    if not isinstance(error, dict):
        return f"CDP command failed: {error}"
    message = str(error.get("message") or "unknown protocol error")
    code = error.get("code")
    data = error.get("data")
    parts = [message]
    if code is not None:
        parts.append(f"code={code}")
    if data not in (None, ""):
        parts.append(f"data={data}")
    return "CDP command failed: " + "; ".join(parts)


def http_json(url: str, timeout_s: float) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=timeout_s) as resp:
        return json.load(resp)


class Adapter:
    def __init__(self, payload: dict[str, Any]):
        self.payload = payload
        self.engine = str(payload.get("engine") or "")
        self.fixture_url = str(payload["task_url"])
        parts = urllib.parse.urlsplit(self.fixture_url)
        self.fixture_origin = f"{parts.scheme}://{parts.netloc}"
        self.fixture_host = parts.netloc
        self.artifact_dir = pathlib.Path(payload.get("artifact_dir") or ".")
        self.action_timeout_ms = int(payload.get("action_timeout_ms") or 8000)
        # Leave a 3s reserve for check evaluation and result emission.
        self.budget_deadline = time.monotonic() + int(payload.get("task_timeout_ms") or 30000) / 1000 - 3
        self.browser = None
        self.tab = None
        self.created_tabs: list[Any] = []
        self.tab_creations: list[dict[str, Any]] = []
        self.op_calls = 0
        self.op_errors = 0
        self.saved: dict[str, str] = {}
        self.step_results: list[dict[str, Any]] = []
        self.binding: dict[str, Any] = {
            "driver": "pydoll",
            "browser_ws": payload.get("browser_ws"),
            "expect_product": payload.get("expect_product") or "",
            "verified": False,
            "gate": None,
        }
        self.cdp_path = self.artifact_dir / "cdp.jsonl"

    async def close_target(
        self,
        target_id: str,
        target_commands: Any,
    ) -> dict[str, Any]:
        closure: dict[str, Any] = {
            "backend": "pydoll.Target.closeTarget",
            "target_id": target_id,
            "confirmed": False,
            "same_connection_as_task": True,
            "attempts": [],
        }
        for attempt in range(1, 3):
            try:
                response = await asyncio.wait_for(
                    self.browser._execute_command(
                        target_commands.close_target(target_id)
                    ),
                    timeout=3.0,
                )
                error = protocol_error_message(response)
                if error is not None:
                    raise RuntimeError(error)
                success = ((response or {}).get("result") or {}).get("success")
                confirmed = success is True
                closure["attempts"].append(
                    {
                        "target_id": target_id,
                        "attempt": attempt,
                        "success": success,
                        "confirmed": confirmed,
                    }
                )
                if confirmed:
                    closure["confirmed"] = True
                    break
            except asyncio.TimeoutError:
                closure["attempts"].append(
                    {
                        "target_id": target_id,
                        "attempt": attempt,
                        "confirmed": False,
                        "timed_out": True,
                        "error": "Target.closeTarget timeout",
                    }
                )
                # Do not overlap a retry with the timed-out command reader.
                break
            except Exception as exc:
                closure["attempts"].append(
                    {
                        "target_id": target_id,
                        "attempt": attempt,
                        "confirmed": False,
                        "error": str(exc),
                    }
                )
        return closure

    async def create_tracked_tab(self) -> Any:
        creation: dict[str, Any] = {
            "attempt": len(self.tab_creations) + 1,
            "state": "requested",
        }
        self.tab_creations.append(creation)
        try:
            tab = await self.new_tab("about:blank")
        except TabBootstrapFailure as exc:
            creation.update(
                state=exc.creation_state,
                error=str(exc),
                bootstrap_cleanup=exc.cleanup,
            )
            if exc.target_id is not None:
                creation["target_id"] = exc.target_id
            raise
        except Exception as exc:
            creation.update(state="ambiguous", error=str(exc))
            raise
        creation["state"] = "created"
        target_id = getattr(tab, "target_id", None) or getattr(tab, "_target_id", None)
        if not target_id:
            creation["state"] = "ambiguous"
            creation["tab"] = tab
            self.created_tabs.append(tab)
            raise RuntimeError("Pydoll created a tab without exposing its target id")
        creation["target_id"] = str(target_id)
        creation["tab"] = tab
        self.created_tabs.append(tab)
        return tab

    async def cleanup_tabs(self) -> dict[str, Any]:
        from pydoll.commands import TargetCommands

        attempts: list[dict[str, Any]] = [
            {**attempt, "phase": "bootstrap"}
            for creation in self.tab_creations
            for attempt in (
                (creation.get("bootstrap_cleanup") or {}).get("attempts") or []
            )
        ]
        for tab in list(self.created_tabs):
            creation = next(
                (item for item in self.tab_creations if item.get("tab") is tab),
                None,
            )
            target_id = creation.get("target_id") if creation else None
            if target_id:
                closure = await self.close_target(target_id, TargetCommands)
            else:
                closure = {
                    "confirmed": False,
                    "attempts": [
                        {
                            "attempt": 1,
                            "target_id": None,
                            "confirmed": False,
                            "error": "created tab has no target id",
                        }
                    ],
                }
            closed = closure.get("confirmed") is True
            attempts.extend(
                {**attempt, "phase": "finalizer"}
                for attempt in closure.get("attempts") or []
            )
            if creation is not None:
                creation["state"] = "closed" if closed else "cleanup_unconfirmed"
            if closed:
                tabs_opened = getattr(self.browser, "_tabs_opened", None)
                if isinstance(tabs_opened, dict) and creation is not None:
                    tabs_opened.pop(creation.get("target_id"), None)
                self.created_tabs.remove(tab)
        safe_creation_states = {"closed", "rejected", "not_requested"}
        confirmed = all(
            item.get("state") in safe_creation_states
            for item in self.tab_creations
        )
        creation_attempts = [
            {key: value for key, value in item.items() if key != "tab"}
            for item in self.tab_creations
        ]
        return {
            "backend": "pydoll.Target.closeTarget",
            "required": any(
                item.get("state") not in {"rejected", "not_requested"}
                for item in self.tab_creations
            ),
            "confirmed": confirmed,
            "same_connection_as_task": True,
            "creation_attempts": creation_attempts,
            "attempts": attempts,
        }

    async def new_tab(self, url: str = "", browser_context_id: Any = None):
        """Create a tab through Pydoll, with Obscura's empty-target bootstrap.

        Obscura gives each browser websocket an empty target registry, while
        Pydoll 2.23.1's stock connect path assumes an initial tab.  This
        compatibility path changes only target/session bootstrap: all task
        operations continue through Pydoll's Tab/WebElement APIs.
        """
        if self.engine != "obscura":
            return await self.browser.new_tab(url, browser_context_id)
        try:
            require_obscura_compat_client()
        except Exception as exc:
            raise TabBootstrapFailure(
                str(exc),
                target_id=None,
                creation_state="not_requested",
                cleanup={
                    "backend": "pydoll.Target.createTarget",
                    "required": False,
                    "confirmed": True,
                    "same_connection_as_task": True,
                    "attempts": [],
                },
            ) from exc

        from pydoll.browser.tab import Tab
        from pydoll.commands import TargetCommands

        try:
            created = await self.browser._execute_command(
                TargetCommands.create_target(
                    browser_context_id=browser_context_id
                )
            )
        except Exception as exc:
            raise TabBootstrapFailure(
                str(exc),
                target_id=None,
                creation_state="ambiguous",
                cleanup={
                    "backend": "pydoll.Target.createTarget",
                    "required": True,
                    "confirmed": False,
                    "same_connection_as_task": True,
                    "attempts": [],
                },
            ) from exc
        error = protocol_error_message(created)
        if error is not None:
            raise TabBootstrapFailure(
                error,
                target_id=None,
                creation_state="rejected",
                cleanup={
                    "backend": "pydoll.Target.createTarget",
                    "required": False,
                    "confirmed": True,
                    "same_connection_as_task": True,
                    "attempts": [],
                },
            )
        target_id = ((created or {}).get("result") or {}).get("targetId")
        if not target_id:
            raise TabBootstrapFailure(
                "Obscura Pydoll bootstrap: Target.createTarget returned no targetId",
                target_id=None,
                creation_state="ambiguous",
                cleanup={
                    "backend": "pydoll.Target.createTarget",
                    "required": True,
                    "confirmed": False,
                    "same_connection_as_task": True,
                    "attempts": [],
                },
            )
        target_id = str(target_id)

        try:
            attached = await self.browser._execute_command(
                TargetCommands.attach_to_target(target_id, flatten=True)
            )
            error = protocol_error_message(attached)
            if error is not None:
                raise RuntimeError(error)
            session_id = ((attached or {}).get("result") or {}).get("sessionId")
            if not session_id:
                raise RuntimeError(
                    "Obscura Pydoll bootstrap: Target.attachToTarget returned no sessionId"
                )

            try:
                connection_handler = self.browser._connection_handler
                tabs_opened = self.browser._tabs_opened
                if not isinstance(tabs_opened, dict):
                    raise TypeError("Chrome._tabs_opened is not a dictionary")
                tab = Tab(
                    self.browser,
                    target_id=target_id,
                    browser_context_id=browser_context_id,
                    connection_handler=connection_handler,
                )
                tab._routing_session_handler = connection_handler
                tab._routing_session_id = session_id
                tabs_opened[target_id] = tab
            except (AttributeError, TypeError) as exc:
                raise obscura_private_api_error(
                    "Tab/session routing internals do not match the pinned client"
                ) from exc

            self.binding["compatibility_bootstrap"] = (
                "obscura_pydoll_flattened_session_v1"
            )
            self.binding["compatibility_target_id"] = target_id
            if url:
                await tab.go_to(url)
            return tab
        except Exception as exc:
            # Target.createTarget succeeded, so every later bootstrap failure
            # must discard the orphan before the worker reuses this browser.
            try:
                tabs_opened = getattr(self.browser, "_tabs_opened", None)
                if isinstance(tabs_opened, dict):
                    tabs_opened.pop(target_id, None)
            except Exception:
                pass
            cleanup = await self.close_target(target_id, TargetCommands)
            raise TabBootstrapFailure(
                str(exc),
                target_id=target_id,
                creation_state=(
                    "closed"
                    if cleanup.get("confirmed") is True
                    else "cleanup_unconfirmed"
                ),
                cleanup=cleanup,
            ) from exc

    def trace(self, obj: dict[str, Any]) -> None:
        try:
            with self.cdp_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(obj) + "\n")
        except Exception:
            pass  # trace failures must not fail the probe

    def substitute(self, raw: Any) -> str:
        text = str(raw)
        text = text.replace("{fixture_url}", self.fixture_url)
        text = text.replace("{fixture_origin}", self.fixture_origin)
        text = text.replace("{fixture_host}", self.fixture_host)
        text = text.replace("{artifact_dir}", str(self.artifact_dir))
        return text

    async def eval_expr(self, expression: str) -> Any:
        # pydoll's has_return_outside_function heuristic false-positives on
        # inline callbacks containing `return` (e.g. `.map(function(x){return x})`),
        # silently wrapping the script in a return-less function so the result
        # becomes null. The arrow+eval form contains no bare `return` token,
        # defeats the heuristic, and keeps Runtime.evaluate completion-value
        # semantics for multi-statement programs.
        wrapped = f"(() => eval({json.dumps(expression)}))()"
        # Tab.execute_script returns the raw CDP Runtime.evaluate envelope.
        response = await self.tab.execute_script(wrapped)
        result = (response or {}).get("result") or {}
        details = result.get("exceptionDetails")
        if details:
            exc = details.get("exception") or {}
            desc = exc.get("description") or exc.get("value") or details.get("text") or "evaluation failed"
            raise RuntimeError(str(desc)[:500])
        return (result.get("result") or {}).get("value")

    def sel_expr(self, sel: str, body: str) -> str:
        quoted = json.dumps(sel)
        escaped = sel.replace('"', '\\"')
        return (
            f'(() => {{ const el = document.querySelector({quoted}); '
            f'if (!el) throw new Error("no element matches {escaped}"); {body} }})()'
        )

    async def poll_until(self, expression: str, timeout_ms: int, what: str) -> Any:
        deadline = time.monotonic() + timeout_ms / 1000
        while True:
            value = None
            try:
                value = await self.eval_expr(expression)
            except Exception:
                pass  # evaluation context may be mid-navigation; retry
            if value:
                return value
            if time.monotonic() > deadline:
                raise RuntimeError(f"timeout after {timeout_ms}ms waiting for {what}")
            await asyncio.sleep(0.05)

    async def query_element(self, sel: str):
        element = await self.tab.query(sel, raise_exc=False)
        if element is None:
            raise RuntimeError(f"no element matches {sel}")
        return element

    async def execute_command_checked(self, command: Any) -> Any:
        response = await self.tab._execute_command(command)
        error = protocol_error_message(response)
        if error is not None:
            raise RuntimeError(error)
        return response

    @staticmethod
    def ax_value(value: Any) -> str:
        if isinstance(value, dict) and "value" in value:
            value = value.get("value")
        return "" if value is None else str(value)

    async def full_ax_tree(self) -> list[dict[str, Any]]:
        from pydoll.commands import AccessibilityCommands

        await self.execute_command_checked(AccessibilityCommands.enable())
        response = await self.execute_command_checked(
            AccessibilityCommands.get_full_ax_tree()
        )
        nodes = ((response or {}).get("result") or {}).get("nodes")
        return nodes if isinstance(nodes, list) else []

    def ax_identity(self, nodes: list[dict[str, Any]], role: str, name: str) -> str:
        node = next(
            (
                candidate
                for candidate in nodes
                if self.ax_value(candidate.get("role")) == role
                and self.ax_value(candidate.get("name")) == name
            ),
            None,
        )
        if node is None:
            raise RuntimeError(f"AX node role={role!r} name={name!r} not found")
        backend_id = node.get("backendDOMNodeId")
        if backend_id is None:
            raise RuntimeError(
                f"AX node role={role!r} name={name!r} has no backendDOMNodeId"
            )
        return f"role={role}|name={name}|backendDOMNodeId={backend_id}"

    @staticmethod
    def format_computed_style(step: dict[str, Any], computed_style: Any) -> str:
        required = step.get("required_properties") or [
            "display",
            "visibility",
            "opacity",
            "pointer-events",
        ]
        minimum = int(step.get("min_property_count") or 100)
        properties = {
            str(entry.get("name")): str(entry.get("value"))
            for entry in (computed_style if isinstance(computed_style, list) else [])
            if isinstance(entry, dict) and entry.get("name") is not None
        }
        readable = all(name in properties and properties[name] != "" for name in required)
        prefix = "breadth-ok" if len(properties) >= minimum and readable else "breadth-insufficient"
        details = "|".join(
            f"{name}={properties.get(name, '<missing>')}" for name in required
        )
        return f"{prefix}|count={len(properties)}|{details}"

    async def click_selector(self, sel: str) -> None:
        # pydoll's WebElement.click computes its click point from box-model
        # quads that go stale when the page reflows between query and dispatch
        # (observed: an 18px miss on the waiting fixture where content is
        # inserted above the target). Dispatch the same CDP mouse events at
        # the element's live viewport center instead, via pydoll's own
        # InputCommands surface.
        from pydoll.commands import InputCommands
        from pydoll.protocol.input.types import MouseButton, MouseEventType

        await self.query_element(sel)  # existence check with the standard error surface
        box = await self.eval_expr(
            self.sel_expr(
                sel,
                "el.scrollIntoView({block: 'center', inline: 'center'});"
                " const r = el.getBoundingClientRect();"
                " return r.x + ':' + r.y + ':' + r.width + ':' + r.height;",
            )
        )
        x, y, w, h = (float(v) for v in str(box).split(":"))
        cx, cy = int(x + w / 2), int(y + h / 2)
        for event_type in (MouseEventType.MOUSE_PRESSED, MouseEventType.MOUSE_RELEASED):
            await self.execute_command_checked(
                InputCommands.dispatch_mouse_event(
                    type=event_type, x=cx, y=cy, button=MouseButton.LEFT, click_count=1
                )
            )

    async def run_op(self, step: dict[str, Any]) -> Any:
        op = step["op"]
        sel = self.substitute(step["selector"]) if step.get("selector") else None
        budget_remaining = int((self.budget_deadline - time.monotonic()) * 1000)
        if budget_remaining <= 0:
            raise RuntimeError("task budget exhausted before op could run")
        timeout = min(int(step["timeout_ms"]) if step.get("timeout_ms") else self.action_timeout_ms, budget_remaining)

        if op == "wait_ms":
            await asyncio.sleep(float(step.get("ms") or 100) / 1000)
            return None
        if op == "version":
            ver = await self.browser.get_version()
            return ver.get("product") if isinstance(ver, dict) else getattr(ver, "product", None)
        if op == "user_agent":
            ver = await self.browser.get_version()
            return ver.get("userAgent") if isinstance(ver, dict) else getattr(ver, "userAgent", None)
        if op == "new_page":
            self.tab = await self.create_tracked_tab()
            return "page_created"

        if self.tab is None:
            self.tab = await self.create_tracked_tab()

        if op == "goto":
            url = self.substitute(step.get("url") or "{fixture_url}")
            await self.tab.go_to(url, timeout=max(1, timeout // 1000))
            return "navigated"
        if op == "reload":
            await self.tab.refresh()
            await self.poll_until('document.readyState === "complete"', timeout, "reload to settle")
            return "reloaded"
        if op in ("go_back", "go_forward"):
            fn = "history.back()" if op == "go_back" else "history.forward()"
            nav_nonce = f"np{time.time_ns()}"
            await self.eval_expr(f"window.__abb_nav_probe = '{nav_nonce}|' + location.href, 'marked'")
            await self.eval_expr(f"{fn}, 'initiated'")
            await self.poll_until(
                f'document.readyState === "complete" && window.__abb_nav_probe !== "{nav_nonce}|" + location.href', timeout, op
            )
            return "ok"
        if op == "click":
            times = int(step.get("times") or 1)
            for _ in range(times):
                await self.click_selector(sel)
            return f"clicked x{times}"
        if op == "fill":
            value = self.substitute("" if step.get("value") is None else step["value"])
            element = await self.query_element(sel)
            await element.click()
            try:
                await element.clear()
            except Exception:
                pass  # empty inputs may not need (or support) clearing
            await element.insert_text(value)
            return "filled"
        if op == "type":
            text = self.substitute("" if step.get("text") is None else step["text"])
            element = await self.query_element(sel)
            await element.click()
            await element.insert_text(text)
            return "typed"
        if op == "keyboard_type":
            text = self.substitute("" if step.get("text") is None else step["text"])
            await self.tab.keyboard.type(text)
            return "typed"
        if op == "press":
            key = str(step.get("key") or "")
            element = await self.query_element(sel) if sel else None
            if element is None:
                raise RuntimeError("press without selector is not supported by the pydoll adapter")
            # pydoll's press_keyboard_key emits events whose `key` field does
            # not reach page-level keydown handlers (event.key stays empty),
            # so handlers matching event.key === "Enter" never fire. Focus the
            # element and dispatch fully-populated key events through pydoll's
            # own InputCommands surface instead.
            from pydoll.commands import InputCommands
            from pydoll.protocol.input.types import KeyEventType

            await self.eval_expr(self.sel_expr(sel, "el.focus(); return 'focused';"))
            key_defs = {
                "Enter": {"code": "Enter", "vk": 13, "text": "\r"},
                "Tab": {"code": "Tab", "vk": 9, "text": None},
                "Escape": {"code": "Escape", "vk": 27, "text": None},
                "Backspace": {"code": "Backspace", "vk": 8, "text": None},
            }
            d = key_defs.get(key)
            if d is None:
                raise RuntimeError(f"unsupported key {key!r} for press")
            down = InputCommands.dispatch_key_event(
                type=KeyEventType.KEY_DOWN, key=key, code=d["code"],
                windows_virtual_key_code=d["vk"], native_virtual_key_code=d["vk"],
                text=d["text"],
            )
            await self.execute_command_checked(down)
            up = InputCommands.dispatch_key_event(
                type=KeyEventType.KEY_UP, key=key, code=d["code"],
                windows_virtual_key_code=d["vk"], native_virtual_key_code=d["vk"],
            )
            await self.execute_command_checked(up)
            return f"pressed {key}"
        if op == "check":
            element = await self.query_element(sel)
            already = await self.eval_expr(self.sel_expr(sel, "return !!el.checked;"))
            if not already:
                await element.click()
            return "checked"
        if op == "select_option":
            value = self.substitute(step.get("value"))
            return await self.eval_expr(
                self.sel_expr(
                    sel,
                    f"el.value = {json.dumps(value)}; "
                    'el.dispatchEvent(new Event("input", {bubbles: true})); '
                    'el.dispatchEvent(new Event("change", {bubbles: true})); '
                    "return [el.value];",
                )
            )
        if op == "focus":
            element = await self.query_element(sel)
            await element.focus()
            return "focused"
        if op == "evaluate":
            return await self.eval_expr(self.substitute(step["expression"]))
        if op == "wait_for_function":
            await self.poll_until(self.substitute(step["expression"]), timeout, "predicate")
            return "predicate_true"
        if op == "wait_for_selector":
            # find_or_wait_element's keyword surface varies across pydoll
            # versions; poll the DOM directly for a stable contract.
            quoted = json.dumps(sel)
            state = step.get("state")
            if state in ("hidden", "detached"):
                expression = (
                    f"(() => {{ const el = document.querySelector({quoted}); if (!el) return true; "
                    'const s = window.getComputedStyle(el); return s.display === "none" || s.visibility === "hidden"; })()'
                )
            elif state == "visible":
                expression = (
                    f"(() => {{ const el = document.querySelector({quoted}); if (!el) return false; "
                    'const s = window.getComputedStyle(el); return s.display !== "none" && s.visibility !== "hidden"; })()'
                )
            else:
                expression = f"!!document.querySelector({quoted})"
            await self.poll_until(expression, timeout, f"selector {sel}")
            return "selector_ready"
        if op == "text_content":
            element = await self.query_element(sel)
            return await element.text
        if op == "inner_text":
            return await self.eval_expr(self.sel_expr(sel, "return el.innerText;"))
        if op == "get_attribute":
            element = await self.query_element(sel)
            return element.get_attribute(step.get("name"))
        if op == "input_value":
            # WebElement.value is the *cached attribute* in pydoll, not the
            # live DOM value; a fill would be invisible through it.
            return await self.eval_expr(self.sel_expr(sel, "return el.value;"))
        if op == "count":
            return await self.eval_expr(f"document.querySelectorAll({json.dumps(sel)}).length")
        if op == "is_visible":
            element = await self.tab.query(sel, raise_exc=False)
            if element is None:
                return False
            return await element.is_visible()
        if op == "is_checked":
            return await self.eval_expr(self.sel_expr(sel, "return !!el.checked;"))
        if op == "is_enabled":
            element = await self.query_element(sel)
            return await element.is_enabled()
        if op == "ax_snapshot":
            return await self.full_ax_tree()
        if op == "ax_node_identity":
            role = str(step.get("role") or "")
            name = self.substitute(step.get("name") or "")
            identity = self.ax_identity(await self.full_ax_tree(), role, name)
            if step.get("compare_to"):
                before = self.saved.get(str(step["compare_to"]))
                if before == identity:
                    return f"stable|{identity}"
                return f"changed|before={before}|after={identity}"
            return identity
        if op == "computed_style_breadth":
            doc = await self.execute_command_checked(
                {"method": "DOM.getDocument", "params": {"depth": 0}}
            )
            root_id = (((doc or {}).get("result") or {}).get("root") or {}).get(
                "nodeId"
            )
            if not root_id:
                raise RuntimeError("DOM.getDocument returned no root nodeId")
            found = await self.execute_command_checked(
                {
                    "method": "DOM.querySelector",
                    "params": {"nodeId": root_id, "selector": sel},
                }
            )
            node_id = ((found or {}).get("result") or {}).get("nodeId")
            if not node_id:
                raise RuntimeError(f"no element matches {sel}")
            await self.execute_command_checked(
                {"method": "CSS.enable", "params": {}}
            )
            result = await self.execute_command_checked(
                {
                    "method": "CSS.getComputedStyleForNode",
                    "params": {"nodeId": node_id},
                }
            )
            computed = ((result or {}).get("result") or {}).get("computedStyle")
            return self.format_computed_style(step, computed)
        if op == "title":
            return await self.tab.title
        if op == "url":
            return await self.tab.current_url
        raise RuntimeError(f"unknown op {op!r}")

    async def execute_step(self, step: dict[str, Any]) -> dict[str, Any]:
        try:
            value = await self.run_op(step)
            result: dict[str, Any] = {"ok": True, "value": value}
        except Exception as exc:
            message = str(exc)[:1000] or type(exc).__name__
            result = {
                "ok": False,
                "error": message,
                "unsupported": is_unsupported_message(message),
            }
            self.op_errors += 1
        self.op_calls += 1
        self.step_results.append(result)
        return result

    def evaluate_check(self, check: dict[str, Any]) -> tuple[bool, str]:
        kind = check.get("kind")
        if kind == "saved_equals":
            value = self.saved.get(check.get("name"))
            expected = str(check.get("expected"))
            return value == expected, f"{check.get('name')}={json.dumps(value)} expected={json.dumps(expected)}"
        if kind == "saved_contains":
            value = self.saved.get(check.get("name"))
            want = self.substitute(str(check.get("expected")))
            ok = isinstance(value, str) and want in value
            return ok, f"{check.get('name')}={json.dumps(value[:300] if isinstance(value, str) else value)} must contain {json.dumps(want)}"
        if kind == "saved_not_contains":
            value = self.saved.get(check.get("name"))
            want = self.substitute(str(check.get("expected")))
            ok = isinstance(value, str) and want not in value
            return ok, f"{check.get('name')}={json.dumps(value[:300] if isinstance(value, str) else value)} must NOT contain {json.dumps(want)}"
        if kind == "saved_truthy":
            value = self.saved.get(check.get("name"))
            truthy = value is not None and value not in ("undefined", "", "null", "false") and not str(value).startswith("ERROR:")
            return truthy, f"{check.get('name')}={json.dumps(str(value)[:300] if value is not None else None)}"
        if kind == "step_ok":
            row = self.step_results[check["step"]] if 0 <= int(check.get("step", -1)) < len(self.step_results) else {}
            return bool(row.get("ok")), f"step {check.get('step')} ok={bool(row.get('ok'))} error={row.get('error') or 'none'}"
        if kind == "step_fails":
            row = self.step_results[check["step"]] if 0 <= int(check.get("step", -1)) < len(self.step_results) else {}
            return row.get("ok") is False, f"step {check.get('step')} ok={bool(row.get('ok'))} (must fail) error={row.get('error') or 'none'}"
        if kind == "file_nonempty":
            file_path = pathlib.Path(self.substitute(check.get("path")))
            size = file_path.stat().st_size if file_path.exists() else 0
            return size > 0, f"{file_path} size={size}"
        if kind == "any_of":
            results = [self.evaluate_check(sub) for sub in check.get("checks") or []]
            evidence = " | ".join(f"{'pass' if ok else 'fail'}: {ev}" for ok, ev in results)
            return any(ok for ok, _ in results), evidence
        return False, f"unknown check kind {kind}"


async def amain() -> None:
    try:
        payload = json.loads(sys.stdin.read())
    except Exception as exc:
        emit({"ok": False, "error": {"class": "script_error", "message": f"invalid payload JSON on stdin: {exc}"}, "observations": {}, "metrics": {}})
        return
    browser_ws = payload.get("browser_ws")
    cdp_port = payload.get("cdp_port")
    expect_product = payload.get("expect_product") or ""
    expect_ua = payload.get("expect_ua") or ""
    expect_product_live = payload.get("expect_product_live") or expect_product
    try:
        expected_remote_identity = (
            require_remote_identity(
                payload.get("expected_remote_identity"),
                "expected_remote_identity",
            )
            if payload.get("remote_cdp") is True
            else None
        )
    except ValueError as exc:
        emit({"ok": False, "error": {"class": "script_error", "message": f"binding gate: {exc}"}, "observations": {}, "metrics": {}})
        return
    steps = payload.get("steps") or []
    checks = payload.get("checks") or []
    connect_timeout_ms = int(payload.get("connect_timeout_ms") or 15000)
    if not browser_ws or not payload.get("task_url"):
        emit({"ok": False, "error": {"class": "script_error", "message": "payload requires browser_ws and task_url"}, "observations": {}, "metrics": {}})
        return

    adapter = Adapter(payload)
    adapter.artifact_dir.mkdir(parents=True, exist_ok=True)
    adapter.cdp_path.write_text("", encoding="utf-8")
    binding = adapter.binding

    # ---- Binding gate 1/2: HTTP identity of the endpoint, verbatim.
    if not cdp_port or not expect_product:
        emit({"ok": False, "error": {"class": "script_error", "message": "binding gate: cdp_port and expect_product are required (refusing to run unverified)"}, "observations": {"binding": binding}, "metrics": {}})
        return
    try:
        version_info = http_json(f"http://127.0.0.1:{cdp_port}/json/version", 4.0)
    except Exception as exc:
        emit({"ok": False, "error": {"class": "script_error", "message": f"binding gate: /json/version unreachable on port {cdp_port}: {exc}"}, "observations": {"binding": binding}, "metrics": {}})
        return
    http_product = str(version_info.get("Browser") or version_info.get("Product") or "")
    http_ua = str(version_info.get("User-Agent") or "")
    binding["http_product"] = http_product
    if http_product != expect_product or (expect_ua and http_ua != expect_ua):
        emit(
            {
                "ok": False,
                "error": {
                    "class": "script_error",
                    "message": (
                        f"binding gate: endpoint on port {cdp_port} reports product={json.dumps(http_product)} "
                        f"ua={json.dumps(http_ua)}; expected product={json.dumps(expect_product)} — refusing to run against an unverified engine"
                    ),
                },
                "observations": {"binding": binding},
                "metrics": {},
            }
        )
        return
    ws_from_version = version_info.get("webSocketDebuggerUrl")
    if ws_from_version and ws_from_version != browser_ws:
        emit({"ok": False, "error": {"class": "script_error", "message": f"binding gate: browser_ws {browser_ws} != verified endpoint {ws_from_version}"}, "observations": {"binding": binding}, "metrics": {}})
        return
    binding["gate"] = "http_json_version"

    from pydoll.browser import Chrome

    connect_error = None
    browser = Chrome()
    try:
        if str(payload.get("engine") or "") == "obscura":
            # The stock connect path immediately indexes the initial tab.
            # Obscura's per-websocket target registry starts empty, so bind
            # Pydoll's own connection handler first and let Adapter.new_tab()
            # establish a flattened Pydoll Tab session.
            await setup_obscura_connection(
                browser,
                browser_ws,
                connect_timeout_ms / 1000,
            )
            binding["compatibility_bootstrap"] = (
                "obscura_pydoll_flattened_session_v1"
            )
        else:
            await asyncio.wait_for(browser.connect(browser_ws), timeout=connect_timeout_ms / 1000)
    except Exception as exc:
        connect_error = str(exc)[:1000]
    binding["client_version"] = CLIENT_VERSION
    adapter.trace({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "direction": "pydoll", "step": "connect", "ok": connect_error is None, "error": connect_error})

    if connect_error is not None:
        # A refused/failed connect is a genuine compatibility result: the
        # engine cannot be driven by this client. Grade every check as failed.
        check_rows = [
            {"name": "driver_connect", "status": "fail", "evidence": f"pydoll@{CLIENT_VERSION} could not connect to {browser_ws}: {connect_error}"}
        ] + [
            {"name": check.get("label") or check.get("kind") or f"check{idx}", "status": "fail", "evidence": "client did not connect; scenario not executed"}
            for idx, check in enumerate(checks)
        ]
        outcome = {
            "ok": True,
            "answer": f"0/{len(check_rows)} checks",
            "observations": {
                "checks": check_rows,
                "saved": {},
                "binding": binding,
                "connect_error": connect_error,
                "failure_class": "cdp_semantic",
            },
            "metrics": {
                "cdp_call_count": 1,
                "cdp_error_count": 1,
                "ws_disconnect_count": 0,
            },
        }
        adapter.browser = browser
        cleanup = await adapter.cleanup_tabs()
        try:
            await browser.close()
        except Exception:
            pass
        emit(apply_cleanup_contract(outcome, cleanup, label="pydoll"))
        return

    adapter.browser = browser
    outcome: dict[str, Any] | None = None
    try:
        # ---- Binding gate 2/2: the live client transport must identify as the
        # engine under test.
        live_identity = None
        live_product = None
        try:
            live_identity = await browser.get_version()
            live_product = str((live_identity or {}).get("product") or "")
        except Exception:
            pass  # handled below: None != expect_product_live
        binding["expect_product_live"] = expect_product_live
        binding["live_product"] = live_product
        binding["live_check"] = "pydoll_get_version"
        if expected_remote_identity is not None:
            binding.update(
                compare_remote_identity(expected_remote_identity, live_identity)
            )
        identity_verified = (
            binding.get("verified") is True
            if expected_remote_identity is not None
            else live_product == expect_product_live
        )
        if not identity_verified:
            outcome = {
                "ok": False,
                "error": {
                    "class": "script_error",
                    "message": (
                        f"binding gate: live pydoll transport reports identity={json.dumps(binding.get('actual'))}; "
                        f"expected {json.dumps(binding.get('expected'))} — the client is not bound to the remote engine under test"
                        if expected_remote_identity is not None
                        else f"binding gate: live pydoll transport reports product={json.dumps(live_product)}; "
                        f"expected {json.dumps(expect_product_live)} — the client is not bound to the engine under test"
                    ),
                },
                "observations": {"binding": binding},
                "metrics": {
                    "cdp_call_count": adapter.op_calls,
                    "cdp_error_count": adapter.op_errors,
                    "ws_disconnect_count": 0,
                },
            }
            return
        binding["verified"] = True
        adapter.trace({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "direction": "pydoll", "step": "binding_verified", "identity": binding.get("actual") or {"product": live_product}})

        try:
            for idx, step in enumerate(steps):
                result = await adapter.execute_step(step)
                adapter.trace(
                    {
                        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                        "direction": "pydoll",
                        "step": idx,
                        "op": step.get("op"),
                        "selector": step.get("selector"),
                        "ok": result["ok"],
                        "error": result.get("error"),
                    }
                )
                if result["ok"] and step.get("expect_fail"):
                    result["unexpected_success"] = True
                if step.get("save_as"):
                    adapter.saved[step["save_as"]] = (
                        to_saved_string(result["value"]) if result["ok"] else f"ERROR: {result['error']}"
                    )

            check_rows = [
                {"name": "driver_connect", "status": "pass", "evidence": f"pydoll@{CLIENT_VERSION} bound to {binding['live_product']}"}
            ]
            for idx, check in enumerate(checks):
                ok, evidence = adapter.evaluate_check(check)
                check_rows.append(
                    {
                        "name": check.get("label") or check.get("kind") or f"check{idx}",
                        "status": "pass" if ok else "fail",
                        "evidence": evidence,
                    }
                )
            passed = sum(1 for row in check_rows if row["status"] == "pass")
            outcome = {
                "ok": True,
                "answer": adapter.saved.get("answer", f"{passed}/{len(check_rows)} checks"),
                "observations": {
                    "checks": check_rows,
                    "saved": adapter.saved,
                    "binding": binding,
                    "driver_ops": len(adapter.step_results),
                    "driver_op_errors": sum(1 for row in adapter.step_results if not row["ok"]),
                    "failure_class": "cdp_semantic",
                },
                "metrics": {
                    "cdp_call_count": adapter.op_calls,
                    "cdp_error_count": adapter.op_errors,
                    "ws_disconnect_count": 0,
                },
            }
        except Exception as exc:
            message = str(exc)
            klass = "engine_unsupported" if is_unsupported_message(message) else "script_error"
            outcome = {
                "ok": False,
                "error": {"class": klass, "message": message},
                "observations": {"saved": adapter.saved, "binding": binding},
                "metrics": {
                    "cdp_call_count": adapter.op_calls,
                    "cdp_error_count": adapter.op_errors,
                    "ws_disconnect_count": 0,
                },
            }
    finally:
        cleanup = await adapter.cleanup_tabs()
        try:
            await browser.close()  # closes the websocket only, never the engine
        except Exception:
            pass  # best effort
        if outcome is not None:
            emit(apply_cleanup_contract(outcome, cleanup, label="pydoll"))


def main() -> None:
    try:
        asyncio.run(amain())
    except Exception as exc:  # last-resort: stdout must stay a single JSON object
        emit({"ok": False, "error": {"class": "script_error", "message": str(exc)}, "observations": {}, "metrics": {}})


if __name__ == "__main__":
    main()
