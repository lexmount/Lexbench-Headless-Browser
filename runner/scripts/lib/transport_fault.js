"use strict";

// Attribution helper for asynchronous framework crashes.
//
// Playwright and Puppeteer throw from event-loop callbacks that no try/catch in
// the probe can reach, so the probe installs global uncaughtException and
// unhandledRejection handlers. The question those handlers have to answer is
// who caused the crash, and "it happened after connect" does not answer it: a
// bug in the probe's own callbacks would land in the same handler and get
// charged to the engine.
//
// What does answer it is where the throw came from. After connect the framework
// only runs for two reasons: an API call the probe awaits — already covered by
// try/catch in main() — or input arriving from the engine. So an *uncaught*
// async throw from inside the framework is engine-driven, while one from the
// probe's own code is not.
//
// Two shapes qualify, recorded separately because they are not equally direct:
//
//   transport_decode    the framework crashed while decoding a message, e.g.
//                       playwright-core routing a response on `sessionId`.
//                       Direct protocol causation.
//   framework_internal  the framework crashed in one of its own handlers with
//                       no decode frame on the stack. Playwright dispatches CDP
//                       *events* through `Promise.resolve().then(...)`
//                       (coreBundle.js, CRSession._onMessage), which drops the
//                       transport frames, so a malformed event lands here.
//                       Engine-driven, but worth being able to audit apart from
//                       the direct case.
//
// Anything else stays harness/infra with its stack kept for review. A clean
// evidence run has no infra rows at all, so a misfiled crash surfaces rather
// than hiding.

// The connection object that reads a frame off the websocket and the session
// object it routes that frame to. Both bundles mangle class names with a
// leading underscore (`_CRSession`), so one is allowed, but the name must still
// start a word — otherwise the bare `Connection` alternative would also match
// `WebSocketConnection`.
const TRANSPORT_DECODE_FRAME = /(?:^|[^A-Za-z0-9])_?(?:CRConnection|CRSession|CDPSession|Connection)\b[^\n]*\b_?(?:onMessage|dispatchMessage)\b/;

// A stack line pointing into the framework's own shipped code.
const FRAMEWORK_FRAME = /[/\\](?:playwright-core|puppeteer-core)[/\\]/;
const STACK_FRAME = /^\s*at\s/;
// Node's own machinery says nothing about who wrote the code that threw. Matches
// both `at node:internal/...` and `at fn (node:internal/...)`.
const RUNTIME_FRAME = /node:internal|node:[a-z_]+:\d/;

/**
 * Why an async crash is attributable to the engine, or null when it is not.
 *
 * Decided by the throw site, not by whether the framework appears anywhere on
 * the stack: the probe registers route, dialog and console listeners that the
 * framework invokes, so a crash in one of those has framework frames *below*
 * it. Blaming the engine for those is the misattribution this exists to stop,
 * and the frame that threw is what separates them.
 *
 * Takes the stack as a string rather than the error so the caller decides how
 * much of it to consider, and the frames the verdict rests on are the same ones
 * written to the artifact.
 *
 * @param {string|null|undefined} stack
 * @returns {{basis: "transport_decode"|"framework_internal", frame: string}|null}
 */
function transportFaultSignature(stack) {
  const text = stack ? String(stack) : "";
  if (!text) return null;
  const frames = text.split("\n").filter((line) => STACK_FRAME.test(line));
  const origin = frames.find((line) => !RUNTIME_FRAME.test(line));
  if (!origin || !FRAMEWORK_FRAME.test(origin)) return null;
  const decode = frames.find(
    (line) => FRAMEWORK_FRAME.test(line) && TRANSPORT_DECODE_FRAME.test(line)
  );
  if (decode) return { basis: "transport_decode", frame: decode.trim().slice(0, 300) };
  return { basis: "framework_internal", frame: origin.trim().slice(0, 300) };
}

module.exports = { transportFaultSignature };
