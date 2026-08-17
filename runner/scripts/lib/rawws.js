"use strict";

// Shared raw-WebSocket CDP client for node_cdp_probe driver scripts.
// Mirrors the hand-rolled client in storage_indexeddb_inventory_001.js.

const crypto = require("crypto");
const fs = require("fs");
const net = require("net");
const tls = require("tls");
const {
  assertRemoteIdentity,
  compareRemoteIdentity,
  parseExpectedRemoteIdentity,
} = require("./remote_identity");
const SKIP_FRAME = Symbol("skip-frame");

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
    const payload = await this.recvFrame();
    if (payload === null) {
      throw new Error("websocket disconnected");
    }
    return JSON.parse(payload.toString("utf8"));
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
    this.targetCreationState = "not_requested";
    this.cleanupTimeoutMs = 1500;
    this.cdpPort = null;
    this.remoteBinding = null;
  }

  async connect() {
    await this.ws.connect();
  }

  async bindRemoteIdentity(identityText) {
    const expected = parseExpectedRemoteIdentity(identityText);
    if (!expected) return null;
    this.remoteBinding = compareRemoteIdentity(expected, {});
    try {
      const actual = await this.command("Browser.getVersion");
      this.remoteBinding = assertRemoteIdentity(expected, actual);
      return this.remoteBinding;
    } catch (error) {
      if (error && error.binding) this.remoteBinding = error.binding;
      else this.remoteBinding.probe_error = String(
        error && error.message ? error.message : error
      );
      throw error;
    }
  }

  // Create a fresh page and address it via a flat Target session (works on
  // Chrome, Moli and Lightpanda; LP rejects /json/new page endpoints with
  // BrowserContextNotLoaded). Falls back to a flat page websocket from
  // /json/new when the Target flow is unavailable.
  async openPage(cdpPort, options = {}) {
    this.cdpPort = cdpPort || null;
    const allowHttpFallback = options.allowHttpFallback !== false;
    let targetError = null;
    this.targetCreationState = "requested";
    try {
      const created = await this.command("Target.createTarget", { url: "about:blank" });
      if (created.targetId) {
        this.targetId = created.targetId;
        this.targetOwner = "browser_cdp";
        this.targetCreationState = "created";
        const attached = await this.command("Target.attachToTarget", { targetId: created.targetId, flatten: true });
        if (attached.sessionId) {
          this.sessionId = attached.sessionId;
          return;
        }
      } else {
        // A success envelope without a targetId cannot prove that the remote
        // side did not create a target.
        this.targetCreationState = "ambiguous";
      }
      throw new Error("Target.createTarget/attachToTarget returned no usable flat session");
    } catch (err) {
      targetError = err;
      if (!this.targetId) {
        this.targetCreationState = err && err.cdpRejected === true
          ? "rejected"
          : "ambiguous";
      }
      const cleanup = await this._closeOwnedTarget();
      if (targetError && typeof targetError === "object") {
        targetError.targetCleanup = cleanup;
      }
      if (cleanup.confirmed !== true) {
        const cleanupError = new Error(
          `page bootstrap cleanup unconfirmed for target ${cleanup.target_id}: ${JSON.stringify(cleanup.attempts)}`
        );
        cleanupError.targetCleanup = cleanup;
        throw cleanupError;
      }
      // Local engines retain their historical /json/new compatibility path.
      // A strict remote attempt must keep the verified WebSocket connection.
    }
    if (!allowHttpFallback) {
      // An explicit CDP error proves create/attach was rejected. Preserve that
      // typed engine response (plus its target-free/closed cleanup evidence)
      // so callers can distinguish unsupported protocol from ambiguous infra.
      if (targetError && targetError.cdpRejected === true) {
        throw targetError;
      }
      const strictError = new Error(
        `strict remote page bootstrap failed without reconnect: ${String(
          targetError && targetError.message ? targetError.message : targetError
        )}`
      );
      if (targetError && targetError.targetCleanup) {
        strictError.targetCleanup = targetError.targetCleanup;
      }
      throw strictError;
    }
    if (cdpPort) {
      this.targetCreationState = "requested";
      try {
        const resp = await fetch(`http://127.0.0.1:${cdpPort}/json/new?about:blank`, { method: "PUT" });
        if (!resp || !resp.ok) {
          this.targetCreationState = "rejected";
          return;
        }
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
          if (!this.targetId) {
            this.targetCreationState = "ambiguous";
            throw new Error("HTTP /json/new returned no target identifier");
          }
          this.targetOwner = "http_json_new";
          this.targetCreationState = "created";
          return;
        }
        this.targetCreationState = "ambiguous";
        throw new Error("HTTP /json/new returned no usable page WebSocket");
      } catch (err) {
        if (this.targetCreationState === "requested") {
          this.targetCreationState = "ambiguous";
        }
        const cleanup = await this._closeOwnedTarget();
        if (cleanup.confirmed !== true) {
          const cleanupError = new Error(
            `HTTP page bootstrap cleanup unconfirmed: ${JSON.stringify(cleanup)}`
          );
          cleanupError.targetCleanup = cleanup;
          throw cleanupError;
        }
        // An explicit HTTP rejection is safely target-free; retain the
        // historical sessionless local-engine compatibility path.
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
          const error = new Error(`${method}: ${payload.error.message || JSON.stringify(payload.error)}`);
          error.cdpRejected = true;
          error.cdpError = payload.error;
          throw error;
        }
        this._trace({ ts: new Date().toISOString(), direction: "recv", id, method, result: payload.result || {} });
        return payload.result || {};
      }
      this._trace({ ts: new Date().toISOString(), direction: "event", method: payload.method, params: payload.params || {} });
    }
  }

  async _closeOwnedTarget() {
    if (!this.targetId) {
      const creationState = this.targetCreationState;
      const ambiguous = creationState === "requested"
        || creationState === "ambiguous"
        || creationState === "cleanup_unconfirmed";
      const result = {
        closed: !ambiguous,
        confirmed: !ambiguous,
        reason: ambiguous
          ? "target_creation_ambiguous"
          : creationState === "rejected"
            ? "target_creation_rejected"
            : "no_owned_target",
        creation_state: creationState,
        target_id: null,
        attempts: []
      };
      if (ambiguous) {
        this._trace({
          ts: new Date().toISOString(),
          direction: "cleanup",
          method: "Target.createTarget",
          targetId: null,
          closed: false,
          reason: result.reason,
          creationState
        });
      }
      return result;
    }
    const targetId = this.targetId;
    const owner = this.targetOwner;
    let closed = false;
    const attempts = [];
    try {
      for (let attempt = 1; attempt <= 2 && !closed; attempt += 1) {
        try {
          if (owner === "browser_cdp") {
            let cleanupTimer;
            const closeCommand = this.command("Target.closeTarget", { targetId });
            // If the deadline wins, terminating the socket below rejects this
            // sole reader. Never start a second recvJson loop while it is live.
            closeCommand.catch(() => {});
            let result;
            try {
              result = await Promise.race([
                closeCommand,
                new Promise((_, reject) => {
                  cleanupTimer = setTimeout(
                    () => {
                      const error = new Error("Target.closeTarget cleanup timeout");
                      error.cleanupTimedOut = true;
                      reject(error);
                    },
                    this.cleanupTimeoutMs
                  );
                  if (cleanupTimer.unref) cleanupTimer.unref();
                })
              ]);
            } finally {
              clearTimeout(cleanupTimer);
            }
            closed = result.success === true;
            attempts.push({ attempt, success: result.success, confirmed: closed });
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
            attempts.push({ attempt, status: response && response.status, confirmed: closed });
          } else {
            attempts.push({ attempt, confirmed: false, error: `unknown target owner ${owner}` });
            break;
          }
        } catch (err) {
          attempts.push({
            attempt,
            confirmed: false,
            error: String(err && err.message ? err.message : err),
            ...(err && err.cleanupTimedOut ? { transport_terminated: true } : {})
          });
          if (err && err.cleanupTimedOut) {
            // The timed-out command still owns the only response reader. Close
            // the transport so it settles before any retry can be attempted.
            this.ws.close();
            break;
          }
        }
      }
    } finally {
      this._trace({
        ts: new Date().toISOString(),
        direction: "cleanup",
        method: owner === "http_json_new" ? "HTTP /json/close" : "Target.closeTarget",
        targetId,
        closed,
        attempts
      });
      this.targetId = null;
      this.targetOwner = null;
      this.sessionId = null;
      this.targetCreationState = closed ? "closed" : "cleanup_unconfirmed";
    }
    return {
      closed,
      confirmed: closed,
      target_id: targetId,
      owner,
      attempts
    };
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
