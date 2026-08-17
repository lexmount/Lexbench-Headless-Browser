#!/usr/bin/env node
"use strict";

// Small non-formal probe for the versioned GitHub Pages fixtures created for
// issue #136. It complements the benchmark-first runs with web-platform cases
// that need only a stable public static origin.

const fs = require("fs");
const path = require("path");
const crypto = require("crypto");
const { execFileSync } = require("child_process");
const { chromium } = require("playwright-core");

const DEFAULT_ENDPOINT = "wss://kitesurf.cloudflare.app/devtools/browser";
const DEFAULT_PRODUCT = "Chrome/145.0.0.0";
const DEFAULT_PROTOCOL_VERSION = "1.3";
const DEFAULT_REVISION = "@kitesurf";
const PAGE_CASE_FIXTURE_PATHS = Object.freeze({
  core_actions: ["v1/core/index.html"],
  nested_frames: [
    "v1/frames/index.html",
    "v1/frames/child.html",
    "v1/frames/grandchild.html",
  ],
  static_fetch: [
    "v1/network/index.html",
    "v1/network/style.css",
    "v1/network/pixel.svg",
    "v1/network/script.js",
    "v1/network/data.json",
  ],
  basic_storage: ["v1/storage/index.html"],
  indexeddb_roundtrip: ["v1/storage/index.html"],
  dedicated_worker: ["v1/workers/index.html", "v1/workers/worker.js"],
  shared_worker: ["v1/workers/index.html", "v1/workers/shared-worker.js"],
  service_worker: ["v1/workers/index.html", "v1/workers/sw.js"],
  history_push_state: ["v1/lifecycle/index.html"],
  csp_blocks_eval: ["v1/security/index.html", "v1/security/security.js"],
  core_dom_click_direct: ["v1/core/index.html"],
  static_fetch_direct: [
    "v1/network/index.html",
    "v1/network/style.css",
    "v1/network/pixel.svg",
    "v1/network/script.js",
    "v1/network/data.json",
  ],
  dedicated_worker_direct: ["v1/workers/index.html", "v1/workers/worker.js"],
  shared_worker_direct: ["v1/workers/index.html", "v1/workers/shared-worker.js"],
  service_worker_direct: ["v1/workers/index.html", "v1/workers/sw.js"],
  history_push_state_direct: ["v1/lifecycle/index.html"],
});

function option(name, fallback) {
  const index = process.argv.indexOf(name);
  return index >= 0 ? process.argv[index + 1] : fallback;
}

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

const REPO_ROOT = path.resolve(__dirname, "..");
const DEFAULT_FIXTURE_MANIFEST = path.join(
  REPO_ROOT,
  "config",
  "kitesurf_static_fixture.json"
);

// The static origin is a run parameter, and the manifest already pins where
// the fixtures are published, so the default comes from there rather than
// from whichever host the probe was first written against.
function defaultBaseUrl() {
  const manifest = JSON.parse(
    fs.readFileSync(DEFAULT_FIXTURE_MANIFEST, "utf8")
  );
  assert(
    typeof manifest.deployment_base_url === "string",
    "fixture manifest deployment_base_url must be a string"
  );
  return manifest.deployment_base_url;
}

function gitText(args) {
  try {
    return execFileSync("git", args, {
      cwd: REPO_ROOT,
      encoding: "utf8",
      stdio: ["ignore", "pipe", "ignore"],
    }).trim() || null;
  } catch (_) {
    return null;
  }
}

function gitBytes(args) {
  try {
    return execFileSync("git", args, {
      cwd: REPO_ROOT,
      encoding: "buffer",
      stdio: ["ignore", "pipe", "ignore"],
    });
  } catch (_) {
    return Buffer.alloc(0);
  }
}

function sha256(value) {
  return crypto.createHash("sha256").update(value).digest("hex");
}

function sourceProvenance() {
  const status = gitBytes(["status", "--porcelain=v1", "--untracked-files=all"])
    .toString("utf8")
    .replace(/\n$/, "");
  const trackedDiff = gitBytes(["diff", "--binary", "HEAD"]);
  const untrackedNames = gitBytes(["ls-files", "--others", "--exclude-standard", "-z"])
    .toString("utf8")
    .split("\0")
    .filter(Boolean)
    .sort();
  const untrackedParts = [];
  for (const name of untrackedNames) {
    const file = path.join(REPO_ROOT, name);
    untrackedParts.push(Buffer.from(name), Buffer.from("\0"));
    try {
      const stat = fs.lstatSync(file);
      if (stat.isSymbolicLink()) {
        untrackedParts.push(Buffer.from("symlink\0"), Buffer.from(fs.readlinkSync(file)));
      } else if (stat.isFile()) {
        untrackedParts.push(Buffer.from("file\0"), fs.readFileSync(file));
      } else {
        untrackedParts.push(Buffer.from("other\0"));
      }
    } catch (error) {
      untrackedParts.push(Buffer.from(`unreadable\0${error.code || error.name}`));
    }
    untrackedParts.push(Buffer.from("\0"));
  }
  const untrackedState = Buffer.concat(untrackedParts);
  const originMain = gitText(["rev-parse", "--verify", "origin/main"]);
  const mergeBase = originMain
    ? gitText(["merge-base", "HEAD", "origin/main"])
    : null;
  const sourceFiles = {};
  for (const name of [
    "tools/kitesurf_pages_probe.js",
    "tools/kitesurf_static_fixture.py",
    "config/kitesurf_static_fixture.json",
    "package.json",
    "package-lock.json",
  ]) {
    sourceFiles[name] = sha256(fs.readFileSync(path.join(REPO_ROOT, name)));
  }
  return {
    schema: "experimental.kitesurf_source.v2",
    head: gitText(["rev-parse", "HEAD"]),
    head_tree: gitText(["rev-parse", "HEAD^{tree}"]),
    branch: gitText(["branch", "--show-current"]),
    origin_main: originMain,
    merge_base_origin_main: mergeBase,
    contains_origin_main: Boolean(originMain && mergeBase === originMain),
    dirty: Boolean(status),
    status_porcelain: status ? status.split("\n") : [],
    tracked_diff_sha256: sha256(trackedDiff),
    untracked_paths: untrackedNames,
    untracked_content_sha256: sha256(untrackedState),
    worktree_state_sha256: sha256(
      Buffer.concat([trackedDiff, Buffer.from("\0untracked\0"), untrackedState])
    ),
    source_files_sha256: sourceFiles,
  };
}

function verifyStaticFixture(baseUrl, manifestPath, outputPath) {
  const helper = path.join(REPO_ROOT, "tools", "kitesurf_static_fixture.py");
  try {
    execFileSync(
      "python3",
      [
        helper,
        "--base-url",
        baseUrl,
        "--manifest",
        manifestPath,
        "--output",
        outputPath,
      ],
      {
        cwd: REPO_ROOT,
        encoding: "utf8",
        stdio: ["ignore", "pipe", "pipe"],
      }
    );
  } catch (error) {
    const stderr = String(error && error.stderr ? error.stderr : "").trim();
    throw new Error(`static fixture verification failed${stderr ? `: ${stderr}` : ""}`);
  }
  const report = JSON.parse(fs.readFileSync(outputPath, "utf8"));
  assert(report.verified === true, "static fixture verification was not confirmed");
  return report;
}

function fixturePathForUrl(baseUrl, responseUrl) {
  const base = new URL(`${baseUrl.replace(/\/$/, "")}/`);
  const observed = new URL(responseUrl);
  if (observed.origin !== base.origin) return null;
  if (!observed.pathname.startsWith(base.pathname)) return null;
  let relative;
  try {
    relative = decodeURIComponent(observed.pathname.slice(base.pathname.length));
  } catch (_) {
    return null;
  }
  if (!relative || relative.endsWith("/")) relative += "index.html";
  return relative;
}

class RuntimeFixtureVerifier {
  constructor(baseUrl, preflightReport) {
    assert(
      preflightReport && preflightReport.verified === true,
      "runtime fixture verifier requires a successful preflight report"
    );
    this.baseUrl = baseUrl.replace(/\/$/, "");
    this.expected = new Map(
      (preflightReport.files || []).map((row) => [
        row.path,
        {
          size: row.expected_size,
          sha256: row.expected_sha256,
        },
      ])
    );
    assert(this.expected.size > 0, "runtime fixture verifier has no files");
    this.activeCase = null;
    this.activeRequiredPaths = null;
    this.pending = new Set();
    this.records = [];
    this.completedCases = [];
    this.context = null;
    this.listener = (response) => this.observe(response);
  }

  attach(context) {
    assert(this.context === null, "runtime fixture verifier is already attached");
    this.context = context;
    context.on("response", this.listener);
  }

  detach() {
    if (this.context && typeof this.context.off === "function") {
      this.context.off("response", this.listener);
    }
    this.context = null;
  }

  beginCase(id) {
    assert(this.activeCase === null, `fixture verifier still owns ${this.activeCase}`);
    const requiredPaths = PAGE_CASE_FIXTURE_PATHS[id];
    assert(Array.isArray(requiredPaths) && requiredPaths.length > 0, `fixture dependencies are not registered for ${id}`);
    const missingManifestPaths = requiredPaths.filter((item) => !this.expected.has(item));
    assert(missingManifestPaths.length === 0, `fixture manifest lacks dependencies for ${id}: ${missingManifestPaths.join(", ")}`);
    this.activeCase = id;
    this.activeRequiredPaths = [...new Set(requiredPaths)];
  }

  observe(response) {
    const relative = fixturePathForUrl(this.baseUrl, response.url());
    if (!relative) return;
    const expected = this.expected.get(relative);
    const record = {
      case_id: this.activeCase,
      path: relative,
      url: response.url(),
      status: response.status(),
      verified: false,
    };
    if (!expected) {
      record.error = "browser response path is absent from pinned fixture manifest";
      this.records.push(record);
      return;
    }
    record.expected_size = expected.size;
    record.expected_sha256 = expected.sha256;
    this.records.push(record);
    const pending = Promise.resolve()
      .then(() => response.body())
      .then((body) => {
        const bytes = Buffer.from(body);
        record.actual_size = bytes.length;
        record.actual_sha256 = sha256(bytes);
        record.verified =
          record.status === 200
          && record.actual_size === record.expected_size
          && record.actual_sha256 === record.expected_sha256;
        if (!record.verified) {
          record.error = "browser response does not match pinned fixture";
        }
      })
      .catch((error) => {
        record.error = `response body unavailable: ${String(
          error && error.message ? error.message : error
        )}`;
      })
      .finally(() => this.pending.delete(pending));
    this.pending.add(pending);
  }

  async settle() {
    // Page/context events can be queued immediately after close resolves. Two
    // empty event-loop turns prevent a late response from escaping the hash
    // gate between sequential cases.
    let emptyTurns = 0;
    while (emptyTurns < 2) {
      await new Promise((resolve) => setImmediate(resolve));
      if (this.pending.size) {
        await Promise.allSettled([...this.pending]);
        emptyTurns = 0;
      } else {
        emptyTurns += 1;
      }
    }
  }

  async finishCase(id) {
    assert(this.activeCase === id, `fixture verifier active case mismatch for ${id}`);
    assert(Array.isArray(this.activeRequiredPaths), `fixture dependencies missing for ${id}`);
    await this.settle();
    const requiredPaths = this.activeRequiredPaths;
    this.activeCase = null;
    this.activeRequiredPaths = null;
    const records = this.records.filter((record) => record.case_id === id);
    const observedPaths = new Set(records.map((record) => record.path));
    const requiredPathSet = new Set(requiredPaths);
    const missingPaths = requiredPaths.filter((item) => !observedPaths.has(item));
    const unexpectedPaths = [...observedPaths]
      .filter((item) => !requiredPathSet.has(item))
      .sort();
    const verified =
      missingPaths.length === 0
      && unexpectedPaths.length === 0
      && records.length > 0
      && records.every((record) => record.verified);
    const result = {
      verified,
      required_paths: requiredPaths,
      missing_paths: missingPaths,
      unexpected_paths: unexpectedPaths,
      response_count: records.length,
      paths: [...observedPaths].sort(),
      responses: records,
    };
    this.completedCases.push({ id, ...result });
    if (!verified) {
      throw new Error(
        `browser-consumed fixture was not verified for ${id}: ${JSON.stringify(result)}`
      );
    }
    return result;
  }

  summary() {
    const unattributed = this.records.filter((record) => record.case_id === null);
    const verified =
      this.completedCases.length > 0
      && this.completedCases.every((item) => item.verified)
      && unattributed.length === 0
      && this.activeCase === null;
    return {
      schema: "experimental.kitesurf_runtime_fixture_verification.v2",
      verified,
      response_count: this.records.length,
      completed_case_count: this.completedCases.length,
      paths: [...new Set(this.records.map((record) => record.path))].sort(),
      unattributed_response_count: unattributed.length,
      active_case: this.activeCase,
      active_required_paths: this.activeRequiredPaths,
      cases: this.completedCases,
      responses: this.records,
    };
  }
}

class CaseAbortError extends Error {
  constructor(
    code,
    id,
    cause,
    started,
    primaryOutcome = null,
    isolationRestored = false
  ) {
    const detail = String(cause && cause.message ? cause.message : cause);
    super(`${code} for ${id}; aborting before another case starts: ${detail}`);
    this.name = "CaseAbortError";
    this.code = code;
    this.abortRound = true;
    this.isolationRestored = isolationRestored;
    this.row = {
      id,
      status: "infra",
      duration_ms: Date.now() - started,
      failure_class: code,
      error: this.message.slice(0, 1200),
      isolation_restored: isolationRestored,
      ...(primaryOutcome ? { primary_outcome: primaryOutcome } : {}),
    };
  }
}

class CaseIsolationError extends CaseAbortError {
  constructor(code, id, cause, started, primaryOutcome = null) {
    super(code, id, cause, started, primaryOutcome, false);
    this.name = "CaseIsolationError";
  }
}

async function closePageConfirmed(page, label) {
  let cleanupTimer;
  try {
    const closeOperation = Promise.resolve().then(
      () => page.close({ runBeforeUnload: false })
    );
    closeOperation.catch(() => {});
    await Promise.race([
      closeOperation,
      new Promise((_, reject) => {
        cleanupTimer = setTimeout(
          () => reject(new Error(`page cleanup timeout: ${label}`)),
          5000
        );
      }),
    ]);
    if (typeof page.isClosed === "function" && !page.isClosed()) {
      throw new Error(`page close returned without closing: ${label}`);
    }
  } catch (error) {
    if (!(typeof page.isClosed === "function" && page.isClosed())) throw error;
  } finally {
    clearTimeout(cleanupTimer);
  }
}

async function closeBrowserConfirmed(browser, label, timeoutMs = 5000) {
  const shutdown = {
    schema: "experimental.kitesurf_browser_shutdown.v1",
    requested: true,
    close_resolved: false,
    connected_after: null,
    confirmed: false,
  };
  let timer;
  const closeOperation = Promise.resolve().then(() => browser.close());
  closeOperation.catch(() => {});
  try {
    await Promise.race([
      closeOperation,
      new Promise((_, reject) => {
        timer = setTimeout(
          () => reject(new Error(`browser shutdown timeout: ${label}`)),
          timeoutMs
        );
      }),
    ]);
    shutdown.close_resolved = true;
  } catch (error) {
    shutdown.error = String(error && error.message ? error.message : error).slice(
      0,
      1200
    );
  } finally {
    clearTimeout(timer);
  }
  try {
    shutdown.connected_after =
      typeof browser.isConnected === "function" ? browser.isConnected() : null;
  } catch (error) {
    shutdown.connection_check_error = String(
      error && error.message ? error.message : error
    ).slice(0, 1200);
  }
  shutdown.confirmed = shutdown.connected_after === false;
  if (!shutdown.confirmed) {
    const error = new Error(
      `browser shutdown was not confirmed for ${label}: ${JSON.stringify(shutdown)}`
    );
    error.browserShutdown = shutdown;
    throw error;
  }
  return shutdown;
}

async function shutdownBrowserForRound(browser, round, priorAbort = null) {
  try {
    return {
      shutdown: await closeBrowserConfirmed(browser, `round ${round}`),
      isolationAbort: priorAbort,
    };
  } catch (error) {
    const shutdown = error.browserShutdown || {
      schema: "experimental.kitesurf_browser_shutdown.v1",
      requested: true,
      confirmed: false,
      error: String(error && error.message ? error.message : error).slice(0, 1200),
    };
    const failure = {
      code: "browser_shutdown_unconfirmed",
      round,
      message: String(error && error.message ? error.message : error).slice(0, 1200),
      isolation_restored: false,
      browser_shutdown: shutdown,
    };
    return {
      shutdown,
      isolationAbort: priorAbort
        ? {
            ...priorAbort,
            isolation_restored: false,
            browser_shutdown_failure: failure,
          }
        : failure,
    };
  }
}

async function cleanupServiceWorkers(context, baseUrl) {
  let cleanupPage = null;
  try {
    cleanupPage = await context.newPage();
    cleanupPage.setDefaultTimeout(5000);
    cleanupPage.setDefaultNavigationTimeout(8000);
    await cleanupPage.goto(`${baseUrl}/v1/workers/`, { waitUntil: "load" });
    const result = await cleanupPage.evaluate(async () => {
      if (!("serviceWorker" in navigator)) {
        return {
          api_available: false,
          registration_count: 0,
          attempts: [],
          remaining: [],
        };
      }
      const registrations = await navigator.serviceWorker.getRegistrations();
      const attempts = await Promise.all(
        registrations.map(async (registration) => ({
          scope: registration.scope,
          unregistered: await registration.unregister(),
        }))
      );
      const deadline = Date.now() + 3000;
      let remaining = await navigator.serviceWorker.getRegistrations();
      while (remaining.length && Date.now() < deadline) {
        await new Promise((resolve) => setTimeout(resolve, 25));
        remaining = await navigator.serviceWorker.getRegistrations();
      }
      return {
        api_available: true,
        registration_count: registrations.length,
        attempts,
        remaining: remaining.map((registration) => registration.scope),
      };
    });
    assert(
      result && Array.isArray(result.remaining) && result.remaining.length === 0,
      `service worker registrations remain: ${JSON.stringify(result)}`
    );
    return result;
  } finally {
    if (cleanupPage) {
      await closePageConfirmed(cleanupPage, "service-worker state cleanup");
    }
  }
}

async function runCase(
  context,
  id,
  fn,
  timeoutMs = 25000,
  stateCleanup = null,
  runtimeFixtureVerifier = null
) {
  const started = Date.now();
  let timer;
  let page = null;
  let operation = null;
  let outcome = null;
  let stateCleanupResult = null;
  let runtimeFixtureResult = null;
  let isolationFailure = false;
  const deadline = new Promise((_, reject) => {
    timer = setTimeout(
      () => reject(new Error(`case timeout after ${timeoutMs}ms: ${id}`)),
      timeoutMs
    );
  });
  try {
    try {
      // Once this request is issued, a rejection without a Page object cannot
      // prove that the remote browser did not create a target. The case clock
      // starts before this request so an endpoint that never answers cannot
      // hang the whole pipeline outside the bounded operation below.
      const pageRequest = Promise.resolve().then(() => context.newPage());
      pageRequest.catch(() => {});
      page = await Promise.race([pageRequest, deadline]);
    } catch (error) {
      throw new CaseIsolationError(
        "page_creation_ambiguous",
        id,
        error,
        started
      );
    }
    if (runtimeFixtureVerifier) runtimeFixtureVerifier.beginCase(id);
    page.setDefaultTimeout(8000);
    page.setDefaultNavigationTimeout(12000);
    operation = Promise.resolve().then(() => fn(page));
    const actual = await Promise.race([operation, deadline]);
    outcome = { id, status: "pass", duration_ms: Date.now() - started, actual };
  } catch (error) {
    if (error instanceof CaseAbortError) {
      isolationFailure = error.isolationRestored !== true;
      throw error;
    }
    outcome = {
      id,
      status: "fail",
      duration_ms: Date.now() - started,
      error: String(error && (error.stack || error.message) ? error.message || error : error).slice(0, 1200),
    };
  } finally {
    clearTimeout(timer);
    // Observe a late rejection, then close and await the page before another
    // case starts. Closing the page cancels pending navigation/evaluation and
    // prevents timed-out work from mutating a later case's page.
    if (operation) operation.catch(() => {});
    if (page) {
      try {
        await closePageConfirmed(page, `case ${id}`);
      } catch (error) {
        throw new CaseIsolationError(
          "page_cleanup_unconfirmed",
          id,
          error,
          started,
          outcome
        );
      }
    }
    if (!isolationFailure && stateCleanup) {
      try {
        stateCleanupResult = await stateCleanup(context, id);
      } catch (error) {
        throw new CaseIsolationError(
          "case_state_cleanup_unconfirmed",
          id,
          error,
          started,
          outcome
        );
      }
    }
    if (!isolationFailure && runtimeFixtureVerifier) {
      try {
        runtimeFixtureResult = await runtimeFixtureVerifier.finishCase(id);
      } catch (error) {
        throw new CaseAbortError(
          "runtime_fixture_unverified",
          id,
          error,
          started,
          outcome,
          true
        );
      }
    }
  }
  return {
    ...outcome,
    isolation_restored: true,
    ...(stateCleanupResult ? { state_cleanup: stateCleanupResult } : {}),
    ...(runtimeFixtureResult
      ? { runtime_fixture_verification: runtimeFixtureResult }
      : {}),
  };
}

async function indexedDbRoundtrip(page, baseUrl) {
  // A case owns a fresh page, so it must establish its own non-opaque origin;
  // it must not inherit navigation performed by the preceding storage case.
  await page.goto(`${baseUrl}/v1/storage/`, { waitUntil: "load" });
  const fixture = await page.evaluate(
    () => document.body.dataset.fixture || null
  );
  assert(fixture === "storage-v1", `fixture=${fixture}`);
  const actual = await page.evaluate(async () => {
    const db = await new Promise((resolve, reject) => {
      const request = indexedDB.open("pages-probe-v1", 1);
      request.onupgradeneeded = () => request.result.createObjectStore("records");
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    });
    await new Promise((resolve, reject) => {
      const tx = db.transaction("records", "readwrite");
      tx.objectStore("records").put("idb-v1", "key");
      tx.oncomplete = resolve;
      tx.onerror = () => reject(tx.error);
    });
    const value = await new Promise((resolve, reject) => {
      const request = db.transaction("records").objectStore("records").get("key");
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    });
    db.close();
    return value;
  });
  assert(actual === "idb-v1", `indexeddb=${actual}`);
  return actual;
}

async function directWorkerOutcome(page, kind) {
  return page.evaluate(async (workerKind) => {
    const diagnostics = {
      kind: workerKind,
      secure_context: window.isSecureContext,
      worker_type: typeof Worker,
      shared_worker_type: typeof SharedWorker,
      service_worker_present: "serviceWorker" in navigator,
      location: location.href,
    };
    const finish = (status, value) => ({ ...diagnostics, status, value });
    const describeError = (error) => ({
      name: String(error?.name || "Error"),
      message: String(error?.message || error || "unknown error"),
    });
    if (workerKind === "dedicated") {
      return new Promise((resolve) => {
        let worker;
        const timer = setTimeout(
          () => resolve(finish("timeout", "no message/error event within 6000ms")),
          6000
        );
        try {
          worker = new Worker("worker.js");
          worker.onmessage = (event) => {
            clearTimeout(timer);
            worker.terminate();
            resolve(finish("message", event.data));
          };
          worker.onerror = (event) => {
            clearTimeout(timer);
            worker.terminate();
            resolve(finish("error_event", {
              message: event.message,
              filename: event.filename,
              lineno: event.lineno,
              colno: event.colno,
            }));
          };
          worker.postMessage(21);
        } catch (error) {
          clearTimeout(timer);
          resolve(finish("constructor_error", describeError(error)));
        }
      });
    }
    if (workerKind === "shared") {
      return new Promise((resolve) => {
        let worker;
        const timer = setTimeout(
          () => resolve(finish("timeout", "no message/error event within 6000ms")),
          6000
        );
        try {
          worker = new SharedWorker("shared-worker.js");
          worker.onerror = (event) => {
            clearTimeout(timer);
            resolve(finish("error_event", {
              message: event.message,
              filename: event.filename,
              lineno: event.lineno,
              colno: event.colno,
            }));
          };
          worker.port.onmessage = (event) => {
            clearTimeout(timer);
            worker.port.close();
            resolve(finish("message", event.data));
          };
          worker.port.start();
          worker.port.postMessage(14);
        } catch (error) {
          clearTimeout(timer);
          resolve(finish("constructor_error", describeError(error)));
        }
      });
    }
    if (workerKind === "service") {
      if (!("serviceWorker" in navigator)) {
        return finish("missing_api", null);
      }
      try {
        const registration = await Promise.race([
          navigator.serviceWorker.register("sw.js", { scope: "./" }),
          new Promise((resolve) => setTimeout(
            () => resolve({ __timeout: "register did not settle within 6000ms" }),
            6000
          )),
        ]);
        if (registration?.__timeout) {
          return finish("timeout", registration.__timeout);
        }
        const ready = await Promise.race([
          navigator.serviceWorker.ready,
          new Promise((resolve) => setTimeout(
            () => resolve({ __timeout: "ready did not settle within 6000ms" }),
            6000
          )),
        ]);
        if (ready?.__timeout) {
          return finish("timeout", ready.__timeout);
        }
        return finish("registered", registration.scope);
      } catch (error) {
        return finish("registration_error", describeError(error));
      }
    }
    return finish("probe_error", `unknown worker kind: ${workerKind}`);
  }, kind);
}

async function runRound(
  endpoint,
  baseUrl,
  expectedIdentity,
  round,
  directControlsOnly,
  fixtureVerification
) {
  const browser = await chromium.connectOverCDP(endpoint, { timeout: 15000 });
  const rows = [];
  let identity = null;
  let isolationAbort = null;
  let context = null;
  let runtimeFixtureVerifier = null;
  let browserShutdown = null;
  try {
    const browserSession = await browser.newBrowserCDPSession();
    identity = await browserSession.send("Browser.getVersion");
    for (const field of ["product", "protocolVersion", "revision"]) {
      assert(
        typeof expectedIdentity[field] === "string" && expectedIdentity[field].trim(),
        `expected identity ${field} must be non-empty`
      );
      assert(
        typeof identity[field] === "string" && identity[field].trim(),
        `task identity ${field} must be non-empty`
      );
      assert(
        identity[field] === expectedIdentity[field],
        `identity ${field} ${identity[field]} != ${expectedIdentity[field]}`
      );
    }
    context = browser.contexts()[0] || (await browser.newContext());
    runtimeFixtureVerifier = new RuntimeFixtureVerifier(
      baseUrl,
      fixtureVerification
    );
    runtimeFixtureVerifier.attach(context);
    const run = (id, fn, timeoutMs = 25000, stateCleanup = null) =>
      runCase(
        context,
        id,
        fn,
        timeoutMs,
        stateCleanup,
        runtimeFixtureVerifier
      );

    if (!directControlsOnly) {
    rows.push(await run("core_actions", async (page) => {
      await page.goto(`${baseUrl}/v1/core/`, { waitUntil: "load" });
      await page.locator("#increment").click();
      await page.locator("#increment").click();
      await page.locator("#name").fill("pages-v1");
      await page.waitForSelector("#late-item");
      const actual = await page.evaluate(() => ({
        fixture: document.body.dataset.fixture,
        counter: document.querySelector("#counter")?.textContent,
        name: document.querySelector("#name")?.value,
        shadow: document.querySelector("#open-shadow-host")?.shadowRoot?.querySelector("#shadow-value")?.textContent,
        late: document.querySelector("#late-item")?.textContent,
      }));
      assert(actual.fixture === "core-v1", `fixture=${actual.fixture}`);
      assert(actual.counter === "2", `counter=${actual.counter}`);
      assert(actual.name === "pages-v1", `name=${actual.name}`);
      assert(actual.shadow === "open-shadow", `shadow=${actual.shadow}`);
      assert(actual.late === "late-ready", `late=${actual.late}`);
      return actual;
    }));

    rows.push(await run("nested_frames", async (page) => {
      await page.goto(`${baseUrl}/v1/frames/`, { waitUntil: "load" });
      const child = page.frameLocator("#static-child");
      const heading = await child.locator("#child-heading").textContent();
      const grandchild = await child.frameLocator("#grandchild").locator("#grandchild-value").textContent();
      assert(heading === "Child frame", `heading=${heading}`);
      assert(grandchild === "grandchild-ready", `grandchild=${grandchild}`);
      return { heading, grandchild };
    }));

    rows.push(await run("static_fetch", async (page) => {
      await page.goto(`${baseUrl}/v1/network/`, { waitUntil: "load" });
      await page.locator("#fetch-json").click();
      await page.waitForFunction(() => document.querySelector("#network-result")?.textContent !== "idle");
      const actual = await page.locator("#network-result").textContent();
      assert(actual === "200:73", `network-result=${actual}`);
      return actual;
    }));

    rows.push(await run("basic_storage", async (page) => {
      await page.goto(`${baseUrl}/v1/storage/`, { waitUntil: "load" });
      const actual = await page.evaluate(() => {
        localStorage.setItem("pages-local", "local-v1");
        sessionStorage.setItem("pages-session", "session-v1");
        document.cookie = "pages-cookie=cookie-v1; path=/";
        return {
          local: localStorage.getItem("pages-local"),
          session: sessionStorage.getItem("pages-session"),
          cookie: document.cookie,
        };
      });
      assert(actual.local === "local-v1", `local=${actual.local}`);
      assert(actual.session === "session-v1", `session=${actual.session}`);
      assert(actual.cookie.includes("pages-cookie=cookie-v1"), `cookie=${actual.cookie}`);
      return actual;
    }));

    rows.push(await run(
      "indexeddb_roundtrip",
      async (page) => indexedDbRoundtrip(page, baseUrl)
    ));

    rows.push(await run("dedicated_worker", async (page) => {
      await page.goto(`${baseUrl}/v1/workers/`, { waitUntil: "load" });
      await page.locator("#dedicated").click();
      await page.waitForFunction(() => document.querySelector("#worker-result")?.textContent === "dedicated:42");
      return await page.locator("#worker-result").textContent();
    }));

    rows.push(await run("shared_worker", async (page) => {
      await page.goto(`${baseUrl}/v1/workers/`, { waitUntil: "load" });
      await page.locator("#shared").click();
      await page.waitForFunction(() => document.querySelector("#worker-result")?.textContent === "shared:42");
      return await page.locator("#worker-result").textContent();
    }));

    rows.push(await run("service_worker", async (page) => {
      await page.goto(`${baseUrl}/v1/workers/`, { waitUntil: "load" });
      await page.locator("#service").click();
      await page.waitForFunction(() => document.querySelector("#worker-result")?.textContent.startsWith("service-worker:"));
      const actual = await page.locator("#worker-result").textContent();
      assert(actual.includes("/v1/workers/"), `service worker scope=${actual}`);
      return actual;
    }, 25000, () => cleanupServiceWorkers(context, baseUrl)));

    rows.push(await run("history_push_state", async (page) => {
      await page.goto(`${baseUrl}/v1/lifecycle/`, { waitUntil: "load" });
      await page.locator("#push-state").click();
      const actual = await page.evaluate(() => ({
        search: location.search,
        events: window.__kitesurfFixture.events,
      }));
      assert(actual.search === "?pushed=1", `search=${actual.search}`);
      assert(actual.events.includes("pushstate"), `events=${JSON.stringify(actual.events)}`);
      return actual;
    }));

    rows.push(await run("csp_blocks_eval", async (page) => {
      await page.goto(`${baseUrl}/v1/security/`, { waitUntil: "load" });
      await page.locator("#try-eval").click();
      const actual = await page.locator("#security-result").textContent();
      assert(actual.startsWith("eval-blocked:"), `security-result=${actual}`);
      return actual;
    }));
    }

    rows.push(await run("core_dom_click_direct", async (page) => {
      await page.goto(`${baseUrl}/v1/core/`, { waitUntil: "load" });
      const actual = await page.evaluate(() => {
        document.querySelector("#increment").click();
        document.querySelector("#increment").click();
        return {
          counter: document.querySelector("#counter")?.textContent,
          shadow: document.querySelector("#open-shadow-host")?.shadowRoot?.querySelector("#shadow-value")?.textContent,
        };
      });
      assert(actual.counter === "2", `counter=${actual.counter}`);
      assert(actual.shadow === "open-shadow", `shadow=${actual.shadow}`);
      return actual;
    }));

    rows.push(await run("static_fetch_direct", async (page) => {
      await page.goto(`${baseUrl}/v1/network/`, { waitUntil: "load" });
      await page.evaluate(() => document.querySelector("#fetch-json").click());
      await page.waitForFunction(() => document.querySelector("#network-result")?.textContent !== "idle");
      const actual = await page.locator("#network-result").textContent();
      assert(actual === "200:73", `network-result=${actual}`);
      return actual;
    }));

    for (const [id, kind, expected] of [
      ["dedicated_worker_direct", "dedicated", "dedicated:42"],
      ["shared_worker_direct", "shared", "shared:42"],
    ]) {
      rows.push(await run(id, async (page) => {
        await page.goto(`${baseUrl}/v1/workers/`, { waitUntil: "load" });
        const actual = await directWorkerOutcome(page, kind);
        assert(
          actual.status === "message" && actual.value === expected,
          `worker outcome=${JSON.stringify(actual)}`
        );
        return actual;
      }));
    }

    rows.push(await run("service_worker_direct", async (page) => {
      await page.goto(`${baseUrl}/v1/workers/`, { waitUntil: "load" });
      const actual = await directWorkerOutcome(page, "service");
      assert(
        actual.status === "registered"
          && String(actual.value).includes("/v1/workers/"),
        `worker outcome=${JSON.stringify(actual)}`
      );
      return actual;
    }, 25000, () => cleanupServiceWorkers(context, baseUrl)));

    rows.push(await run("history_push_state_direct", async (page) => {
      await page.goto(`${baseUrl}/v1/lifecycle/`, { waitUntil: "load" });
      const actual = await page.evaluate(() => {
        document.querySelector("#push-state").click();
        return { search: location.search, events: window.__kitesurfFixture.events };
      });
      assert(actual.search === "?pushed=1", `search=${actual.search}`);
      assert(actual.events.includes("pushstate"), `events=${JSON.stringify(actual.events)}`);
      return actual;
    }));

  } catch (error) {
    if (!(error instanceof CaseAbortError)) throw error;
    rows.push(error.row);
    isolationAbort = {
      code: error.code,
      case_id: error.row.id,
      message: error.message,
      isolation_restored: error.isolationRestored,
    };
  } finally {
    if (runtimeFixtureVerifier) {
      await runtimeFixtureVerifier.settle();
      runtimeFixtureVerifier.detach();
    }
    const shutdownResult = await shutdownBrowserForRound(
      browser,
      round,
      isolationAbort
    );
    browserShutdown = shutdownResult.shutdown;
    isolationAbort = shutdownResult.isolationAbort;
  }
  return {
    round,
    identity,
    rows,
    aborted: isolationAbort !== null,
    abort_reason: isolationAbort,
    isolation_restored:
      isolationAbort === null || isolationAbort.isolation_restored === true,
    browser_shutdown: browserShutdown,
    runtime_fixture_verification: runtimeFixtureVerifier
      ? runtimeFixtureVerifier.summary()
      : {
          schema: "experimental.kitesurf_runtime_fixture_verification.v2",
          verified: false,
          response_count: 0,
          completed_case_count: 0,
          error: "runtime fixture verifier was not initialized",
        },
  };
}

async function main() {
  const endpoint = option("--endpoint", DEFAULT_ENDPOINT);
  const baseUrl = option("--base-url", defaultBaseUrl()).replace(/\/$/, "");
  const expectedIdentity = {
    product: option("--expect-product", DEFAULT_PRODUCT),
    protocolVersion: option(
      "--expect-protocol-version",
      DEFAULT_PROTOCOL_VERSION
    ),
    revision: option("--expect-revision", DEFAULT_REVISION),
  };
  const manifestOption = option(
    "--fixture-manifest",
    DEFAULT_FIXTURE_MANIFEST
  );
  const fixtureManifest = path.isAbsolute(manifestOption)
    ? manifestOption
    : path.resolve(REPO_ROOT, manifestOption);
  const rounds = Number(option("--rounds", "2"));
  const directControlsOnly = process.argv.includes("--direct-controls-only");
  const output = path.resolve(option("--output", `runs/experimental_kitesurf_pages_${Date.now()}`));
  const source = sourceProvenance();
  assert(Number.isInteger(rounds) && rounds > 0 && rounds <= 5, "--rounds must be 1 through 5");
  assert(!fs.existsSync(output), `output already exists: ${output}`);
  fs.mkdirSync(output, { recursive: true });
  fs.writeFileSync(
    path.join(output, "provenance.json"),
    JSON.stringify(source, null, 2) + "\n"
  );
  const fixtureVerification = verifyStaticFixture(
    baseUrl,
    fixtureManifest,
    path.join(output, "fixture_preflight_verification.json")
  );

  const all = [];
  const runtimeFixtureRounds = [];
  const browserShutdownRounds = [];
  let roundsStarted = 0;
  let roundsCompleted = 0;
  let isolationAbort = null;
  for (let round = 1; round <= rounds; round += 1) {
    roundsStarted += 1;
    const result = await runRound(
      endpoint,
      baseUrl,
      expectedIdentity,
      round,
      directControlsOnly,
      fixtureVerification
    );
    runtimeFixtureRounds.push(result.runtime_fixture_verification);
    browserShutdownRounds.push(result.browser_shutdown);
    for (const row of result.rows) {
      const record = {
        ...row,
        round,
        product: result.identity.product,
        identity: result.identity,
        expected_identity: expectedIdentity,
      };
      all.push(record);
      fs.appendFileSync(path.join(output, "results.jsonl"), JSON.stringify(record) + "\n");
      process.stdout.write(`[${round}/${rounds}] ${row.status.padEnd(4)} ${String(row.duration_ms).padStart(6)} ms ${row.id}\n`);
    }
    if (result.aborted) {
      isolationAbort = { round, ...result.abort_reason };
      process.stderr.write(`abort: ${JSON.stringify(isolationAbort)}\n`);
      break;
    }
    if (result.runtime_fixture_verification.verified !== true) {
      isolationAbort = {
        round,
        code: "runtime_fixture_unverified",
        message: "browser-consumed fixture responses were not fully verified",
        isolation_restored: true,
      };
      process.stderr.write(`abort: ${JSON.stringify(isolationAbort)}\n`);
      break;
    }
    roundsCompleted += 1;
  }
  const runtimeFixtureVerification = {
    schema: "experimental.kitesurf_runtime_fixture_verification.v2",
    verified:
      runtimeFixtureRounds.length === roundsStarted
      && runtimeFixtureRounds.length > 0
      && runtimeFixtureRounds.every((item) => item.verified === true),
    rounds: runtimeFixtureRounds,
    response_count: runtimeFixtureRounds.reduce(
      (total, item) => total + Number(item.response_count || 0),
      0
    ),
    completed_case_count: runtimeFixtureRounds.reduce(
      (total, item) => total + Number(item.completed_case_count || 0),
      0
    ),
    paths: [
      ...new Set(runtimeFixtureRounds.flatMap((item) => item.paths || [])),
    ].sort(),
  };
  fs.writeFileSync(
    path.join(output, "runtime_fixture_verification.json"),
    JSON.stringify(runtimeFixtureVerification, null, 2) + "\n"
  );
  const combinedFixtureVerification = {
    schema: "experimental.kitesurf_fixture_verification.v3",
    verified:
      fixtureVerification.verified === true
      && runtimeFixtureVerification.verified === true,
    manifest_sha256: fixtureVerification.manifest_sha256,
    source: fixtureVerification.source,
    file_count: fixtureVerification.file_count,
    preflight: {
      verified: fixtureVerification.verified,
      report: "fixture_preflight_verification.json",
    },
    runtime: {
      verified: runtimeFixtureVerification.verified,
      report: "runtime_fixture_verification.json",
      response_count: runtimeFixtureVerification.response_count,
      completed_case_count: runtimeFixtureVerification.completed_case_count,
      paths: runtimeFixtureVerification.paths,
    },
  };
  fs.writeFileSync(
    path.join(output, "fixture_verification.json"),
    JSON.stringify(combinedFixtureVerification, null, 2) + "\n"
  );
  const statusCounts = Object.fromEntries(
    [...new Set(all.map((row) => row.status))].sort().map((status) => [status, all.filter((row) => row.status === status).length])
  );
  const summary = {
    schema: "experimental.kitesurf_pages_probe.v3",
    created_at: new Date().toISOString(),
    source_commit: source.head,
    source,
    endpoint,
    fixture_base_url: baseUrl,
    expected_identity: expectedIdentity,
    observed_identities: [
      ...new Map(
        all.map((row) => [
          JSON.stringify(row.identity),
          row.identity,
        ])
      ).values(),
    ],
    fixture_verification: combinedFixtureVerification,
    rounds,
    rounds_requested: rounds,
    rounds_started: roundsStarted,
    rounds_completed: roundsCompleted,
    direct_controls_only: directControlsOnly,
    cases_per_round: isolationAbort ? null : all.length / rounds,
    attempts: all.length,
    status_counts: statusCounts,
    concurrency: 1,
    session_strategy: "one fresh public browser WebSocket per round",
    case_isolation: "fresh page per case; service-worker cases close their task page, unregister and verify all origin registrations, then close a dedicated cleanup page before continuation",
    browser_shutdown: {
      schema: "experimental.kitesurf_browser_shutdown.v1",
      confirmed:
        browserShutdownRounds.length === roundsStarted
        && browserShutdownRounds.length > 0
        && browserShutdownRounds.every((item) => item?.confirmed === true),
      rounds: browserShutdownRounds,
    },
    formal_score_eligible: false,
    aborted: isolationAbort !== null,
    abort_reason: isolationAbort,
    isolation_restored:
      isolationAbort === null || isolationAbort.isolation_restored === true,
  };
  fs.writeFileSync(path.join(output, "summary.json"), JSON.stringify(summary, null, 2) + "\n");
  process.stdout.write(`output=${output}\n`);
  if (isolationAbort) process.exitCode = 2;
}

if (require.main === module) {
  main().catch((error) => {
    process.stderr.write(`${error.stack || error}\n`);
    process.exitCode = 1;
  });
}

module.exports = {
  CaseAbortError,
  CaseIsolationError,
  RuntimeFixtureVerifier,
  closeBrowserConfirmed,
  cleanupServiceWorkers,
  closePageConfirmed,
  fixturePathForUrl,
  indexedDbRoundtrip,
  runCase,
  runRound,
  shutdownBrowserForRound,
  verifyStaticFixture,
};
