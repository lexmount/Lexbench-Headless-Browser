#!/usr/bin/env node
"use strict";

// Stagehand scenario adapter.
//
// Drives the engine under test with the pinned `@browserbasehq/stagehand`
// npm package's deterministic surface only: `page.goto`,
// `page.locator(css).click/fill/selectOption/...`, `page.evaluate`. The
// LLM-driven face (act/extract/observe/agent) is out of the benchmark's
// deterministic scope and is never called — no model client is configured.
//
// Speaks the abb_scenario_adapter/1 contract (see PROTOCOL.md): payload JSON
// on stdin, result JSON on stdout, mandatory two-layer binding gate. Stagehand
// v3 connects to the engine's browser websocket via
// `{env: "LOCAL", localBrowserLaunchOptions: {cdpUrl}}`; it drives CDP
// directly (no bundled Playwright) and has been verified not to fallback-
// launch a browser when the endpoint is unreachable.

const fs = require("fs");
const http = require("http");
const path = require("path");
const {
  compareRemoteIdentity,
  requireRemoteIdentity,
} = require("../lib/remote_identity");
const { applyCleanupContract } = require("../lib/remote_cleanup");
const { selectStagehandInitOwnedPages } = require("../lib/stagehand_ownership");

// Stagehand's internal logger writes DEBUG lines (e.g. "css pierce-fallback"
// when a selector misses) directly to process.stdout even with verbose: 0,
// which would corrupt the single-JSON-object result contract. Divert every
// stdout write to stderr and reserve the real stdout for the final emit.
const realStdoutWrite = process.stdout.write.bind(process.stdout);
process.stdout.write = (chunk, encoding, callback) => process.stderr.write(chunk, encoding, callback);

let resultEmitted = false;

function emit(obj) {
  resultEmitted = true;
  realStdoutWrite(JSON.stringify(obj));
}

// Stagehand can crash from event-loop callbacks that no op-level try/catch
// reaches (observed: frameRegistry.collect infinite recursion — a stack
// overflow — when an engine reports a cyclic frame tree). After the binding
// gate has verified the engine, such a crash is the engine driving the
// client into the ground: a benchmark result (cdp_semantic), not infra.
const crashState = { binding: null, checks: [], saved: {} };

function handleAsyncCrash(err) {
  const msg = String(err && (err.stack || err.message) ? err.message || err : err).slice(0, 1000);
  if (resultEmitted) process.exit(0);
  if (crashState.binding && crashState.binding.verified) {
    const checkRows = [
      { name: "driver_connect", status: "pass", evidence: `bound, then stagehand crashed: ${msg}` },
    ].concat(
      crashState.checks.map((check, idx) => ({
        name: check.label || check.kind || `check${idx}`,
        status: "fail",
        evidence: `stagehand crashed mid-scenario (engine drove the client into a fatal state): ${msg}`,
      }))
    );
    const primaryOutcome = {
      ok: true,
      answer: `1/${checkRows.length} checks`,
      observations: {
        checks: checkRows,
        saved: crashState.saved,
        binding: crashState.binding,
        transport_crash: msg,
        failure_class: "cdp_semantic",
      },
      metrics: { cdp_call_count: 1, cdp_error_count: 1, ws_disconnect_count: 1 },
    };
    emit(applyCleanupContract(primaryOutcome, {
      backend: "stagehand_page.close",
      required: true,
      confirmed: false,
      same_connection_as_task: true,
      error: "async crash prevented confirmed target cleanup",
    }, "Stagehand"));
    process.exit(0);
  }
  emit({ ok: false, error: { class: "script_error", message: `async crash before binding verified: ${msg}` }, observations: { binding: crashState.binding || {} }, metrics: {} });
  process.exit(1);
}

process.on("uncaughtException", handleAsyncCrash);
process.on("unhandledRejection", handleAsyncCrash);

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

// Same delay, but the timer does not keep the event loop alive. Use this for
// the losing side of a Promise.race deadline: that timer stays armed for its
// full duration, and a ref'd one would hold the process open long after the
// result has been emitted.
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
  return /not found|wasn't found|unsupported|unknown method|not implemented|not supported|is not a function/i.test(String(msg || ""));
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
  crashState.checks = checks;
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
  const binding = { driver: "stagehand", browser_ws: browserWs, expect_product: expectProduct, verified: false, gate: null };
  crashState.binding = binding;
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

  const clientVersion = require("@browserbasehq/stagehand/package.json").version;
  let stagehand = null;
  let connectError = null;
  try {
    const { Stagehand } = require("@browserbasehq/stagehand");
    stagehand = new Stagehand({
      env: "LOCAL",
      localBrowserLaunchOptions: { cdpUrl: browserWs },
      verbose: 0,
      disablePino: true,
    });
    await Promise.race([
      stagehand.init(),
      waitUnref(connectTimeoutMs).then(() => {
        throw new Error(`connect timeout after ${connectTimeoutMs}ms`);
      }),
    ]);
  } catch (err) {
    connectError = String(err && err.message ? err.message : err).slice(0, 1000);
  }
  binding.client_version = clientVersion;
  trace({ ts: new Date().toISOString(), direction: "stagehand", step: "connect", ok: !connectError, error: connectError || undefined });

  let opCalls = 0;
  let opErrors = 0;
  const saved = {};
  crashState.saved = saved;
  const stepResults = [];
  const createdPages = [];
  const pageCreations = [];
  let page = null;
  if (connectError && stagehand) {
    // Stagehand.init may create a target before its promise rejects or times
    // out. Its high-level error does not expose a target id, so continuing the
    // experiment would be unsafe even after disconnecting the client.
    pageCreations.push({
      attempt: 0,
      state: "ambiguous",
      source: "stagehand.init",
      error: connectError,
    });
  }

  const registerTrackedPage = (created, source) => {
    if (createdPages.includes(created)) return created;
    const targetId = typeof created.targetId === "function" ? created.targetId() : null;
    pageCreations.push({
      attempt: pageCreations.length + 1,
      state: targetId ? "created" : "ambiguous",
      source,
      target_id: targetId || undefined,
      ...(targetId ? {} : { error: "Stagehand page exposes no target id" }),
      page: created,
    });
    createdPages.push(created);
    return created;
  };

  if (stagehand && !connectError) {
    const visibleAfterInit = stagehand.context.pages();
    const ownedAfterInit = selectStagehandInitOwnedPages(
      visibleAfterInit,
      payload.remote_cdp === true
    );
    binding.init_context_ownership = {
      strategy: payload.remote_cdp === true
        ? "fresh_remote_attempt_pages_owned"
        : "local_preexisting_pages_borrowed",
      observed: Array.isArray(visibleAfterInit) ? visibleAfterInit.length : 0,
      tracked: ownedAfterInit.length,
    };
    for (const existing of ownedAfterInit) {
      registerTrackedPage(existing, "stagehand.init_context_page");
    }
  }

  const createTrackedPage = async () => {
    const creation = {
      attempt: pageCreations.length + 1,
      state: "requested",
    };
    pageCreations.push(creation);
    try {
      const created = await stagehand.context.newPage();
      creation.state = "created";
      creation.target_id = typeof created.targetId === "function" ? created.targetId() : undefined;
      creation.page = created;
      createdPages.push(created);
      if (!creation.target_id) {
        creation.state = "ambiguous";
        creation.error = "Stagehand newPage returned no target id";
        throw new Error(creation.error);
      }
      return created;
    } catch (error) {
      creation.state = "ambiguous";
      creation.error = String(error && error.message ? error.message : error);
      throw error;
    }
  };

  const cleanupCreatedPages = async () => {
    const attempts = [];
    for (const created of [...createdPages]) {
      const creation = pageCreations.find((entry) => entry.page === created);
      const targetId = creation && creation.target_id;
      const root = created && created.mainSession && created.mainSession.root;
      let pageClosed = false;
      for (let attempt = 1; attempt <= 2 && !pageClosed; attempt += 1) {
        let timer;
        try {
          if (!targetId || !root || typeof root.send !== "function") {
            throw new Error("Stagehand root connection or target id unavailable during cleanup");
          }
          const closeOperation = (async () => {
            let response = null;
            let closeError = null;
            try {
              response = await root.send("Target.closeTarget", { targetId });
            } catch (error) {
              closeError = error;
            }
            const deadline = Date.now() + 2000;
            do {
              const inventory = await root.send("Target.getTargets", {});
              const targetInfos = Array.isArray(inventory && inventory.targetInfos)
                ? inventory.targetInfos
                : [];
              if (!targetInfos.some((item) => item.targetId === targetId)) {
                return { response, close_error: closeError, inventory_confirmed: true };
              }
              await wait(25);
            } while (Date.now() < deadline);
            if (closeError) throw closeError;
            return { response, inventory_confirmed: false };
          })();
          closeOperation.catch(() => {});
          const result = await Promise.race([
            closeOperation,
            new Promise((_, reject) => {
              timer = setTimeout(() => {
                const error = new Error("Stagehand page cleanup timeout");
                error.cleanupTimedOut = true;
                reject(error);
              }, 3000);
              if (timer.unref) timer.unref();
            }),
          ]);
          pageClosed = result.inventory_confirmed === true;
          attempts.push({
            target_id: targetId,
            attempt,
            success: result.response && result.response.success,
            close_error: result.close_error
              ? String(result.close_error.message || result.close_error)
              : undefined,
            inventory_confirmed: result.inventory_confirmed,
            confirmed: pageClosed,
          });
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
          clearTimeout(timer);
        }
      }
      if (creation) creation.state = pageClosed ? "closed" : "cleanup_unconfirmed";
      if (pageClosed) createdPages.splice(createdPages.indexOf(created), 1);
    }
    const confirmed = pageCreations.every((entry) => entry.state === "closed");
    return {
      backend: "stagehand_page.close",
      required: pageCreations.length > 0,
      confirmed,
      same_connection_as_task: true,
      creation_attempts: pageCreations.map(({ attempt, state, source, target_id, error }) => ({
        attempt,
        state,
        ...(source ? { source } : {}),
        ...(target_id ? { target_id } : {}),
        ...(error ? { error } : {}),
      })),
      attempts,
    };
  };

  // ---- Binding gate 2/2: the live client transport must identify as the
  // engine under test. Stagehand's page.sendCDP rides its own connection.
  if (stagehand && !connectError) {
    let liveIdentity = null;
    let liveProduct = null;
    let liveCheck = null;
    let probe = null;
    try {
      const existing = stagehand.context.pages();
      probe = Array.isArray(existing) && existing.length ? existing[0] : await createTrackedPage();
      liveIdentity = await probe.sendCDP("Browser.getVersion");
      liveProduct = String((liveIdentity && liveIdentity.product) || "");
      liveCheck = "stagehand_send_cdp_browser_get_version";
    } catch { /* fall through to the root-connection check */ }
    const firstIdentityVerified = expectedRemoteIdentity
      ? compareRemoteIdentity(expectedRemoteIdentity, liveIdentity).verified
      : liveProduct === expectProductLive;
    if (!firstIdentityVerified && probe && probe.mainSession && probe.mainSession.root) {
      // Some engines (observed: Lightpanda) answer session-scoped
      // Browser.getVersion with an empty result under Stagehand's auto-attach
      // flow while answering correctly on the root of the same websocket. The
      // root IS Stagehand's live transport, so identity read there still
      // satisfies the gate.
      try {
        liveIdentity = await probe.mainSession.root.send("Browser.getVersion", {});
        liveProduct = String((liveIdentity && liveIdentity.product) || "");
        liveCheck = "stagehand_root_connection_browser_get_version";
      } catch { /* handled below: mismatch fails the gate */ }
    }
    binding.expect_product_live = expectProductLive;
    binding.live_product = liveProduct;
    binding.live_check = liveCheck;
    if (expectedRemoteIdentity) {
      Object.assign(binding, compareRemoteIdentity(expectedRemoteIdentity, liveIdentity));
    }
    const identityVerified = expectedRemoteIdentity
      ? binding.verified === true
      : liveProduct === expectProductLive;
    if (!identityVerified) {
      const cleanup = await cleanupCreatedPages();
      try {
        await stagehand.close();
      } catch { /* best effort */ }
      emit(applyCleanupContract({
        ok: false,
        error: {
          class: "script_error",
          message: expectedRemoteIdentity
            ? `binding gate: live stagehand transport reports identity=${JSON.stringify(binding.actual)}; expected ${JSON.stringify(binding.expected)} — the client is not bound to the remote engine under test`
            : `binding gate: live stagehand transport reports product=${JSON.stringify(liveProduct)}; expected ${JSON.stringify(expectProductLive)} — the client is not bound to the engine under test`,
        },
        observations: { binding },
        metrics: { cdp_call_count: 1, cdp_error_count: 1, ws_disconnect_count: 0 },
      }, cleanup, "Stagehand"));
      return;
    }
    binding.verified = true;
    trace({ ts: new Date().toISOString(), direction: "stagehand", step: "binding_verified", identity: expectedRemoteIdentity ? binding.actual : { product: liveProduct } });
  }

  const evalExpr = async (expression) => {
    opCalls += 1;
    return await page.evaluate(expression);
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

  const ensurePage = async () => {
    if (page) return page;
    page = await createTrackedPage();
    return page;
  };

  const sendPageCDP = async (method, params) => {
    opCalls += 1;
    return await page.sendCDP(method, params || {});
  };

  const axValue = (value) => {
    if (value && typeof value === "object" && Object.prototype.hasOwnProperty.call(value, "value")) {
      return value.value == null ? "" : String(value.value);
    }
    return value == null ? "" : String(value);
  };

  const fullAXTree = async () => {
    await sendPageCDP("Accessibility.enable");
    const result = await sendPageCDP("Accessibility.getFullAXTree");
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

    if (op === "wait_ms") {
      await wait(Number(step.ms || 100));
      return undefined;
    }
    if (op === "version") {
      await ensurePage();
      const ver = await page.sendCDP("Browser.getVersion");
      return ver.product;
    }
    if (op === "user_agent") {
      await ensurePage();
      const ver = await page.sendCDP("Browser.getVersion");
      return ver.userAgent;
    }
    if (op === "new_page") {
      page = null;
      await ensurePage();
      return "page_created";
    }

    await ensurePage();

    switch (op) {
      case "goto": {
        const url = substitute(step.url || "{fixture_url}");
        await page.goto(url, { timeout });
        await settleNavigation(url, timeout);
        return "navigated";
      }
      case "reload": {
        await evalExpr("window.__abb_reload_probe = 1; 'marked'");
        await page.reload({ timeout });
        await pollUntil(`document.readyState === "complete" && !window.__abb_reload_probe`, timeout, "reload to settle");
        return "reloaded";
      }
      case "go_back":
      case "go_forward": {
        const navNonce = `np${Date.now()}${Math.floor(Math.random() * 1e6)}`;
        await evalExpr(`window.__abb_nav_probe = ${JSON.stringify(navNonce)} + "|" + location.href, "marked"`);
        if (op === "go_back") await page.goBack({ timeout });
        else await page.goForward({ timeout });
        await pollUntil(`document.readyState === "complete" && window.__abb_nav_probe !== ${JSON.stringify(navNonce)} + "|" + location.href`, timeout, op);
        return "ok";
      }
      case "click": {
        const times = Number(step.times || 1);
        for (let i = 0; i < times; i += 1) {
          opCalls += 1;
          await page.locator(sel).click();
        }
        return `clicked x${times}`;
      }
      case "fill": {
        opCalls += 1;
        await page.locator(sel).fill(substitute(step.value == null ? "" : step.value));
        return "filled";
      }
      case "type": {
        opCalls += 1;
        await page.locator(sel).type(substitute(step.text == null ? "" : step.text));
        return "typed";
      }
      case "press": {
        opCalls += 1;
        if (sel) await evalExpr(`document.querySelector(${JSON.stringify(sel)}).focus(); "focused"`);
        await page.keyPress(step.key);
        return `pressed ${step.key}`;
      }
      case "check": {
        // Stagehand's locator has no check(); the deterministic route is
        // isChecked + click, mirroring the puppeteer path.
        opCalls += 1;
        const already = await page.locator(sel).isChecked();
        if (!already) await page.locator(sel).click();
        return "checked";
      }
      case "select_option": {
        opCalls += 1;
        return await page.locator(sel).selectOption(substitute(step.value));
      }
      case "focus": {
        await evalExpr(`document.querySelector(${JSON.stringify(sel)}).focus(); "focused"`);
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
        opCalls += 1;
        const options = { timeout };
        if (step.state) options.state = step.state;
        await page.waitForSelector(sel, options);
        return "selector_ready";
      }
      case "text_content": {
        opCalls += 1;
        return await page.locator(sel).textContent();
      }
      case "inner_text": {
        opCalls += 1;
        return await page.locator(sel).innerText();
      }
      case "get_attribute":
        return await evalExpr(`(() => { const el = document.querySelector(${JSON.stringify(sel)}); return el ? el.getAttribute(${JSON.stringify(step.name)}) : null; })()`);
      case "input_value": {
        opCalls += 1;
        return await page.locator(sel).inputValue();
      }
      case "count": {
        opCalls += 1;
        return await page.locator(sel).count();
      }
      case "is_visible": {
        opCalls += 1;
        return await page.locator(sel).isVisible();
      }
      case "is_checked": {
        opCalls += 1;
        return await page.locator(sel).isChecked();
      }
      case "is_enabled":
        return await evalExpr(`(() => { const el = document.querySelector(${JSON.stringify(sel)}); return el ? !el.disabled : false; })()`);
      case "title":
        return await page.title();
      case "url":
        return page.url();
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
        const doc = await sendPageCDP("DOM.getDocument", { depth: 0 });
        const found = await sendPageCDP("DOM.querySelector", { nodeId: doc.root.nodeId, selector: sel });
        if (!found.nodeId) throw new Error(`no element matches ${sel}`);
        await sendPageCDP("CSS.enable");
        const result = await sendPageCDP("CSS.getComputedStyleForNode", { nodeId: found.nodeId });
        return formatComputedStyle(step, result.computedStyle);
      }
      default:
        throw new Error(`unknown op ${JSON.stringify(op)}`);
    }
  }

  let outcome;
  try {
    if (connectError) {
      // A refused/failed connect is a genuine compatibility result: the engine
      // cannot be driven by this client. Grade every check as failed.
      const checkRows = [
        { name: "driver_connect", status: "fail", evidence: `stagehand@${clientVersion} could not connect to ${browserWs}: ${connectError}` },
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
          opErrors += 1;
        }
        stepResults.push(result);
        trace({ ts: new Date().toISOString(), direction: "stagehand", step: i, op: step.op, selector: step.selector, ok: result.ok, error: result.error || undefined });
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
        { name: "driver_connect", status: "pass", evidence: `stagehand@${clientVersion} bound to ${binding.live_product}` },
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
    const cleanup = await cleanupCreatedPages();
    emit(applyCleanupContract(outcome, cleanup, "Stagehand"));
    if (stagehand) {
      try {
        // Verified safe: with cdpUrl connect, close() tears down the client
        // connection only and never the engine under test.
        await stagehand.close();
      } catch { /* best effort */ }
    }
    // Stagehand keeps internal timers/queues alive; the result is already on
    // stdout, so end the process deterministically.
    setTimeout(() => process.exit(0), 50).unref();
  }
}

main().catch((err) => {
  emit({ ok: false, error: { class: "script_error", message: String(err && err.message ? err.message : err) }, observations: {}, metrics: {} });
});
