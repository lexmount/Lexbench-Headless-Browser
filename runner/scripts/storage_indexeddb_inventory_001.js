#!/usr/bin/env node
"use strict";

const crypto = require("crypto");
const fs = require("fs");
const net = require("net");
const path = require("path");
const tls = require("tls");
const {
  assertRemoteIdentity,
  compareRemoteIdentity,
  parseExpectedRemoteIdentity,
} = require("./lib/remote_identity");
const SKIP_FRAME = Symbol("skip-frame");

const artifactDir = process.env.ARTIFACT_DIR || ".";
const cdpPath = path.join(artifactDir, "cdp.jsonl");
let nextId = 1;
let cdpCallCount = 0;
let cdpErrorCount = 0;

function nowIso() {
  return new Date().toISOString();
}

function writeJsonl(filePath, obj) {
  fs.appendFileSync(filePath, JSON.stringify(obj) + "\n", "utf8");
}

function fail(errorClass, message, binding = null, extraObservations = {}) {
  process.stdout.write(JSON.stringify({
    ok: false,
    error: { class: errorClass, message },
    observations: { ...(binding ? { binding } : {}), ...extraObservations },
    metrics: { cdp_call_count: cdpCallCount, cdp_error_count: cdpErrorCount, ws_disconnect_count: 0 }
  }));
}

class RawWebSocket {
  constructor(wsUrl) {
    this.url = new URL(wsUrl);
    this.socket = null;
    this.buffer = Buffer.alloc(0);
    this.waiters = [];
    this.fragmentOpcode = null;
    this.fragments = [];
  }

  async connect() {
    if (!["ws:", "wss:"].includes(this.url.protocol)) {
      throw new Error(`unsupported websocket protocol: ${this.url.protocol}`);
    }
    const secure = this.url.protocol === "wss:";
    const host = this.url.hostname;
    const port = Number(this.url.port || (secure ? 443 : 80));
    const requestPath = `${this.url.pathname || "/"}${this.url.search || ""}`;
    const key = crypto.randomBytes(16).toString("base64");
    this.socket = secure
      ? tls.connect({ host, port, servername: host })
      : net.createConnection({ host, port });
    this.socket.on("data", (chunk) => {
      this.buffer = Buffer.concat([this.buffer, chunk]);
      this._drainWaiters();
    });
    this.socket.on("close", () => this._drainWaiters());
    await new Promise((resolve, reject) => {
      this.socket.once(secure ? "secureConnect" : "connect", resolve);
      this.socket.once("error", reject);
    });
    this.socket.write([
      `GET ${requestPath} HTTP/1.1`,
      `Host: ${host}:${port}`,
      "Upgrade: websocket",
      "Connection: Upgrade",
      `Sec-WebSocket-Key: ${key}`,
      "Sec-WebSocket-Version: 13",
      "",
      ""
    ].join("\r\n"));

    while (true) {
      const split = this.buffer.indexOf("\r\n\r\n");
      if (split >= 0) {
        const head = this.buffer.subarray(0, split).toString("utf8");
        this.buffer = this.buffer.subarray(split + 4);
        if (!head.startsWith("HTTP/1.1 101") && !head.startsWith("HTTP/1.0 101")) {
          throw new Error(`websocket handshake failed: ${head.split("\r\n")[0]}`);
        }
        return;
      }
      await this._waitForData();
    }
  }

  send(text) {
    const payload = Buffer.from(text, "utf8");
    const mask = crypto.randomBytes(4);
    let header;
    if (payload.length < 126) {
      header = Buffer.alloc(2);
      header[0] = 0x81;
      header[1] = 0x80 | payload.length;
    } else if (payload.length < 65536) {
      header = Buffer.alloc(4);
      header[0] = 0x81;
      header[1] = 0x80 | 126;
      header.writeUInt16BE(payload.length, 2);
    } else {
      header = Buffer.alloc(10);
      header[0] = 0x81;
      header[1] = 0x80 | 127;
      header.writeBigUInt64BE(BigInt(payload.length), 2);
    }
    const masked = Buffer.alloc(payload.length);
    for (let i = 0; i < payload.length; i += 1) {
      masked[i] = payload[i] ^ mask[i % 4];
    }
    this.socket.write(Buffer.concat([header, mask, masked]));
  }

  async recvJson() {
    while (true) {
      const payload = await this.recvFrame();
      if (payload === null) {
        throw new Error("websocket disconnected");
      }
      const text = payload.toString("utf8");
      return JSON.parse(text);
    }
  }

  async recvFrame() {
    while (true) {
      const frame = this._tryReadFrame();
      if (frame === SKIP_FRAME) {
        continue;
      }
      if (frame !== undefined) {
        return frame;
      }
      await this._waitForData();
    }
  }

  _tryReadFrame() {
    if (this.buffer.length < 2) return undefined;
    const first = this.buffer[0];
    const second = this.buffer[1];
    const final = Boolean(first & 0x80);
    const opcode = first & 0x0f;
    let offset = 2;
    let length = second & 0x7f;
    if (length === 126) {
      if (this.buffer.length < offset + 2) return undefined;
      length = this.buffer.readUInt16BE(offset);
      offset += 2;
    } else if (length === 127) {
      if (this.buffer.length < offset + 8) return undefined;
      length = Number(this.buffer.readBigUInt64BE(offset));
      offset += 8;
    }
    const masked = Boolean(second & 0x80);
    let mask = null;
    if (masked) {
      if (this.buffer.length < offset + 4) return undefined;
      mask = this.buffer.subarray(offset, offset + 4);
      offset += 4;
    }
    if (this.buffer.length < offset + length) return undefined;
    let payload = this.buffer.subarray(offset, offset + length);
    this.buffer = this.buffer.subarray(offset + length);
    if (mask) {
      const unmasked = Buffer.alloc(payload.length);
      for (let i = 0; i < payload.length; i += 1) {
        unmasked[i] = payload[i] ^ mask[i % 4];
      }
      payload = unmasked;
    }
    if ([8, 9, 10].includes(opcode)) {
      if (!final || length > 125) {
        throw new Error("invalid fragmented WebSocket control frame");
      }
      if (opcode === 8) return null;
      if (opcode === 9) this._sendPong(payload);
      return SKIP_FRAME;
    }
    if (opcode === 1 || opcode === 2) {
      if (this.fragmentOpcode !== null) {
        throw new Error("new WebSocket data frame before fragmented message ended");
      }
      if (final) return payload;
      this.fragmentOpcode = opcode;
      this.fragments = [payload];
      return SKIP_FRAME;
    }
    if (opcode === 0) {
      if (this.fragmentOpcode === null) {
        throw new Error("unexpected WebSocket continuation frame");
      }
      this.fragments.push(payload);
      if (!final) return SKIP_FRAME;
      const complete = Buffer.concat(this.fragments);
      this.fragmentOpcode = null;
      this.fragments = [];
      return complete;
    }
    return SKIP_FRAME;
  }

  _sendPong(payload) {
    const mask = crypto.randomBytes(4);
    const masked = Buffer.alloc(payload.length);
    for (let i = 0; i < payload.length; i += 1) {
      masked[i] = payload[i] ^ mask[i % 4];
    }
    const header = Buffer.from([0x8a, 0x80 | payload.length]);
    this.socket.write(Buffer.concat([header, mask, masked]));
  }

  _waitForData() {
    if (!this.socket || this.socket.destroyed) {
      return Promise.reject(new Error("socket closed"));
    }
    return new Promise((resolve) => this.waiters.push(resolve));
  }

  _drainWaiters() {
    const waiters = this.waiters.splice(0);
    for (const waiter of waiters) waiter();
  }

  close() {
    if (this.socket && !this.socket.destroyed) {
      this.socket.destroy();
    }
  }
}

let sessionId = null;
let ownedTargetId = null;
let targetCreationState = "not_requested";

async function command(ws, method, params = {}) {
  const id = nextId++;
  cdpCallCount += 1;
  const payload = { id, method, params };
  if (sessionId && !method.startsWith("Target.")) {
    payload.sessionId = sessionId;
  }
  writeJsonl(cdpPath, { ts: nowIso(), direction: "send", id, method, params, sessionId: payload.sessionId });
  ws.send(JSON.stringify(payload));
  while (true) {
    const payload = await ws.recvJson();
    if (payload.id === id) {
      if (payload.error) {
        cdpErrorCount += 1;
        writeJsonl(cdpPath, { ts: nowIso(), direction: "recv", id, method, error: payload.error });
        const error = new Error(`${method}: ${payload.error.message || JSON.stringify(payload.error)}`);
        error.cdpRejected = true;
        error.cdpError = payload.error;
        throw error;
      }
      writeJsonl(cdpPath, { ts: nowIso(), direction: "recv", id, method, result: payload.result || {} });
      return payload.result || {};
    }
    writeJsonl(cdpPath, { ts: nowIso(), direction: "event", method: payload.method, params: payload.params || {} });
  }
}

async function closeOwnedTarget(ws) {
  if (!ownedTargetId) {
    const ambiguous = targetCreationState === "requested"
      || targetCreationState === "ambiguous"
      || targetCreationState === "cleanup_unconfirmed";
    const result = {
      confirmed: !ambiguous,
      closed: !ambiguous,
      reason: ambiguous
        ? "target_creation_ambiguous"
        : targetCreationState === "rejected"
          ? "target_creation_rejected"
          : "no_owned_target",
      creation_state: targetCreationState,
      target_id: null,
      attempts: []
    };
    if (ambiguous) {
      writeJsonl(cdpPath, {
        ts: nowIso(), direction: "cleanup", method: "Target.createTarget",
        targetId: null, closed: false, reason: result.reason,
        creationState: targetCreationState
      });
    }
    return result;
  }
  const targetId = ownedTargetId;
  const attempts = [];
  let closed = false;
  for (let attempt = 1; attempt <= 2 && !closed; attempt += 1) {
    try {
      const result = await command(ws, "Target.closeTarget", { targetId });
      closed = result.success === true;
      attempts.push({ attempt, success: result.success, confirmed: closed });
    } catch (err) {
      attempts.push({
        attempt,
        confirmed: false,
        error: String(err && err.message ? err.message : err)
      });
    }
  }
  writeJsonl(cdpPath, {
    ts: nowIso(), direction: "cleanup", method: "Target.closeTarget",
    targetId, closed, attempts
  });
  ownedTargetId = null;
  sessionId = null;
  targetCreationState = closed ? "closed" : "cleanup_unconfirmed";
  return { confirmed: closed, closed, target_id: targetId, attempts };
}

function wait(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function waitForTaskFunction(ws) {
  const deadline = Date.now() + 5000;
  let lastError = null;
  while (Date.now() < deadline) {
    try {
      const result = await command(ws, "Runtime.evaluate", {
        expression: "typeof window.__ABB_RUN_IDB_TASK__",
        returnByValue: true
      });
      if (result.result && result.result.value === "function") {
        return;
      }
    } catch (err) {
      lastError = err;
    }
    await wait(100);
  }
  if (lastError) {
    throw lastError;
  }
  throw new Error("__ABB_RUN_IDB_TASK__ is not available");
}

async function main() {
  const browserWs = process.env.BROWSER_WS;
  const taskUrl = process.env.TASK_URL;
  if (!browserWs || !taskUrl) {
    fail("script_error", "BROWSER_WS and TASK_URL are required.");
    return;
  }

  fs.mkdirSync(artifactDir, { recursive: true });
  fs.writeFileSync(cdpPath, "", "utf8");
  let ws = new RawWebSocket(browserWs);
  let expectedRemoteIdentity = null;
  let remoteBinding = null;
  try {
    expectedRemoteIdentity = parseExpectedRemoteIdentity(
      process.env.REMOTE_CDP_IDENTITY_JSON || ""
    );
    await ws.connect();
    if (expectedRemoteIdentity) {
      remoteBinding = compareRemoteIdentity(expectedRemoteIdentity, {});
      const actualIdentity = await command(ws, "Browser.getVersion");
      remoteBinding = assertRemoteIdentity(expectedRemoteIdentity, actualIdentity);
    }
  } catch (err) {
    if (err && err.binding) remoteBinding = err.binding;
    else if (remoteBinding) {
      remoteBinding.probe_error = String(err && err.message ? err.message : err);
    }
    fail(
      "script_error",
      `remote binding failed: ${String(err && err.message ? err.message : err)}`,
      remoteBinding
    );
    ws.close();
    return;
  }

  // Open a fresh page via a flat Target session (uniform across engines;
  // Lightpanda rejects /json/new page endpoints with BrowserContextNotLoaded).
  targetCreationState = "requested";
  try {
    const created = await command(ws, "Target.createTarget", { url: "about:blank" });
    if (!created.targetId) {
      targetCreationState = "ambiguous";
      throw new Error("Target.createTarget returned no targetId");
    }
    ownedTargetId = created.targetId;
    targetCreationState = "created";
    const attached = await command(ws, "Target.attachToTarget", { targetId: created.targetId, flatten: true });
    if (!attached.sessionId) throw new Error("Target.attachToTarget returned no sessionId");
    sessionId = attached.sessionId;
  } catch (err) {
    if (!ownedTargetId) {
      targetCreationState = err && err.cdpRejected === true
        ? "rejected"
        : "ambiguous";
    }
    const cleanup = await closeOwnedTarget(ws);
    if (cleanup.confirmed !== true) {
      fail(
        "script_error",
        `page creation outcome was ambiguous; isolation cannot be confirmed: ${String(err && err.message ? err.message : err)}`,
        remoteBinding,
        { target_cleanup: cleanup, isolation_restored: false }
      );
      ws.close();
      return;
    }
    if (expectedRemoteIdentity) {
      fail(
        "script_error",
        `strict remote page bootstrap failed without reconnect: ${String(err && err.message ? err.message : err)}`,
        remoteBinding,
        { target_cleanup: cleanup, isolation_restored: cleanup.confirmed === true }
      );
      ws.close();
      return;
    }
    const cdpPort = process.env.CDP_PORT;
    if (cdpPort) {
      try {
        const resp = await fetch(`http://127.0.0.1:${cdpPort}/json/new?about:blank`, { method: "PUT" });
        const json = resp && resp.ok ? await resp.json() : null;
        if (json && json.webSocketDebuggerUrl) {
          ws.close();
          ws = new RawWebSocket(json.webSocketDebuggerUrl);
          await ws.connect();
        }
      } catch (err2) {
        // continue on the original connection, sessionless
      }
    }
  }

  let outcome;
  try {
    await command(ws, "Page.enable");
    await command(ws, "Runtime.enable");
    if (process.env.TASK_URL_PRELOADED !== "1") {
      await command(ws, "Page.navigate", { url: taskUrl });
    }
    await waitForTaskFunction(ws);

    const expression = `
      (async () => {
        if (typeof window.__ABB_RUN_IDB_TASK__ !== "function") {
          throw new Error("__ABB_RUN_IDB_TASK__ is not available");
        }
        return await window.__ABB_RUN_IDB_TASK__();
      })()
    `;
    const evalResult = await command(ws, "Runtime.evaluate", {
      expression,
      awaitPromise: true,
      returnByValue: true
    });
    const remote = evalResult.result || {};
    if (remote.subtype === "error" || evalResult.exceptionDetails) {
      const details = evalResult.exceptionDetails || {};
      const message =
        (details.exception && details.exception.description) ||
        details.text ||
        remote.description ||
        "Runtime.evaluate returned exceptionDetails";
      throw new Error(message);
    }

    const value = remote.value || {};
    outcome = {
      ok: true,
      answer: String(value.answer ?? ""),
      observations: {
        ...(value.observations || {}),
        ...(remoteBinding ? { binding: remoteBinding } : {}),
      },
      metrics: { cdp_call_count: cdpCallCount, cdp_error_count: cdpErrorCount, ws_disconnect_count: 0 }
    };
  } catch (err) {
    const msg = String(err && err.message ? err.message : err);
    const klass = /not found|unsupported|method|browsercontextnotloaded|is not defined/i.test(msg) ? "engine_unsupported" : "script_error";
    outcome = {
      ok: false,
      error: { class: klass, message: msg },
      observations: remoteBinding ? { binding: remoteBinding } : {},
      metrics: { cdp_call_count: cdpCallCount, cdp_error_count: cdpErrorCount, ws_disconnect_count: 0 }
    };
  }
  const cleanup = await closeOwnedTarget(ws);
  ws.close();
  outcome.observations = {
    ...(outcome.observations || {}),
    target_cleanup: cleanup,
    isolation_restored: cleanup.confirmed === true
  };
  outcome.metrics = {
    ...(outcome.metrics || {}),
    cdp_call_count: cdpCallCount,
    cdp_error_count: cdpErrorCount,
    ws_disconnect_count: 0
  };
  if (expectedRemoteIdentity && cleanup.confirmed !== true) {
    outcome = {
      ok: false,
      error: {
        class: "script_error",
        message: `remote target cleanup was not confirmed: ${JSON.stringify(cleanup)}`
      },
      observations: { ...outcome.observations, primary_outcome: outcome },
      metrics: outcome.metrics
    };
  }
  process.stdout.write(JSON.stringify(outcome));
}

main().catch((err) => {
  fail("script_error", String(err && err.message ? err.message : err));
});
