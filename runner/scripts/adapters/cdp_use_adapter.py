#!/usr/bin/env python3
"""cdp-use scenario adapter.

Drives the engine under test with the pinned `cdp-use` PyPI package — the
browser-use ecosystem's typed Python CDP client. Speaks the
abb_scenario_adapter/1 contract (see PROTOCOL.md in this directory): payload
JSON on stdin, result JSON on stdout, mandatory two-layer binding gate,
framework_probe.js op/check vocabulary.

Ops are implemented the way a thin-client user would have to: sessions via
Target.createTarget + Target.attachToTarget(flatten), interaction via
Input.dispatchMouseEvent / Input.insertText / Input.dispatchKeyEvent, and
observation via Runtime.evaluate — the same protocol-direct semantics as the
chrome-remote-interface adapter, so a per-op divergence between the two thin
clients isolates the client library rather than the engine.
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

    CLIENT_VERSION = _pkg_version("cdp-use")
except Exception:  # pragma: no cover - metadata lookup is best effort
    CLIENT_VERSION = "unknown"

# Minimal key table for `press`: the keys the fixture library actually
# exercises. Values follow Chrome's expectations for Input.dispatchKeyEvent.
KEY_DEFS = {
    "Enter": {"keyCode": 13, "key": "Enter", "code": "Enter", "text": "\r"},
    "Tab": {"keyCode": 9, "key": "Tab", "code": "Tab"},
    "Escape": {"keyCode": 27, "key": "Escape", "code": "Escape"},
    "Backspace": {"keyCode": 8, "key": "Backspace", "code": "Backspace"},
    "Space": {"keyCode": 32, "key": " ", "code": "Space", "text": " "},
    "ArrowLeft": {"keyCode": 37, "key": "ArrowLeft", "code": "ArrowLeft"},
    "ArrowUp": {"keyCode": 38, "key": "ArrowUp", "code": "ArrowUp"},
    "ArrowRight": {"keyCode": 39, "key": "ArrowRight", "code": "ArrowRight"},
    "ArrowDown": {"keyCode": 40, "key": "ArrowDown", "code": "ArrowDown"},
}

UNSUPPORTED_MARKERS = ("not found", "wasn't found", "unsupported", "unknown method", "not implemented", "not supported")


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


def http_json(url: str, timeout_s: float) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=timeout_s) as resp:
        return json.load(resp)


class Adapter:
    def __init__(self, payload: dict[str, Any]):
        self.payload = payload
        self.fixture_url = str(payload["task_url"])
        parts = urllib.parse.urlsplit(self.fixture_url)
        self.fixture_origin = f"{parts.scheme}://{parts.netloc}"
        self.fixture_host = parts.netloc
        self.artifact_dir = pathlib.Path(payload.get("artifact_dir") or ".")
        self.action_timeout_ms = int(payload.get("action_timeout_ms") or 8000)
        # Leave a 3s reserve for check evaluation and result emission.
        self.budget_deadline = time.monotonic() + int(payload.get("task_timeout_ms") or 30000) / 1000 - 3
        self.client = None
        self.session_id: str | None = None
        self.created_targets: list[str] = []
        self.target_creations: list[dict[str, Any]] = []
        self.op_calls = 0
        self.op_errors = 0
        self.saved: dict[str, str] = {}
        self.step_results: list[dict[str, Any]] = []
        self.binding: dict[str, Any] = {
            "driver": "cdp_use",
            "browser_ws": payload.get("browser_ws"),
            "expect_product": payload.get("expect_product") or "",
            "verified": False,
            "gate": None,
        }
        self.cdp_path = self.artifact_dir / "cdp.jsonl"

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

    async def send(self, method: str, params: dict[str, Any] | None = None, use_session: bool = True) -> dict[str, Any]:
        self.op_calls += 1
        session_id = self.session_id if use_session else None
        try:
            result = await self.client.send_raw(method, params or {}, session_id)
            self.trace({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "direction": "cdp_use", "method": method, "sessionId": session_id, "ok": True})
            return result
        except Exception as exc:
            self.op_errors += 1
            message = str(exc)
            self.trace({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "direction": "cdp_use", "method": method, "sessionId": session_id, "ok": False, "error": message})
            wrapped = RuntimeError(f"{method}: {message}")
            wrapped.cdp_rejected = (
                isinstance(exc, RuntimeError)
                and bool(exc.args)
                and isinstance(exc.args[0], dict)
                and exc.args[0].get("code") is not None
            )
            raise wrapped from None

    async def eval_expr(self, expression: str) -> Any:
        result = await self.send(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
        )
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

    async def ensure_session(self) -> str:
        if self.session_id:
            return self.session_id
        creation: dict[str, Any] = {
            "attempt": len(self.target_creations) + 1,
            "state": "requested",
        }
        self.target_creations.append(creation)
        try:
            created = await self.send(
                "Target.createTarget", {"url": "about:blank"}, use_session=False
            )
        except Exception as exc:
            creation.update(
                state=("rejected" if getattr(exc, "cdp_rejected", False) else "ambiguous"),
                error=str(exc),
            )
            raise
        target_id = created.get("targetId")
        if not target_id:
            creation["state"] = "ambiguous"
            raise RuntimeError("Target.createTarget returned no targetId")
        creation.update(state="created", target_id=target_id)
        self.created_targets.append(target_id)
        attached = await self.send("Target.attachToTarget", {"targetId": target_id, "flatten": True}, use_session=False)
        self.session_id = attached["sessionId"]
        await self.send("Page.enable")
        await self.send("Runtime.enable")
        return self.session_id

    async def cleanup_targets(self) -> dict[str, Any]:
        attempts: list[dict[str, Any]] = []
        for target_id in list(self.created_targets):
            closed = False
            for attempt in range(1, 3):
                try:
                    result = await asyncio.wait_for(
                        self.send(
                            "Target.closeTarget",
                            {"targetId": target_id},
                            use_session=False,
                        ),
                        timeout=3.0,
                    )
                    closed = result.get("success") is True
                    attempts.append(
                        {
                            "target_id": target_id,
                            "attempt": attempt,
                            "success": result.get("success"),
                            "confirmed": closed,
                        }
                    )
                except asyncio.TimeoutError:
                    attempts.append(
                        {
                            "target_id": target_id,
                            "attempt": attempt,
                            "confirmed": False,
                            "timed_out": True,
                            "error": "Target.closeTarget timeout",
                        }
                    )
                    break
                except Exception as exc:
                    attempts.append(
                        {
                            "target_id": target_id,
                            "attempt": attempt,
                            "confirmed": False,
                            "error": str(exc),
                        }
                    )
                if closed:
                    break
            creation = next(
                (
                    item
                    for item in self.target_creations
                    if item.get("target_id") == target_id
                ),
                None,
            )
            if creation is not None:
                creation["state"] = "closed" if closed else "cleanup_unconfirmed"
            if closed:
                self.created_targets.remove(target_id)
        confirmed = all(
            item.get("state") in {"closed", "rejected"}
            for item in self.target_creations
        )
        return {
            "backend": "cdp_use.Target.closeTarget",
            "required": bool(self.target_creations),
            "confirmed": confirmed,
            "same_connection_as_task": True,
            "creation_attempts": self.target_creations,
            "attempts": attempts,
        }

    @staticmethod
    def ax_value(value: Any) -> str:
        if isinstance(value, dict) and "value" in value:
            value = value.get("value")
        return "" if value is None else str(value)

    async def full_ax_tree(self) -> list[dict[str, Any]]:
        await self.send("Accessibility.enable")
        result = await self.send("Accessibility.getFullAXTree")
        nodes = result.get("nodes")
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

    async def navigate_and_settle(self, url: str, timeout_ms: int) -> None:
        nav = await self.send("Page.navigate", {"url": url})
        if nav.get("errorText"):
            raise RuntimeError(f"navigation failed: {nav['errorText']}")
        parts = urllib.parse.urlsplit(url)
        want_path = parts.path + (f"?{parts.query}" if parts.query else "")
        await self.poll_until(
            f'document.readyState === "complete" && (location.pathname + location.search) === {json.dumps(want_path)}',
            timeout_ms,
            f"navigation to {url}",
        )

    async def click_selector(self, sel: str) -> None:
        point = await self.eval_expr(
            self.sel_expr(
                sel,
                'el.scrollIntoView({block: "center", inline: "center"}); '
                "const r = el.getBoundingClientRect(); "
                "return {x: r.x + r.width / 2, y: r.y + r.height / 2};",
            )
        )
        base = {"x": point["x"], "y": point["y"], "button": "left", "clickCount": 1, "pointerType": "mouse"}
        await self.send("Input.dispatchMouseEvent", {"type": "mousePressed", **base})
        await self.send("Input.dispatchMouseEvent", {"type": "mouseReleased", **base})

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
            ver = await self.send("Browser.getVersion", use_session=False)
            return ver.get("product")
        if op == "user_agent":
            ver = await self.send("Browser.getVersion", use_session=False)
            return ver.get("userAgent")
        if op == "new_page":
            self.session_id = None
            await self.ensure_session()
            return "page_created"

        await self.ensure_session()

        if op == "goto":
            await self.navigate_and_settle(self.substitute(step.get("url") or "{fixture_url}"), timeout)
            return "navigated"
        if op == "reload":
            await self.eval_expr("window.__abb_reload_probe = 1; 'marked'")
            await self.send("Page.reload")
            await self.poll_until(
                'document.readyState === "complete" && !window.__abb_reload_probe',
                timeout,
                "reload to settle",
            )
            return "reloaded"
        if op in ("go_back", "go_forward"):
            hist = await self.send("Page.getNavigationHistory")
            idx = hist["currentIndex"] + (-1 if op == "go_back" else 1)
            entries = hist.get("entries") or []
            if idx < 0 or idx >= len(entries):
                return "no_history"
            nav_nonce = f"np{time.time_ns()}"
            await self.eval_expr(f"window.__abb_nav_probe = '{nav_nonce}|' + location.href, 'marked'")
            await self.send("Page.navigateToHistoryEntry", {"entryId": entries[idx]["id"]})
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
            await self.eval_expr(self.sel_expr(sel, 'el.focus(); if (typeof el.select === "function") el.select(); return "focused";'))
            await self.send("Input.insertText", {"text": value})
            return "filled"
        if op == "type":
            text = self.substitute("" if step.get("text") is None else step["text"])
            await self.eval_expr(
                self.sel_expr(
                    sel,
                    'el.focus(); if (typeof el.setSelectionRange === "function") { const n = (el.value || "").length; el.setSelectionRange(n, n); } return "focused";',
                )
            )
            await self.send("Input.insertText", {"text": text})
            return "typed"
        if op == "keyboard_type":
            await self.send("Input.insertText", {"text": self.substitute("" if step.get("text") is None else step["text"])})
            return "typed"
        if op == "press":
            key = str(step.get("key") or "")
            key_def = KEY_DEFS.get(key)
            if key_def is None and len(key) == 1:
                key_def = {"keyCode": ord(key.upper()), "key": key, "code": f"Key{key.upper()}", "text": key}
            if key_def is None:
                raise RuntimeError(f"unsupported key {key!r} for press")
            if sel:
                await self.eval_expr(self.sel_expr(sel, 'el.focus(); return "focused";'))
            down = {
                "type": "keyDown",
                "key": key_def["key"],
                "code": key_def["code"],
                "windowsVirtualKeyCode": key_def["keyCode"],
                "nativeVirtualKeyCode": key_def["keyCode"],
            }
            if key_def.get("text"):
                down["text"] = key_def["text"]
            await self.send("Input.dispatchKeyEvent", down)
            await self.send(
                "Input.dispatchKeyEvent",
                {
                    "type": "keyUp",
                    "key": key_def["key"],
                    "code": key_def["code"],
                    "windowsVirtualKeyCode": key_def["keyCode"],
                    "nativeVirtualKeyCode": key_def["keyCode"],
                },
            )
            return f"pressed {key}"
        if op == "check":
            already = await self.eval_expr(self.sel_expr(sel, "return !!el.checked;"))
            if not already:
                await self.click_selector(sel)
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
            await self.eval_expr(self.sel_expr(sel, 'el.focus(); return "focused";'))
            return "focused"
        if op == "evaluate":
            return await self.eval_expr(self.substitute(step["expression"]))
        if op == "wait_for_function":
            await self.poll_until(self.substitute(step["expression"]), timeout, "predicate")
            return "predicate_true"
        if op == "wait_for_selector":
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
            return await self.eval_expr(self.sel_expr(sel, "return el.textContent;"))
        if op == "inner_text":
            return await self.eval_expr(self.sel_expr(sel, "return el.innerText;"))
        if op == "get_attribute":
            return await self.eval_expr(self.sel_expr(sel, f"return el.getAttribute({json.dumps(step.get('name'))});"))
        if op == "input_value":
            return await self.eval_expr(self.sel_expr(sel, "return el.value;"))
        if op == "count":
            return await self.eval_expr(f"document.querySelectorAll({json.dumps(sel)}).length")
        if op == "is_visible":
            return await self.eval_expr(
                f"(() => {{ const el = document.querySelector({json.dumps(sel)}); if (!el) return false; "
                'const s = window.getComputedStyle(el); return s.display !== "none" && s.visibility !== "hidden"; })()'
            )
        if op == "is_checked":
            return await self.eval_expr(self.sel_expr(sel, "return !!el.checked;"))
        if op == "is_enabled":
            return await self.eval_expr(self.sel_expr(sel, "return !el.disabled;"))
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
            doc = await self.send("DOM.getDocument", {"depth": 0})
            root_id = (doc.get("root") or {}).get("nodeId")
            if not root_id:
                raise RuntimeError("DOM.getDocument returned no root nodeId")
            found = await self.send(
                "DOM.querySelector", {"nodeId": root_id, "selector": sel}
            )
            node_id = found.get("nodeId")
            if not node_id:
                raise RuntimeError(f"no element matches {sel}")
            await self.send("CSS.enable")
            result = await self.send(
                "CSS.getComputedStyleForNode", {"nodeId": node_id}
            )
            return self.format_computed_style(step, result.get("computedStyle"))
        if op == "title":
            return await self.eval_expr("document.title")
        if op == "url":
            return await self.eval_expr("location.href")
        raise RuntimeError(f"unknown op {op!r}")

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

    from cdp_use import CDPClient

    connect_error = None
    client = CDPClient(browser_ws)
    try:
        await asyncio.wait_for(client.start(), timeout=connect_timeout_ms / 1000)
    except Exception as exc:
        connect_error = str(exc)[:1000]
    binding["client_version"] = CLIENT_VERSION
    adapter.trace({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "direction": "cdp_use", "step": "connect", "ok": connect_error is None, "error": connect_error})

    if connect_error is not None:
        # A refused/failed connect is a genuine compatibility result: the
        # engine cannot be driven by this client. Grade every check as failed.
        check_rows = [
            {"name": "driver_connect", "status": "fail", "evidence": f"cdp-use@{CLIENT_VERSION} could not connect to {browser_ws}: {connect_error}"}
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
        adapter.client = client
        cleanup = await adapter.cleanup_targets()
        try:
            await client.stop()
        except Exception:
            pass
        emit(apply_cleanup_contract(outcome, cleanup, label="cdp-use"))
        return

    adapter.client = client
    outcome: dict[str, Any] | None = None
    try:
        # ---- Binding gate 2/2: the live client transport must identify as the
        # engine under test.
        live_identity = None
        live_product = None
        try:
            live_identity = await adapter.send("Browser.getVersion", use_session=False)
            live_product = str(live_identity.get("product") or "")
        except Exception:
            pass  # handled below: None != expect_product_live
        binding["expect_product_live"] = expect_product_live
        binding["live_product"] = live_product
        binding["live_check"] = "cdp_use_browser_get_version"
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
                        f"binding gate: live cdp-use transport reports identity={json.dumps(binding.get('actual'))}; "
                        f"expected {json.dumps(binding.get('expected'))} — the client is not bound to the remote engine under test"
                        if expected_remote_identity is not None
                        else f"binding gate: live cdp-use transport reports product={json.dumps(live_product)}; "
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
        adapter.trace({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "direction": "cdp_use", "step": "binding_verified", "identity": binding.get("actual") or {"product": live_product}})

        try:
            for idx, step in enumerate(steps):
                try:
                    value = await adapter.run_op(step)
                    result: dict[str, Any] = {"ok": True, "value": value}
                except Exception as exc:
                    message = str(exc)[:1000]
                    result = {"ok": False, "error": message, "unsupported": is_unsupported_message(message)}
                adapter.step_results.append(result)
                adapter.trace(
                    {
                        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                        "direction": "cdp_use",
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
                {"name": "driver_connect", "status": "pass", "evidence": f"cdp-use@{CLIENT_VERSION} bound to {binding['live_product']}"}
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
        cleanup = await adapter.cleanup_targets()
        try:
            await client.stop()
        except Exception:
            pass  # best effort
        if outcome is not None:
            emit(apply_cleanup_contract(outcome, cleanup, label="cdp-use"))


def main() -> None:
    try:
        asyncio.run(amain())
    except Exception as exc:  # last-resort: stdout must stay a single JSON object
        emit({"ok": False, "error": {"class": "script_error", "message": str(exc)}, "observations": {}, "metrics": {}})


if __name__ == "__main__":
    main()
