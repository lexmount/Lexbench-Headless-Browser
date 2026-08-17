#!/usr/bin/env node
"use strict";

// Generic agent-browser probe driver: replays a declarative agent-browser
// command sequence against a running engine and grades it locally
// (grader.kind = inline_assertions; checks are returned via observations).
//
// Env:
//   CDP_PORT      port of the running engine's CDP endpoint (required)
//   BROWSER_WS    runner-discovered browser WebSocket (required identity gate)
//   TASK_URL      fixture page URL; substituted for {fixture_url} (required)
//   ARTIFACT_DIR  artifact dir; ab-step trace goes to cdp.jsonl
//   AB_STEPS      JSON list of steps (required):
//     {"ab": ["open", "{fixture_url}"], "ignore_error": false, "timeout_ms": 30000}
//     {"eval": "1+1", "save_as": "v"}
//     {"sleep_ms": 200}
//   AB_CHECKS     JSON list of checks:
//     {"kind":"saved_equals","name":"v","expected":"2"}
//     {"kind":"saved_contains","name":"v","expected":"x"}
//     {"kind":"saved_truthy","name":"v"}
//     {"kind":"last_ab_ok"} {"kind":"ab_ok","step":N} {"kind":"ab_fails","step":N}
//     {"kind":"ab_data_contains","step":N,"expected":"..."}
//     {"kind":"ab_data_not_contains","step":N,"expected":"..."}
//     {"kind":"ab_data_array_min_len","step":N,"path":"requests","min":1}
//     {"kind":"ab_refs_nonempty","step":N}
//     {"kind":"file_nonempty","path":"{artifact_dir}/shot.png"}
//     {"kind":"any_of","checks":[...]}
//   AB_BIN        agent-browser binary (default: "agent-browser")

const { execFile } = require("child_process");
const crypto = require("crypto");
const fs = require("fs");
const os = require("os");
const path = require("path");

const artifactDir = process.env.ARTIFACT_DIR || ".";
const cdpPath = path.join(artifactDir, "cdp.jsonl");
const BIN = process.env.AB_BIN || "agent-browser";

function emit(obj) {
  process.stdout.write(JSON.stringify(obj));
}

function trace(obj) {
  try {
    fs.appendFileSync(cdpPath, JSON.stringify(obj) + "\n", "utf8");
  } catch (err) {
    // trace failures must not fail the probe
  }
}

function runAB(session, argv, timeoutMs) {
  // One isolated daemon session per attempt. NEVER pass commands without an
  // explicit prior `connect`: on a dangling session agent-browser silently
  // auto-launches its own browser (a hidden fallback, which this native bench
  // forbids).
  const fullArgs = ["--session", session, "--json", ...argv];
  return new Promise((resolve) => {
    execFile(BIN, fullArgs, { timeout: timeoutMs || 30000, maxBuffer: 16 * 1024 * 1024 }, (err, stdout, stderr) => {
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
      resolve({
        ok,
        timedOut: !!(err && err.killed),
        json,
        data: json && json.data ? json.data : null,
        error: json && json.error ? json.error : (err && !json ? String(err.message || err) : null),
        stderr: (stderr || "").slice(0, 2000)
      });
    });
  });
}

function slug(text) {
  return String(text || "")
    .replace(/[^0-9A-Za-z]+/g, "_")
    .replace(/^_+|_+$/g, "")
    .toLowerCase();
}

function shortPart(text, maxLen, fallback) {
  const value = slug(text) || fallback;
  return value.length <= maxLen ? value : value.slice(0, maxLen).replace(/_+$/g, "");
}

function makeABSession() {
  const runId = process.env.RUN_ID || "run";
  const taskId = process.env.TASK_ID || "task";
  const engine = process.env.ENGINE || "eng";
  const attempt = process.env.ATTEMPT || "1";
  const digest = crypto
    .createHash("sha256")
    .update(`${runId}\0${taskId}\0${engine}\0${attempt}`)
    .digest("hex")
    .slice(0, 10);
  return `abb-${shortPart(taskId, 24, "task")}-${shortPart(engine, 10, "eng")}-${shortPart(attempt, 6, "1")}-${digest}`;
}

function wait(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function lookupPath(value, dotted) {
  if (!dotted) return value;
  let current = value;
  for (const segment of dotted.split(".")) {
    if (current == null) return undefined;
    current = current[segment];
  }
  return current;
}

function resolveRefPlaceholder(text, lastSnapshotData) {
  // {ref:role=button,name=Increment} -> first matching ref id from the most
  // recent snapshot step's refs map.
  const match = /^\{ref:(.+)\}$/.exec(text);
  if (!match) return text;
  const wantsRole = /role=([^,}]+)/.exec(match[1]);
  const wantsName = /name=([^,}]+)/.exec(match[1]);
  const refs = lastSnapshotData && lastSnapshotData.refs;
  if (!refs) throw new Error("ref placeholder used before a snapshot step");
  for (const [refId, info] of Object.entries(refs)) {
    const role = info && (info.role || info.type);
    const name = info && (info.name || info.text || "");
    if (wantsRole && String(role) !== wantsRole[1]) continue;
    if (wantsName && !String(name).includes(wantsName[1])) continue;
    return refId;
  }
  throw new Error(`no snapshot ref matches ${match[1]}`);
}

async function main() {
  const port = process.env.CDP_PORT;
  const browserWs = process.env.BROWSER_WS;
  const fixtureUrl = process.env.TASK_URL;
  if (!port || !browserWs || !fixtureUrl) {
    emit({ ok: false, error: { class: "script_error", message: "CDP_PORT, BROWSER_WS and TASK_URL are required." }, observations: {}, metrics: {} });
    return;
  }
  let steps;
  let checks;
  try {
    steps = JSON.parse(process.env.AB_STEPS || "[]");
    checks = JSON.parse(process.env.AB_CHECKS || "[]");
  } catch (err) {
    emit({ ok: false, error: { class: "script_error", message: `AB_STEPS/AB_CHECKS invalid JSON: ${err.message}` }, observations: {}, metrics: {} });
    return;
  }

  fs.mkdirSync(artifactDir, { recursive: true });
  fs.writeFileSync(cdpPath, "", "utf8");
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "abb-ab-"));
  const fixtureOrigin = new URL(fixtureUrl).origin;
  const session = makeABSession();

  const saved = {};
  const stepResults = [];
  const binding = {
    driver: "agent_browser",
    browser_ws: browserWs,
    live_cdp_url: null,
    verified: false,
  };
  let abCalls = 0;
  let abErrors = 0;

  const substitute = (raw) => {
    let text = String(raw);
    text = text.split("{fixture_url}").join(fixtureUrl);
    text = text.split("{fixture_origin}").join(fixtureOrigin);
    text = text.split("{fixture_host}").join(new URL(fixtureUrl).host);
    text = text.split("{artifact_dir}").join(artifactDir);
    const tmpFile = /^\{tmp_file:([^:}]+):(.*)\}$/.exec(text);
    if (tmpFile) {
      const filePath = path.join(tmpDir, tmpFile[1]);
      fs.writeFileSync(filePath, tmpFile[2]);
      return filePath;
    }
    const lastSnapshot = [...stepResults].reverse().find((r) => r && r.isSnapshot && r.data);
    if (text.startsWith("{ref:")) {
      return resolveRefPlaceholder(text, lastSnapshot ? lastSnapshot.data : null);
    }
    return text;
  };

  try {
    // Bind this session to the engine under test before ANY other command.
    abCalls += 1;
    const bound = await runAB(session, ["connect", String(port)], 15000);
    trace({ ts: new Date().toISOString(), direction: "ab", step: "connect", argv: ["connect", String(port)], ok: bound.ok, error: bound.error || undefined });
    if (!bound.ok) {
      abErrors += 1;
      emit({
        ok: false,
        error: { class: "script_error", message: `agent-browser could not bind CDP port ${port}: ${bound.error || bound.stderr || "connect failed"}` },
        observations: { binding },
        metrics: { cdp_call_count: abCalls, cdp_error_count: abErrors, ws_disconnect_count: 0 }
      });
      return;
    }

    abCalls += 1;
    const cdpUrlResult = await runAB(session, ["get", "cdp-url"], 10000);
    binding.live_cdp_url = String(
      (cdpUrlResult.data && cdpUrlResult.data.cdpUrl) || ""
    );
    const endpointMatches =
      cdpUrlResult.ok && binding.live_cdp_url === browserWs;
    trace({
      ts: new Date().toISOString(),
      direction: "ab",
      step: "binding_verified",
      argv: ["get", "cdp-url"],
      ok: endpointMatches,
      expected: browserWs,
      actual: binding.live_cdp_url,
      error: cdpUrlResult.error || undefined,
    });
    if (!endpointMatches) {
      abErrors += 1;
      emit({
        ok: false,
        error: {
          class: "script_error",
          message:
            `agent-browser live CDP URL ${JSON.stringify(binding.live_cdp_url)} ` +
            `does not equal runner endpoint ${JSON.stringify(browserWs)}`,
        },
        observations: { binding },
        metrics: { cdp_call_count: abCalls, cdp_error_count: abErrors, ws_disconnect_count: 0 },
      });
      return;
    }
    binding.verified = true;

    for (let i = 0; i < steps.length; i += 1) {
      const step = steps[i];
      if (step.sleep_ms != null && !step.ab && !step.eval) {
        await wait(Number(step.sleep_ms));
        stepResults.push({ kind: "sleep" });
        continue;
      }
      let argv;
      let isEval = false;
      if (step.eval != null) {
        argv = ["eval", substitute(step.eval)];
        isEval = true;
      } else if (Array.isArray(step.ab)) {
        argv = step.ab.map(substitute);
      } else {
        throw new Error(`step ${i} has neither ab nor eval`);
      }
      abCalls += 1;
      const result = await runAB(session, argv, step.timeout_ms);
      result.isSnapshot = argv[0] === "snapshot";
      stepResults.push(result);
      trace({ ts: new Date().toISOString(), direction: "ab", step: i, argv, ok: result.ok, error: result.error || undefined });
      if (!result.ok) {
        abErrors += 1;
        if (!step.ignore_error && !step.expect_fail) {
          // Keep going: checks decide pass/fail; a hard engine death will
          // surface as every later step failing too.
        }
      }
      if (isEval && step.save_as) {
        const value = result.data ? result.data.result : undefined;
        saved[step.save_as] = value === undefined ? "undefined" : String(value);
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
        return [typeof value === "string" && value.includes(String(check.expected)), `${check.name}=${JSON.stringify(value)} must contain ${JSON.stringify(String(check.expected))}`];
      }
      if (kind === "saved_truthy") {
        const value = saved[check.name];
        const truthy = value !== undefined && value !== "undefined" && value !== "" && value !== "null" && value !== "false";
        return [truthy, `${check.name}=${JSON.stringify(value)}`];
      }
      if (kind === "last_ab_ok") {
        const last = [...stepResults].reverse().find((r) => r && r.json !== undefined);
        return [!!(last && last.ok), `last ab ok=${!!(last && last.ok)} error=${last ? last.error : "none"}`];
      }
      if (kind === "ab_ok") {
        const r = stepAt(check.step);
        return [!!r.ok, `step ${check.step} ok=${!!r.ok} error=${r.error || "none"}`];
      }
      if (kind === "ab_fails") {
        const r = stepAt(check.step);
        return [!r.ok, `step ${check.step} ok=${!!r.ok} (must fail)`];
      }
      if (kind === "ab_data_contains") {
        const r = stepAt(check.step);
        const text = JSON.stringify(r.data == null ? {} : r.data);
        const want = substitute(String(check.expected));
        return [text.includes(want), `step ${check.step} data must contain ${JSON.stringify(want)}; data=${text.slice(0, 200)}`];
      }
      if (kind === "ab_data_not_contains") {
        const r = stepAt(check.step);
        const text = JSON.stringify(r.data == null ? {} : r.data);
        const want = substitute(String(check.expected));
        return [!text.includes(want), `step ${check.step} data must NOT contain ${JSON.stringify(want)}; data=${text.slice(0, 200)}`];
      }
      if (kind === "ab_data_array_min_len") {
        const r = stepAt(check.step);
        const arr = lookupPath(r.data, check.path);
        const ok = Array.isArray(arr) && arr.length >= Number(check.min || 0);
        return [ok, `step ${check.step} data.${check.path} len=${Array.isArray(arr) ? arr.length : "not-array"} min=${check.min}`];
      }
      if (kind === "ab_refs_nonempty") {
        const r = stepAt(check.step);
        const refs = r.data && r.data.refs;
        const count = refs ? Object.keys(refs).length : 0;
        return [count > 0, `step ${check.step} refs=${count}`];
      }
      if (kind === "file_nonempty") {
        const filePath = substitute(check.path);
        let size = 0;
        try { size = fs.statSync(filePath).size; } catch { size = 0; }
        return [size > 0, `${filePath} size=${size}`];
      }
      if (kind === "any_of") {
        const results = (check.checks || []).map(evaluateCheck);
        return [results.some(([ok]) => ok), results.map(([ok, ev]) => `${ok ? "pass" : "fail"}: ${ev}`).join(" | ")];
      }
      return [false, `unknown check kind ${kind}`];
    };

    const checkRows = checks.map((check, idx) => {
      const [ok, evidence] = evaluateCheck(check);
      return { name: check.label || check.kind || `check${idx}`, status: ok ? "pass" : "fail", evidence };
    });

    emit({
      ok: true,
      answer: `${checkRows.filter((c) => c.status === "pass").length}/${checkRows.length} checks`,
      observations: {
        checks: checkRows,
        saved,
        binding,
        ab_calls: abCalls,
        ab_errors: abErrors,
        failure_class: "cdp_semantic"
      },
      metrics: { cdp_call_count: abCalls, cdp_error_count: abErrors, ws_disconnect_count: 0 }
    });
  } catch (err) {
    const msg = String(err && err.message ? err.message : err);
    const klass = /not found|unsupported|unknown method/i.test(msg) ? "engine_unsupported" : "script_error";
    emit({ ok: false, error: { class: klass, message: msg }, observations: { saved, binding }, metrics: { cdp_call_count: abCalls, cdp_error_count: abErrors, ws_disconnect_count: 0 } });
  } finally {
    try { await runAB(session, ["close"], 10000); } catch { /* best effort */ }
    try { fs.rmSync(tmpDir, { recursive: true, force: true }); } catch { /* best effort */ }
  }
}

main().catch((err) => {
  emit({ ok: false, error: { class: "script_error", message: String(err && err.message ? err.message : err) }, observations: {}, metrics: {} });
});
