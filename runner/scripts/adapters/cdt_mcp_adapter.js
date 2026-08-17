#!/usr/bin/env node
"use strict";

// chrome-devtools-mcp scenario adapter.
//
// Drives the engine under test through the pinned `chrome-devtools-mcp` npm
// package — the Chrome DevTools team's MCP server. The adapter
// is an MCP *client*: it spawns the server with `--wsEndpoint <browser_ws>`
// (connect-only; the server never launches a browser in this mode), speaks
// MCP JSON-RPC 2.0 over the server's stdio, and maps the shared op vocabulary
// onto the server's tools:
//
//   goto → navigate_page          click/fill → click/fill by snapshot uid
//   observation ops → evaluate_script (results arrive as ```json fences)
//
// Selector→uid resolution is deterministic and uses only MCP tools:
// take_snapshot lists `uid=N_M role "name"` rows; one batched evaluate_script
// call receives every uid as an element argument and returns the index whose
// element matches the CSS selector.
//
// Speaks the abb_scenario_adapter/1 contract (see PROTOCOL.md): payload JSON
// on stdin, result JSON on stdout, mandatory two-layer binding gate. The MCP
// server exposes no raw-CDP tool, so the live-transport identity check reads
// `navigator.userAgent` through evaluate_script and compares it to the
// engine's /json/version User-Agent captured at launch.

const fs = require("fs");
const http = require("http");
const path = require("path");
const readline = require("readline");
const { spawn } = require("child_process");

const MCP_SERVER_BIN = path.join(__dirname, "..", "..", "..", "node_modules", "chrome-devtools-mcp", "build", "src", "bin", "chrome-devtools-mcp.js");

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

// MCP stdio client for one chrome-devtools-mcp server process.
class McpClient {
  constructor(argv, traceFn) {
    this.trace = traceFn;
    this.pending = new Map();
    this.nextId = 1;
    this.exited = false;
    this.exitError = null;
    this.stderrTail = [];
    this.server = spawn(process.execPath, argv, { stdio: ["pipe", "pipe", "pipe"] });
    this.server.on("exit", (code) => {
      this.exited = true;
      const why = new Error(`mcp server exited with code ${code}; stderr tail: ${this.stderrTail.join(" | ").slice(0, 500)}`);
      for (const [, rec] of this.pending) rec.reject(why);
      this.pending.clear();
    });
    this.server.stderr.on("data", (chunk) => {
      const text = String(chunk).trim();
      if (text) {
        this.stderrTail.push(text.slice(0, 200));
        if (this.stderrTail.length > 5) this.stderrTail.shift();
      }
    });
    this.rl = readline.createInterface({ input: this.server.stdout });
    this.rl.on("line", (line) => {
      let msg;
      try {
        msg = JSON.parse(line);
      } catch {
        return; // non-protocol output
      }
      if (msg.id !== undefined && this.pending.has(msg.id)) {
        const rec = this.pending.get(msg.id);
        this.pending.delete(msg.id);
        if (msg.error) rec.reject(new Error(`MCP ${msg.error.code}: ${msg.error.message}`));
        else rec.resolve(msg.result);
      }
    });
  }

  rpc(method, params, timeoutMs) {
    if (this.exited) return Promise.reject(new Error("mcp server already exited"));
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.server.stdin.write(JSON.stringify({ jsonrpc: "2.0", id, method, params }) + "\n");
      const timer = setTimeout(() => {
        if (this.pending.has(id)) {
          this.pending.delete(id);
          reject(new Error(`timeout after ${timeoutMs}ms on ${method}`));
        }
      }, timeoutMs || 30000);
      timer.unref();
    });
  }

  notify(method, params) {
    this.server.stdin.write(JSON.stringify({ jsonrpc: "2.0", method, params }) + "\n");
  }

  kill() {
    try {
      this.server.kill();
    } catch { /* best effort */ }
  }
}

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
  const binding = { driver: "chrome_devtools_mcp", browser_ws: browserWs, expect_product: expectProduct, verified: false, gate: null };
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

  const clientVersion = require("chrome-devtools-mcp/package.json").version;
  binding.client_version = clientVersion;
  let opCalls = 0;
  let opErrors = 0;

  const client = new McpClient([MCP_SERVER_BIN, "--wsEndpoint", browserWs], trace);
  let connectError = null;
  try {
    await client.rpc(
      "initialize",
      { protocolVersion: "2024-11-05", capabilities: {}, clientInfo: { name: "agent-browser-bench", version: "0.4" } },
      connectTimeoutMs
    );
    client.notify("notifications/initialized", {});
  } catch (err) {
    connectError = String(err && err.message ? err.message : err).slice(0, 1000);
  }
  trace({ ts: new Date().toISOString(), direction: "mcp", step: "initialize", ok: !connectError, error: connectError || undefined });

  const callTool = async (name, args, timeoutMs) => {
    opCalls += 1;
    let result;
    try {
      result = await client.rpc("tools/call", { name, arguments: args || {} }, timeoutMs || actionTimeoutMs + 25000);
    } catch (err) {
      opErrors += 1;
      trace({ ts: new Date().toISOString(), direction: "mcp", tool: name, ok: false, error: String(err.message).slice(0, 300) });
      throw err;
    }
    const text = (result.content || []).map((c) => (c && c.type === "text" ? c.text : "")).join("\n");
    trace({ ts: new Date().toISOString(), direction: "mcp", tool: name, ok: !result.isError, text: text.slice(0, 300) });
    if (result.isError) {
      opErrors += 1;
      throw new Error(`${name}: ${text.slice(0, 500)}`);
    }
    return text;
  };

  // evaluate_script responses embed the value as a ```json fence.
  const parseEvalResult = (text) => {
    const match = /```json\n([\s\S]*?)\n?```/.exec(text);
    if (!match) return undefined;
    try {
      return JSON.parse(match[1]);
    } catch {
      return match[1];
    }
  };

  const evalFn = async (fnSource, args) => {
    const text = await callTool("evaluate_script", args && args.length ? { function: fnSource, args } : { function: fnSource });
    return parseEvalResult(text);
  };

  const evalExpr = async (expression) =>
    // The tool takes a function; eval() of the raw program keeps
    // completion-value semantics so multi-statement expressions ("a; b; c")
    // stay legal, matching the raw Runtime.evaluate adapters.
    await evalFn(`() => eval(${JSON.stringify(expression)})`);

  // ---- Binding gate 2/2: the MCP server exposes no raw-CDP tool, so the
  // live-transport identity is the page's navigator.userAgent, which must
  // equal the /json/version User-Agent captured at engine launch.
  if (!connectError) {
    let liveUA = null;
    try {
      try {
        liveUA = String((await evalFn("() => navigator.userAgent")) || "");
      } catch (err) {
        // Engines whose targets the server cannot enumerate start with no
        // selected page; create one through the server and retry once.
        if (/No page selected/i.test(String(err && err.message))) {
          await callTool("new_page", { url: "about:blank" });
          liveUA = String((await evalFn("() => navigator.userAgent")) || "");
        } else {
          throw err;
        }
      }
    } catch (err) {
      connectError = `binding probe failed: ${String(err && err.message ? err.message : err).slice(0, 500)}`;
    }
    if (!connectError) {
      binding.expect_ua = expectUA;
      binding.live_user_agent = liveUA;
      binding.live_check = "mcp_evaluate_navigator_user_agent";
      if (expectUA && liveUA !== expectUA) {
        client.kill();
        emit({
          ok: false,
          error: {
            class: "script_error",
            message: `binding gate: live MCP session reports userAgent=${JSON.stringify(liveUA)}; expected ${JSON.stringify(expectUA)} — the server is not bound to the engine under test`,
          },
          observations: { binding },
          metrics: { cdp_call_count: opCalls, cdp_error_count: opErrors, ws_disconnect_count: 0 },
        });
        return;
      }
      binding.verified = true;
      trace({ ts: new Date().toISOString(), direction: "mcp", step: "binding_verified", ua: liveUA });
    }
  }

  const saved = {};
  const stepResults = [];

  const snapshotNodes = (snapshotText) => {
    const nodes = [];
    for (const line of String(snapshotText || "").split("\n")) {
      const match = /^\s*uid=(\S+)\s+(\S+)(?:\s+"([^"]*)")?/.exec(line);
      if (match) nodes.push({ uid: match[1], role: match[2], name: match[3] || "" });
    }
    return nodes;
  };

  const resolveUid = async (sel) => {
    const snapshotText = await callTool("take_snapshot", {});
    const uids = [...snapshotText.matchAll(/uid=(\d+_\d+)/g)].map((m) => m[1]);
    if (!uids.length) throw new Error(`no element matches ${sel} (snapshot is empty)`);
    try {
      const index = await evalFn(
        `(...els) => els.findIndex((el) => { try { return el && el.matches(${JSON.stringify(sel)}); } catch { return false; } })`,
        uids
      );
      if (typeof index !== "number" || index < 0) throw new Error(`no element matches ${sel} among ${uids.length} snapshot nodes`);
      return uids[index];
    } catch (err) {
      // When the page has iframes the snapshot mixes uids from several frames
      // and evaluate_script refuses them in one call ("Elements from
      // different frames can't be evaluated together"); probe uid-by-uid in
      // snapshot order instead, which preserves first-match semantics within
      // the main frame.
      if (!/different frames/i.test(String(err && err.message ? err.message : err))) throw err;
      for (const uid of uids) {
        let match = false;
        try {
          match = await evalFn(
            `(el) => { try { return !!(el && el.matches(${JSON.stringify(sel)})); } catch { return false; } }`,
            [uid]
          );
        } catch { /* stale uid mid-render; keep scanning */ }
        if (match === true) return uid;
      }
      throw new Error(`no element matches ${sel} among ${uids.length} snapshot nodes (per-frame scan)`);
    }
  };

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

  const settleNavigation = async (url, timeoutMs) => {
    const target = new URL(url);
    const wantPath = JSON.stringify(target.pathname + target.search);
    await pollUntil(
      `document.readyState === "complete" && (location.pathname + location.search) === ${wantPath}`,
      timeoutMs,
      `navigation to ${url}`
    );
  };

  async function runOp(step) {
    const op = step.op;
    const sel = step.selector ? substitute(step.selector) : undefined;
    const budgetRemaining = budgetDeadline - Date.now();
    if (budgetRemaining <= 0) throw new Error("task budget exhausted before op could run");
    const timeout = Math.min(step.timeout_ms ? Number(step.timeout_ms) : actionTimeoutMs, budgetRemaining);

    switch (op) {
      case "wait_ms":
        await wait(Number(step.ms || 100));
        return undefined;
      case "user_agent":
        return await evalFn("() => navigator.userAgent");
      case "new_page": {
        await callTool("new_page", { url: "about:blank" });
        return "page_created";
      }
      case "goto": {
        const url = substitute(step.url || "{fixture_url}");
        await callTool("navigate_page", { url, timeout: timeout + 5000 });
        await settleNavigation(url, timeout);
        return "navigated";
      }
      case "reload": {
        await evalExpr("window.__abb_reload_probe = 1, 'marked'");
        await callTool("navigate_page", { type: "reload", timeout: timeout + 5000 });
        await pollUntil(`document.readyState === "complete" && !window.__abb_reload_probe`, timeout, "reload to settle");
        return "reloaded";
      }
      case "go_back":
      case "go_forward": {
        const navNonce = `np${Date.now()}${Math.floor(Math.random() * 1e6)}`;
        await evalExpr(`window.__abb_nav_probe = ${JSON.stringify(navNonce)} + "|" + location.href, "marked"`);
        await callTool("navigate_page", { type: op === "go_back" ? "back" : "forward", timeout: timeout + 5000 });
        await pollUntil(`document.readyState === "complete" && window.__abb_nav_probe !== ${JSON.stringify(navNonce)} + "|" + location.href`, timeout, op);
        return "ok";
      }
      case "click": {
        const times = Number(step.times || 1);
        for (let i = 0; i < times; i += 1) {
          const uid = await resolveUid(sel);
          await callTool("click", { uid });
        }
        return `clicked x${times}`;
      }
      case "fill": {
        const uid = await resolveUid(sel);
        await callTool("fill", { uid, value: substitute(step.value == null ? "" : step.value) });
        return "filled";
      }
      case "type": {
        const uid = await resolveUid(sel);
        await callTool("fill", { uid, value: substitute(step.text == null ? "" : step.text) });
        return "typed";
      }
      case "press": {
        if (sel) {
          const uid = await resolveUid(sel);
          await callTool("click", { uid });
        }
        await callTool("press_key", { key: step.key });
        return `pressed ${step.key}`;
      }
      case "check": {
        const already = await evalExpr(`!!document.querySelector(${JSON.stringify(sel)}).checked`);
        if (!already) {
          const uid = await resolveUid(sel);
          await callTool("click", { uid });
        }
        return "checked";
      }
      case "select_option": {
        // The MCP model selects options by clicking the <option> node's uid
        // (role=option routes through the server's selectNativeSelectOption);
        // fill on the <select> itself expects typeable input and fails.
        const value = substitute(step.value);
        const optionSel = `${sel} option[value="${value.replace(/"/g, '\\"')}"]`;
        const uid = await resolveUid(optionSel);
        await callTool("click", { uid });
        return await evalExpr(`[document.querySelector(${JSON.stringify(sel)}).value]`);
      }
      case "focus":
        await evalExpr(`document.querySelector(${JSON.stringify(sel)}).focus(), "focused"`);
        return "focused";
      case "evaluate":
        return await evalExpr(substitute(step.expression));
      case "wait_for_function":
        await pollUntil(substitute(step.expression), timeout, "predicate");
        return "predicate_true";
      case "wait_for_selector": {
        const quoted = JSON.stringify(sel);
        const state = step.state;
        let expression;
        if (state === "hidden" || state === "detached") {
          expression = `(() => { const el = document.querySelector(${quoted}); if (!el) return true; const s = window.getComputedStyle(el); return s.display === "none" || s.visibility === "hidden"; })()`;
        } else if (state === "visible") {
          expression = `(() => { const el = document.querySelector(${quoted}); if (!el) return false; const s = window.getComputedStyle(el); return s.display !== "none" && s.visibility !== "hidden"; })()`;
        } else {
          expression = `!!document.querySelector(${quoted})`;
        }
        await pollUntil(expression, timeout, `selector ${sel}`);
        return "selector_ready";
      }
      case "text_content":
        return await evalExpr(`(() => { const el = document.querySelector(${JSON.stringify(sel)}); if (!el) throw new Error("no element matches ${sel.replace(/"/g, '\\"')}"); return el.textContent; })()`);
      case "inner_text":
        return await evalExpr(`document.querySelector(${JSON.stringify(sel)}).innerText`);
      case "get_attribute":
        return await evalExpr(`document.querySelector(${JSON.stringify(sel)}).getAttribute(${JSON.stringify(step.name)})`);
      case "input_value":
        return await evalExpr(`document.querySelector(${JSON.stringify(sel)}).value`);
      case "count":
        return await evalExpr(`document.querySelectorAll(${JSON.stringify(sel)}).length`);
      case "is_visible":
        return await evalExpr(
          `(() => { const el = document.querySelector(${JSON.stringify(sel)}); if (!el) return false; const s = window.getComputedStyle(el); return s.display !== "none" && s.visibility !== "hidden"; })()`
        );
      case "is_checked":
        return await evalExpr(`!!document.querySelector(${JSON.stringify(sel)}).checked`);
      case "is_enabled":
        return await evalExpr(`!document.querySelector(${JSON.stringify(sel)}).disabled`);
      case "title":
        return await evalExpr("document.title");
      case "url":
        return await evalExpr("location.href");
      case "ax_snapshot":
      case "aria_snapshot":
        return await callTool("take_snapshot", { verbose: true });
      case "ax_node_identity": {
        const role = String(step.role || "");
        const name = substitute(step.name || "");
        const snapshot = await callTool("take_snapshot", { verbose: true });
        const node = snapshotNodes(snapshot).find((candidate) =>
          candidate.role === role && candidate.name === name
        );
        if (!node) throw new Error(`AX node role=${JSON.stringify(role)} name=${JSON.stringify(name)} not found in take_snapshot`);
        const identity = `role=${role}|name=${name}|uid=${node.uid}`;
        if (step.compare_to) {
          const before = saved[String(step.compare_to)];
          return before === identity
            ? `stable|${identity}`
            : `changed|before=${before}|after=${identity}`;
        }
        return identity;
      }
      default:
        throw new Error(`unknown op ${JSON.stringify(op)}`);
    }
  }

  try {
    if (connectError) {
      // A refused/failed connect is a genuine compatibility result: the engine
      // cannot be driven by this client. Grade every check as failed.
      const checkRows = [
        { name: "driver_connect", status: "fail", evidence: `chrome-devtools-mcp@${clientVersion} could not drive ${browserWs}: ${connectError}` },
      ].concat(
        checks.map((check, idx) => ({
          name: check.label || check.kind || `check${idx}`,
          status: "fail",
          evidence: "MCP server did not connect; scenario not executed",
        }))
      );
      emit({
        ok: true,
        answer: `0/${checkRows.length} checks`,
        observations: { checks: checkRows, saved: {}, binding, connect_error: connectError, failure_class: "cdp_semantic" },
        metrics: { cdp_call_count: opCalls || 1, cdp_error_count: opErrors || 1, ws_disconnect_count: 0 },
      });
      return;
    }

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
      trace({ ts: new Date().toISOString(), direction: "mcp", step: i, op: step.op, selector: step.selector, ok: result.ok, error: result.error || undefined });
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
      { name: "driver_connect", status: "pass", evidence: `chrome-devtools-mcp@${clientVersion} bound (ua match) to ${binding.http_product}` },
    ].concat(
      checks.map((check, idx) => {
        const [ok, evidence] = evaluateCheck(check);
        return { name: check.label || check.kind || `check${idx}`, status: ok ? "pass" : "fail", evidence };
      })
    );

    emit({
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
    });
  } catch (err) {
    const msg = String(err && err.message ? err.message : err);
    const klass = isUnsupportedMessage(msg) ? "engine_unsupported" : "script_error";
    emit({
      ok: false,
      error: { class: klass, message: msg },
      observations: { saved, binding },
      metrics: { cdp_call_count: opCalls, cdp_error_count: opErrors, ws_disconnect_count: 0 },
    });
  } finally {
    client.kill();
    // The MCP server child and readline keep the loop alive; the result is
    // already on stdout, so end the process deterministically.
    setTimeout(() => process.exit(0), 100).unref();
  }
}

main().catch((err) => {
  emit({ ok: false, error: { class: "script_error", message: String(err && err.message ? err.message : err) }, observations: {}, metrics: {} });
});
