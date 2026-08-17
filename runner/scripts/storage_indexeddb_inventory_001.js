#!/usr/bin/env node
"use strict";

const crypto = require("crypto");
const fs = require("fs");
const net = require("net");
const path = require("path");

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

function fail(errorClass, message) {
  process.stdout.write(JSON.stringify({
    ok: false,
    error: { class: errorClass, message },
    observations: {},
    metrics: { cdp_call_count: cdpCallCount, cdp_error_count: cdpErrorCount, ws_disconnect_count: 0 }
  }));
}

class RawWebSocket {
  constructor(wsUrl) {
    this.url = new URL(wsUrl);
    this.socket = null;
    this.buffer = Buffer.alloc(0);
    this.waiters = [];
  }

  async connect() {
    if (this.url.protocol !== "ws:") {
      throw new Error(`unsupported websocket protocol: ${this.url.protocol}`);
    }
    const host = this.url.hostname;
    const port = Number(this.url.port || 80);
    const requestPath = `${this.url.pathname || "/"}${this.url.search || ""}`;
    const key = crypto.randomBytes(16).toString("base64");
    this.socket = net.createConnection({ host, port });
    this.socket.on("data", (chunk) => {
      this.buffer = Buffer.concat([this.buffer, chunk]);
      this._drainWaiters();
    });
    this.socket.on("close", () => this._drainWaiters());
    await new Promise((resolve, reject) => {
      this.socket.once("connect", resolve);
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
    if (opcode === 8) return null;
    if (opcode === 9) {
      this._sendPong(payload);
      return undefined;
    }
    if (opcode !== 1 && opcode !== 2 && opcode !== 0) {
      return undefined;
    }
    return payload;
  }

  _sendPong(payload) {
    const header = Buffer.from([0x8a, payload.length]);
    this.socket.write(Buffer.concat([header, payload]));
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
        throw new Error(`${method}: ${payload.error.message || JSON.stringify(payload.error)}`);
      }
      writeJsonl(cdpPath, { ts: nowIso(), direction: "recv", id, method, result: payload.result || {} });
      return payload.result || {};
    }
    writeJsonl(cdpPath, { ts: nowIso(), direction: "event", method: payload.method, params: payload.params || {} });
  }
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
  await ws.connect();

  // Open a fresh page via a flat Target session (uniform across engines;
  // Lightpanda rejects /json/new page endpoints with BrowserContextNotLoaded).
  try {
    const created = await command(ws, "Target.createTarget", { url: "about:blank" });
    if (created.targetId) {
      const attached = await command(ws, "Target.attachToTarget", { targetId: created.targetId, flatten: true });
      if (attached.sessionId) sessionId = attached.sessionId;
    }
  } catch (err) {
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
    process.stdout.write(JSON.stringify({
      ok: true,
      answer: String(value.answer ?? ""),
      observations: value.observations || {},
      metrics: { cdp_call_count: cdpCallCount, cdp_error_count: cdpErrorCount, ws_disconnect_count: 0 }
    }));
  } catch (err) {
    const msg = String(err && err.message ? err.message : err);
    const klass = /not found|unsupported|method|browsercontextnotloaded|is not defined/i.test(msg) ? "engine_unsupported" : "script_error";
    fail(klass, msg);
  } finally {
    ws.close();
  }
}

main().catch((err) => {
  fail("script_error", String(err && err.message ? err.message : err));
});
