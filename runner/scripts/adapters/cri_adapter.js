#!/usr/bin/env node
"use strict";

// chrome-remote-interface scenario adapter.
//
// Drives the engine under test with the pinned `chrome-remote-interface` npm
// package as the protocol-direct control: there is no framework runtime and
// every op is hand-rolled CDP. Speaks the
// abb_scenario_adapter/1 contract (see PROTOCOL.md in this directory): payload
// JSON on stdin, result JSON on stdout, mandatory two-layer binding gate,
// framework_probe.js op/check vocabulary.
//
// Ops are implemented the way a thin-client user would have to: sessions via
// Target.createTarget + Target.attachToTarget(flatten), interaction via
// Input.dispatchMouseEvent / Input.insertText / Input.dispatchKeyEvent, and
// observation via Runtime.evaluate. `local: true` keeps the client on its
// bundled protocol descriptor instead of fetching /json/protocol, which
// non-Chrome engines may not serve.

const fs = require("fs");
const http = require("http");
const path = require("path");
const CDP = require("chrome-remote-interface");
const {
  compareRemoteIdentity,
  requireRemoteIdentity,
} = require("../lib/remote_identity");
const { applyCleanupContract } = require("../lib/remote_cleanup");

function emit(obj) {
  process.stdout.write(JSON.stringify(obj));
}

function readStdin() {
  return new Promise((resolve, reject) => {
    let data = "";
    process.stdin.setEncoding("utf8");
    process.stdin.on("data", (chunk) => (data += chunk));
    process.stdin.on("end", () => resolve(data));
    process.stdin.on("error", reject);
  });
}

function wait(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

// Same delay, but the timer does not keep the event loop alive. Used for
// Promise.race deadlines whose loser would otherwise stall process exit.
function waitUnref(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms).unref());
}

function httpJson(url, timeoutMs) {
  return new Promise((resolve, reject) => {
    const req = http.get(url, { timeout: timeoutMs || 3000 }, (res) => {
      let body = "";
      res.on("data", (chunk) => (body += chunk));
      res.on("end", () => {
        try {
          resolve(JSON.parse(body));
        } catch (err) {
          reject(new Error(`invalid JSON from ${url}: ${err.message}`));
        }
      });
    });
    req.on("timeout", () => req.destroy(new Error(`timeout fetching ${url}`)));
    req.on("error", reject);
  });
}

function toSavedString(value) {
  if (value === undefined) return "undefined";
  if (value === null) return "null";
  if (typeof value === "object") {
    try {
      return JSON.stringify(value);
    } catch {
      return String(value);
    }
  }
  return String(value);
}

function isUnsupportedMessage(msg) {
  return /not found|wasn't found|unsupported|unknown method|not implemented|not supported/i.test(String(msg || ""));
}

// Minimal key table for `press`: the keys the fixture library actually
// exercises. Values follow Chrome's expectations for Input.dispatchKeyEvent.
const KEY_DEFS = {
  Enter: { keyCode: 13, key: "Enter", code: "Enter", text: "\r" },
  Tab: { keyCode: 9, key: "Tab", code: "Tab" },
  Escape: { keyCode: 27, key: "Escape", code: "Escape" },
  Backspace: { keyCode: 8, key: "Backspace", code: "Backspace" },
  Space: { keyCode: 32, key: " ", code: "Space", text: " " },
  ArrowLeft: { keyCode: 37, key: "ArrowLeft", code: "ArrowLeft" },
  ArrowUp: { keyCode: 38, key: "ArrowUp", code: "ArrowUp" },
  ArrowRight: { keyCode: 39, key: "ArrowRight", code: "ArrowRight" },
  ArrowDown: { keyCode: 40, key: "ArrowDown", code: "ArrowDown" },
};

async function main() {
  let payload;
  try {
    payload = JSON.parse(await readStdin());
  } catch (err) {
    emit({ ok: false, error: { class: "script_error", message: `invalid payload JSON on stdin: ${err.message}` }, observations: {}, metrics: {} });
    return;
  }
  const browserWs = payload.browser_ws;
  const cdpPort = payload.cdp_port;
  const fixtureUrl = payload.task_url;
  const expectProduct = payload.expect_product || "";
  const expectUA = payload.expect_ua || "";
  const expectProductLive = payload.expect_product_live || expectProduct;
  let expectedRemoteIdentity = null;
  try {
    if (payload.remote_cdp === true) {
      expectedRemoteIdentity = requireRemoteIdentity(
        payload.expected_remote_identity,
        "expected_remote_identity"
      );
    }
  } catch (err) {
    emit({ ok: false, error: { class: "script_error", message: `binding gate: ${err.message}` }, observations: {}, metrics: {} });
    return;
  }
  const steps = payload.steps || [];
  const checks = payload.checks || [];
  const artifactDir = payload.artifact_dir || ".";
  const connectTimeoutMs = Number(payload.connect_timeout_ms || 15000);
  const actionTimeoutMs = Number(payload.action_timeout_ms || 8000);
  // Leave a 3s reserve for check evaluation and result emission.
  const budgetDeadline = Date.now() + Number(payload.task_timeout_ms || 30000) - 3000;
  if (!browserWs || !fixtureUrl) {
    emit({ ok: false, error: { class: "script_error", message: "payload requires browser_ws and task_url" }, observations: {}, metrics: {} });
    return;
  }

  fs.mkdirSync(artifactDir, { recursive: true });
  const cdpPath = path.join(artifactDir, "cdp.jsonl");
  fs.writeFileSync(cdpPath, "", "utf8");
  const trace = (obj) => {
    try {
      fs.appendFileSync(cdpPath, JSON.stringify(obj) + "\n", "utf8");
    } catch { /* trace failures must not fail the probe */ }
  };

  const fixtureOrigin = new URL(fixtureUrl).origin;
  const substitute = (raw) => {
    let text = String(raw);
    text = text.split("{fixture_url}").join(fixtureUrl);
    text = text.split("{fixture_origin}").join(fixtureOrigin);
    text = text.split("{fixture_host}").join(new URL(fixtureUrl).host);
    text = text.split("{artifact_dir}").join(artifactDir);
    return text;
  };

  // ---- Binding gate 1/2: HTTP identity of the endpoint, verbatim.
  const binding = { driver: "chrome_remote_interface", browser_ws: browserWs, expect_product: expectProduct, verified: false, gate: null };
  if (!cdpPort || !expectProduct) {
    emit({ ok: false, error: { class: "script_error", message: "binding gate: cdp_port and expect_product are required (refusing to run unverified)" }, observations: { binding }, metrics: {} });
    return;
  }
  let versionInfo;
  try {
    versionInfo = await httpJson(`http://127.0.0.1:${cdpPort}/json/version`, 4000);
  } catch (err) {
    emit({ ok: false, error: { class: "script_error", message: `binding gate: /json/version unreachable on port ${cdpPort}: ${err.message}` }, observations: { binding }, metrics: {} });
    return;
  }
  const httpProduct = String(versionInfo.Browser || versionInfo.Product || "");
  const httpUA = String(versionInfo["User-Agent"] || "");
  binding.http_product = httpProduct;
  if (httpProduct !== expectProduct || (expectUA && httpUA !== expectUA)) {
    emit({
      ok: false,
      error: {
        class: "script_error",
        message: `binding gate: endpoint on port ${cdpPort} reports product=${JSON.stringify(httpProduct)} ua=${JSON.stringify(httpUA)}; expected product=${JSON.stringify(expectProduct)} — refusing to run against an unverified engine`,
      },
      observations: { binding },
      metrics: {},
    });
    return;
  }
  const wsFromVersion = versionInfo.webSocketDebuggerUrl;
  if (wsFromVersion && wsFromVersion !== browserWs) {
    emit({ ok: false, error: { class: "script_error", message: `binding gate: browser_ws ${browserWs} != verified endpoint ${wsFromVersion}` }, observations: { binding }, metrics: {} });
    return;
  }
  binding.gate = "http_json_version";

  let opCalls = 0;
  let opErrors = 0;
  let client = null;
  let connectError = null;
  const clientVersion = require("chrome-remote-interface/package.json").version;
  try {
    client = await Promise.race([
      CDP({ target: browserWs, local: true }),
      waitUnref(connectTimeoutMs).then(() => {
        throw new Error(`connect timeout after ${connectTimeoutMs}ms`);
      }),
    ]);
  } catch (err) {
    connectError = String(err && err.message ? err.message : err).slice(0, 1000);
  }
  binding.client_version = clientVersion;
  trace({ ts: new Date().toISOString(), direction: "cri", step: "connect", ok: !connectError, error: connectError || undefined });

  const send = async (method, params, sessionId) => {
    opCalls += 1;
    try {
      const result = await client.send(method, params || {}, sessionId);
      trace({ ts: new Date().toISOString(), direction: "cri", method, sessionId: sessionId || undefined, ok: true });
      return result;
    } catch (err) {
      opErrors += 1;
      const message = String((err && err.response && err.response.message) || (err && err.message) || err);
      trace({ ts: new Date().toISOString(), direction: "cri", method, sessionId: sessionId || undefined, ok: false, error: message });
      const wrapped = new Error(`${method}: ${message}`);
      wrapped.cdpRejected = Boolean(err && err.response && err.response.code != null);
      throw wrapped;
    }
  };

  // ---- Binding gate 2/2: the live client transport must identify as the
  // engine under test.
  if (client) {
    let liveIdentity = null;
    let liveProduct = null;
    try {
      liveIdentity = await send("Browser.getVersion");
      liveProduct = String(liveIdentity.product || "");
    } catch { /* handled below: null !== expectProductLive */ }
    binding.expect_product_live = expectProductLive;
    binding.live_product = liveProduct;
    binding.live_check = "cri_browser_get_version";
    if (expectedRemoteIdentity) {
      Object.assign(binding, compareRemoteIdentity(expectedRemoteIdentity, liveIdentity));
    }
    const identityVerified = expectedRemoteIdentity
      ? binding.verified === true
      : liveProduct === expectProductLive;
    if (!identityVerified) {
      try {
        await client.close();
      } catch { /* best effort */ }
      emit({
        ok: false,
        error: {
          class: "script_error",
          message: expectedRemoteIdentity
            ? `binding gate: live chrome-remote-interface transport reports identity=${JSON.stringify(binding.actual)}; expected ${JSON.stringify(binding.expected)} — the client is not bound to the remote engine under test`
            : `binding gate: live chrome-remote-interface transport reports product=${JSON.stringify(liveProduct)}; expected ${JSON.stringify(expectProductLive)} — the client is not bound to the engine under test`,
        },
        observations: { binding },
        metrics: { cdp_call_count: opCalls, cdp_error_count: opErrors, ws_disconnect_count: 0 },
      });
      return;
    }
    binding.verified = true;
    trace({ ts: new Date().toISOString(), direction: "cri", step: "binding_verified", identity: expectedRemoteIdentity ? binding.actual : { product: liveProduct } });
  }

  const saved = {};
  const stepResults = [];
  const createdTargets = [];
  const targetCreations = [];
  let sessionId = null;

  const evalExpr = async (expression, opts) => {
    const params = Object.assign({ expression, returnByValue: true, awaitPromise: true }, opts || {});
    const result = await send("Runtime.evaluate", params, sessionId);
    if (result.exceptionDetails) {
      const detail = result.exceptionDetails;
      const desc = (detail.exception && (detail.exception.description || detail.exception.value)) || detail.text || "evaluation failed";
      throw new Error(String(desc).slice(0, 500));
    }
    return result.result ? result.result.value : undefined;
  };

  const selExpr = (sel, body) =>
    `(() => { const el = document.querySelector(${JSON.stringify(sel)}); if (!el) throw new Error("no element matches ${sel.replace(/"/g, '\\"')}"); ${body} })()`;

  const pollUntil = async (expression, timeoutMs, what) => {
    const deadline = Date.now() + timeoutMs;
    for (;;) {
      let value = null;
      try {
        value = await evalExpr(expression);
      } catch { /* evaluation context may be mid-navigation; retry */ }
      if (value) return value;
      if (Date.now() > deadline) throw new Error(`timeout after ${timeoutMs}ms waiting for ${what}`);
      await wait(50);
    }
  };

  const ensureSession = async () => {
    if (sessionId) return sessionId;
    const creation = {
      attempt: targetCreations.length + 1,
      state: "requested",
    };
    targetCreations.push(creation);
    let targetId;
    try {
      ({ targetId } = await send("Target.createTarget", { url: "about:blank" }));
    } catch (error) {
      creation.state = error && error.cdpRejected ? "rejected" : "ambiguous";
      creation.error = String(error && error.message ? error.message : error);
      throw error;
    }
    if (!targetId) {
      creation.state = "ambiguous";
      throw new Error("Target.createTarget returned no targetId");
    }
    creation.state = "created";
    creation.target_id = targetId;
    createdTargets.push(targetId);
    const attached = await send("Target.attachToTarget", { targetId, flatten: true });
    sessionId = attached.sessionId;
    await send("Page.enable", {}, sessionId);
    await send("Runtime.enable", {}, sessionId);
    return sessionId;
  };

  const axValue = (value) => {
    if (value && typeof value === "object" && Object.prototype.hasOwnProperty.call(value, "value")) {
      return value.value == null ? "" : String(value.value);
    }
    return value == null ? "" : String(value);
  };

  const fullAXTree = async () => {
    await send("Accessibility.enable", {}, sessionId);
    const result = await send("Accessibility.getFullAXTree", {}, sessionId);
    return Array.isArray(result.nodes) ? result.nodes : [];
  };

  const findAXIdentity = (nodes, role, name) => {
    const node = nodes.find((candidate) =>
      axValue(candidate.role) === role && axValue(candidate.name) === name
    );
    if (!node) throw new Error(`AX node role=${JSON.stringify(role)} name=${JSON.stringify(name)} not found`);
    if (node.backendDOMNodeId == null) {
      throw new Error(`AX node role=${JSON.stringify(role)} name=${JSON.stringify(name)} has no backendDOMNodeId`);
    }
    return `role=${role}|name=${name}|backendDOMNodeId=${node.backendDOMNodeId}`;
  };

  const formatComputedStyle = (step, computedStyle) => {
    const required = Array.isArray(step.required_properties) && step.required_properties.length
      ? step.required_properties.map(String)
      : ["display", "visibility", "opacity", "pointer-events"];
    const minimum = Number(step.min_property_count || 100);
    const properties = new Map(
      (Array.isArray(computedStyle) ? computedStyle : []).map((entry) => [String(entry.name), String(entry.value)])
    );
    const readable = required.every((name) => properties.has(name) && properties.get(name) !== "");
    const prefix = properties.size >= minimum && readable ? "breadth-ok" : "breadth-insufficient";
    const details = required.map((name) => `${name}=${properties.has(name) ? properties.get(name) : "<missing>"}`);
    return `${prefix}|count=${properties.size}|${details.join("|")}`;
  };

  const navigateAndSettle = async (url, timeoutMs) => {
    const nav = await send("Page.navigate", { url }, sessionId);
    if (nav && nav.errorText) throw new Error(`navigation failed: ${nav.errorText}`);
    const target = new URL(url);
    const wantPath = JSON.stringify(target.pathname + target.search);
    await pollUntil(
      `document.readyState === "complete" && (location.pathname + location.search) === ${wantPath}`,
      timeoutMs,
      `navigation to ${url}`
    );
  };

  const clickSelector = async (sel) => {
    const point = await evalExpr(
      selExpr(sel, `el.scrollIntoView({block: "center", inline: "center"}); const r = el.getBoundingClientRect(); return {x: r.x + r.width / 2, y: r.y + r.height / 2};`)
    );
    const base = { x: point.x, y: point.y, button: "left", clickCount: 1, pointerType: "mouse" };
    await send("Input.dispatchMouseEvent", Object.assign({ type: "mousePressed" }, base), sessionId);
    await send("Input.dispatchMouseEvent", Object.assign({ type: "mouseReleased" }, base), sessionId);
  };

  async function runOp(step) {
    const op = step.op;
    const sel = step.selector ? substitute(step.selector) : undefined;
    const budgetRemaining = budgetDeadline - Date.now();
    if (budgetRemaining <= 0) throw new Error("task budget exhausted before op could run");
    const timeout = Math.min(step.timeout_ms ? Number(step.timeout_ms) : actionTimeoutMs, budgetRemaining);

    if (op === "wait_ms") {
      await wait(Number(step.ms || 100));
      return undefined;
    }
    if (op === "version") {
      const ver = await send("Browser.getVersion");
      return ver.product;
    }
    if (op === "user_agent") {
      const ver = await send("Browser.getVersion");
      return ver.userAgent;
    }
    if (op === "new_page") {
      sessionId = null;
      await ensureSession();
      return "page_created";
    }

    await ensureSession();

    switch (op) {
      case "goto": {
        await navigateAndSettle(substitute(step.url || "{fixture_url}"), timeout);
        return "navigated";
      }
      case "reload": {
        await evalExpr("window.__abb_reload_probe = 1; 'marked'");
        await send("Page.reload", {}, sessionId);
        await pollUntil(
          `document.readyState === "complete" && !window.__abb_reload_probe`,
          timeout,
          "reload to settle"
        );
        return "reloaded";
      }
      case "go_back":
      case "go_forward": {
        const hist = await send("Page.getNavigationHistory", {}, sessionId);
        const idx = hist.currentIndex + (op === "go_back" ? -1 : 1);
        if (!hist.entries || idx < 0 || idx >= hist.entries.length) return "no_history";
        const navNonce = `np${Date.now()}${Math.floor(Math.random() * 1e6)}`;
        await evalExpr(`window.__abb_nav_probe = ${JSON.stringify(navNonce)} + "|" + location.href, "marked"`);
        await send("Page.navigateToHistoryEntry", { entryId: hist.entries[idx].id }, sessionId);
        await pollUntil(`document.readyState === "complete" && window.__abb_nav_probe !== ${JSON.stringify(navNonce)} + "|" + location.href`, timeout, op);
        return "ok";
      }
      case "click": {
        const times = Number(step.times || 1);
        for (let i = 0; i < times; i += 1) await clickSelector(sel);
        return `clicked x${times}`;
      }
      case "fill": {
        const value = substitute(step.value == null ? "" : step.value);
        await evalExpr(selExpr(sel, `el.focus(); if (typeof el.select === "function") el.select(); return "focused";`));
        await send("Input.insertText", { text: value }, sessionId);
        return "filled";
      }
      case "type": {
        const text = substitute(step.text == null ? "" : step.text);
        await evalExpr(selExpr(sel, `el.focus(); if (typeof el.setSelectionRange === "function") { const n = (el.value || "").length; el.setSelectionRange(n, n); } return "focused";`));
        await send("Input.insertText", { text }, sessionId);
        return "typed";
      }
      case "keyboard_type": {
        await send("Input.insertText", { text: substitute(step.text == null ? "" : step.text) }, sessionId);
        return "typed";
      }
      case "press": {
        const key = String(step.key || "");
        const def = KEY_DEFS[key] || (key.length === 1 ? { keyCode: key.toUpperCase().charCodeAt(0), key, code: `Key${key.toUpperCase()}`, text: key } : null);
        if (!def) throw new Error(`unsupported key ${JSON.stringify(key)} for press`);
        if (sel) await evalExpr(selExpr(sel, `el.focus(); return "focused";`));
        const down = { type: "keyDown", key: def.key, code: def.code, windowsVirtualKeyCode: def.keyCode, nativeVirtualKeyCode: def.keyCode };
        if (def.text) down.text = def.text;
        await send("Input.dispatchKeyEvent", down, sessionId);
        await send("Input.dispatchKeyEvent", { type: "keyUp", key: def.key, code: def.code, windowsVirtualKeyCode: def.keyCode, nativeVirtualKeyCode: def.keyCode }, sessionId);
        return `pressed ${key}`;
      }
      case "check": {
        const already = await evalExpr(selExpr(sel, `return !!el.checked;`));
        if (!already) await clickSelector(sel);
        return "checked";
      }
      case "select_option": {
        const value = substitute(step.value);
        return await evalExpr(
          selExpr(
            sel,
            `el.value = ${JSON.stringify(value)}; el.dispatchEvent(new Event("input", {bubbles: true})); el.dispatchEvent(new Event("change", {bubbles: true})); return [el.value];`
          )
        );
      }
      case "focus": {
        await evalExpr(selExpr(sel, `el.focus(); return "focused";`));
        return "focused";
      }
      case "evaluate": {
        return await evalExpr(substitute(step.expression));
      }
      case "wait_for_function": {
        await pollUntil(substitute(step.expression), timeout, "predicate");
        return "predicate_true";
      }
      case "wait_for_selector": {
        const visible = step.state === "visible";
        const hidden = step.state === "hidden" || step.state === "detached";
        let expression;
        if (hidden) {
          expression = `(() => { const el = document.querySelector(${JSON.stringify(sel)}); if (!el) return true; const s = window.getComputedStyle(el); return s.display === "none" || s.visibility === "hidden"; })()`;
        } else if (visible) {
          expression = `(() => { const el = document.querySelector(${JSON.stringify(sel)}); if (!el) return false; const s = window.getComputedStyle(el); return s.display !== "none" && s.visibility !== "hidden"; })()`;
        } else {
          expression = `!!document.querySelector(${JSON.stringify(sel)})`;
        }
        await pollUntil(expression, timeout, `selector ${sel}`);
        return "selector_ready";
      }
      case "text_content":
        return await evalExpr(selExpr(sel, `return el.textContent;`));
      case "inner_text":
        return await evalExpr(selExpr(sel, `return el.innerText;`));
      case "get_attribute":
        return await evalExpr(selExpr(sel, `return el.getAttribute(${JSON.stringify(step.name)});`));
      case "input_value":
        return await evalExpr(selExpr(sel, `return el.value;`));
      case "count":
        return await evalExpr(`document.querySelectorAll(${JSON.stringify(sel)}).length`);
      case "is_visible":
        return await evalExpr(
          `(() => { const el = document.querySelector(${JSON.stringify(sel)}); if (!el) return false; const s = window.getComputedStyle(el); return s.display !== "none" && s.visibility !== "hidden"; })()`
        );
      case "is_checked":
        return await evalExpr(selExpr(sel, `return !!el.checked;`));
      case "is_enabled":
        return await evalExpr(selExpr(sel, `return !el.disabled;`));
      case "title":
        return await evalExpr("document.title");
      case "url":
        return await evalExpr("location.href");
      case "ax_snapshot":
        return await fullAXTree();
      case "ax_node_identity": {
        const role = String(step.role || "");
        const name = substitute(step.name || "");
        const identity = findAXIdentity(await fullAXTree(), role, name);
        if (step.compare_to) {
          const before = saved[String(step.compare_to)];
          return before === identity
            ? `stable|${identity}`
            : `changed|before=${before}|after=${identity}`;
        }
        return identity;
      }
      case "computed_style_breadth": {
        const doc = await send("DOM.getDocument", { depth: 0 }, sessionId);
        const found = await send("DOM.querySelector", { nodeId: doc.root.nodeId, selector: sel }, sessionId);
        if (!found.nodeId) throw new Error(`no element matches ${sel}`);
        await send("CSS.enable", {}, sessionId);
        const result = await send("CSS.getComputedStyleForNode", { nodeId: found.nodeId }, sessionId);
        return formatComputedStyle(step, result.computedStyle);
      }
      default:
        throw new Error(`unknown op ${JSON.stringify(op)}`);
    }
  }

  async function cleanupCreatedTargets() {
    const attempts = [];
    for (const targetId of [...createdTargets]) {
      let closed = false;
      for (let attempt = 1; attempt <= 2 && !closed; attempt += 1) {
        let cleanupTimer;
        try {
          const closeOperation = send("Target.closeTarget", { targetId });
          closeOperation.catch(() => {});
          const result = await Promise.race([
            closeOperation,
            new Promise((_, reject) => {
              cleanupTimer = setTimeout(() => {
                const error = new Error("Target.closeTarget cleanup timeout");
                error.cleanupTimedOut = true;
                reject(error);
              }, 3000);
              if (cleanupTimer.unref) cleanupTimer.unref();
            }),
          ]);
          closed = result.success === true;
          attempts.push({ target_id: targetId, attempt, success: result.success, confirmed: closed });
        } catch (error) {
          attempts.push({
            target_id: targetId,
            attempt,
            confirmed: false,
            error: String(error && error.message ? error.message : error),
            ...(error && error.cleanupTimedOut ? { timed_out: true } : {}),
          });
          if (error && error.cleanupTimedOut) break;
        } finally {
          clearTimeout(cleanupTimer);
        }
      }
      const creation = targetCreations.find((entry) => entry.target_id === targetId);
      if (creation) creation.state = closed ? "closed" : "cleanup_unconfirmed";
      if (closed) createdTargets.splice(createdTargets.indexOf(targetId), 1);
    }
    const confirmed = targetCreations.every((entry) =>
      ["closed", "rejected"].includes(entry.state)
    );
    return {
      backend: "chrome_remote_interface.Target.closeTarget",
      required: targetCreations.length > 0,
      confirmed,
      same_connection_as_task: true,
      creation_attempts: targetCreations.map((entry) => ({ ...entry })),
      attempts,
    };
  }

  let outcome;
  try {
    if (connectError) {
      // A refused/failed connect is a genuine compatibility result: the engine
      // cannot be driven by this client. Grade every check as failed.
      const checkRows = [
        { name: "driver_connect", status: "fail", evidence: `chrome-remote-interface@${clientVersion} could not connect to ${browserWs}: ${connectError}` },
      ].concat(
        checks.map((check, idx) => ({
          name: check.label || check.kind || `check${idx}`,
          status: "fail",
          evidence: "client did not connect; scenario not executed",
        }))
      );
      outcome = {
        ok: true,
        answer: `0/${checkRows.length} checks`,
        observations: { checks: checkRows, saved: {}, binding, connect_error: connectError, failure_class: "cdp_semantic" },
        metrics: { cdp_call_count: 1, cdp_error_count: 1, ws_disconnect_count: 0 },
      };
    } else {
      for (let i = 0; i < steps.length; i += 1) {
        const step = steps[i];
        let result;
        try {
          const value = await runOp(step);
          result = { ok: true, value };
        } catch (err) {
          const message = String(err && err.message ? err.message : err).slice(0, 1000);
          result = { ok: false, error: message, unsupported: isUnsupportedMessage(message) };
        }
        stepResults.push(result);
        trace({ ts: new Date().toISOString(), direction: "cri", step: i, op: step.op, selector: step.selector, ok: result.ok, error: result.error || undefined });
        if (result.ok && step.expect_fail) result.unexpected_success = true;
        if (step.save_as) {
          saved[step.save_as] = toSavedString(result.ok ? result.value : `ERROR: ${result.error}`);
        }
      }

      const evaluateCheck = (check) => {
        const kind = check.kind;
        const stepAt = (idx) => stepResults[idx] || {};
        if (kind === "saved_equals") {
          const value = saved[check.name];
          return [value === String(check.expected), `${check.name}=${JSON.stringify(value)} expected=${JSON.stringify(String(check.expected))}`];
        }
        if (kind === "saved_contains") {
          const value = saved[check.name];
          const want = substitute(String(check.expected));
          return [typeof value === "string" && value.includes(want), `${check.name}=${JSON.stringify(value && value.slice(0, 300))} must contain ${JSON.stringify(want)}`];
        }
        if (kind === "saved_not_contains") {
          const value = saved[check.name];
          const want = substitute(String(check.expected));
          return [typeof value === "string" && !value.includes(want), `${check.name}=${JSON.stringify(value && value.slice(0, 300))} must NOT contain ${JSON.stringify(want)}`];
        }
        if (kind === "saved_truthy") {
          const value = saved[check.name];
          const truthy = value !== undefined && value !== "undefined" && value !== "" && value !== "null" && value !== "false" && !String(value).startsWith("ERROR:");
          return [truthy, `${check.name}=${JSON.stringify(value && String(value).slice(0, 300))}`];
        }
        if (kind === "step_ok") {
          const r = stepAt(check.step);
          return [!!r.ok, `step ${check.step} ok=${!!r.ok} error=${r.error || "none"}`];
        }
        if (kind === "step_fails") {
          const r = stepAt(check.step);
          return [r.ok === false, `step ${check.step} ok=${!!r.ok} (must fail) error=${r.error || "none"}`];
        }
        if (kind === "file_nonempty") {
          const filePath = substitute(check.path);
          let size = 0;
          try {
            size = fs.statSync(filePath).size;
          } catch {
            size = 0;
          }
          return [size > 0, `${filePath} size=${size}`];
        }
        if (kind === "any_of") {
          const results = (check.checks || []).map(evaluateCheck);
          return [results.some(([ok]) => ok), results.map(([ok, ev]) => `${ok ? "pass" : "fail"}: ${ev}`).join(" | ")];
        }
        return [false, `unknown check kind ${kind}`];
      };

      const checkRows = [
        { name: "driver_connect", status: "pass", evidence: `chrome-remote-interface@${clientVersion} bound to ${binding.live_product}` },
      ].concat(
        checks.map((check, idx) => {
          const [ok, evidence] = evaluateCheck(check);
          return { name: check.label || check.kind || `check${idx}`, status: ok ? "pass" : "fail", evidence };
        })
      );

      outcome = {
        ok: true,
        answer: saved.answer !== undefined ? saved.answer : `${checkRows.filter((c) => c.status === "pass").length}/${checkRows.length} checks`,
        observations: {
          checks: checkRows,
          saved,
          binding,
          driver_ops: stepResults.length,
          driver_op_errors: stepResults.filter((r) => !r.ok).length,
          failure_class: "cdp_semantic",
        },
        metrics: { cdp_call_count: opCalls, cdp_error_count: opErrors, ws_disconnect_count: 0 },
      };
    }
  } catch (err) {
    const msg = String(err && err.message ? err.message : err);
    const klass = isUnsupportedMessage(msg) ? "engine_unsupported" : "script_error";
    outcome = {
      ok: false,
      error: { class: klass, message: msg },
      observations: { saved, binding },
      metrics: { cdp_call_count: opCalls, cdp_error_count: opErrors, ws_disconnect_count: 0 },
    };
  } finally {
    const cleanup = await cleanupCreatedTargets();
    emit(applyCleanupContract(outcome, cleanup, "CRI"));
    if (client) {
      try {
        await client.close();
      } catch { /* best effort */ }
    }
  }
}

main().catch((err) => {
  emit({ ok: false, error: { class: "script_error", message: String(err && err.message ? err.message : err) }, observations: {}, metrics: {} });
});
