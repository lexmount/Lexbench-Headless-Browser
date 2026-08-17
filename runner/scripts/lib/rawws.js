"use strict";

// Shared raw-WebSocket CDP client for node_cdp_probe driver scripts.
// Mirrors the hand-rolled client in storage_indexeddb_inventory_001.js.

const crypto = require("crypto");
const fs = require("fs");
const net = require("net");

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
    const payload = await this.recvFrame();
    if (payload === null) {
      throw new Error("websocket disconnected");
    }
    return JSON.parse(payload.toString("utf8"));
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

class CdpSession {
  constructor(wsUrl, cdpPath) {
    this.ws = new RawWebSocket(wsUrl);
    this.cdpPath = cdpPath;
    this.nextId = 1;
    this.callCount = 0;
    this.errorCount = 0;
    this.sessionId = null;
    this.targetId = null;
    this.targetOwner = null;
    this.cdpPort = null;
  }

  async connect() {
    await this.ws.connect();
  }

  // Create a fresh page and address it via a flat Target session (works on
  // Chrome, Moli and Lightpanda; LP rejects /json/new page endpoints with
  // BrowserContextNotLoaded). Falls back to a flat page websocket from
  // /json/new when the Target flow is unavailable.
  async openPage(cdpPort) {
    this.cdpPort = cdpPort || null;
    try {
      const created = await this.command("Target.createTarget", { url: "about:blank" });
      if (created.targetId) {
        this.targetId = created.targetId;
        this.targetOwner = "browser_cdp";
        const attached = await this.command("Target.attachToTarget", { targetId: created.targetId, flatten: true });
        if (attached.sessionId) {
          this.sessionId = attached.sessionId;
          return;
        }
        // The target is ours even if attach failed or returned an incomplete
        // response. Do not leak it before trying the /json/new fallback.
        await this._closeOwnedTarget();
      }
    } catch (err) {
      await this._closeOwnedTarget();
      // fall through to /json/new
    }
    if (cdpPort) {
      try {
        const resp = await fetch(`http://127.0.0.1:${cdpPort}/json/new?about:blank`, { method: "PUT" });
        const json = resp && resp.ok ? await resp.json() : null;
        if (json && json.webSocketDebuggerUrl) {
          this.ws.close();
          this.ws = new RawWebSocket(json.webSocketDebuggerUrl);
          await this.ws.connect();
          this.sessionId = null;
          this.targetId =
            json.id ||
            json.targetId ||
            new URL(json.webSocketDebuggerUrl).pathname.split("/").filter(Boolean).pop() ||
            null;
          this.targetOwner = this.targetId ? "http_json_new" : null;
          return;
        }
      } catch (err) {
        // keep the existing connection; commands go sessionless
      }
    }
  }

  _trace(obj) {
    if (this.cdpPath) {
      fs.appendFileSync(this.cdpPath, JSON.stringify(obj) + "\n", "utf8");
    }
  }

  async command(method, params = {}) {
    const id = this.nextId++;
    this.callCount += 1;
    const payload = { id, method, params };
    if (this.sessionId && !method.startsWith("Target.")) {
      payload.sessionId = this.sessionId;
    }
    this._trace({ ts: new Date().toISOString(), direction: "send", id, method, params, sessionId: payload.sessionId });
    this.ws.send(JSON.stringify(payload));
    while (true) {
      const payload = await this.ws.recvJson();
      if (payload.id === id) {
        if (payload.error) {
          this.errorCount += 1;
          this._trace({ ts: new Date().toISOString(), direction: "recv", id, method, error: payload.error });
          throw new Error(`${method}: ${payload.error.message || JSON.stringify(payload.error)}`);
        }
        this._trace({ ts: new Date().toISOString(), direction: "recv", id, method, result: payload.result || {} });
        return payload.result || {};
      }
      this._trace({ ts: new Date().toISOString(), direction: "event", method: payload.method, params: payload.params || {} });
    }
  }

  async _closeOwnedTarget() {
    if (!this.targetId) return { closed: false, reason: "no_owned_target" };
    const targetId = this.targetId;
    const owner = this.targetOwner;
    let closed = false;
    let error = null;
    try {
      if (owner === "browser_cdp") {
        const result = await Promise.race([
          this.command("Target.closeTarget", { targetId }),
          new Promise((_, reject) => {
            const timer = setTimeout(
              () => reject(new Error("Target.closeTarget cleanup timeout")),
              1500
            );
            if (timer.unref) timer.unref();
          })
        ]);
        closed = result.success !== false;
      } else if (owner === "http_json_new" && this.cdpPort) {
        const response = await Promise.race([
          fetch(`http://127.0.0.1:${this.cdpPort}/json/close/${encodeURIComponent(targetId)}`),
          new Promise((_, reject) => {
            const timer = setTimeout(
              () => reject(new Error("HTTP target cleanup timeout")),
              1500
            );
            if (timer.unref) timer.unref();
          })
        ]);
        closed = Boolean(response && response.ok);
        if (!closed) error = `HTTP ${response && response.status}`;
      }
    } catch (err) {
      error = String(err && err.message ? err.message : err);
    } finally {
      this._trace({
        ts: new Date().toISOString(),
        direction: "cleanup",
        method: owner === "http_json_new" ? "HTTP /json/close" : "Target.closeTarget",
        targetId,
        closed,
        error
      });
      this.targetId = null;
      this.targetOwner = null;
      this.sessionId = null;
    }
    return { closed, error };
  }

  async close() {
    try {
      return await this._closeOwnedTarget();
    } finally {
      this.ws.close();
    }
  }
}

function wait(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function classifyError(message) {
  return /not found|unsupported|method|browsercontextnotloaded|is not defined/i.test(message) ? "engine_unsupported" : "script_error";
}

module.exports = { RawWebSocket, CdpSession, wait, classifyError };
