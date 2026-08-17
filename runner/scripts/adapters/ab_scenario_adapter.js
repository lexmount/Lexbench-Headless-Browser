#!/usr/bin/env node
"use strict";

// agent-browser scenario adapter.
//
// Drives the engine under test through the pinned `agent-browser` CLI — the
// agent-facing tool surface this benchmark is ultimately for.
// Speaks the abb_scenario_adapter/1 contract (PROTOCOL.md in this directory)
// and maps the shared op vocabulary onto agent-browser commands:
//
//   goto → open      click → click      fill → fill      check → check
//   select_option → select              observation → get text/value/count…
//   evaluate → eval                     wait_for_selector → wait <sel>
//
// One isolated daemon session per attempt (l1_ab_probe.js discipline). NEVER
// run commands without an explicit prior `connect <target>`: on a dangling
// session agent-browser silently auto-launches its own browser — the hidden
// fallback this bench forbids. Local engines connect through their discovery
// port; an explicitly marked remote-CDP run connects to the exact browser WSS.
// The `lifecycle.launched` fields in its JSON refer to daemon bootstrap, not
// to a browser launch.
//
// The CLI exposes no raw-CDP call. For local engines, explicit connect,
// `get cdp-url`, and a successful page eval remain the audited route. For a
// remote browser WebSocket these observations cannot prove that
// Browser.getVersion and the task ran on one backend connection: URL equality
// and navigator.userAgent are not engine identity. Remote attempts therefore
// fail closed as unverified until agent-browser exposes a live control-plane
// identity operation.

const { execFile } = require("child_process");
const crypto = require("crypto");
const fs = require("fs");
const http = require("http");
const path = require("path");

const BIN = process.env.AB_BIN || "agent-browser";

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
  const remoteCdp = payload.remote_cdp === true;
  const taskTimeoutMs = Number(payload.task_timeout_ms || 30000);
  // Leave a 3s reserve for check evaluation and result emission.
  const budgetDeadline = Date.now() + taskTimeoutMs - 3000;
  if (!browserWs || !fixtureUrl || !cdpPort) {
    emit({ ok: false, error: { class: "script_error", message: "payload requires browser_ws, cdp_port and task_url" }, observations: {}, metrics: {} });
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

  // One isolated daemon session per attempt.
  const digest = crypto
    .createHash("sha256")
    .update(`${payload.run_id}\0${payload.task_id}\0${payload.engine}\0${payload.attempt}`)
    .digest("hex")
    .slice(0, 10);
  const session = `abbsc-${String(payload.task_id || "task").replace(/[^0-9A-Za-z]+/g, "_").slice(0, 24)}-${digest}`;

  let abCalls = 0;
  let abErrors = 0;

  const runAB = (argv, timeoutMs) => {
    abCalls += 1;
    const remaining = budgetDeadline - Date.now();
    if (remaining <= 0) {
      abErrors += 1;
      return Promise.resolve({ ok: false, error: "task budget exhausted before op could run", data: null });
    }
    const budgeted = Math.min(timeoutMs || actionTimeoutMs + 15000, remaining);
    const fullArgs = ["--session", session, "--json", ...argv];
    return new Promise((resolve) => {
      execFile(BIN, fullArgs, { timeout: budgeted, maxBuffer: 16 * 1024 * 1024 }, (err, stdout, stderr) => {
        let json = null;
        const text = (stdout || "").trim();
        if (text) {
          try {
            json = JSON.parse(text);
          } catch {
            const line = text.split("\n").reverse().find((l) => l.trim().startsWith("{"));
            if (line) {
              try { json = JSON.parse(line); } catch { /* leave null */ }
            }
          }
        }
        const ok = !!(json && json.success === true);
        if (!ok) abErrors += 1;
        const result = {
          ok,
          timedOut: !!(err && err.killed),
          data: json && json.data ? json.data : null,
          error: json && json.error ? String(json.error) : (err && !json ? String(err.message || err) : null),
        };
        trace({ ts: new Date().toISOString(), direction: "ab", argv: argv.slice(0, 4), ok, error: result.error || undefined });
        resolve(result);
      });
    });
  };

  // Cleanup must not share the functional task budget.  In particular, a
  // timed-out last operation used to make runAB() return early without ever
  // issuing `close`, leaving a daemon behind for the rest of the benchmark.
  const runABCleanup = (timeoutMs) => {
    const fullArgs = ["--session", session, "--json", "close"];
    return new Promise((resolve) => {
      execFile(BIN, fullArgs, { timeout: timeoutMs || 8000, maxBuffer: 1024 * 1024 }, (err, stdout) => {
        const text = (stdout || "").trim();
        let json = null;
        let parseError = null;
        try {
          json = text ? JSON.parse(text) : null;
        } catch (error) {
          parseError = error;
        }
        // Process status alone cannot prove that the named daemon session was
        // closed. JSON mode must return one complete object that explicitly
        // acknowledges success; empty, noisy, malformed, array, and primitive
        // output all leave cleanup unconfirmed.
        const validObject = Boolean(json && typeof json === "object" && !Array.isArray(json));
        const ok = !err && validObject && json.success === true;
        let cleanupError = null;
        if (err) {
          cleanupError = String(err.message || err);
        } else if (!text) {
          cleanupError = "agent-browser close returned empty JSON output";
        } else if (parseError) {
          cleanupError = `agent-browser close returned malformed JSON: ${parseError.message}`;
        } else if (!validObject) {
          cleanupError = "agent-browser close returned a non-object JSON value";
        } else if (json.success !== true) {
          cleanupError = "agent-browser close did not acknowledge success=true";
        }
        trace({
          ts: new Date().toISOString(),
          direction: "ab_cleanup",
          argv: ["close"],
          ok,
          error: cleanupError || undefined,
        });
        resolve(ok);
      });
    });
  };

  // A binding exclusion returns before scenario operations start, but it may
  // already own a live agent-browser daemon connection.  Close that exact
  // named session before emitting the exclusion so the orchestrator can
  // distinguish a safely isolated exclusion from an abandoned attempt.
  let cleanupPromise = null;
  const closeAttemptSession = () => {
    if (!cleanupPromise) {
      cleanupPromise = runABCleanup(8000).catch(() => false);
    }
    return cleanupPromise;
  };

  const abOrThrow = async (argv, timeoutMs) => {
    const result = await runAB(argv, timeoutMs);
    if (!result.ok) throw new Error(`${argv[0]}: ${result.error || (result.timedOut ? "timed out" : "failed")}`);
    return result;
  };

  const evalExpr = async (expression) => {
    const result = await abOrThrow(["eval", expression]);
    return result.data ? result.data.result : undefined;
  };

  // ---- Binding gate 1/2: HTTP identity of the endpoint, verbatim.
  const binding = { driver: "agent_browser", browser_ws: browserWs, expect_product: expectProduct, verified: false, gate: null };
  if (!expectProduct) {
    emit({ ok: false, error: { class: "script_error", message: "binding gate: expect_product is required (refusing to run unverified)" }, observations: { binding }, metrics: {} });
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

  let clientVersion = "unknown";
  try {
    clientVersion = require(path.join(__dirname, "..", "..", "..", "node_modules", "agent-browser", "package.json")).version;
  } catch { /* version display only */ }
  binding.client_version = clientVersion;

  // ---- Connect (explicit; never let a command auto-launch).
  const connectTarget = remoteCdp ? browserWs : String(cdpPort);
  binding.connect_target_kind = remoteCdp ? "browser_ws" : "cdp_port";
  const bound = await runAB(["connect", connectTarget], connectTimeoutMs);
  let connectError = bound.ok ? null : String(bound.error || "connect failed").slice(0, 1000);
  const bindingExcluded = remoteCdp;
  if (bindingExcluded) {
    binding.excluded = true;
    binding.exclusion_reason =
      "agent-browser does not expose Browser.getVersion on its connected session";
  }

  // ---- Binding gate 2/2: the daemon must report the exact runner-owned CDP
  // endpoint and the explicitly connected session must execute a live page
  // command. UA is evidence about the page, not engine identity.
  if (!connectError) {
    const cdpUrlResult = await runAB(["get", "cdp-url"]);
    const liveCdpUrl = String(
      (cdpUrlResult.data && cdpUrlResult.data.cdpUrl) || ""
    );
    binding.live_cdp_url = liveCdpUrl;
    if (!cdpUrlResult.ok) {
      connectError = `binding probe failed: get cdp-url: ${String(cdpUrlResult.error || "failed").slice(0, 500)}`;
    } else if (!liveCdpUrl) {
      connectError = "binding probe failed: agent-browser returned an empty cdpUrl";
    } else if (liveCdpUrl !== browserWs) {
      connectError = `binding probe failed: agent-browser reports cdpUrl=${JSON.stringify(liveCdpUrl)}; expected ${JSON.stringify(browserWs)}`;
    }
  }
  if (!connectError && remoteCdp) {
    binding.gate = "remote_live_identity_unavailable";
    binding.live_check = "unavailable";
    connectError =
      "binding unverified: agent-browser can report the configured CDP URL but cannot expose Browser.getVersion on the live connected session";
  }
  if (!connectError) {
    let liveUA = null;
    try {
      liveUA = String((await evalExpr("navigator.userAgent")) || "");
    } catch (err) {
      connectError = `binding probe failed: ${String(err && err.message ? err.message : err).slice(0, 500)}`;
    }
    if (!connectError && !liveUA) {
      connectError = "binding probe failed: live agent-browser session returned an empty navigator.userAgent";
    }
    if (!connectError) {
      binding.expect_ua = expectUA;
      binding.live_user_agent = liveUA;
      binding.live_cdp_port = Number(cdpPort);
      binding.user_agent_matches_http = !expectUA || liveUA === expectUA;
      binding.live_check = "agent_browser_connect_then_get_cdp_url_then_eval";
      binding.verified = true;
      trace({
        ts: new Date().toISOString(),
        direction: "ab",
        step: "binding_verified",
        cdp_port: Number(cdpPort),
        cdp_url: binding.live_cdp_url,
        ua: liveUA,
        ua_matches_http: binding.user_agent_matches_http,
      });
    }
  }

  const saved = {};
  const stepResults = [];

  const pollUntil = async (expression, timeoutMs, what) => {
    const deadline = Date.now() + timeoutMs;
    for (;;) {
      let value = null;
      try {
        value = await evalExpr(expression);
      } catch { /* evaluation context may be mid-navigation; retry */ }
      if (value) return value;
      if (Date.now() > deadline || Date.now() > budgetDeadline) {
        throw new Error(`timeout after ${timeoutMs}ms waiting for ${what}`);
      }
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
    const timeout = step.timeout_ms ? Number(step.timeout_ms) : actionTimeoutMs;

    switch (op) {
      case "wait_ms":
        await wait(Number(step.ms || 100));
        return undefined;
      case "user_agent":
        return await evalExpr("navigator.userAgent");
      case "new_page":
        // `tab new` is the tool's real multi-target surface. `open` would
        // only navigate the active tab and let multi-tab scenarios pass
        // without ever exercising Target.createTarget.
        await abOrThrow(["tab", "new", "about:blank"]);
        return "page_created";
      case "goto": {
        const url = substitute(step.url || "{fixture_url}");
        await abOrThrow(["open", url], timeout + 15000);
        await settleNavigation(url, timeout);
        return "navigated";
      }
      case "reload": {
        await evalExpr("window.__abb_reload_probe = 1, 'marked'");
        await abOrThrow(["reload"], timeout + 15000);
        await pollUntil(`document.readyState === "complete" && !window.__abb_reload_probe`, timeout, "reload to settle");
        return "reloaded";
      }
      case "go_back":
      case "go_forward": {
        const navNonce = `np${Date.now()}${Math.floor(Math.random() * 1e6)}`;
        await evalExpr(`window.__abb_nav_probe = ${JSON.stringify(navNonce)} + "|" + location.href, "marked"`);
        await abOrThrow([op === "go_back" ? "back" : "forward"], timeout + 15000);
        await pollUntil(`document.readyState === "complete" && window.__abb_nav_probe !== ${JSON.stringify(navNonce)} + "|" + location.href`, timeout, op);
        return "ok";
      }
      case "click": {
        const times = Number(step.times || 1);
        for (let i = 0; i < times; i += 1) await abOrThrow(["click", sel]);
        return `clicked x${times}`;
      }
      case "fill":
        await abOrThrow(["fill", sel, substitute(step.value == null ? "" : step.value)]);
        return "filled";
      case "type":
        await abOrThrow(["type", sel, substitute(step.text == null ? "" : step.text)]);
        return "typed";
      case "keyboard_type":
        await abOrThrow(["keyboard", "type", substitute(step.text == null ? "" : step.text)]);
        return "typed";
      case "press": {
        if (sel) await abOrThrow(["focus", sel]);
        await abOrThrow(["press", step.key]);
        return `pressed ${step.key}`;
      }
      case "check":
        await abOrThrow(["check", sel]);
        return "checked";
      case "select_option": {
        const value = substitute(step.value);
        await abOrThrow(["select", sel, value]);
        return await evalExpr(`[document.querySelector(${JSON.stringify(sel)}).value]`);
      }
      case "focus":
        await abOrThrow(["focus", sel]);
        return "focused";
      case "evaluate":
        return await evalExpr(substitute(step.expression));
      case "wait_for_function":
        await pollUntil(substitute(step.expression), timeout, "predicate");
        return "predicate_true";
      case "wait_for_selector":
        await abOrThrow(["wait", sel], timeout + 15000);
        return "selector_ready";
      case "text_content": {
        const result = await abOrThrow(["get", "text", sel]);
        return result.data ? result.data.text : undefined;
      }
      case "inner_text":
        return await evalExpr(`document.querySelector(${JSON.stringify(sel)}).innerText`);
      case "get_attribute":
        return await evalExpr(`document.querySelector(${JSON.stringify(sel)}).getAttribute(${JSON.stringify(step.name)})`);
      case "input_value": {
        const result = await abOrThrow(["get", "value", sel]);
        return result.data ? result.data.value : undefined;
      }
      case "count": {
        const result = await abOrThrow(["get", "count", sel]);
        return result.data ? result.data.count : undefined;
      }
      case "is_visible": {
        const result = await runAB(["is", "visible", sel]);
        return !!(result.ok && result.data && result.data.visible);
      }
      case "is_checked": {
        const result = await abOrThrow(["is", "checked", sel]);
        return !!(result.data && result.data.checked);
      }
      case "is_enabled": {
        const result = await abOrThrow(["is", "enabled", sel]);
        return !!(result.data && result.data.enabled);
      }
      case "title": {
        const result = await abOrThrow(["get", "title"]);
        return result.data ? result.data.title : undefined;
      }
      case "url": {
        const result = await abOrThrow(["get", "url"]);
        return result.data ? result.data.url : undefined;
      }
      case "ax_snapshot":
      case "aria_snapshot": {
        const result = await abOrThrow(["snapshot"]);
        if (!result.data) return undefined;
        return result.data.snapshot === undefined ? result.data : result.data.snapshot;
      }
      default:
        throw new Error(`unknown op ${JSON.stringify(op)}`);
    }
  }

  try {
    if (connectError) {
      if (bindingExcluded) {
        const sessionClosed = await closeAttemptSession();
        emit({
          ok: false,
          error: {
            class: "script_error",
            message:
              `remote agent-browser attempt excluded because live engine identity cannot be verified: ${connectError}`,
          },
          observations: {
            binding,
            connect_error: connectError,
            failure_class: "binding_unverified",
            formal_score_eligible: false,
            binding_exclusion_isolation: {
              schema: "abb.binding_exclusion_isolation.v1",
              driver: "agent_browser",
              phase: "driver_session_closed",
              scenario_started: false,
              target_creation_requested: false,
              cleanup: {
                backend: "agent_browser_named_session_close",
                required: true,
                confirmed: sessionClosed === true,
                same_named_session_as_attempt: true,
                session,
              },
            },
            isolation_restored: sessionClosed === true,
          },
          metrics: {
            cdp_call_count: abCalls,
            cdp_error_count: abErrors,
            ws_disconnect_count: 0,
          },
        });
        return;
      }
      // A refused/failed connect is a genuine compatibility result: the engine
      // cannot be driven by this client. Grade every check as failed.
      const checkRows = [
        { name: "driver_connect", status: "fail", evidence: `agent-browser@${clientVersion} could not bind ${binding.connect_target_kind} ${connectTarget}: ${connectError}` },
      ].concat(
        checks.map((check, idx) => ({
          name: check.label || check.kind || `check${idx}`,
          status: "fail",
          evidence: "agent-browser did not connect; scenario not executed",
        }))
      );
      emit({
        ok: true,
        answer: `0/${checkRows.length} checks`,
        observations: { checks: checkRows, saved: {}, binding, connect_error: connectError, failure_class: "cdp_semantic" },
        metrics: { cdp_call_count: abCalls || 1, cdp_error_count: abErrors || 1, ws_disconnect_count: 0 },
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
      trace({ ts: new Date().toISOString(), direction: "ab", step: i, op: step.op, selector: step.selector, ok: result.ok, error: result.error || undefined });
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
      {
        name: "driver_connect",
        status: "pass",
        evidence:
          `agent-browser@${clientVersion} bound to ${binding.live_cdp_url}; ` +
          `page_ua_matches_http=${binding.user_agent_matches_http}`,
      },
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
      metrics: { cdp_call_count: abCalls, cdp_error_count: abErrors, ws_disconnect_count: 0 },
    });
  } catch (err) {
    const msg = String(err && err.message ? err.message : err);
    const klass = isUnsupportedMessage(msg) ? "engine_unsupported" : "script_error";
    emit({
      ok: false,
      error: { class: klass, message: msg },
      observations: { saved, binding },
      metrics: { cdp_call_count: abCalls, cdp_error_count: abErrors, ws_disconnect_count: 0 },
    });
  } finally {
    // Tear down the daemon session (never the engine: close ends the
    // agent-browser session; the engine outlives it — verified).
    await closeAttemptSession();
  }
}

main().catch((err) => {
  emit({ ok: false, error: { class: "script_error", message: String(err && err.message ? err.message : err) }, observations: {}, metrics: {} });
});
