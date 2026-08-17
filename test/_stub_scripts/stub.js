#!/usr/bin/env node
// Stub node_cdp_probe driver for the test suite (TESTING.md §3 scripted rows).
// Behavior is selected via STUB_MODE, delivered through task driver.env.
// It never connects to BROWSER_WS: it only echoes env and prints JSON.
"use strict";

const mode = process.env.STUB_MODE || "ok_inline";

function out(obj) {
  process.stdout.write(JSON.stringify(obj));
}

const envEcho = {};
for (const key of [
  "MY_SEED",
  "MY_SESSION",
  "MY_BASE",
  "MY_ART",
  "CFG_JSON",
  "CDP_PORT",
  "BROWSER_WS",
  "SEED",
  "TASK_URL",
  "TASK_ID",
  "RUN_ID",
  "ATTEMPT",
  "ARTIFACT_DIR",
  "AGENT_BROWSER_SOCKET_DIR",
  "AGENT_BROWSER_NAMESPACE",
  "AGENT_BROWSER_IDLE_TIMEOUT_MS",
]) {
  envEcho[key] = process.env[key] === undefined ? null : process.env[key];
}

if (mode === "not_json") {
  process.stdout.write("this is not json {oops");
  process.exit(0);
} else if (mode === "exit_nonzero") {
  out({ ok: true, answer: "x", observations: {} });
  process.exit(3);
} else if (mode === "unsupported") {
  out({ ok: false, error: { class: "engine_unsupported", message: "IndexedDB is not implemented" } });
  process.exit(0);
} else if (mode === "script_fail") {
  out({ ok: false, error: { message: "stub exploded" } });
  process.exit(0);
} else if (mode === "sleep") {
  // Outlive timeouts.task_ms so subprocess.run raises TimeoutExpired.
  setTimeout(() => process.exit(0), 30000);
} else {
  let checks;
  if (mode === "ok_inline_fail") {
    checks = [{ name: "stub_check", status: "fail", evidence: "forced fail" }];
  } else if (mode === "ok_no_checks") {
    checks = undefined;
  } else {
    checks = [{ name: "stub_check", status: "pass", evidence: "forced pass" }];
  }
  const observations = { env: envEcho };
  if (checks !== undefined) {
    observations.checks = checks;
  }
  out({
    ok: true,
    answer: process.env.STUB_ANSWER || "42",
    observations,
    metrics: { cdp_call_count: 1, cdp_error_count: 0, ws_disconnect_count: 0 },
  });
  process.exit(0);
}
