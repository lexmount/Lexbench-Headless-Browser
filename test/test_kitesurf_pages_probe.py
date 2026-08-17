from __future__ import annotations

import hashlib
import json
import shutil
import subprocess

import pytest

from runner import run as bench


pytestmark = pytest.mark.skipif(
    shutil.which("node") is None
    or not (bench.BENCH_ROOT / "node_modules" / "playwright-core").exists(),
    reason="needs node and repo-local playwright-core (npm ci)",
)


def test_timed_out_pages_case_is_closed_before_next_case() -> None:
    probe = bench.BENCH_ROOT / "tools" / "kitesurf_pages_probe.js"
    script = f"""
const {{ runCase }} = require({json.dumps(str(probe))});

const pages = [];
let firstReject = null;
let priorClosedWhenSecondOpened = null;
const context = {{
  async newPage() {{
    if (pages.length === 1) priorClosedWhenSecondOpened = pages[0].closed;
    const page = {{
      closed: false,
      setDefaultTimeout() {{}},
      setDefaultNavigationTimeout() {{}},
      async close() {{
        this.closed = true;
        if (firstReject) firstReject(new Error("page closed"));
      }},
    }};
    pages.push(page);
    return page;
  }},
}};

(async () => {{
  const first = await runCase(
    context,
    "hang",
    async () => new Promise((_, reject) => {{ firstReject = reject; }}),
    10
  );
  const second = await runCase(context, "next", async () => "ok", 100);
  process.stdout.write(JSON.stringify({{
    first,
    second,
    pageCount: pages.length,
    allClosed: pages.every((page) => page.closed),
    priorClosedWhenSecondOpened,
  }}));
}})().catch((error) => {{
  console.error(error);
  process.exit(1);
}});
"""
    proc = subprocess.run(
        ["node", "-e", script],
        cwd=bench.BENCH_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)
    assert result["first"]["status"] == "fail"
    assert "case timeout" in result["first"]["error"]
    assert result["second"]["status"] == "pass"
    assert result["second"]["actual"] == "ok"
    assert result["pageCount"] == 2
    assert result["priorClosedWhenSecondOpened"] is True
    assert result["allClosed"] is True


def test_page_acquisition_failure_aborts_before_another_case() -> None:
    probe = bench.BENCH_ROOT / "tools" / "kitesurf_pages_probe.js"
    script = f"""
const {{ runCase }} = require({json.dumps(str(probe))});

let pageRequests = 0;
let secondCaseStarted = false;
const context = {{
  async newPage() {{
    pageRequests += 1;
    throw new Error("synthetic lost create response");
  }},
}};

(async () => {{
  let observed = null;
  try {{
    await runCase(context, "ambiguous_create", async () => "unreachable", 100);
    secondCaseStarted = true;
    await runCase(context, "must_not_start", async () => "unreachable", 100);
  }} catch (error) {{
    observed = {{
      name: error.name,
      code: error.code,
      abortRound: error.abortRound,
      isolationRestored: error.isolationRestored,
      row: error.row,
    }};
  }}
  process.stdout.write(JSON.stringify({{
    observed,
    pageRequests,
    secondCaseStarted,
  }}));
}})().catch((error) => {{
  console.error(error);
  process.exit(1);
}});
"""
    proc = subprocess.run(
        ["node", "-e", script],
        cwd=bench.BENCH_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)
    assert result["pageRequests"] == 1
    assert result["secondCaseStarted"] is False
    assert result["observed"]["name"] == "CaseIsolationError"
    assert result["observed"]["code"] == "page_creation_ambiguous"
    assert result["observed"]["abortRound"] is True
    assert result["observed"]["isolationRestored"] is False
    assert result["observed"]["row"]["status"] == "infra"
    assert result["observed"]["row"]["isolation_restored"] is False


def test_page_acquisition_timeout_is_bounded_and_aborts_round() -> None:
    probe = bench.BENCH_ROOT / "tools" / "kitesurf_pages_probe.js"
    script = f"""
const {{ runCase }} = require({json.dumps(str(probe))});

let pageRequests = 0;
let operationStarted = false;
const context = {{
  async newPage() {{
    pageRequests += 1;
    return new Promise(() => {{}});
  }},
}};

(async () => {{
  const started = Date.now();
  let observed = null;
  try {{
    await runCase(context, "hung_create", async () => {{
      operationStarted = true;
      return "unreachable";
    }}, 15);
  }} catch (error) {{
    observed = {{
      name: error.name,
      code: error.code,
      abortRound: error.abortRound,
      isolationRestored: error.isolationRestored,
      message: error.message,
      row: error.row,
    }};
  }}
  process.stdout.write(JSON.stringify({{
    observed,
    pageRequests,
    operationStarted,
    elapsedMs: Date.now() - started,
  }}));
}})().catch((error) => {{
  console.error(error);
  process.exit(1);
}});
"""
    proc = subprocess.run(
        ["node", "-e", script],
        cwd=bench.BENCH_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)
    assert result["pageRequests"] == 1
    assert result["operationStarted"] is False
    assert result["elapsedMs"] < 1000
    assert result["observed"]["name"] == "CaseIsolationError"
    assert result["observed"]["code"] == "page_creation_ambiguous"
    assert result["observed"]["abortRound"] is True
    assert result["observed"]["isolationRestored"] is False
    assert "case timeout after 15ms" in result["observed"]["message"]
    assert result["observed"]["row"]["status"] == "infra"
    assert result["observed"]["row"]["isolation_restored"] is False


def test_unconfirmed_browser_shutdown_aborts_before_another_round() -> None:
    probe = bench.BENCH_ROOT / "tools" / "kitesurf_pages_probe.js"
    script = f"""
const {{ shutdownBrowserForRound }} = require({json.dumps(str(probe))});

let connected = true;
let nextRoundStarted = false;
const browser = {{
  async close() {{ throw new Error("synthetic close rejection"); }},
  isConnected() {{ return connected; }},
}};

(async () => {{
  const result = await shutdownBrowserForRound(browser, 1);
  if (result.isolationAbort === null) nextRoundStarted = true;
  process.stdout.write(JSON.stringify({{ result, nextRoundStarted }}));
}})().catch((error) => {{
  console.error(error);
  process.exit(1);
}});
"""
    proc = subprocess.run(
        ["node", "-e", script],
        cwd=bench.BENCH_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)
    assert result["nextRoundStarted"] is False
    assert result["result"]["shutdown"]["confirmed"] is False
    assert result["result"]["shutdown"]["connected_after"] is True
    assert result["result"]["isolationAbort"]["code"] == (
        "browser_shutdown_unconfirmed"
    )
    assert result["result"]["isolationAbort"]["isolation_restored"] is False


def test_browser_shutdown_requires_disconnection_even_after_close_resolves() -> None:
    probe = bench.BENCH_ROOT / "tools" / "kitesurf_pages_probe.js"
    script = f"""
const {{ closeBrowserConfirmed }} = require({json.dumps(str(probe))});

(async () => {{
  let observed = null;
  try {{
    await closeBrowserConfirmed({{
      async close() {{}},
      isConnected() {{ return true; }},
    }}, "test round", 50);
  }} catch (error) {{
    observed = {{ message: error.message, shutdown: error.browserShutdown }};
  }}
  process.stdout.write(JSON.stringify(observed));
}})().catch((error) => {{
  console.error(error);
  process.exit(1);
}});
"""
    proc = subprocess.run(
        ["node", "-e", script],
        cwd=bench.BENCH_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)
    assert "shutdown was not confirmed" in result["message"]
    assert result["shutdown"]["close_resolved"] is True
    assert result["shutdown"]["connected_after"] is True
    assert result["shutdown"]["confirmed"] is False


def test_case_state_cleanup_finishes_before_next_case() -> None:
    probe = bench.BENCH_ROOT / "tools" / "kitesurf_pages_probe.js"
    script = f"""
const {{ runCase }} = require({json.dumps(str(probe))});

const events = [];
const pages = [];
let registrationPresent = true;
const context = {{
  async newPage() {{
    events.push(`new-page-${{pages.length + 1}}`);
    const page = {{
      closed: false,
      setDefaultTimeout() {{}},
      setDefaultNavigationTimeout() {{}},
      isClosed() {{ return this.closed; }},
      async close() {{
        this.closed = true;
        events.push(`close-page-${{pages.indexOf(this) + 1}}`);
      }},
    }};
    pages.push(page);
    return page;
  }},
}};

(async () => {{
  const first = await runCase(
    context,
    "service_worker",
    async () => "registered",
    100,
    async () => {{
      events.push(`state-cleanup-page-closed-${{pages[0].closed}}`);
      registrationPresent = false;
      return {{ remaining: [] }};
    }}
  );
  const second = await runCase(context, "next", async () => {{
    events.push(`second-sees-registration-${{registrationPresent}}`);
    return registrationPresent;
  }}, 100);
  process.stdout.write(JSON.stringify({{ first, second, events }}));
}})().catch((error) => {{
  console.error(error);
  process.exit(1);
}});
"""
    proc = subprocess.run(
        ["node", "-e", script],
        cwd=bench.BENCH_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)
    assert result["first"]["state_cleanup"] == {"remaining": []}
    assert result["second"]["actual"] is False
    assert result["events"] == [
        "new-page-1",
        "close-page-1",
        "state-cleanup-page-closed-true",
        "new-page-2",
        "second-sees-registration-false",
        "close-page-2",
    ]


def test_service_worker_cleanup_unregisters_and_confirms_page_close() -> None:
    probe = bench.BENCH_ROOT / "tools" / "kitesurf_pages_probe.js"
    script = f"""
const {{ cleanupServiceWorkers }} = require({json.dumps(str(probe))});

const events = [];
const page = {{
  closed: false,
  setDefaultTimeout(value) {{ events.push(["timeout", value]); }},
  setDefaultNavigationTimeout(value) {{ events.push(["navigation-timeout", value]); }},
  async goto(url, options) {{ events.push(["goto", url, options.waitUntil]); }},
  async evaluate() {{
    events.push(["evaluate"]);
    return {{
      api_available: true,
      registration_count: 1,
      attempts: [{{ scope: "https://fixtures.example/v1/workers/", unregistered: true }}],
      remaining: [],
    }};
  }},
  async close(options) {{
    events.push(["close", options.runBeforeUnload]);
    this.closed = true;
  }},
  isClosed() {{ return this.closed; }},
}};
const context = {{ async newPage() {{ events.push(["new-page"]); return page; }} }};

(async () => {{
  const result = await cleanupServiceWorkers(context, "https://fixtures.example");
  process.stdout.write(JSON.stringify({{ result, events, closed: page.closed }}));
}})().catch((error) => {{
  console.error(error);
  process.exit(1);
}});
"""
    proc = subprocess.run(
        ["node", "-e", script],
        cwd=bench.BENCH_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)
    assert result["closed"] is True
    assert result["result"]["remaining"] == []
    assert result["events"] == [
        ["new-page"],
        ["timeout", 5000],
        ["navigation-timeout", 8000],
        ["goto", "https://fixtures.example/v1/workers/", "load"],
        ["evaluate"],
        ["close", False],
    ]


def test_runtime_fixture_verifier_hashes_browser_consumed_response() -> None:
    probe = bench.BENCH_ROOT / "tools" / "kitesurf_pages_probe.js"
    body = b"pinned browser response\n"
    digest = hashlib.sha256(body).hexdigest()
    script = f"""
const {{ RuntimeFixtureVerifier }} = require({json.dumps(str(probe))});

let listener = null;
let detached = false;
const context = {{
  on(event, callback) {{ if (event === "response") listener = callback; }},
  off(event, callback) {{
    if (event === "response" && callback === listener) detached = true;
  }},
}};
const verifier = new RuntimeFixtureVerifier(
  "https://fixtures.example/base",
  {{
    verified: true,
    files: [{{
      path: "v1/core/index.html",
      expected_size: {len(body)},
      expected_sha256: {json.dumps(digest)},
    }}],
  }}
);
verifier.attach(context);
verifier.beginCase("core_actions");
listener({{
  url: () => "https://fixtures.example/base/v1/core/?cache=ignored",
  status: () => 200,
  body: async () => Buffer.from({json.dumps(body.decode())}),
}});

(async () => {{
  const result = await verifier.finishCase("core_actions");
  verifier.detach();
  process.stdout.write(JSON.stringify({{
    result,
    summary: verifier.summary(),
    detached,
  }}));
}})().catch((error) => {{
  console.error(error);
  process.exit(1);
}});
"""
    proc = subprocess.run(
        ["node", "-e", script],
        cwd=bench.BENCH_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)
    assert result["detached"] is True
    assert result["result"]["verified"] is True
    assert result["result"]["required_paths"] == ["v1/core/index.html"]
    assert result["result"]["missing_paths"] == []
    assert result["result"]["unexpected_paths"] == []
    assert result["result"]["paths"] == ["v1/core/index.html"]
    assert (
        result["summary"]["schema"]
        == "experimental.kitesurf_runtime_fixture_verification.v2"
    )
    assert result["summary"]["verified"] is True
    assert result["summary"]["responses"][0]["actual_sha256"] == digest


def test_runtime_fixture_verifier_requires_every_case_dependency() -> None:
    probe = bench.BENCH_ROOT / "tools" / "kitesurf_pages_probe.js"
    body = b"pinned frame response\n"
    digest = hashlib.sha256(body).hexdigest()
    required_paths = [
        "v1/frames/index.html",
        "v1/frames/child.html",
        "v1/frames/grandchild.html",
    ]
    files = [
        {
            "path": path,
            "expected_size": len(body),
            "expected_sha256": digest,
        }
        for path in required_paths
    ]
    script = f"""
const {{ RuntimeFixtureVerifier }} = require({json.dumps(str(probe))});

let listener = null;
const context = {{
  on(event, callback) {{ if (event === "response") listener = callback; }},
  off() {{}},
}};
const verifier = new RuntimeFixtureVerifier(
  "https://fixtures.example",
  {{ verified: true, files: {json.dumps(files)} }}
);
verifier.attach(context);
verifier.beginCase("nested_frames");
listener({{
  url: () => "https://fixtures.example/v1/frames/",
  status: () => 200,
  body: async () => Buffer.from({json.dumps(body.decode())}),
}});

(async () => {{
  let error = null;
  try {{
    await verifier.finishCase("nested_frames");
  }} catch (caught) {{
    error = caught.message;
  }}
  process.stdout.write(JSON.stringify({{ error, summary: verifier.summary() }}));
}})().catch((error) => {{
  console.error(error);
  process.exit(1);
}});
"""
    proc = subprocess.run(
        ["node", "-e", script],
        cwd=bench.BENCH_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)
    case = result["summary"]["cases"][0]
    assert "browser-consumed fixture was not verified" in result["error"]
    assert case["verified"] is False
    assert case["required_paths"] == required_paths
    assert case["missing_paths"] == required_paths[1:]
    assert case["unexpected_paths"] == []


def test_runtime_fixture_verifier_rejects_unmanifested_response() -> None:
    probe = bench.BENCH_ROOT / "tools" / "kitesurf_pages_probe.js"
    body = b"pinned core response\n"
    digest = hashlib.sha256(body).hexdigest()
    script = f"""
const {{ RuntimeFixtureVerifier }} = require({json.dumps(str(probe))});

let listener = null;
const context = {{
  on(event, callback) {{ if (event === "response") listener = callback; }},
  off() {{}},
}};
const verifier = new RuntimeFixtureVerifier(
  "https://fixtures.example",
  {{
    verified: true,
    files: [{{
      path: "v1/core/index.html",
      expected_size: {len(body)},
      expected_sha256: {json.dumps(digest)},
    }}],
  }}
);
verifier.attach(context);
verifier.beginCase("core_actions");
listener({{
  url: () => "https://fixtures.example/v1/core/",
  status: () => 200,
  body: async () => Buffer.from({json.dumps(body.decode())}),
}});
listener({{
  url: () => "https://fixtures.example/v1/core/unpinned.js",
  status: () => 200,
  body: async () => Buffer.from("untrusted"),
}});

(async () => {{
  try {{
    await verifier.finishCase("core_actions");
  }} catch (_) {{}}
  process.stdout.write(JSON.stringify(verifier.summary()));
}})().catch((error) => {{
  console.error(error);
  process.exit(1);
}});
"""
    proc = subprocess.run(
        ["node", "-e", script],
        cwd=bench.BENCH_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    summary = json.loads(proc.stdout)
    case = summary["cases"][0]
    assert case["verified"] is False
    assert case["missing_paths"] == []
    assert case["unexpected_paths"] == ["v1/core/unpinned.js"]
    assert case["responses"][1]["verified"] is False
    assert "absent from pinned fixture manifest" in case["responses"][1]["error"]


def test_runtime_fixture_mismatch_aborts_after_closing_case_page() -> None:
    probe = bench.BENCH_ROOT / "tools" / "kitesurf_pages_probe.js"
    expected = b"expected fixture"
    digest = hashlib.sha256(expected).hexdigest()
    script = f"""
const {{ RuntimeFixtureVerifier, runCase }} = require({json.dumps(str(probe))});

let listener = null;
const page = {{
  closed: false,
  setDefaultTimeout() {{}},
  setDefaultNavigationTimeout() {{}},
  isClosed() {{ return this.closed; }},
  async close() {{ this.closed = true; }},
}};
const context = {{
  on(event, callback) {{ if (event === "response") listener = callback; }},
  off() {{}},
  async newPage() {{ return page; }},
}};
const verifier = new RuntimeFixtureVerifier(
  "https://fixtures.example",
  {{
    verified: true,
    files: [{{
      path: "v1/core/index.html",
      expected_size: {len(expected)},
      expected_sha256: {json.dumps(digest)},
    }}],
  }}
);
verifier.attach(context);

(async () => {{
  let observed = null;
  try {{
    await runCase(
      context,
      "core_actions",
      async () => {{
        listener({{
          url: () => "https://fixtures.example/v1/core/",
          status: () => 200,
          body: async () => Buffer.from("mutated fixture"),
        }});
        return "functional-pass";
      }},
      100,
      null,
      verifier
    );
  }} catch (error) {{
    observed = {{
      name: error.name,
      code: error.code,
      isolationRestored: error.isolationRestored,
      row: error.row,
    }};
  }}
  process.stdout.write(JSON.stringify({{
    observed,
    pageClosed: page.closed,
    summary: verifier.summary(),
  }}));
}})().catch((error) => {{
  console.error(error);
  process.exit(1);
}});
"""
    proc = subprocess.run(
        ["node", "-e", script],
        cwd=bench.BENCH_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)
    assert result["pageClosed"] is True
    assert result["observed"]["name"] == "CaseAbortError"
    assert result["observed"]["code"] == "runtime_fixture_unverified"
    assert result["observed"]["isolationRestored"] is True
    assert result["observed"]["row"]["primary_outcome"]["status"] == "pass"
    assert result["summary"]["verified"] is False


def test_indexeddb_case_establishes_its_own_fixture_origin() -> None:
    probe = bench.BENCH_ROOT / "tools" / "kitesurf_pages_probe.js"
    script = f"""
const {{ indexedDbRoundtrip }} = require({json.dumps(str(probe))});
const calls = [];
let evaluation = 0;
const page = {{
  async goto(url, options) {{ calls.push(["goto", url, options.waitUntil]); }},
  async evaluate() {{
    calls.push(["evaluate"]);
    evaluation += 1;
    return evaluation === 1 ? "storage-v1" : "idb-v1";
  }},
}};
(async () => {{
  const actual = await indexedDbRoundtrip(page, "https://fixtures.example/base");
  process.stdout.write(JSON.stringify({{ actual, calls }}));
}})().catch((error) => {{
  console.error(error);
  process.exit(1);
}});
"""
    proc = subprocess.run(
        ["node", "-e", script],
        cwd=bench.BENCH_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)
    assert result == {
        "actual": "idb-v1",
        "calls": [
            ["goto", "https://fixtures.example/base/v1/storage/", "load"],
            ["evaluate"],
            ["evaluate"],
        ],
    }


def test_pages_preflight_uses_the_shared_fixture_verifier(
    tmp_path,
    fixture_server,
) -> None:
    body = b"<h1>pinned fixture</h1>\n"
    fixture_root = tmp_path / "public"
    fixture_root.mkdir()
    (fixture_root / "index.html").write_bytes(body)
    server = fixture_server(fixture_root)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema": "experimental.kitesurf_static_fixture.v1",
                "deployment_base_url": f"{server.base_url}/fixtures",
                "source": {
                    "repository": "https://github.com/example/fixtures",
                    "commit": "a" * 40,
                },
                "files": [
                    {
                        "path": "index.html",
                        "size": len(body),
                        "sha256": hashlib.sha256(body).hexdigest(),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    report = tmp_path / "fixture-verification.json"
    probe = bench.BENCH_ROOT / "tools" / "kitesurf_pages_probe.js"
    script = f"""
const {{ verifyStaticFixture }} = require({json.dumps(str(probe))});
const result = verifyStaticFixture(
  {json.dumps(server.base_url + "/fixtures")},
  {json.dumps(str(manifest))},
  {json.dumps(str(report))}
);
process.stdout.write(JSON.stringify(result));
"""

    proc = subprocess.run(
        ["node", "-e", script],
        cwd=bench.BENCH_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=15,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)
    assert result["verified"] is True
    assert result["verified_file_count"] == 1
    assert json.loads(report.read_text(encoding="utf-8"))["verified"] is True
