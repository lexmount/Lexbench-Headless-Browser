#!/usr/bin/env node
"use strict";

// Generic L2 fixture probe: navigate to TASK_URL, optionally click gates,
// wait for the answer element to settle, extract its text, and return it as
// the answer. Grading happens server-side (expected_answer endpoint).
//
// Env:
//   BROWSER_WS       page-target websocket (required)
//   TASK_URL         full fixture URL (required)
//   ARTIFACT_DIR     where cdp.jsonl goes
//   PROBE_SPEC       JSON: {
//     "selector": "#out",                     // answer element
//     "read": "textContent" | "innerText",
//     "settle": "none"|"not_pending"|"nonempty",
//     "settle_timeout_ms": 5000,
//     "pre_clicks": ["#go", ...],             // clicked in order after load
//     "wait_ms": 200                          // fixed wait after load/clicks
//   }

const fs = require("fs");
const path = require("path");
const { CdpSession, wait, classifyError } = require("./lib/rawws");

const artifactDir = process.env.ARTIFACT_DIR || ".";
const cdpPath = path.join(artifactDir, "cdp.jsonl");

function emit(obj) {
  process.stdout.write(JSON.stringify(obj));
}

function bindingObservations(session) {
  return session && session.remoteBinding
    ? { binding: session.remoteBinding }
    : {};
}

function fail(errorClass, message, session, extraObservations = {}) {
  emit({
    ok: false,
    error: { class: errorClass, message },
    observations: { ...bindingObservations(session), ...extraObservations },
    metrics: {
      cdp_call_count: session ? session.callCount : 0,
      cdp_error_count: session ? session.errorCount : 0,
      ws_disconnect_count: 0
    }
  });
}

async function evalJs(session, expression) {
  const result = await session.command("Runtime.evaluate", {
    expression,
    returnByValue: true,
    awaitPromise: true
  });
  if (result.exceptionDetails) {
    const details = result.exceptionDetails;
    const detail =
      (details.exception && details.exception.description) ||
      details.text ||
      (result.result && result.result.description) ||
      "evaluate threw";
    throw new Error(detail);
  }
  return result.result ? result.result.value : undefined;
}

async function main() {
  const browserWs = process.env.BROWSER_WS;
  const taskUrl = process.env.TASK_URL;
  if (!browserWs || !taskUrl) {
    fail("script_error", "BROWSER_WS and TASK_URL are required.");
    return;
  }
  let spec = {};
  try {
    spec = JSON.parse(process.env.PROBE_SPEC || "{}");
  } catch (err) {
    fail("script_error", `PROBE_SPEC is not valid JSON: ${err.message}`);
    return;
  }
  const selector = spec.selector || "#out";
  const read = spec.read === "innerText" ? "innerText" : "textContent";
  const settle = spec.settle || "none";
  const settleTimeout = Number(spec.settle_timeout_ms || 5000);
  const preClicks = Array.isArray(spec.pre_clicks) ? spec.pre_clicks : [];
  const waitMs = Number(spec.wait_ms || 200);

  fs.mkdirSync(artifactDir, { recursive: true });
  fs.writeFileSync(cdpPath, "", "utf8");
  const session = new CdpSession(browserWs, cdpPath);
  const remoteIdentity = process.env.REMOTE_CDP_IDENTITY_JSON || "";

  try {
    await session.connect();
    await session.bindRemoteIdentity(remoteIdentity);
    await session.openPage(process.env.CDP_PORT, {
      allowHttpFallback: !remoteIdentity,
    });
  } catch (err) {
    const closeResult = await session.close().catch((cleanupError) => ({
      confirmed: false,
      error: String(cleanupError && cleanupError.message ? cleanupError.message : cleanupError)
    }));
    const cleanup = err && err.targetCleanup ? err.targetCleanup : closeResult;
    fail(
      err && err.cdpRejected === true ? "engine_unsupported" : "script_error",
      `connection/binding/page bootstrap failed: ${err.message}`,
      session,
      { target_cleanup: cleanup, isolation_restored: cleanup.confirmed === true }
    );
    return;
  }

  let outcome;
  try {
    await session.command("Page.enable");
    await session.command("Runtime.enable");
    await session.command("Page.navigate", { url: taskUrl });

    // Wait for the document to be at least interactive.
    const loadDeadline = Date.now() + 8000;
    while (Date.now() < loadDeadline) {
      try {
        const state = await evalJs(session, "document.readyState");
        if (state === "complete" || state === "interactive") break;
      } catch (err) {
        // Runtime not ready yet on some engines; retry until deadline.
      }
      await wait(100);
    }
    if (waitMs > 0) await wait(waitMs);

    for (const clickSel of preClicks) {
      await evalJs(
        session,
        `(function(){var el=document.querySelector(${JSON.stringify(clickSel)});` +
          `if(!el){throw new Error("pre_click target not found: "+${JSON.stringify(clickSel)});}` +
          `el.click();return true;})()`
      );
      await wait(120);
    }

    const readExpr =
      `(function(){var el=document.querySelector(${JSON.stringify(selector)});` +
      `return el ? String(el.${read} == null ? "" : el.${read}) : null;})()`;

    let text = null;
    let settled = false;
    const deadline = Date.now() + settleTimeout;
    while (true) {
      text = await evalJs(session, readExpr);
      const trimmed = text == null ? "" : String(text).trim();
      if (settle === "none") {
        settled = true;
      } else if (settle === "not_pending") {
        settled = trimmed !== "" && trimmed !== "PENDING";
      } else if (settle === "nonempty") {
        settled = trimmed !== "";
      } else {
        settled = true;
      }
      if (settled || Date.now() >= deadline) break;
      await wait(150);
    }

    const answer = text == null ? "" : String(text).trim();
    outcome = {
      ok: true,
      answer,
      observations: {
        selector,
        settled,
        element_found: text !== null,
        ...(session.remoteBinding ? { binding: session.remoteBinding } : {})
      },
      metrics: {
        cdp_call_count: session.callCount,
        cdp_error_count: session.errorCount,
        ws_disconnect_count: 0
      }
    };
  } catch (err) {
    const msg = String(err && err.message ? err.message : err);
    outcome = {
      ok: false,
      error: { class: classifyError(msg), message: msg },
      observations: bindingObservations(session),
      metrics: {
        cdp_call_count: session.callCount,
        cdp_error_count: session.errorCount,
        ws_disconnect_count: 0
      }
    };
  }

  const cleanup = await session.close().catch((err) => ({
    confirmed: false,
    error: String(err && err.message ? err.message : err)
  }));
  outcome.observations = {
    ...(outcome.observations || {}),
    target_cleanup: cleanup,
    isolation_restored: cleanup.confirmed === true
  };
  outcome.metrics = {
    ...(outcome.metrics || {}),
    cdp_call_count: session.callCount,
    cdp_error_count: session.errorCount,
    ws_disconnect_count: 0
  };
  if (remoteIdentity && cleanup.confirmed !== true) {
    outcome = {
      ok: false,
      error: {
        class: "script_error",
        message: `remote target cleanup was not confirmed: ${JSON.stringify(cleanup)}`
      },
      observations: {
        ...outcome.observations,
        primary_outcome: outcome
      },
      metrics: outcome.metrics
    };
  }
  emit(outcome);
}

main().catch((err) => {
  fail("script_error", String(err && err.message ? err.message : err));
});
