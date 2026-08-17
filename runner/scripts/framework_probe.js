#!/usr/bin/env node
"use strict";

// Framework probe driver (v0.3): drives the engine under test with a REAL
// pinned automation framework (playwright-core via connectOverCDP, or
// puppeteer-core via connect({browserWSEndpoint})), replays a declarative
// scenario, and grades it locally (grader.kind = inline_assertions; checks are
// returned via observations, same contract as l1_ab_probe.js).
//
// Binding invariant: the framework must bind to the engine under test.
// It must NEVER auto-launch a browser. connectOverCDP/connect cannot launch,
// and this script additionally verifies per attempt that the endpoint it is
// about to use identifies as the expected engine (HTTP /json/version) and that
// the live framework transport reports the same Browser.getVersion product.
// A mismatch is a clean infra failure, never a fallback.
//
// Env:
//   FRAMEWORK        "playwright" | "puppeteer" (required)
//   BROWSER_WS       browser-level CDP websocket of the engine under test (required)
//   CDP_PORT         HTTP port of the same endpoint, for /json/version identity
//   EXPECT_PRODUCT   exact Browser product string captured at engine launch
//   EXPECT_UA        exact User-Agent string captured at engine launch
//   EXPECT_PRODUCT_LIVE  CDP-level Browser.getVersion product captured by the
//                    runner (can differ from EXPECT_PRODUCT, e.g. Lightpanda);
//                    the live framework session must report exactly this
//   FW_CONNECT_TIMEOUT_MS  framework connect timeout (default 15000)
//   FW_ACTION_TIMEOUT_MS   per-action default timeout (default 8000)
//   TASK_URL         fixture page URL; substituted for {fixture_url}
//   ARTIFACT_DIR     artifact dir; op trace goes to cdp.jsonl
//   FW_STEPS         JSON list of scenario steps (required), e.g.
//     {"op":"new_page"}
//     {"op":"goto","url":"{fixture_url}"}
//     {"op":"click","selector":"#add3","times":3}
//     {"op":"fill","selector":"#name","value":"x"}
//     {"op":"evaluate","expression":"1+1","save_as":"v"}
//     {"op":"text_content","selector":"#total","save_as":"t"}
//     {"op":"route_fulfill","pattern":"**/api/*","status":200,"content_type":"application/json","body":"{}"}
//     {"op":"wait_ms","ms":200}
//     Any step may set save_as / timeout_ms / expect_fail / optional.
//   FW_CHECKS        JSON list of checks (same evaluator family as AB_CHECKS):
//     {"kind":"saved_equals","name":"v","expected":"2"}
//     {"kind":"saved_contains","name":"v","expected":"x"}
//     {"kind":"saved_truthy","name":"v"}
//     {"kind":"step_ok","step":N} {"kind":"step_fails","step":N}
//     {"kind":"file_nonempty","path":"{artifact_dir}/trace.zip"}
//     {"kind":"any_of","checks":[...]}
//   FW_CONNECT_OPTIONS  optional JSON merged into the framework connect call
//                       (e.g. {"defaultViewport":{"width":800,"height":600}})

const fs = require("fs");
const http = require("http");
const path = require("path");
const { transportFaultSignature } = require("./lib/transport_fault");

const artifactDir = process.env.ARTIFACT_DIR || ".";
const cdpPath = path.join(artifactDir, "cdp.jsonl");

let resultEmitted = false;

function emit(obj) {
  resultEmitted = true;
  process.stdout.write(JSON.stringify(obj));
}

// Frameworks throw from event-loop callbacks (e.g. Playwright's _CRSession
// asserts on a malformed CDP message) which no try/catch in main() can reach.
// Blaming the engine for one needs two things: the transport was up, and the
// throw came from inside the framework rather than from this probe. Timing
// alone is not causation — a bug in the probe's own callbacks would land in the
// same handler — so a crash with no framework frame stays infra with its stack
// kept for review. See lib/transport_fault.js for why a frame
// inside the framework is enough. Whether the identity gate had finished is not
// the boundary: an engine whose reply kills the framework inside gate 2/2 is
// reporting the same incompatibility as one that kills it mid-scenario.
const crashState = { binding: null, checks: [], saved: {}, transportUp: false };

function handleAsyncCrash(err) {
  const msg = String(err && err.message ? err.message : err).slice(0, 1000);
  const stack = err && err.stack ? String(err.stack).slice(0, 4000) : null;
  // Classify the same frames the artifact records, so a verdict can never rest
  // on a frame a reader cannot see.
  const transportFault = crashState.transportUp ? transportFaultSignature(stack) : null;
  const faultFrame = transportFault ? transportFault.frame : null;
  const faultBasis = transportFault ? transportFault.basis : null;
  trace({
    ts: new Date().toISOString(),
    direction: "fw",
    step: "async_crash",
    error: msg,
    stack: stack || undefined,
    transport_up: crashState.transportUp,
    transport_fault_frame: faultFrame || undefined,
    transport_fault_basis: faultBasis || undefined,
  });
  if (resultEmitted) process.exit(0);
  if (transportFault) {
    const verified = Boolean(crashState.binding && crashState.binding.verified);
    const checkRows = [
      {
        name: "framework_connect",
        status: verified ? "pass" : "fail",
        evidence: verified
          ? `bound, then framework transport crashed: ${msg}`
          : `connected, then the framework transport crashed inside the binding identity gate: ${msg}`,
      },
    ].concat(
      crashState.checks.map((check, idx) => ({
        name: check.label || check.kind || `check${idx}`,
        status: "fail",
        evidence:
          `framework transport crashed ${verified ? "mid-scenario" : "inside the binding identity gate"}` +
          ` (engine protocol violation): ${msg}`,
      }))
    );
    const passed = checkRows.filter((row) => row.status === "pass").length;
    emit({
      ok: true,
      answer: `${passed}/${checkRows.length} checks`,
      observations: {
        checks: checkRows,
        saved: crashState.saved,
        binding: crashState.binding || {},
        transport_crash: msg,
        transport_crash_stack: stack,
        transport_fault_frame: faultFrame,
        transport_fault_basis: faultBasis,
        failure_class: "cdp_semantic",
      },
      metrics: { cdp_call_count: 1, cdp_error_count: 1, ws_disconnect_count: 1 },
    });
    process.exit(0);
  }
  emit({
    ok: false,
    error: {
      class: "script_error",
      message: crashState.transportUp
        ? `async crash after connect with no transport-fault signature: ${msg}`
        : `async crash before framework transport: ${msg}`,
    },
    observations: {
      binding: crashState.binding || {},
      transport_up: crashState.transportUp,
      transport_crash_stack: stack,
    },
    metrics: {},
  });
  process.exit(1);
}

process.on("uncaughtException", handleAsyncCrash);
process.on("unhandledRejection", handleAsyncCrash);

function trace(obj) {
  try {
    fs.appendFileSync(cdpPath, JSON.stringify(obj) + "\n", "utf8");
  } catch (err) {
    // trace failures must not fail the probe
  }
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

function globToRegExp(pattern) {
  // Minimal glob for route patterns: ** = any chars, * = any chars except /.
  // \u0000 as the ** placeholder cannot collide with a literal character in a
  // URL pattern (a space could).
  const escaped = String(pattern)
    .replace(/[.+^${}()|[\]\\]/g, "\\$&")
    .replace(/\*\*/g, "\u0000")
    .replace(/\*/g, "[^/]*")
    .replace(/\u0000/g, ".*")
    .replace(/\?/g, ".");
  return new RegExp(`^${escaped}$`);
}

function urlMatches(url, pattern) {
  if (pattern.includes("*") || pattern.includes("?")) {
    return globToRegExp(pattern).test(url);
  }
  return url.includes(pattern);
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
  const framework = process.env.FRAMEWORK;
  const browserWs = process.env.BROWSER_WS;
  const fixtureUrl = process.env.TASK_URL;
  const cdpPort = process.env.CDP_PORT;
  const expectProduct = process.env.EXPECT_PRODUCT || "";
  const expectUA = process.env.EXPECT_UA || "";
  // CDP-level Browser.getVersion product captured by the runner at engine
  // launch. It can legitimately differ from the HTTP identity (Lightpanda
  // spoofs "Chrome/124..." over CDP while /json/version says "Lightpanda/1.0"),
  // so the live-session check compares against this reference.
  const expectProductLive = process.env.EXPECT_PRODUCT_LIVE || expectProduct;
  if (!framework || !browserWs || !fixtureUrl) {
    emit({ ok: false, error: { class: "script_error", message: "FRAMEWORK, BROWSER_WS and TASK_URL are required." }, observations: {}, metrics: {} });
    return;
  }
  if (framework !== "playwright" && framework !== "puppeteer") {
    emit({ ok: false, error: { class: "script_error", message: `unknown FRAMEWORK ${framework}` }, observations: {}, metrics: {} });
    return;
  }
  let steps;
  let checks;
  let connectOptions;
  try {
    steps = JSON.parse(process.env.FW_STEPS || "[]");
    checks = JSON.parse(process.env.FW_CHECKS || "[]");
    connectOptions = JSON.parse(process.env.FW_CONNECT_OPTIONS || "{}");
  } catch (err) {
    emit({ ok: false, error: { class: "script_error", message: `FW_STEPS/FW_CHECKS/FW_CONNECT_OPTIONS invalid JSON: ${err.message}` }, observations: {}, metrics: {} });
    return;
  }

  fs.mkdirSync(artifactDir, { recursive: true });
  fs.writeFileSync(cdpPath, "", "utf8");
  const fixtureOrigin = new URL(fixtureUrl).origin;

  const substitute = (raw) => {
    let text = String(raw);
    text = text.split("{fixture_url}").join(fixtureUrl);
    text = text.split("{fixture_origin}").join(fixtureOrigin);
    text = text.split("{fixture_host}").join(new URL(fixtureUrl).host);
    text = text.split("{artifact_dir}").join(artifactDir);
    return text;
  };

  // ---- Binding gate 1/2: the endpoint we were handed must identify as the
  // engine under test BEFORE any framework code touches it.
  const binding = { framework, browser_ws: browserWs, expect_product: expectProduct, verified: false, gate: null };
  crashState.binding = binding;
  crashState.checks = checks;
  if (cdpPort && expectProduct) {
    let versionInfo;
    try {
      versionInfo = await httpJson(`http://127.0.0.1:${cdpPort}/json/version`, 4000);
    } catch (err) {
      emit({
        ok: false,
        error: { class: "script_error", message: `binding gate: /json/version unreachable on port ${cdpPort}: ${err.message}` },
        observations: { binding },
        metrics: {},
      });
      return;
    }
    const product = String(versionInfo.Browser || versionInfo.Product || "");
    const ua = String(versionInfo["User-Agent"] || "");
    binding.http_product = product;
    if (product !== expectProduct || (expectUA && ua !== expectUA)) {
      emit({
        ok: false,
        error: {
          class: "script_error",
          message: `binding gate: endpoint on port ${cdpPort} reports product=${JSON.stringify(product)} ua=${JSON.stringify(ua)}; expected product=${JSON.stringify(expectProduct)} — refusing to run against an unverified engine`,
        },
        observations: { binding },
        metrics: {},
      });
      return;
    }
    const wsFromVersion = versionInfo.webSocketDebuggerUrl;
    if (wsFromVersion && wsFromVersion !== browserWs) {
      // Same port, different socket path would mean the harness wired us to a
      // different endpoint than the verified one.
      emit({
        ok: false,
        error: { class: "script_error", message: `binding gate: BROWSER_WS ${browserWs} != verified endpoint ${wsFromVersion}` },
        observations: { binding },
        metrics: {},
      });
      return;
    }
    binding.gate = "http_json_version";
  } else {
    emit({
      ok: false,
      error: { class: "script_error", message: "binding gate: CDP_PORT and EXPECT_PRODUCT are required (refusing to run unverified)" },
      observations: { binding },
      metrics: {},
    });
    return;
  }

  const saved = {};
  crashState.saved = saved;
  const stepResults = [];
  let opCalls = 0;
  let opErrors = 0;
  const recordedRequests = [];
  const recordedConsole = [];
  const recordedDialogs = [];
  const createdPages = [];

  let fw = null; // { kind, browser, version, close(), newPage(), ops }

  // ---- Connect with the real framework. A connect failure is a genuine
  // compatibility result (the engine cannot be driven), not an infra error:
  // report it through checks so it grades as a normal fail.
  const connectTimeoutMs = Number(process.env.FW_CONNECT_TIMEOUT_MS || 15000);
  let connectError = null;
  try {
    if (framework === "playwright") {
      const { chromium } = require("playwright-core");
      const pkg = require("playwright-core/package.json");
      const browser = await chromium.connectOverCDP(browserWs, { timeout: connectTimeoutMs });
      fw = { kind: "playwright", browser, version: pkg.version };
    } else {
      const puppeteer = require("puppeteer-core");
      const pkg = require("puppeteer-core/package.json");
      const options = Object.assign(
        { browserWSEndpoint: browserWs, defaultViewport: null, protocolTimeout: 20000 },
        connectOptions
      );
      const browser = await puppeteer.connect(options);
      fw = { kind: "puppeteer", browser, version: pkg.version };
    }
  } catch (err) {
    connectError = String(err && err.message ? err.message : err).slice(0, 1000);
  }
  // From here on the engine owns any framework crash (see handleAsyncCrash).
  crashState.transportUp = Boolean(fw);
  binding.framework_version = fw ? fw.version : null;
  trace({ ts: new Date().toISOString(), direction: "fw", step: "connect", framework, ok: !connectError, error: connectError || undefined });

  // ---- Binding gate 2/2: the LIVE framework transport must report the same
  // Browser.getVersion product as the verified endpoint. Preferred mechanism is
  // a framework-owned CDP session; engines that cannot even provide that are
  // cross-checked via the framework's own cached identity.
  if (fw) {
    let liveProduct = null;
    let method = null;
    try {
      if (fw.kind === "playwright") {
        const cdp = await fw.browser.newBrowserCDPSession();
        const ver = await cdp.send("Browser.getVersion");
        liveProduct = String(ver.product || "");
        method = "pw_browser_cdp_session";
        await cdp.detach().catch(() => {});
      } else {
        liveProduct = String(await fw.browser.version());
        method = "pp_browser_version";
      }
    } catch (err) {
      // Fallback: framework-cached identity (both frameworks derive it from
      // Browser.getVersion during their own handshake). Playwright's version()
      // is the product string with the name prefix stripped.
      try {
        if (fw.kind === "playwright") {
          const v = String(fw.browser.version() || "");
          if (v && expectProductLive.includes(v)) {
            liveProduct = expectProductLive;
            method = "pw_version_fallback";
          }
        }
      } catch { /* handled below */ }
    }
    binding.expect_product_live = expectProductLive;
    binding.live_product = liveProduct;
    binding.live_check = method;
    if (liveProduct !== expectProductLive) {
      try {
        if (fw.kind === "playwright") await fw.browser.close();
        else fw.browser.disconnect();
      } catch { /* best effort */ }
      emit({
        ok: false,
        error: {
          class: "script_error",
          message: `binding gate: live framework session reports product=${JSON.stringify(liveProduct)} (via ${method}); expected ${JSON.stringify(expectProductLive)} — the framework is not bound to the engine under test`,
        },
        observations: { binding },
        metrics: { cdp_call_count: 1, cdp_error_count: 1, ws_disconnect_count: 0 },
      });
      return;
    }
    binding.verified = true;
    trace({ ts: new Date().toISOString(), direction: "fw", step: "binding_verified", product: liveProduct, method });
  }

  let page = null;
  let context = null;

  async function ensurePage() {
    if (page) return page;
    if (fw.kind === "playwright") {
      context = fw.browser.contexts()[0];
      if (!context) context = await fw.browser.newContext();
      page = await context.newPage();
      page.setDefaultTimeout(Number(process.env.FW_ACTION_TIMEOUT_MS || 8000));
    } else {
      page = await fw.browser.newPage();
      page.setDefaultTimeout(Number(process.env.FW_ACTION_TIMEOUT_MS || 8000));
    }
    createdPages.push(page);
    page.on("console", (msg) => {
      try {
        recordedConsole.push({ type: msg.type(), text: msg.text() });
      } catch { /* ignore */ }
    });
    return page;
  }

  async function withPageCDP(fn) {
    const session = fw.kind === "playwright"
      ? await context.newCDPSession(page)
      : await page.target().createCDPSession();
    try {
      return await fn(session);
    } finally {
      await session.detach().catch(() => {});
    }
  }

  const axValue = (value) => {
    if (value && typeof value === "object" && Object.prototype.hasOwnProperty.call(value, "value")) {
      return value.value == null ? "" : String(value.value);
    }
    return value == null ? "" : String(value);
  };

  async function fullAXTree() {
    return await withPageCDP(async (cdp) => {
      await cdp.send("Accessibility.enable");
      const result = await cdp.send("Accessibility.getFullAXTree");
      return Array.isArray(result.nodes) ? result.nodes : [];
    });
  }

  function findAXIdentity(nodes, role, name) {
    const node = nodes.find((candidate) =>
      axValue(candidate.role) === role && axValue(candidate.name) === name
    );
    if (!node) throw new Error(`AX node role=${JSON.stringify(role)} name=${JSON.stringify(name)} not found`);
    if (node.backendDOMNodeId == null) {
      throw new Error(`AX node role=${JSON.stringify(role)} name=${JSON.stringify(name)} has no backendDOMNodeId`);
    }
    return `role=${role}|name=${name}|backendDOMNodeId=${node.backendDOMNodeId}`;
  }

  function formatComputedStyle(step, computedStyle) {
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
  }

  function findFrame(step) {
    const needle = step.url_contains || step.name;
    const frames = page.frames();
    for (const frame of frames) {
      if (step.url_contains && String(frame.url()).includes(step.url_contains)) return frame;
      if (step.name && frame.name && frame.name() === step.name) return frame;
    }
    throw new Error(`no frame matches ${JSON.stringify(needle)} among ${frames.length} frames`);
  }

  let ppInterceptionOn = false;
  const ppRoutes = []; // { regex-ish pattern, action, payload }

  async function ensurePPInterception() {
    if (ppInterceptionOn) return;
    await page.setRequestInterception(true);
    ppInterceptionOn = true;
    page.on("request", (request) => {
      const url = request.url();
      for (const route of ppRoutes) {
        if (!urlMatches(url, route.pattern)) continue;
        if (route.action === "fulfill") {
          request
            .respond({ status: route.status, contentType: route.contentType, body: route.body })
            .catch(() => {});
          return;
        }
        if (route.action === "abort") {
          request.abort().catch(() => {});
          return;
        }
      }
      request.continue().catch(() => {});
    });
  }

  async function runOp(step) {
    const op = step.op;
    const sel = step.selector ? substitute(step.selector) : undefined;
    const timeout = step.timeout_ms ? Number(step.timeout_ms) : undefined;

    // Ops that do not need a page.
    if (op === "wait_ms") {
      await wait(Number(step.ms || 100));
      return undefined;
    }
    if (op === "version") {
      return fw.kind === "playwright" ? fw.browser.version() : await fw.browser.version();
    }
    if (op === "user_agent") {
      if (fw.kind === "puppeteer") return await fw.browser.userAgent();
      const cdp = await fw.browser.newBrowserCDPSession();
      const ver = await cdp.send("Browser.getVersion");
      await cdp.detach().catch(() => {});
      return ver.userAgent;
    }
    if (op === "contexts_count") {
      if (fw.kind === "playwright") return fw.browser.contexts().length;
      return (await fw.browser.browserContexts()).length;
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
        const options = {};
        if (timeout) options.timeout = timeout;
        if (step.wait_until) options.waitUntil = step.wait_until;
        const resp = await page.goto(url, options);
        return resp ? resp.status() : "no_response";
      }
      case "reload": {
        const resp = await page.reload(timeout ? { timeout } : {});
        return resp ? resp.status() : "no_response";
      }
      case "go_back": {
        const resp = await page.goBack(timeout ? { timeout } : {});
        return resp ? "ok" : "no_history";
      }
      case "go_forward": {
        const resp = await page.goForward(timeout ? { timeout } : {});
        return resp ? "ok" : "no_history";
      }
      case "click": {
        const times = Number(step.times || 1);
        for (let i = 0; i < times; i += 1) {
          if (fw.kind === "playwright") await page.click(sel, timeout ? { timeout } : {});
          else await page.click(sel);
        }
        return `clicked x${times}`;
      }
      case "fill": {
        const value = substitute(step.value == null ? "" : step.value);
        if (fw.kind === "playwright") await page.fill(sel, value, timeout ? { timeout } : {});
        else await page.locator(sel).fill(value);
        return "filled";
      }
      case "type": {
        const text = substitute(step.text == null ? "" : step.text);
        await page.type(sel, text, step.delay_ms ? { delay: Number(step.delay_ms) } : {});
        return "typed";
      }
      case "press": {
        if (sel) {
          if (fw.kind === "playwright") await page.press(sel, step.key);
          else {
            await page.focus(sel);
            await page.keyboard.press(step.key);
          }
        } else {
          await page.keyboard.press(step.key);
        }
        return `pressed ${step.key}`;
      }
      case "keyboard_type": {
        await page.keyboard.type(substitute(step.text == null ? "" : step.text));
        return "typed";
      }
      case "check": {
        if (fw.kind === "playwright") await page.check(sel, timeout ? { timeout } : {});
        else {
          const checked = await page.$eval(sel, (el) => el.checked);
          if (!checked) await page.click(sel);
        }
        return "checked";
      }
      case "select_option": {
        if (fw.kind === "playwright") return await page.selectOption(sel, substitute(step.value));
        return await page.select(sel, substitute(step.value));
      }
      case "focus": {
        await page.focus(sel);
        return "focused";
      }
      case "hover": {
        await page.hover(sel);
        return "hovered";
      }
      case "evaluate": {
        // Both frameworks accept an expression string; wrap so `return` is not needed.
        return await page.evaluate(substitute(step.expression));
      }
      case "wait_for_function": {
        await page.waitForFunction(substitute(step.expression), timeout ? { timeout } : {});
        return "predicate_true";
      }
      case "wait_for_selector": {
        const options = timeout ? { timeout } : {};
        if (step.state) {
          if (fw.kind === "playwright") options.state = step.state;
          else if (step.state === "hidden" || step.state === "detached") options.hidden = true;
          else options.visible = true;
        }
        await page.waitForSelector(sel, options);
        return "selector_ready";
      }
      case "text_content": {
        if (fw.kind === "playwright") return await page.textContent(sel);
        return await page.$eval(sel, (el) => el.textContent);
      }
      case "inner_text": {
        if (fw.kind === "playwright") return await page.innerText(sel);
        return await page.$eval(sel, (el) => el.innerText);
      }
      case "get_attribute": {
        if (fw.kind === "playwright") return await page.getAttribute(sel, step.name);
        return await page.$eval(sel, (el, name) => el.getAttribute(name), step.name);
      }
      case "input_value": {
        if (fw.kind === "playwright") return await page.inputValue(sel);
        return await page.$eval(sel, (el) => el.value);
      }
      case "count": {
        if (fw.kind === "playwright") return await page.locator(sel).count();
        return (await page.$$(sel)).length;
      }
      case "is_visible": {
        if (fw.kind === "playwright") return await page.isVisible(sel);
        return await page.$eval(sel, (el) => {
          const style = window.getComputedStyle(el);
          return style.display !== "none" && style.visibility !== "hidden";
        }).catch(() => false);
      }
      case "is_checked": {
        if (fw.kind === "playwright") return await page.isChecked(sel);
        return await page.$eval(sel, (el) => !!el.checked);
      }
      case "is_enabled": {
        if (fw.kind === "playwright") return await page.isEnabled(sel);
        return await page.$eval(sel, (el) => !el.disabled);
      }
      case "title":
        return await page.title();
      case "url":
        return page.url();
      case "content":
        return await page.content();
      case "frame_evaluate": {
        const frame = findFrame(step);
        return await frame.evaluate(substitute(step.expression));
      }
      case "route_fulfill": {
        const pattern = substitute(step.pattern);
        const body = substitute(step.body == null ? "" : step.body);
        const status = Number(step.status || 200);
        const contentType = step.content_type || "application/json";
        if (fw.kind === "playwright") {
          await page.route(pattern, (route) => route.fulfill({ status, contentType, body }).catch(() => {}));
        } else {
          ppRoutes.push({ pattern, action: "fulfill", status, contentType, body });
          await ensurePPInterception();
        }
        return "route_registered";
      }
      case "route_abort": {
        const pattern = substitute(step.pattern);
        if (fw.kind === "playwright") {
          await page.route(pattern, (route) => route.abort().catch(() => {}));
        } else {
          ppRoutes.push({ pattern, action: "abort" });
          await ensurePPInterception();
        }
        return "route_registered";
      }
      case "set_extra_headers": {
        await page.setExtraHTTPHeaders(step.headers || {});
        return "headers_set";
      }
      case "record_requests": {
        // Default to "**": a single "*" glob never matches a URL (it
        // excludes "/"), which would silently record nothing.
        const pattern = substitute(step.pattern || "**");
        page.on("request", (request) => {
          const url = request.url();
          if (urlMatches(url, pattern)) recordedRequests.push({ url, method: request.method() });
        });
        return "recording";
      }
      case "get_recorded_requests":
        return recordedRequests;
      case "get_console":
        return recordedConsole;
      case "on_dialog": {
        const action = step.action || "accept";
        page.on("dialog", (dialog) => {
          recordedDialogs.push({ type: dialog.type(), message: dialog.message() });
          const done = action === "accept" ? dialog.accept(step.text) : dialog.dismiss();
          done.catch(() => {});
        });
        return "dialog_handler_set";
      }
      case "get_dialogs":
        return recordedDialogs;
      case "write_file": {
        // Materialise a payload (e.g. an upload source) under {artifact_dir}.
        const filePath = substitute(step.path);
        const content = String(step.content == null ? "" : step.content);
        fs.writeFileSync(filePath, content, "utf8");
        return `wrote ${Buffer.byteLength(content)} bytes`;
      }
      case "set_input_files": {
        const filePath = substitute(step.path);
        if (fw.kind === "playwright") {
          await page.locator(sel).setInputFiles(filePath, timeout ? { timeout } : {});
        } else {
          const handle = await page.$(sel);
          if (!handle) throw new Error(`no element matches ${sel}`);
          await handle.uploadFile(filePath);
        }
        return "files_set";
      }
      case "download": {
        // Downloads over an attached CDP session need browser-level control:
        // Playwright's default context over connectOverCDP never emits the
        // "download" event, and page-scoped Browser.setDownloadBehavior is
        // ignored. allowAndName + downloadProgress events work identically
        // for both frameworks.
        const crypto = require("crypto");
        const savePath = substitute(step.path);
        const downloadDir = path.dirname(savePath);
        fs.mkdirSync(downloadDir, { recursive: true });
        const cdp = fw.kind === "playwright"
          ? await fw.browser.newBrowserCDPSession()
          : await fw.browser.target().createCDPSession();
        await cdp.send("Browser.setDownloadBehavior", { behavior: "allowAndName", downloadPath: downloadDir, eventsEnabled: true });
        const completed = new Promise((resolve, reject) => {
          const timer = setTimeout(() => reject(new Error("download did not complete before the timeout")), timeout || 8000);
          cdp.on("Browser.downloadProgress", (ev) => {
            if (ev.state === "completed") {
              clearTimeout(timer);
              resolve(ev.guid);
            } else if (ev.state === "canceled") {
              clearTimeout(timer);
              reject(new Error("download was canceled"));
            }
          });
        });
        if (fw.kind === "playwright") await page.locator(sel).click(timeout ? { timeout } : {});
        else await page.click(sel);
        const guid = await completed;
        fs.copyFileSync(path.join(downloadDir, guid), savePath);
        const bytes = fs.readFileSync(savePath);
        return `${crypto.createHash("sha256").update(bytes).digest("hex").slice(0, 12)}:${bytes.length}`;
      }
      case "set_cookie": {
        const cookie = {
          name: step.name,
          value: substitute(step.value == null ? "" : step.value),
          url: substitute(step.url || "{fixture_url}"),
        };
        if (fw.kind === "playwright") await context.addCookies([cookie]);
        else await page.setCookie(cookie);
        return "cookie_set";
      }
      case "get_cookies": {
        if (fw.kind === "playwright") return await context.cookies(substitute(step.url || "{fixture_url}"));
        return await page.cookies(substitute(step.url || "{fixture_url}"));
      }
      case "ax_snapshot": {
        return await fullAXTree();
      }
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
        const style = await withPageCDP(async (cdp) => {
          const doc = await cdp.send("DOM.getDocument", { depth: 0 });
          const found = await cdp.send("DOM.querySelector", { nodeId: doc.root.nodeId, selector: sel });
          if (!found.nodeId) throw new Error(`no element matches ${sel}`);
          await cdp.send("CSS.enable");
          const result = await cdp.send("CSS.getComputedStyleForNode", { nodeId: found.nodeId });
          return result.computedStyle;
        });
        return formatComputedStyle(step, style);
      }
      case "aria_snapshot": {
        if (fw.kind === "playwright") return await page.locator(sel || "body").ariaSnapshot();
        return await page.accessibility.snapshot();
      }
      case "close_page": {
        await page.close();
        const idx = createdPages.indexOf(page);
        if (idx >= 0) createdPages.splice(idx, 1);
        page = null;
        return "page_closed";
      }
      default:
        throw new Error(`unknown op ${JSON.stringify(op)}`);
    }
  }

  try {
    if (connectError) {
      // Grade every declared check as failed; the connect check carries the evidence.
      const checkRows = [
        { name: "framework_connect", status: "fail", evidence: `${framework} could not connect to ${browserWs}: ${connectError}` },
      ].concat(
        checks.map((check, idx) => ({
          name: check.label || check.kind || `check${idx}`,
          status: "fail",
          evidence: "framework did not connect; scenario not executed",
        }))
      );
      emit({
        ok: true,
        answer: `0/${checkRows.length} checks`,
        observations: { checks: checkRows, saved: {}, binding, connect_error: connectError, failure_class: "cdp_semantic" },
        metrics: { cdp_call_count: 1, cdp_error_count: 1, ws_disconnect_count: 0 },
      });
      return;
    }

    // The runner kills the probe at task_ms; racing every op against the
    // remaining budget (minus a reserve for grading/emit) means an engine
    // that hangs mid-scenario still produces a graded result instead of an
    // infra kill.
    const taskBudgetMs = Number(process.env.FW_TASK_TIMEOUT_MS || 0);
    const budgetDeadline = taskBudgetMs > 0 ? Date.now() + taskBudgetMs - 3000 : null;

    for (let i = 0; i < steps.length; i += 1) {
      const step = steps[i];
      opCalls += 1;
      let result;
      try {
        const remaining = budgetDeadline === null ? Infinity : budgetDeadline - Date.now();
        if (remaining <= 0) throw new Error("task budget exhausted before op could run");
        const value = budgetDeadline === null
          ? await runOp(step)
          : await Promise.race([
              runOp(step),
              // .unref(): the losing timer stays armed until the budget
              // deadline. Without this it keeps the event loop alive after the
              // result is emitted, so every attempt burns the full task budget
              // before node exits and the runner kills it as a timeout.
              new Promise((_, reject) =>
                setTimeout(() => reject(new Error(`task budget exhausted during ${step.op}`)), remaining).unref()
              ),
            ]);
        result = { ok: true, value };
      } catch (err) {
        const message = String(err && err.message ? err.message : err).slice(0, 1000);
        result = { ok: false, error: message, unsupported: isUnsupportedMessage(message) };
        opErrors += 1;
      }
      stepResults.push(result);
      trace({
        ts: new Date().toISOString(),
        direction: "fw",
        step: i,
        op: step.op,
        selector: step.selector,
        ok: result.ok,
        error: result.error || undefined,
      });
      if (result.ok && step.expect_fail) {
        result.unexpected_success = true;
      }
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

    const implicitChecks = [
      { name: "framework_connect", status: "pass", evidence: `${framework}@${fw.version} bound to ${binding.live_product}` },
    ];
    const checkRows = implicitChecks.concat(
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
        fw_ops: opCalls,
        fw_op_errors: opErrors,
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
    if (fw) {
      for (const created of createdPages) {
        try {
          await created.close();
        } catch { /* target may already be gone */ }
      }
      try {
        if (fw.kind === "playwright") await fw.browser.close();
        else await fw.browser.disconnect();
      } catch { /* best effort */ }
    }
  }
}

main().catch((err) => {
  emit({ ok: false, error: { class: "script_error", message: String(err && err.message ? err.message : err) }, observations: {}, metrics: {} });
});
