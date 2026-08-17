from __future__ import annotations

import argparse
import base64
import concurrent.futures
import datetime as dt
import errno
import glob
import hashlib
import http.server
import json
import os
import pathlib
import re
import secrets
import select
import shlex
import shutil
import signal
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

if __package__:
    from runner import bindings as binding_catalog
    from runner import resources as resource_metrics
    from runner import semantics as semantic_model
    from runner.launch_profiles import DEFAULT_LAUNCH_PROFILE, LAUNCH_PROFILES
else:
    # Keep the historical direct-script entry point (`python3 runner/run.py`)
    # working as well as the preferred module form (`python3 -m runner.run`).
    import bindings as binding_catalog
    import resources as resource_metrics
    import semantics as semantic_model
    from launch_profiles import DEFAULT_LAUNCH_PROFILE, LAUNCH_PROFILES


BENCH_ROOT = pathlib.Path(__file__).resolve().parents[1]
REPO_ROOT = BENCH_ROOT
DEFAULT_MANIFEST = BENCH_ROOT / "manifest.json"
DEFAULT_RUNS_DIR = BENCH_ROOT / "runs"
DEFAULT_K_RUNS = 1
LOCAL_HOST = "127.0.0.1"
DEFAULT_AGENT_BROWSER_SOCKET_DIR = pathlib.Path(tempfile.gettempdir()) / "ab"

STATUS_VALUES = {
    "pass",
    "fail",
    "unsupported",
    "timeout",
    "crash",
    "infra",
    "chrome_gate_fail",
}
FAILURE_CLASSES = {
    "engine_unsupported",
    "cdp_semantic",
    "observability",
    "script_error",
    "infra",
}
ARTIFACT_PROFILES = {
    "l1_standard": ["run.json", "cdp.jsonl", "grader.json", "stdout.log", "stderr.log"],
    "l2_standard": ["run.json", "cdp.jsonl", "grader.json", "stdout.log", "stderr.log"],
}
ENGINE_ORDER = ("chrome", "moli", "lightpanda", "obscura")
NATIVE_CANDIDATES = ("moli", "lightpanda", "obscura")
GATE_POLICIES = {"required", "best_effort", "off"}
TAG_NAMESPACES = {"version", "purpose", "coverage", "family"}
VERSION_TAGS = {"version.v0_1", "version.v0_2", "version.v0_3", "version.v0_4"}
# Retired feature spellings include merged duplicates and error-mode suffixes.
# Error modes live in tags/checks, not in the feature name.
RETIRED_FEATURES = {
    "web.url_search_params",
    "web.dom.parser",
    "web.dom.domparser",
    "web.dom.treewalker",
    "web.storage.cookie",
    "web.intersection_observer",
    "web.css.cssom.constructable",
    "web.css.selectors.case_insensitive_attr",
    "cdp.moli.inspect_everything.nonexistent",
}
RETIRED_FEATURE_SUFFIXES = (".invalid_params", ".deprecated", ".nonexistent")
NON_TARGET_CDP_METHODS = {"Page.captureScreenshot", "Page.printToPDF"}
EVENT_MATCH_ONE_OF = "$one_of"
NON_TARGET_FRAMEWORK_OPS = {"screenshot", "pdf"}
NON_TARGET_FEATURES = {
    "cdp.page.capture_screenshot",
    "cdp.page.print_to_pdf",
    "fw.playwright.page.screenshot",
    "fw.playwright.page.pdf",
    "fw.puppeteer.page.screenshot",
    "fw.puppeteer.page.pdf",
    "tool.agent_browser.screenshot",
    "tool.agent_browser.pdf",
}
PROGRESS_STATUS_ORDER = ("pass", "fail", "unsupported", "timeout", "crash", "infra", "chrome_gate_fail")
PROGRESS_STATUS_LABELS = {"chrome_gate_fail": "baseline_skip"}
COLOR_MODES = {"auto", "always", "never"}
RUN_ID_CONFLICT_MODES = {"error", "suffix"}
ANSI_RESET = "\033[0m"
ANSI_COLORS = {
    "bold": "\033[1m",
    "dim": "\033[2m",
    "blue": "\033[34m",
    "cyan": "\033[36m",
    "green": "\033[32m",
    "magenta": "\033[35m",
    "red": "\033[31m",
    "yellow": "\033[33m",
}
STATUS_COLORS = {
    "pass": "green",
    "fail": "red",
    "unsupported": "yellow",
    "timeout": "magenta",
    "crash": "red",
    "infra": "red",
    "chrome_gate_fail": "cyan",
}
ANSI_RE = re.compile(r"\033\[[0-9;]*m")
WINDOWS_DRIVE_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")
RUNNER_LOCAL_PATH_TOKENS = frozenset({"{artifact_dir}", "{fixture_path}"})
ARTIFACT_LOCAL_PATH_TOKENS = frozenset({"{artifact_dir}"})
COMMAND_PATH_FLAGS = {
    "d",
    "dir",
    "directory",
    "download-path",
    "dest",
    "destination",
    "f",
    "file",
    "file-path",
    "o",
    "out",
    "output",
    "output-file",
    "output-path",
    "path",
    "profile",
    "profile-directory",
    "trace",
    "user-data-dir",
}
ETIMEDOUT_ERRNO = getattr(errno, "ETIMEDOUT", None)
SOCKET_TRANSPORT_ERRNOS = {
    value
    for value in (
        getattr(errno, "ECONNABORTED", None),
        getattr(errno, "ECONNREFUSED", None),
        getattr(errno, "ECONNRESET", None),
        getattr(errno, "EHOSTDOWN", None),
        getattr(errno, "EHOSTUNREACH", None),
        getattr(errno, "ENETDOWN", None),
        getattr(errno, "ENETRESET", None),
        getattr(errno, "ENETUNREACH", None),
        getattr(errno, "ENOTCONN", None),
        getattr(errno, "EPIPE", None),
        getattr(errno, "ESHUTDOWN", None),
    )
    if value is not None
}


ENGINE_DEFS = {
    "moli": {
        "binary": REPO_ROOT / "build_artifacts/moli/bin/moli",
        "version": "moli 0.1.1",
        "sha256": "74e08f8d3eb6f0c937baa48a9eec9c0771be71fbd830bb26fbc3864816833a26",
        "sha256_12": "74e08f8d3eb6",
        "upstream_commit": "63eb3d6bc284950e2eb7f8a4dadd813208a22818",
        "cdp_port": 9222,
        "role": "native_candidate",
        # Most tasks intentionally use Moli's crawler-oriented default. Tasks
        # whose observable contract requires visual resources opt into this
        # profile explicitly.
        "launch_profile_args": {
            "all_resources": ("--resource",),
        },
    },
    "lightpanda": {
        "binary": REPO_ROOT / "build_artifacts/lightpanda/zig-out/bin/lightpanda",
        "version": "1.0.0-dev.321+b04c99a9",
        "sha256": "70f5ab69b0ce9740ae2bf9cf682ad44a2a17362621f55df8f750b79af095c574",
        "sha256_12": "70f5ab69b0ce",
        "upstream_commit": "b04c99a9111564ebe06317f644680eda5e3ee83e",
        "cdp_port": 9223,
        "role": "native_candidate",
    },
    "chrome": {
        "binary": REPO_ROOT / "build_artifacts/chrome-for-testing/bin/chrome",
        "version": "151.0.7922.47",
        "sha256": "3b0be9872ea937893cb1e1523fde071d38c1ed4ef866b3f7976240094a868c93",
        "sha256_12": "3b0be9872ea9",
        "cdp_port": 9224,
        "role": "gold_baseline",
        "mode": "headless=new",
    },
    "obscura": {
        "binary": REPO_ROOT / "build_artifacts/obscura/bin/obscura",
        "version": "obscura 0.1.11",
        "sha256": "42c7eac0f635959f09a7d32adfdd3a9bb5c852c65532630308fb93aee483f96f",
        "sha256_12": "42c7eac0f635",
        "cdp_port": 9225,
        "role": "native_candidate",
        "upstream_tag": "v0.1.11",
        "upstream_commit": "e78b5e60261599a850c053eaecc2de92625496d7",
        "release_archive_sha256": "c9343428c9692c49e837487f1d0a8f308b280dd854981c5a06f2aece0048230e",
        "version_argv": ["{binary}", "--version"],
        # The fixture is runner-owned and loopback-only. Obscura blocks this
        # address class unless the narrow permission is explicit.
        "serve_args": ("--allow-private-network",),
    },
}
ACTIVE_ENGINE_SET_PATH = REPO_ROOT / "build_artifacts/active-set.json"
ACTIVE_ENGINE_SET: dict[str, Any] = {}


def apply_active_engine_set() -> None:
    """Overlay local, untracked engine identities for a deployed workbench.

    The repository defaults remain the historical evidence-run pins. A machine
    that keeps multiple immutable artifact sets can select one by writing
    build_artifacts/active-set.json and repointing the conventional binary
    symlinks. Keeping this file under build_artifacts makes the deployment
    local without changing the benchmark dataset.
    """
    global ACTIVE_ENGINE_SET
    if not ACTIVE_ENGINE_SET_PATH.exists():
        return
    try:
        payload = json.loads(ACTIVE_ENGINE_SET_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"invalid active engine set {ACTIVE_ENGINE_SET_PATH}: {exc}") from exc
    engines = payload.get("engines")
    if not isinstance(engines, dict):
        raise RuntimeError(f"invalid active engine set {ACTIVE_ENGINE_SET_PATH}: `engines` must be an object")
    for engine, override in engines.items():
        if engine not in ENGINE_DEFS or not isinstance(override, dict):
            raise RuntimeError(f"invalid active engine override: {engine}")
        for key in ("version", "sha256", "sha256_12", "cdp_port"):
            if key in override:
                ENGINE_DEFS[engine][key] = override[key]
        if "binary" in override:
            path = pathlib.Path(str(override["binary"]))
            ENGINE_DEFS[engine]["binary"] = path if path.is_absolute() else REPO_ROOT / path
    ACTIVE_ENGINE_SET = payload


apply_active_engine_set()


class BenchError(Exception):
    pass


class CDPTransportError(BenchError, ConnectionError):
    """A failure establishing or maintaining the CDP transport."""

    pass


class CDPTransportTimeout(CDPTransportError, TimeoutError):
    """A transport deadline, classified as a timeout by formal runners."""

    pass


def is_cdp_transport_exception(exc: BaseException) -> bool:
    """Return whether a raw-CDP failure retains transport attribution."""

    return (
        isinstance(exc, ConnectionError)
        or isinstance(exc, ssl.SSLError)
        or (
            isinstance(exc, OSError)
            and is_socket_transport_os_error(exc)
        )
    )


class CDPCommandError(Exception):
    def __init__(self, method: str, error: dict[str, Any]):
        self.method = method
        self.error = error
        super().__init__(f"{method}: {error.get('message') or error}")


@dataclass
class ResolvedTask:
    layer: str
    subset_id: str
    task_id: str
    task_version: int
    path: pathlib.Path
    rel_path: str
    sha256: str
    description: str
    features: list[str]
    tags: list[str]
    scene: dict[str, Any]
    driver: dict[str, Any]
    grader: dict[str, Any]
    chrome_gate: str | None
    subset_chrome_gate: str | None
    launch_profile: str
    artifact_profile: str
    task: dict[str, Any]
    semantic_capability: dict[str, Any] | None = None

    def to_run_manifest(
        self,
        semantic_capability: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        chrome_baseline = self.chrome_gate or self.subset_chrome_gate or "off"
        payload = {
            "layer": self.layer,
            "subset_id": self.subset_id,
            "task_id": self.task_id,
            "task_version": self.task_version,
            "path": self.rel_path,
            "sha256": self.sha256,
            "description": self.description,
            "features": self.features,
            "tags": self.tags,
            "scene": self.scene.get("kind"),
            "driver": self.driver.get("kind"),
            "grader": self.grader.get("kind"),
            "chrome_baseline": chrome_baseline,
            # Legacy schema key retained for older tooling and existing artifacts.
            "chrome_gate": chrome_baseline,
            "launch_profile": self.launch_profile,
            "artifact_profile": self.artifact_profile,
        }
        if self.layer == "L1":
            payload["evaluation_axis"] = "protocol_driver_compatibility"
        else:
            semantic_capability = semantic_capability or self.semantic_capability
        if self.layer == "L2" and semantic_capability:
            payload["evaluation_axis"] = "web_platform_workflow_semantic_correctness"
            payload["semantic_capability"] = semantic_capability
        return payload


@dataclass
class BrowserProcess:
    engine: str
    port: int
    process: subprocess.Popen[str]
    version_info: dict[str, Any]
    binary: pathlib.Path | None = None
    binary_sha256: str | None = None
    launch_command: tuple[str, ...] = ()
    serve_args: tuple[str, ...] = ()
    # CDP-level Browser.getVersion product, captured lazily on first use. It can
    # differ from the HTTP /json/version identity (Lightpanda reports
    # "Lightpanda/1.0" over HTTP but a spoofed "Chrome/124..." over CDP), so the
    # framework-driver binding gate needs both reference values.
    cdp_product: str | None = None
    cgroup: resource_metrics.CgroupV2Group | None = None
    cgroup_error: str | None = None
    cold_start: dict[str, Any] | None = None
    # Which worker owns this process and how many times that worker has had to
    # (re)launch the engine. A non-pass that only reproduces under load is
    # usually a story about process reuse, so the row has to say which process
    # it ran on.
    worker_slot: int = 0
    generation: int = 1
    # Previous task this worker ran on this engine's process, so a failing
    # attempt can be replayed with its actual predecessor rather than with the
    # global task order.
    prev_task_id: str | None = None

    @property
    def base_url(self) -> str:
        return f"http://{LOCAL_HOST}:{self.port}"


class ResourceRuntime:
    """Run-scoped state shared by browser managers and attempt profilers."""

    def __init__(self, run_dir: pathlib.Path, run_id: str, sample_interval_ms: int) -> None:
        self.run_dir = run_dir
        self.run_id = run_id
        self.sample_interval_ms = max(10, int(sample_interval_ms))
        self.traffic = resource_metrics.FixtureTrafficTracker()
        self.cold_starts: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._cold_path = run_dir / "cold_start.jsonl"

    def register_cold_start(self, payload: dict[str, Any]) -> None:
        with self._lock:
            self.cold_starts.append(payload)
            with self._cold_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, sort_keys=True) + "\n")


def now_iso() -> str:
    # Provenance timestamps are always UTC so run artifacts carry no local
    # timezone fingerprint.
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def run_id_slug(text: str) -> str:
    return re.sub(r"[^0-9A-Za-z]+", "_", str(text)).strip("_").lower()


def default_run_id(label: str = "native_v0_1", subsets: list[str] | None = None) -> str:
    return run_id_conflict_stamp()


def run_id_conflict_stamp() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dt%H%Mz")


def compact_run_id(run_id: str, max_len: int = 64) -> str:
    base = run_id_slug(run_id) or "run"
    if len(base) <= max_len:
        return base
    digest = hashlib.sha256(base.encode("utf-8")).hexdigest()[:8]
    keep = max(1, max_len - len(digest) - 1)
    return f"{base[:keep].rstrip('_')}_{digest}"


def run_id_with_suffix(run_id: str, suffix: str, max_len: int = 64) -> str:
    """Append a uniqueness suffix while keeping the path segment compact."""
    base = compact_run_id(run_id, max_len=max_len)
    suffix = run_id_slug(suffix) or run_id_conflict_stamp()
    room = max_len - len(suffix) - 1
    if room <= 0:
        return suffix[-max_len:]
    if len(base) > room:
        digest = hashlib.sha256(base.encode("utf-8")).hexdigest()[:8]
        keep = max(1, room - len(digest) - 1)
        base = f"{base[:keep].rstrip('_')}_{digest}"
    return f"{base}_{suffix}"


def reserve_run_dir(out_root: pathlib.Path, requested_run_id: str | None = None, conflict_mode: str = "suffix") -> tuple[str, pathlib.Path]:
    if conflict_mode not in RUN_ID_CONFLICT_MODES:
        raise BenchError(f"unknown run-id conflict mode `{conflict_mode}`")

    # An explicit --run-id is used verbatim (after slug compaction) so a
    # release run carries no host-time fingerprint in its directory name.
    # Without one, the run id falls back to a UTC stamp.
    base_run_id = compact_run_id(requested_run_id) if requested_run_id else ""
    out_root.mkdir(parents=True, exist_ok=True)
    if conflict_mode == "error":
        run_id = base_run_id or run_id_conflict_stamp()
        run_dir = out_root / run_id
        try:
            run_dir.mkdir()
        except FileExistsError as exc:
            raise BenchError(f"run directory already exists; refusing to overwrite: {run_dir}") from exc
        return run_id, run_dir

    for idx in range(1000):
        base = base_run_id or run_id_conflict_stamp()
        if idx == 0:
            candidate = base
        else:
            candidate = run_id_with_suffix(base, f"{idx + 1:03d}")
        run_dir = out_root / candidate
        try:
            run_dir.mkdir()
            return candidate, run_dir
        except FileExistsError:
            continue
    raise BenchError(f"could not allocate a unique run directory under {out_root}")


PROXY_ENV_VARS = (
    "HTTP_PROXY", "http_proxy",
    "HTTPS_PROXY", "https_proxy",
    "ALL_PROXY", "all_proxy",
    "WS_PROXY", "ws_proxy",
)


def subprocess_env() -> dict[str, str]:
    """Environment for engine and driver subprocesses.

    Everything the harness spawns talks over loopback only. Inherited proxy
    variables must not apply: native drivers (e.g. the agent-browser daemon)
    honor HTTP(S)_PROXY but not necessarily NO_PROXY, and a proxied loopback
    CDP connection fails in ways that masquerade as engine defects.
    """
    env = dict(os.environ)
    for name in PROXY_ENV_VARS:
        env.pop(name, None)
    return env


def ensure_agent_browser_socket_dir(env: dict[str, str]) -> pathlib.Path:
    socket_dir = pathlib.Path(env.get("AGENT_BROWSER_SOCKET_DIR") or DEFAULT_AGENT_BROWSER_SOCKET_DIR)
    socket_dir.mkdir(parents=True, exist_ok=True)
    env["AGENT_BROWSER_SOCKET_DIR"] = str(socket_dir)
    return socket_dir


def configure_agent_browser_attempt_env(
    env: dict[str, str],
    run_id: str,
    task_id: str,
    engine: str,
    attempt: int,
    seed: str,
) -> str:
    """Isolate one agent-browser daemon and give abandoned daemons a fuse.

    A probe normally closes its own session.  The idle timeout is a secondary
    guard for hard subprocess kills, where JavaScript ``finally`` blocks
    cannot run.  A per-attempt namespace makes ``close --all`` safe: it cannot
    affect another worker's live agent-browser attempt.
    """
    ensure_agent_browser_socket_dir(env)
    digest = hashlib.sha256(
        f"{run_id}\0{task_id}\0{engine}\0{attempt}\0{seed}".encode("utf-8")
    ).hexdigest()[:12]
    # Keep the namespace short: agent-browser combines it with the explicit
    # session in a Unix-domain socket path, whose portable limit is only about
    # 103 bytes.  Twelve hex digits still give 48 bits of per-attempt identity.
    namespace = f"a{digest}"
    env["AGENT_BROWSER_NAMESPACE"] = namespace
    env["AGENT_BROWSER_IDLE_TIMEOUT_MS"] = "15000"
    return namespace


def force_close_agent_browser_attempt(env: dict[str, str]) -> None:
    """Best-effort close for an adapter killed before its own cleanup."""
    ab_bin = env.get("AB_BIN") or "agent-browser"
    try:
        subprocess.run(
            [ab_bin, "--json", "close", "--all"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=8,
            env=env,
            check=False,
        )
    except Exception:
        # Target cleanup and the daemon idle fuse remain available.  Cleanup
        # failure must never replace the attempt's real timeout/error result.
        pass


def remove_agent_browser_namespace_state(env: dict[str, str]) -> None:
    """Remove disposable state after this isolated daemon has stopped.

    Successful ``close`` leaves a per-session ``.config`` behind.  With a
    unique namespace per attempt those files are never reusable and would
    otherwise grow linearly across a full run.  Refuse cleanup if any socket,
    pid file, symlink, directory, or unknown file is present: those can mean a
    daemon is still live.
    """
    namespace = env.get("AGENT_BROWSER_NAMESPACE") or ""
    if not re.fullmatch(r"a[0-9a-f]{12}", namespace):
        return
    socket_dir = pathlib.Path(
        env.get("AGENT_BROWSER_SOCKET_DIR") or DEFAULT_AGENT_BROWSER_SOCKET_DIR
    )
    namespace_dir = socket_dir / "namespaces" / namespace
    run_dir = namespace_dir / "run"
    # The close CLI can acknowledge just before the daemon removes its socket
    # and pid files.  Poll briefly; returning immediately at that edge leaves
    # the subsequently written config behind.
    deadline = time.monotonic() + 1.0
    while True:
        try:
            entries = list(run_dir.iterdir())
        except OSError:
            entries = []
        disposable = all(
            entry.is_file()
            and not entry.is_symlink()
            and entry.suffix == ".config"
            for entry in entries
        )
        if disposable:
            for entry in entries:
                try:
                    entry.unlink()
                except OSError:
                    return
            break
        if time.monotonic() >= deadline:
            return
        time.sleep(0.02)
    for path in (run_dir, namespace_dir):
        try:
            path.rmdir()
        except OSError:
            pass


def load_json(path: pathlib.Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: pathlib.Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}-{time.time_ns()}")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
        f.write("\n")
    tmp.replace(path)


_RESULTS_LOCK = threading.Lock()


def append_jsonl(path: pathlib.Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, sort_keys=True) + "\n")


def append_result(path: pathlib.Path, payload: dict[str, Any]) -> None:
    # Single writer lock: parallel workers all append to one results.jsonl.
    with _RESULTS_LOCK:
        append_jsonl(path, payload)


def read_jsonl(path: pathlib.Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    with path.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise BenchError(f"{path}:{line_no}: invalid JSONL row: {exc}") from exc
    return rows


def sha256_file(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


RUNNER_SOURCE_PATH = pathlib.Path(__file__).resolve()
# Capture this once while the module is loaded. A long-lived tunneled fixture
# process must not claim a newly checked-out on-disk runner after it has already
# loaded an older implementation into memory.
RUNNER_SOURCE_SHA256 = sha256_file(RUNNER_SOURCE_PATH)


def rel_to_bench(path: pathlib.Path) -> str:
    try:
        return path.resolve().relative_to(BENCH_ROOT.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def resolve_path(path_text: str | None, default: pathlib.Path) -> pathlib.Path:
    if not path_text:
        return default
    path = pathlib.Path(path_text)
    if not path.is_absolute():
        path = pathlib.Path.cwd() / path
    return path.resolve()


def http_json(url: str, timeout: float = 3.0, method: str = "GET", data: Any | None = None) -> Any:
    body = None
    headers: dict[str, str] = {}
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def http_text(url: str, timeout: float = 3.0) -> str:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def port_is_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex((LOCAL_HOST, port)) == 0


def parse_engines(value: str) -> list[str]:
    engines = [item.strip() for item in value.split(",") if item.strip()]
    bad = [item for item in engines if item not in ENGINE_ORDER]
    if bad:
        raise BenchError(f"unknown engine(s): {', '.join(bad)}; expected {', '.join(ENGINE_ORDER)}")
    seen: set[str] = set()
    ordered: list[str] = []
    for engine in ENGINE_ORDER:
        if engine in engines and engine not in seen:
            ordered.append(engine)
            seen.add(engine)
    return ordered


def load_suite(manifest_path: pathlib.Path) -> dict[str, Any]:
    if not manifest_path.exists():
        raise BenchError(f"manifest not found: {manifest_path}")
    suite = load_json(manifest_path)
    if not isinstance(suite, dict):
        raise BenchError(f"manifest must be a JSON object: {manifest_path}")
    return suite


def load_l2_semantic_capability_map(
    manifest_path: pathlib.Path,
    suite: dict[str, Any],
) -> tuple[pathlib.Path | None, dict[str, Any] | None]:
    """Load the manifest-declared L2 semantic map, failing closed when required."""
    try:
        map_path, required = semantic_model.capability_map_reference(manifest_path, suite)
    except ValueError as exc:
        raise BenchError(f"{manifest_path}: {exc}") from exc
    if map_path is None:
        if required:
            raise BenchError(
                f"{manifest_path}: L2 requires `semantic_capability_map`"
            )
        return None, None
    if not map_path.is_file():
        raise BenchError(f"{manifest_path}: semantic capability map not found: {map_path}")
    try:
        payload = semantic_model.load_capability_map(map_path)
    except Exception as exc:
        raise BenchError(f"{map_path}: cannot parse semantic capability map: {exc}") from exc
    return map_path, payload


def all_layer_task_objects(
    manifest_path: pathlib.Path,
    suite: dict[str, Any],
    layer_id: str,
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Load every task in one layer, independent of a narrow CLI selection."""
    tasks: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    for subset in subset_index(suite).values():
        if subset.get("_layer_id") != layer_id:
            continue
        for path in task_paths_for_subset(manifest_path.parent, subset):
            try:
                task = load_json(path)
            except Exception as exc:
                errors.append(f"{path}: cannot parse task JSON for {layer_id} map: {exc}")
                continue
            task_id = task.get("task_id") if isinstance(task, dict) else None
            if not isinstance(task_id, str) or not task_id:
                errors.append(f"{path}: cannot index task without a non-empty task_id")
                continue
            if task_id in tasks:
                errors.append(f"{path}: duplicate active task_id `{task_id}` in {layer_id}")
                continue
            tasks[task_id] = task
    return tasks, errors


def semantic_task_index(
    manifest_path: pathlib.Path,
    suite: dict[str, Any],
) -> tuple[pathlib.Path | None, dict[str, Any] | None, dict[str, dict[str, Any]]]:
    map_path, payload = load_l2_semantic_capability_map(manifest_path, suite)
    return (
        map_path,
        payload,
        semantic_model.capability_task_index(payload) if payload else {},
    )


def subset_index(suite: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for layer in suite.get("layers", []):
        layer_id = layer.get("layer_id")
        for subset in layer.get("subsets", []):
            subset_id = subset.get("subset_id")
            if not subset_id:
                continue
            item = dict(subset)
            item["_layer_id"] = layer_id
            # Manifest semantics are opt-out: omitted `enabled` means enabled.
            item["_layer_enabled"] = layer.get("enabled", True) is not False
            item["_layer_name"] = layer.get("name")
            result[subset_id] = item
    return result


def selected_subset_ids(
    suite: dict[str, Any],
    requested_subsets: list[str] | None,
    requested_tasks: list[str] | None,
    requested_layers: list[str] | None = None,
    for_list: bool = False,
) -> list[str]:
    index = subset_index(suite)
    available_layers = [layer.get("layer_id") for layer in suite.get("layers", []) if layer.get("layer_id")]
    if requested_layers:
        missing = [layer for layer in requested_layers if layer not in available_layers]
        if missing:
            raise BenchError(
                f"unknown layer(s): {', '.join(missing)}; expected {', '.join(available_layers)}"
            )
    if requested_subsets:
        missing = [sid for sid in requested_subsets if sid not in index]
        if missing:
            raise BenchError(f"unknown subset(s): {', '.join(missing)}")
        subset_ids = list(dict.fromkeys(requested_subsets))
    elif requested_tasks:
        subset_ids = list(index.keys())
    else:
        subset_ids = [sid for sid, subset in index.items() if subset.get("_layer_enabled") and subset.get("enabled", True)]
    if requested_layers:
        wanted = set(requested_layers)
        subset_ids = [sid for sid in subset_ids if index[sid].get("_layer_id") in wanted]
    if not subset_ids and (requested_layers or requested_subsets):
        raise BenchError("layer/subset selection resolved to no subsets")
    return subset_ids


def task_paths_for_subset(bench_root: pathlib.Path, subset: dict[str, Any]) -> list[pathlib.Path]:
    pattern = subset.get("task_glob")
    if not pattern:
        return []
    return [pathlib.Path(p).resolve() for p in sorted(glob.glob(str(bench_root / pattern)))]


def task_to_resolved(path: pathlib.Path, task: dict[str, Any], subset: dict[str, Any]) -> ResolvedTask:
    return ResolvedTask(
        layer=str(task.get("layer", "")),
        subset_id=str(task.get("subset_id", "")),
        task_id=str(task.get("task_id", "")),
        task_version=int(task.get("task_version", 0)),
        path=path,
        rel_path=rel_to_bench(path),
        sha256=sha256_file(path),
        description=str(task.get("description", "")),
        features=list(task.get("features", [])),
        tags=list(task.get("tags", [])),
        scene=dict(task.get("scene", {})),
        driver=dict(task.get("driver", {})),
        grader=dict(task.get("grader", {})),
        chrome_gate=task.get("chrome_gate"),
        subset_chrome_gate=subset.get("chrome_gate"),
        launch_profile=str(task.get("launch_profile", DEFAULT_LAUNCH_PROFILE)),
        artifact_profile=str(task.get("artifact_profile", "")),
        task=task,
    )


def expand_tasks(
    manifest_path: pathlib.Path,
    requested_subsets: list[str] | None = None,
    requested_tasks: list[str] | None = None,
    requested_features: list[str] | None = None,
    requested_tags: list[str] | None = None,
    requested_layers: list[str] | None = None,
    for_list: bool = False,
) -> tuple[dict[str, Any], list[ResolvedTask]]:
    suite = load_suite(manifest_path)
    bench_root = manifest_path.parent
    index = subset_index(suite)
    subset_ids = selected_subset_ids(suite, requested_subsets, requested_tasks, requested_layers, for_list=for_list)
    resolved: list[ResolvedTask] = []
    errors: list[str] = []

    for subset_id in subset_ids:
        subset = index[subset_id]
        for path in task_paths_for_subset(bench_root, subset):
            try:
                task = load_json(path)
            except Exception as exc:
                errors.append(f"{path}: cannot parse task JSON: {exc}")
                continue
            if requested_tasks and task.get("task_id") not in requested_tasks:
                continue
            resolved.append(task_to_resolved(path, task, subset))

    if requested_tasks:
        found = {task.task_id for task in resolved}
        missing = [task_id for task_id in requested_tasks if task_id not in found]
        if missing:
            scopes = []
            if requested_subsets:
                scopes.append(f"subset(s) {', '.join(requested_subsets)}")
            if requested_layers:
                scopes.append(f"layer(s) {', '.join(requested_layers)}")
            scope = f" in {' and '.join(scopes)}" if scopes else ""
            raise BenchError(f"task(s) not found{scope}: {', '.join(missing)}")

    if requested_features:
        wanted = set(requested_features)
        resolved = [task for task in resolved if wanted.intersection(task.features)]
    if requested_tags:
        wanted = set(requested_tags)
        resolved = [task for task in resolved if wanted.intersection(task.tags)]

    if requested_layers and any((requested_subsets, requested_tasks, requested_features, requested_tags)) and not resolved:
        raise BenchError("selector intersection resolved to no tasks")

    if errors:
        raise BenchError("\n".join(errors))

    deduped: list[ResolvedTask] = []
    seen: set[pathlib.Path] = set()
    for task in resolved:
        if task.path not in seen:
            deduped.append(task)
            seen.add(task.path)
    if any(task.layer == "L2" for task in deduped):
        _semantic_path, _semantic_map, semantic_index = semantic_task_index(
            manifest_path, suite
        )
        for task in deduped:
            task.semantic_capability = semantic_index.get(task.task_id)
    return suite, deduped


def validate_raw_cdp_steps(rel_path: str, steps: list[Any]) -> list[str]:
    """Schema-check raw_cdp driver steps (v0.2 primitives included).

    Each step is one of: a command step (`method`), an event-wait step
    (`wait_for_event`), or a bare sleep step (`sleep_ms`). Session references
    (`session` / `save_session_as`) are tracked so a typo'd `session` name is
    caught at validate time rather than at run time.
    """
    errors: list[str] = []
    known_sessions = {"browser", "page"}

    def validate_event_match(where: str, match: Any) -> None:
        if isinstance(match, dict):
            if EVENT_MATCH_ONE_OF in match:
                if set(match) != {EVENT_MATCH_ONE_OF}:
                    errors.append(f"{where} matcher `{EVENT_MATCH_ONE_OF}` cannot be combined with sibling keys")
                    return
                choices = match[EVENT_MATCH_ONE_OF]
                if not isinstance(choices, list) or not choices:
                    errors.append(f"{where}.{EVENT_MATCH_ONE_OF} must be a non-empty list")
                    return
                for idx, choice in enumerate(choices):
                    validate_event_match(f"{where}.{EVENT_MATCH_ONE_OF}[{idx}]", choice)
                return
            for key, child in match.items():
                validate_event_match(f"{where}.{key}", child)
        elif isinstance(match, list):
            for idx, child in enumerate(match):
                validate_event_match(f"{where}[{idx}]", child)

    for idx, step in enumerate(steps):
        where = f"{rel_path}: driver.steps[{idx}]"
        if not isinstance(step, dict):
            errors.append(f"{where} must be an object")
            continue
        has_method = "method" in step
        has_wait = "wait_for_event" in step
        has_sleep = "sleep_ms" in step
        if not (has_method or has_wait or has_sleep):
            errors.append(f"{where} must declare one of `method`, `wait_for_event`, or `sleep_ms`")
        if has_method and has_wait:
            errors.append(f"{where} cannot be both a command and a wait_for_event step")
        if has_wait and has_sleep:
            errors.append(f"{where} cannot combine wait_for_event with sleep_ms (the sleep would be ignored)")
        if has_method and not isinstance(step["method"], str):
            errors.append(f"{where}.method must be a string")
        elif step.get("method") in NON_TARGET_CDP_METHODS:
            errors.append(f"{where}.method `{step['method']}` is outside the benchmark target")
        if has_wait and not isinstance(step["wait_for_event"], str):
            errors.append(f"{where}.wait_for_event must be a string")
        if has_wait and "match" in step:
            validate_event_match(f"{where}.match", step["match"])
        if has_sleep and not isinstance(step["sleep_ms"], (int, float)):
            errors.append(f"{where}.sleep_ms must be a number")
        if "timeout_ms" in step and not isinstance(step["timeout_ms"], (int, float)):
            errors.append(f"{where}.timeout_ms must be a number")
        if step.get("expect_unsupported") is not None:
            if not isinstance(step["expect_unsupported"], bool):
                errors.append(f"{where}.expect_unsupported must be a boolean")
            if not has_method:
                errors.append(f"{where}.expect_unsupported is only valid on a command step")
        if "session" in step:
            session = step["session"]
            if not isinstance(session, str):
                errors.append(f"{where}.session must be a string")
            elif session not in known_sessions:
                errors.append(f"{where}.session references unknown session `{session}` (define it earlier via save_session_as)")
        for capture in ("save_session_as", "save_result_as", "save_as"):
            if capture in step and not isinstance(step[capture], str):
                errors.append(f"{where}.{capture} must be a string")
        if isinstance(step.get("save_session_as"), str):
            known_sessions.add(step["save_session_as"])
    return errors


def _task_json_location(path: tuple[str | int, ...]) -> str:
    location = ""
    for item in path:
        if isinstance(item, int):
            location += f"[{item}]"
        else:
            location += ("." if location else "") + item
    return location or "<root>"


def validate_task_host_paths(rel_path: str, task_obj: Any) -> list[str]:
    """Reject non-portable paths only where the task contract executes a path.

    A recursive string scan cannot distinguish a cookie's legal ``path="/"``
    or an origin-relative ``url="/home"`` from a host filesystem path.  Keep
    this validator method/field-aware so prose, expected values, selectors, and
    JavaScript source remain data while actual file-bearing fields are checked.
    """
    errors: list[str] = []

    def report(path: tuple[str | int, ...], value: str) -> None:
        errors.append(
            f"{rel_path}: {_task_json_location(path)} contains host absolute path "
            f"or unsafe traversal `{value}`; use a runner path token such as "
            "{fixture_path} or {artifact_dir}"
        )

    def is_file_uri(value: str) -> bool:
        return value.strip().lower().startswith("file:")

    def is_windows_rooted(value: str) -> bool:
        stripped = value.strip()
        return bool(WINDOWS_DRIVE_PATH_RE.match(stripped) or stripped.startswith("\\"))

    def has_parent_traversal(value: str) -> bool:
        return ".." in value.replace("\\", "/").split("/")

    def validate_local_path(
        value: Any,
        path: tuple[str | int, ...],
        allowed_tokens: frozenset[str] = RUNNER_LOCAL_PATH_TOKENS,
        require_authorized_token: bool = False,
    ) -> None:
        if isinstance(value, list):
            for idx, child in enumerate(value):
                validate_local_path(
                    child,
                    path + (idx,),
                    allowed_tokens,
                    require_authorized_token,
                )
            return
        if not isinstance(value, str):
            return
        stripped = value.strip()
        if require_authorized_token and stripped != value:
            report(path, value)
            return
        has_authorized_token = any(
            token in allowed_tokens
            and (
                stripped == token
                or stripped.startswith(f"{token}/")
            )
            for token in RUNNER_LOCAL_PATH_TOKENS
        )
        if (
            any(
                token in stripped
                and (
                    token not in allowed_tokens
                    or not (
                        stripped == token
                        or stripped.startswith(f"{token}/")
                    )
                )
                for token in RUNNER_LOCAL_PATH_TOKENS
            )
            or (require_authorized_token and not has_authorized_token)
            or is_file_uri(stripped)
            or stripped.startswith("/")
            or is_windows_rooted(stripped)
            or stripped.startswith("~")
            or has_parent_traversal(stripped)
        ):
            report(path, value)

    def validate_url(value: Any, path: tuple[str | int, ...]) -> None:
        if isinstance(value, list):
            for idx, child in enumerate(value):
                validate_url(child, path + (idx,))
            return
        if not isinstance(value, str):
            return
        if is_file_uri(value) or is_windows_rooted(value):
            report(path, value)

    def validate_url_fields(value: Any, path: tuple[str | int, ...]) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                normalized = str(key).replace("_", "").replace("-", "").lower()
                child_path = path + (str(key),)
                if normalized in {"url", "urls", "uri", "uris", "href"} or normalized.endswith(
                    ("url", "urls", "uri", "uris")
                ):
                    validate_url(child, child_path)
                elif isinstance(child, (dict, list)):
                    validate_url_fields(child, child_path)
        elif isinstance(value, list):
            for idx, child in enumerate(value):
                validate_url_fields(child, path + (idx,))

    def command_words(value: str) -> list[str]:
        try:
            # posix=False preserves Windows separators so C:\... cannot be
            # normalized into an apparently relative string by the validator.
            return shlex.split(value, posix=False)
        except ValueError:
            return value.split()

    def validate_command(value: Any, path: tuple[str | int, ...]) -> None:
        if isinstance(value, list):
            words = [str(item) for item in value if isinstance(item, (str, int, float))]
        elif isinstance(value, str):
            words = command_words(value)
        else:
            return

        def shell_word(word: str) -> str:
            stripped = word.strip()
            if (
                len(stripped) >= 2
                and stripped[0] == stripped[-1]
                and stripped[0] in {"'", '"'}
            ):
                return stripped[1:-1]
            return stripped

        if words:
            validate_local_path(
                shell_word(words[0]),
                path + ((0,) if isinstance(value, list) else ()),
            )
        expect_path = False
        for idx, word in enumerate(words):
            word_path = path + ((idx,) if isinstance(value, list) else ())
            if expect_path:
                validate_local_path(shell_word(word), word_path)
                expect_path = False
                continue
            if not word.startswith("-"):
                continue
            flag, separator, flag_value = word.lstrip("-").partition("=")
            if flag.lower() not in COMMAND_PATH_FLAGS:
                continue
            if separator:
                validate_local_path(shell_word(flag_value), word_path)
            else:
                expect_path = True
        if expect_path:
            errors.append(
                f"{rel_path}: {_task_json_location(path)} command ends with a path "
                "flag that has no following argument"
            )

    if not isinstance(task_obj, dict):
        return errors

    scene = task_obj.get("scene")
    if isinstance(scene, dict) and "url" in scene:
        validate_url(scene["url"], ("scene", "url"))
    grader = task_obj.get("grader")
    if isinstance(grader, dict) and "endpoint" in grader:
        validate_url(grader["endpoint"], ("grader", "endpoint"))

    driver = task_obj.get("driver")
    if not isinstance(driver, dict):
        return errors

    for field in ("command", "argv", "args"):
        if field in driver:
            validate_command(driver[field], ("driver", field))
    if "script" in driver:
        validate_local_path(
            driver["script"],
            ("driver", "script"),
            frozenset(),
        )

    steps = driver.get("steps")
    if isinstance(steps, list):
        raw_file_fields = {
            "DOM.setFileInputFiles": {
                "files": RUNNER_LOCAL_PATH_TOKENS,
            },
            "Browser.setDownloadBehavior": {
                "downloadPath": ARTIFACT_LOCAL_PATH_TOKENS,
            },
            "Page.setDownloadBehavior": {
                "downloadPath": ARTIFACT_LOCAL_PATH_TOKENS,
            },
        }
        file_ops = {
            "download": ARTIFACT_LOCAL_PATH_TOKENS,
            "set_input_files": RUNNER_LOCAL_PATH_TOKENS,
            "write_file": ARTIFACT_LOCAL_PATH_TOKENS,
        }
        for idx, step in enumerate(steps):
            if not isinstance(step, dict):
                continue
            step_path = ("driver", "steps", idx)
            for field in ("command", "argv", "args"):
                if field in step:
                    validate_command(step[field], step_path + (field,))
            method = step.get("method")
            params = step.get("params")
            if isinstance(params, dict):
                validate_url_fields(params, step_path + ("params",))
                for field, allowed_tokens in raw_file_fields.get(
                    str(method), {}
                ).items():
                    if field in params:
                        validate_local_path(
                            params[field],
                            step_path + ("params", field),
                            allowed_tokens,
                            True,
                        )
            if step.get("op") in file_ops and "path" in step:
                validate_local_path(
                    step["path"],
                    step_path + ("path",),
                    file_ops[str(step["op"])],
                    True,
                )
            if "url" in step:
                validate_url(step["url"], step_path + ("url",))

    checks = driver.get("checks")
    if isinstance(checks, list):
        for idx, check in enumerate(checks):
            if (
                isinstance(check, dict)
                and str(check.get("kind", "")).startswith("file_")
                and "path" in check
            ):
                validate_local_path(
                    check["path"],
                    ("driver", "checks", idx, "path"),
                    ARTIFACT_LOCAL_PATH_TOKENS,
                    True,
                )

    env = driver.get("env")
    if isinstance(env, dict):
        ab_checks = env.get("AB_CHECKS")
        if isinstance(ab_checks, list):
            for idx, check in enumerate(ab_checks):
                if (
                    isinstance(check, dict)
                    and str(check.get("kind", "")).startswith("file_")
                    and "path" in check
                ):
                    validate_local_path(
                        check["path"],
                        ("driver", "env", "AB_CHECKS", idx, "path"),
                        ARTIFACT_LOCAL_PATH_TOKENS,
                        True,
                    )
        ab_steps = env.get("AB_STEPS")
        if isinstance(ab_steps, list):
            for idx, step in enumerate(ab_steps):
                args = step.get("ab") if isinstance(step, dict) else None
                if not isinstance(args, list):
                    continue
                file_arg: int | None = None
                if args[:1] in (["download"], ["upload"]):
                    file_arg = 2
                elif args[:2] in (["profiler", "stop"], ["trace", "stop"]):
                    file_arg = 2
                elif args[:3] in (
                    ["network", "har", "start"],
                    ["network", "har", "stop"],
                ):
                    file_arg = 3
                if file_arg is not None and file_arg < len(args):
                    if (
                        args[:1] == ["upload"]
                        and isinstance(args[file_arg], str)
                    ):
                        tmp_file = re.fullmatch(
                            r"\{tmp_file:([^:}]+):(.*)\}",
                            args[file_arg],
                            flags=re.DOTALL,
                        )
                        if (
                            tmp_file
                            and tmp_file.group(1) not in {".", ".."}
                            and "/" not in tmp_file.group(1)
                            and "\\" not in tmp_file.group(1)
                        ):
                            continue
                    allowed_tokens = (
                        RUNNER_LOCAL_PATH_TOKENS
                        if args[:1] == ["upload"]
                        else ARTIFACT_LOCAL_PATH_TOKENS
                    )
                    validate_local_path(
                        args[file_arg],
                        ("driver", "env", "AB_STEPS", idx, "ab", file_arg),
                        allowed_tokens,
                        True,
                    )

    return errors


FRAMEWORK_DRIVER_KINDS = {
    "framework_playwright": "playwright",
    "framework_puppeteer": "puppeteer",
}
FRAMEWORK_PROBE_SCRIPT = BENCH_ROOT / "runner" / "scripts" / "framework_probe.js"
HARNESS_PINS_PATH = BENCH_ROOT / "harness_pins.json"


def effective_harness_pins() -> dict[str, Any]:
    pins = load_json(HARNESS_PINS_PATH)
    if not isinstance(pins, dict):
        raise BenchError("harness_pins.json root must be an object")
    drivers = pins.get("drivers")
    if not isinstance(drivers, dict):
        raise BenchError("harness_pins.json.drivers must be an object")
    overrides = ACTIVE_ENGINE_SET.get("harness_drivers") or {}
    if not isinstance(overrides, dict):
        raise BenchError("active engine set `harness_drivers` must be an object")
    for name, override in overrides.items():
        if not isinstance(override, dict):
            raise BenchError(f"invalid active harness driver override: {name}")
        # Unit tests and custom manifests may intentionally provide only a
        # reduced driver pin table. An active-set override for an absent driver
        # is irrelevant to that table and must not make it invalid.
        if name not in drivers:
            continue
        if not isinstance(drivers[name], dict):
            raise BenchError(f"invalid harness driver pin: {name}")
        drivers[name].update(override)
    return pins


# Scenario adapters are per-driver programs that replay the
# framework_probe.js op vocabulary through the driver's real client library,
# speaking the abb_scenario_adapter/1 stdin/stdout contract
# (runner/scripts/adapters/PROTOCOL.md). One entry per thin-client driver.
SCENARIO_ADAPTER_KINDS: dict[str, dict[str, Any]] = {
    "thin_chrome_remote_interface": {
        "driver_key": "chrome_remote_interface",
        "argv": ["node"],
        "script": "runner/scripts/adapters/cri_adapter.js",
    },
    "thin_cdp_use": {
        "driver_key": "cdp_use",
        "argv": ["python3"],
        "script": "runner/scripts/adapters/cdp_use_adapter.py",
    },
    "thin_pydoll": {
        "driver_key": "pydoll",
        "argv": ["python3"],
        "script": "runner/scripts/adapters/pydoll_adapter.py",
    },
    "framework_stagehand": {
        "driver_key": "stagehand",
        "argv": ["node"],
        "script": "runner/scripts/adapters/stagehand_adapter.js",
    },
    "mcp_chrome_devtools": {
        "driver_key": "chrome_devtools_mcp",
        "argv": ["node"],
        "script": "runner/scripts/adapters/cdt_mcp_adapter.js",
    },
    "thin_chromedp": {
        "driver_key": "chromedp",
        # The compiled binary, not `go run`: see the note on COMPILED_ADAPTER_
        # BUILD_HINTS. Built from the committed go.mod/go.sum pin.
        "argv": ["{script}/chromedp_adapter"],
        "script": "runner/scripts/adapters/chromedp_adapter",
    },
    "thin_rod": {
        "driver_key": "rod",
        "argv": ["{script}/rod_adapter"],
        "script": "runner/scripts/adapters/rod_adapter",
    },
    "tool_agent_browser": {
        "driver_key": "agent_browser",
        "argv": ["node"],
        "script": "runner/scripts/adapters/ab_scenario_adapter.js",
        # The AB daemon needs the isolated socket dir and the repo-local
        # pinned binary; run_scenario_adapter_driver applies this hook.
        "env_setup": "agent_browser",
    },
    "thin_ferrum": {
        "driver_key": "ferrum",
        "argv": ["ruby"],
        "script": "runner/scripts/adapters/ferrum_adapter.rb",
    },
    "webdriver_selenium": {
        "driver_key": "selenium",
        "argv": ["python3"],
        "script": "runner/scripts/adapters/selenium_adapter.py",
    },
    "thin_chromiumoxide": {
        "driver_key": "chromiumoxide",
        # The compiled binary, not `cargo run`: see COMPILED_ADAPTER_BUILD_HINTS.
        # Built against the committed Cargo.toml/Cargo.lock pin.
        "argv": ["{script}/target/debug/chromiumoxide_adapter"],
        "script": "runner/scripts/adapters/chromiumoxide_adapter",
    },
}


def scenario_adapter_argv(spec: dict[str, Any]) -> list[str]:
    """Resolve the exact adapter argv used by the subprocess runner."""

    script_path = str(BENCH_ROOT / str(spec["script"]))
    if any("{script}" in str(arg) for arg in spec["argv"]):
        return [
            str(arg).replace("{script}", script_path)
            for arg in spec["argv"]
        ]
    return [str(arg) for arg in spec["argv"]] + [script_path]


CATALOG_DRIVER_BY_TASK_KIND = {
    **{
        task_kind: driver_id
        for task_kind, driver_id in FRAMEWORK_DRIVER_KINDS.items()
    },
    **{
        task_kind: spec["driver_key"]
        for task_kind, spec in SCENARIO_ADAPTER_KINDS.items()
    },
}


def _binding_assertion_payload(assertion: binding_catalog.Assertion) -> dict[str, Any]:
    row: dict[str, Any] = {
        "mechanism": assertion.mechanism,
        "actual_path": assertion.actual_path,
        "operator": assertion.operator,
        "condition": assertion.condition,
    }
    if assertion.expected_ref is not None:
        row["expected_ref"] = assertion.expected_ref
    if assertion.expected_literal is not None:
        row["expected_literal"] = assertion.expected_literal
    if assertion.fallback_actual_path is not None:
        row["fallback_actual_path"] = assertion.fallback_actual_path
    if assertion.fallback_operator is not None:
        row["fallback_operator"] = assertion.fallback_operator
    return row


def _harness_pin_payload(
    reference: binding_catalog.Reference,
    driver_pins: dict[str, Any],
    *,
    executable_required: bool,
) -> dict[str, Any]:
    metadata = driver_pins.get(reference.key)
    if not isinstance(metadata, dict):
        raise BenchError(
            f"selenium binding pin `{reference.ref_id}` resolves to missing "
            f"harness_pins.json.drivers.{reference.key}"
        )
    if not isinstance(metadata.get("version"), str) or not metadata["version"].strip():
        raise BenchError(
            f"selenium binding pin `{reference.ref_id}` must declare a non-empty version"
        )
    if (
        reference.ref_id == "driver.selenium"
        and metadata.get("pip_package") != "selenium"
    ):
        raise BenchError(
            "selenium driver pin must declare pip_package `selenium`"
        )
    selected_metadata = {
        key: metadata[key]
        for key in ("version", "pip_package", "binary_path", "sha256_12")
        if key in metadata
    }
    row: dict[str, Any] = {
        "ref_id": reference.ref_id,
        "key": reference.key,
        "metadata": selected_metadata,
    }
    binary_path = metadata.get("binary_path")
    if executable_required:
        if not isinstance(binary_path, str) or not binary_path.strip():
            raise BenchError(
                f"selenium bridge pin `{reference.ref_id}` has no binary_path"
            )
        relative = pathlib.Path(binary_path)
        if relative.is_absolute():
            raise BenchError(
                f"selenium bridge pin `{reference.ref_id}` binary_path must be repository-relative"
            )
        if ".." in relative.parts:
            raise BenchError(
                f"selenium bridge pin `{reference.ref_id}` binary_path escapes the repository"
            )
        candidate = BENCH_ROOT / relative
        try:
            executable = candidate.resolve(strict=True)
        except FileNotFoundError as exc:
            raise BenchError(
                f"selenium bridge pin `{reference.ref_id}` executable is missing: {candidate}"
            ) from exc
        try:
            executable.relative_to(BENCH_ROOT.resolve())
        except ValueError as exc:
            raise BenchError(
                f"selenium bridge pin `{reference.ref_id}` executable escapes the repository"
            ) from exc
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise BenchError(
                f"selenium bridge pin `{reference.ref_id}` executable is not an executable file: "
                f"{executable}"
            )
        expected_sha = metadata.get("sha256_12")
        if (
            not isinstance(expected_sha, str)
            or re.fullmatch(r"[0-9a-f]{12}", expected_sha) is None
        ):
            raise BenchError(
                f"selenium bridge pin `{reference.ref_id}` must declare a 12-character "
                "lowercase hex sha256_12"
            )
        try:
            actual_sha = sha256_file(executable)[:12]
        except OSError as exc:
            raise BenchError(
                f"selenium bridge pin `{reference.ref_id}` executable could not be read"
            ) from exc
        if actual_sha != expected_sha:
            raise BenchError(
                f"selenium bridge pin `{reference.ref_id}` sha256 mismatch: "
                f"expected {expected_sha}, got {actual_sha}"
            )
        row["executable"] = str(executable)
    return row


def normalize_selenium_binding(
    binding: binding_catalog.Binding,
    driver_pins: dict[str, Any],
) -> dict[str, Any]:
    """Serialize one frozen Catalog record for the Selenium adapter.

    The adapter intentionally receives facts, not a catalog path.  This keeps
    route selection in the runner and makes the payload a complete,
    language-neutral record of the route attempted for this result.
    """
    route = binding.route
    if route is None:
        raise BenchError(
            f"selenium binding `{binding.binding_id}` has no route: "
            f"{binding.unavailable_reason or 'reason not provided'}"
        )
    bridge_required = route.lifecycle.bridge_owner != "none"
    bridge_pins = [
        _harness_pin_payload(pin, driver_pins, executable_required=bridge_required)
        for pin in binding.bridge_pins
    ]
    if bridge_required and not bridge_pins:
        raise BenchError(
            f"selenium binding `{binding.binding_id}` requires a bridge but has no bridge pin"
        )
    if not bridge_required and bridge_pins:
        raise BenchError(
            f"selenium binding `{binding.binding_id}` declares bridge pins for a native route"
        )
    driver_pin = _harness_pin_payload(
        binding.driver_pin,
        driver_pins,
        executable_required=False,
    )
    return {
        "binding_id": binding.binding_id,
        "browser_id": binding.browser_id,
        "driver_id": binding.driver_id,
        "route": {
            "route_id": route.route_id,
            "client_protocol": route.client_protocol,
            "client_endpoint_kind": route.client_endpoint_kind,
            "browser_endpoint_kind": route.browser_endpoint_kind,
            "connect_mode": route.connect_mode,
            "provider": route.provider,
            "ordered_hops": [
                {
                    "from": hop.source,
                    "to": hop.destination,
                    "protocol": hop.protocol,
                    "transport": hop.transport,
                    "endpoint_kind": hop.endpoint_kind,
                }
                for hop in route.ordered_hops
            ],
            "lifecycle": {
                "browser_owner": route.lifecycle.browser_owner,
                "bridge_owner": route.lifecycle.bridge_owner,
                "adapter_owner": route.lifecycle.adapter_owner,
            },
            "discovery": {
                "browser": {
                    "kind": route.discovery.browser.kind,
                    "endpoint_kind": route.discovery.browser.endpoint_kind,
                    "probe": route.discovery.browser.probe,
                    "readiness_owner": route.discovery.browser.readiness_owner,
                },
                "client": {
                    "kind": route.discovery.client.kind,
                    "endpoint_kind": route.discovery.client.endpoint_kind,
                    "probe": route.discovery.client.probe,
                    "readiness_owner": route.discovery.client.readiness_owner,
                },
            },
            "identity": {
                "http_assertions": [
                    _binding_assertion_payload(assertion)
                    for assertion in route.identity.http_assertions
                ],
                "live_transport_assertions": [
                    _binding_assertion_payload(assertion)
                    for assertion in route.identity.live_transport_assertions
                ],
            },
        },
        "pins": {
            "browser": {
                "ref_id": binding.browser_pin.ref_id,
                "key": binding.browser_pin.key,
            },
            "driver": driver_pin,
            "bridges": bridge_pins,
        },
        "fallback_allowed": binding.fallback_allowed,
    }


def unavailable_binding_payload(
    binding: binding_catalog.Binding,
) -> dict[str, Any]:
    if binding.route is not None or not binding.unavailable_reason:
        raise BenchError(
            f"binding `{binding.binding_id}` is not an unavailable binding"
        )
    return {
        "binding_id": binding.binding_id,
        "browser_id": binding.browser_id,
        "driver_id": binding.driver_id,
        "route": None,
        "unavailable_reason": binding.unavailable_reason,
        "classification": "protocol_incompatibility",
        "pins": {
            "browser": {
                "ref_id": binding.browser_pin.ref_id,
                "key": binding.browser_pin.key,
            },
            "driver": {
                "ref_id": binding.driver_pin.ref_id,
                "key": binding.driver_pin.key,
            },
            "bridges": [],
        },
        "fallback_allowed": False,
        "verified": False,
    }


def resolve_unavailable_runtime_bindings(
    tasks: list[ResolvedTask],
    engines: list[str],
) -> dict[tuple[str, str], dict[str, Any]]:
    """Resolve catalog-declared incompatibilities before browsers launch."""
    driver_ids = {
        CATALOG_DRIVER_BY_TASK_KIND[kind]
        for task in tasks
        for kind in [str(task.driver.get("kind") or "")]
        if kind in CATALOG_DRIVER_BY_TASK_KIND
    }
    if not driver_ids:
        return {}
    try:
        catalog = binding_catalog.load_catalog()
        resolved: dict[tuple[str, str], dict[str, Any]] = {}
        for engine in engines:
            for driver_id in driver_ids:
                binding = catalog.require_binding(engine, driver_id)
                if binding.route is None:
                    resolved[(engine, driver_id)] = unavailable_binding_payload(
                        binding
                    )
        return resolved
    except (binding_catalog.CatalogError, OSError, json.JSONDecodeError) as exc:
        raise BenchError(f"driver binding configuration error: {exc}") from exc


def _validate_installed_selenium_client(driver_pins: dict[str, Any]) -> None:
    metadata = driver_pins.get("selenium")
    if not isinstance(metadata, dict):
        raise BenchError(
            "selenium binding pin `driver.selenium` resolves to missing "
            "harness_pins.json.drivers.selenium"
        )
    pinned_version = metadata.get("version")
    if not isinstance(pinned_version, str) or not pinned_version.strip():
        raise BenchError(
            "selenium binding pin `driver.selenium` must declare a non-empty version"
        )
    if metadata.get("pip_package") != "selenium":
        raise BenchError("selenium driver pin must declare pip_package `selenium`")
    installed_version = installed_pip_version("selenium")
    if installed_version is None:
        raise BenchError(
            f"pinned Selenium client is not installed (expected {pinned_version})"
        )
    if installed_version != pinned_version:
        raise BenchError(
            "installed Selenium client version does not match harness pin: "
            f"expected {pinned_version}, got {installed_version}"
        )


def resolve_selenium_runtime_bindings(
    tasks: list[ResolvedTask],
    engines: list[str],
) -> dict[str, dict[str, Any]]:
    """Resolve every Selenium pair before any browser worker is launched."""
    if not any(task.driver.get("kind") == "webdriver_selenium" for task in tasks):
        return {}
    try:
        catalog = binding_catalog.load_catalog()
        # Materialize the same effective pin set that `doctor` verifies.  The
        # active engine set may replace Chrome and its matching ChromeDriver;
        # reading harness_pins.json directly here would validate one binary
        # but hand a stale bridge pin to the Selenium binding.
        pins_raw = effective_harness_pins()
        if not isinstance(pins_raw, dict):
            raise BenchError("effective harness pins root must be an object")
        driver_pins = pins_raw.get("drivers")
        if not isinstance(driver_pins, dict):
            raise BenchError("effective harness pins drivers must be an object")
        bindings_by_engine = {
            engine: catalog.require_binding(engine, "selenium")
            for engine in engines
        }
        if any(binding.route is not None for binding in bindings_by_engine.values()):
            _validate_installed_selenium_client(driver_pins)
        resolved: dict[str, dict[str, Any]] = {}
        for engine, binding in bindings_by_engine.items():
            if binding.route is None:
                resolved[engine] = unavailable_binding_payload(binding)
            else:
                resolved[engine] = normalize_selenium_binding(
                    binding,
                    driver_pins,
                )
        return resolved
    except (binding_catalog.CatalogError, OSError, json.JSONDecodeError) as exc:
        raise BenchError(f"selenium binding configuration error: {exc}") from exc

# Adapters that run as a pre-built binary rather than through a build wrapper
# (`cargo run` / `go run`).
#
# `timeouts.task_ms` is an absolute wall-clock budget covering the whole adapter
# process, so any fixed per-attempt cost that has nothing to do with the engine
# eats into the engine's share of it. Build wrappers are exactly that cost:
# cargo takes a global lock on the package cache for every invocation, even a
# fully warm no-op, so at --jobs 64 each attempt blocks ~15s waiting for the
# lock and every task in the subset hits the budget and is recorded as an
# engine `timeout`. `go run` is milder (~2.2s at the same concurrency) but is
# the same trade: build-system work charged to the benchmark.
#
# Build them once after checkout, like `npm ci`; `doctor` reports any that are
# missing so a fresh checkout fails loudly instead of timing out 255 times.
COMPILED_ADAPTER_BUILD_HINTS: dict[str, str] = {
    "chromedp": "go build -C runner/scripts/adapters/chromedp_adapter -o chromedp_adapter .",
    "rod": "go build -C runner/scripts/adapters/rod_adapter -o rod_adapter .",
    "chromiumoxide": "cargo build --manifest-path runner/scripts/adapters/chromiumoxide_adapter/Cargo.toml",
}


def validate_framework_steps(rel_path: str, driver: dict[str, Any], probe_script: pathlib.Path = FRAMEWORK_PROBE_SCRIPT) -> list[str]:
    """Schema-check declarative-op driver scenarios.

    Covers framework drivers (framework_playwright / framework_puppeteer,
    executed by framework_probe.js) and thin-client scenario adapters
    (SCENARIO_ADAPTER_KINDS), which share the same op vocabulary and check
    evaluator family.
    """
    errors: list[str] = []
    steps = driver.get("steps")
    if not isinstance(steps, list) or not steps:
        errors.append(f"{rel_path}: framework driver task must set a non-empty driver.steps list")
        return errors
    for idx, step in enumerate(steps):
        where = f"{rel_path}: driver.steps[{idx}]"
        if not isinstance(step, dict):
            errors.append(f"{where} must be an object")
            continue
        if not isinstance(step.get("op"), str) or not step.get("op"):
            errors.append(f"{where} must declare a string `op`")
        elif step["op"] in NON_TARGET_FRAMEWORK_OPS:
            errors.append(f"{where}.op `{step['op']}` is outside the benchmark target")
        if "timeout_ms" in step and not isinstance(step["timeout_ms"], (int, float)):
            errors.append(f"{where}.timeout_ms must be a number")
        if "save_as" in step and not isinstance(step["save_as"], str):
            errors.append(f"{where}.save_as must be a string")
    checks = driver.get("checks")
    if checks is not None and not isinstance(checks, list):
        errors.append(f"{rel_path}: driver.checks must be a list when present")
    connect_options = driver.get("connect_options")
    if connect_options is not None and not isinstance(connect_options, dict):
        errors.append(f"{rel_path}: driver.connect_options must be an object when present")
    if not probe_script.exists():
        errors.append(f"{rel_path}: driver probe/adapter script missing: {rel_to_repo(probe_script)}")
    return errors


def validate_framework_scene_url(rel_path: str, scene: dict[str, Any]) -> list[str]:
    """The scene.url template only understands {seed} and {session}; any other
    literal {token} would raise KeyError at run time, so catch it at validate."""
    url = scene.get("url")
    if not isinstance(url, str):
        return []
    try:
        url.format(seed="s", session="x")
    except (KeyError, IndexError, ValueError) as exc:
        return [f"{rel_path}: scene.url has an unsupported placeholder ({exc}); only {{seed}} and {{session}} are available"]
    return []


def validate_task(task: ResolvedTask, subset: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    task_obj = task.task
    errors.extend(validate_task_host_paths(task.rel_path, task_obj))
    required = [
        "task_id",
        "task_version",
        "layer",
        "subset_id",
        "description",
        "features",
        "tags",
        "scene",
        "driver",
        "grader",
        "timeouts",
        "artifact_profile",
    ]
    for field in required:
        if field not in task_obj:
            errors.append(f"{task.rel_path}: missing required field `{field}`")
    for field in (
        "score_lane",
        "source_reference",
        "migration_notes",
        "expected_status_claim",
    ):
        if field in task_obj:
            errors.append(f"{task.rel_path}: obsolete field `{field}` is not allowed")

    if task.layer != subset.get("_layer_id"):
        errors.append(f"{task.rel_path}: layer `{task.layer}` does not match subset layer `{subset.get('_layer_id')}`")
    if task.subset_id != subset.get("subset_id"):
        errors.append(f"{task.rel_path}: subset_id `{task.subset_id}` does not match `{subset.get('subset_id')}`")
    if not isinstance(task.features, list) or not all(isinstance(item, str) for item in task.features):
        errors.append(f"{task.rel_path}: features must be a string list")
    else:
        for feature in task.features:
            if feature in NON_TARGET_FEATURES:
                errors.append(f"{task.rel_path}: feature `{feature}` is outside the benchmark target")
            elif feature in RETIRED_FEATURES:
                errors.append(f"{task.rel_path}: feature `{feature}` is a retired spelling; use the canonical name")
            elif feature.endswith(RETIRED_FEATURE_SUFFIXES):
                errors.append(
                    f"{task.rel_path}: feature `{feature}` encodes an error mode in its name; "
                    "keep the real method name and express the error mode via tags/checks"
                )
    if not isinstance(task_obj.get("description"), str) or not task_obj.get("description", "").strip():
        errors.append(f"{task.rel_path}: description must be a non-empty string")
    if not isinstance(task.tags, list) or not all(isinstance(item, str) for item in task.tags):
        errors.append(f"{task.rel_path}: tags must be a string list")
    else:
        for tag in task.tags:
            namespace, _, _ = tag.partition(".")
            if namespace not in TAG_NAMESPACES:
                errors.append(
                    f"{task.rel_path}: tag `{tag}` uses unknown namespace `{namespace}` "
                    f"(allowed: {', '.join(sorted(TAG_NAMESPACES))})"
                )
            elif namespace == "version" and tag not in VERSION_TAGS:
                errors.append(f"{task.rel_path}: unknown version tag `{tag}` (allowed: {', '.join(sorted(VERSION_TAGS))})")
        version_tags = [tag for tag in task.tags if tag.startswith("version.")]
        if len(version_tags) > 1:
            errors.append(f"{task.rel_path}: at most one version.* tag is allowed, got {version_tags}")
        if not version_tags and "purpose.demo" not in task.tags:
            errors.append(f"{task.rel_path}: task must carry a version.* tag (demo tasks carry purpose.demo instead)")

    driver_kind = task.driver.get("kind")
    known_kinds = {"raw_cdp", "node_cdp_probe", "framework_playwright", "framework_puppeteer"} | set(SCENARIO_ADAPTER_KINDS)
    if driver_kind not in known_kinds:
        errors.append(f"{task.rel_path}: unsupported driver.kind `{driver_kind}`")
    if subset.get("driver") and driver_kind != subset.get("driver"):
        errors.append(f"{task.rel_path}: driver.kind `{driver_kind}` does not match subset driver `{subset.get('driver')}`")
    if driver_kind in FRAMEWORK_DRIVER_KINDS or driver_kind in SCENARIO_ADAPTER_KINDS:
        probe_script = FRAMEWORK_PROBE_SCRIPT
        if driver_kind in SCENARIO_ADAPTER_KINDS:
            probe_script = BENCH_ROOT / SCENARIO_ADAPTER_KINDS[driver_kind]["script"]
        errors.extend(validate_framework_steps(task.rel_path, task.driver, probe_script))
        if task.scene.get("kind") != "self_hosted_fixture":
            errors.append(f"{task.rel_path}: framework driver tasks must use a self_hosted_fixture scene")
        else:
            errors.extend(validate_framework_scene_url(task.rel_path, task.scene))
    if driver_kind == "node_cdp_probe":
        script = task.driver.get("script")
        if not script:
            errors.append(f"{task.rel_path}: node_cdp_probe task must set driver.script")
        elif not (BENCH_ROOT / script).exists():
            errors.append(f"{task.rel_path}: driver.script not found: {script}")
    if driver_kind == "raw_cdp":
        steps = task.driver.get("steps")
        if not isinstance(steps, list) or not steps:
            errors.append(f"{task.rel_path}: raw_cdp task must set a non-empty driver.steps list")
        else:
            errors.extend(validate_raw_cdp_steps(task.rel_path, steps))

    scene_kind = task.scene.get("kind")
    if scene_kind not in {"about_blank", "self_hosted_fixture"}:
        errors.append(f"{task.rel_path}: unsupported scene.kind `{scene_kind}`")
    if scene_kind == "self_hosted_fixture":
        url = task.scene.get("url")
        if not isinstance(url, str) or not url.startswith("/"):
            errors.append(f"{task.rel_path}: self_hosted_fixture scene.url must be an absolute fixture path")

    grader_kind = task.grader.get("kind")
    if grader_kind not in {"inline_assertions", "server_side"}:
        errors.append(f"{task.rel_path}: unsupported grader.kind `{grader_kind}`")
    if grader_kind == "inline_assertions":
        inline_checks = task.grader.get("checks") or task.driver.get("checks")
        if not isinstance(inline_checks, list) or not inline_checks:
            errors.append(
                f"{task.rel_path}: inline_assertions must declare non-empty "
                "grader.checks or driver.checks; a return envelope alone is not verifiable"
            )
    if grader_kind == "server_side":
        endpoint = task.grader.get("endpoint")
        if not isinstance(endpoint, str) or not endpoint.startswith("/"):
            errors.append(f"{task.rel_path}: server_side grader.endpoint must be an absolute fixture path")

    if task.artifact_profile not in ARTIFACT_PROFILES:
        errors.append(f"{task.rel_path}: unsupported artifact_profile `{task.artifact_profile}`")

    if task.launch_profile not in LAUNCH_PROFILES:
        errors.append(
            f"{task.rel_path}: unsupported launch_profile `{task.launch_profile}` "
            f"(allowed: {', '.join(sorted(LAUNCH_PROFILES))})"
        )

    effective_gate = task.chrome_gate or subset.get("chrome_gate") or "off"
    if effective_gate not in GATE_POLICIES:
        errors.append(f"{task.rel_path}: task must resolve to a valid Chrome baseline policy")

    return errors


def validate_manifest(
    manifest_path: pathlib.Path,
    requested_subsets: list[str] | None = None,
    requested_tasks: list[str] | None = None,
    requested_layers: list[str] | None = None,
) -> tuple[dict[str, Any], list[ResolvedTask], list[str]]:
    errors: list[str] = []
    try:
        suite = load_suite(manifest_path)
    except Exception as exc:
        return {}, [], [str(exc)]

    for field in ["bench_id", "bench_version", "root_dir", "fallback_allowed", "default_k_runs", "site", "engines", "layers"]:
        if field not in suite:
            errors.append(f"{manifest_path}: missing required field `{field}`")
    if suite.get("fallback_allowed") is not False:
        errors.append(f"{manifest_path}: fallback_allowed must be false for native runs")
    if not isinstance(suite.get("layers"), list):
        errors.append(f"{manifest_path}: layers must be a list")
    manifest_engines = suite.get("engines")
    if not isinstance(manifest_engines, dict):
        errors.append(f"{manifest_path}: engines must be an object")
    elif set(manifest_engines) != set(ENGINE_ORDER):
        errors.append(
            f"{manifest_path}: engines must be exactly {', '.join(ENGINE_ORDER)}"
        )
    else:
        for engine in ENGINE_ORDER:
            expected_role = ENGINE_DEFS[engine]["role"]
            if (manifest_engines.get(engine) or {}).get("role") != expected_role:
                errors.append(
                    f"{manifest_path}: engines.{engine}.role must be `{expected_role}`"
                )

    index = subset_index(suite)
    for layer in suite.get("layers", []) if isinstance(suite.get("layers"), list) else []:
        if "score_policy" in layer:
            errors.append(f"{manifest_path}: obsolete field `score_policy` is not allowed")
        for subset in layer.get("subsets", []) if isinstance(layer.get("subsets"), list) else []:
            if "status" in subset:
                sid = subset.get("subset_id", "<unknown>")
                errors.append(f"{manifest_path}: subset `{sid}` uses obsolete field `status`")
    try:
        subset_ids = selected_subset_ids(suite, requested_subsets, requested_tasks, requested_layers)
    except BenchError as exc:
        return suite, [], [str(exc)]

    resolved: list[ResolvedTask] = []
    for subset_id in subset_ids:
        subset = index.get(subset_id)
        if not subset:
            errors.append(f"{manifest_path}: unknown subset `{subset_id}`")
            continue
        if subset.get("chrome_gate", "off") not in GATE_POLICIES:
            errors.append(f"{manifest_path}: subset `{subset_id}` has invalid chrome_gate `{subset.get('chrome_gate')}`")
        paths = task_paths_for_subset(manifest_path.parent, subset)
        if not paths and not subset.get("allow_empty"):
            errors.append(f"{manifest_path}: enabled subset `{subset_id}` expands to no tasks")
        for path in paths:
            try:
                task_obj = load_json(path)
                task = task_to_resolved(path, task_obj, subset)
                if requested_tasks and task.task_id not in requested_tasks:
                    continue
                resolved.append(task)
                errors.extend(validate_task(task, subset))
            except Exception as exc:
                errors.append(f"{path}: cannot validate task: {exc}")

    try:
        semantic_map_path, semantic_map = load_l2_semantic_capability_map(
            manifest_path, suite
        )
    except BenchError as exc:
        errors.append(str(exc))
        semantic_map_path, semantic_map = None, None
    if semantic_map_path is not None and semantic_map is not None:
        l2_task_objects, l2_load_errors = all_layer_task_objects(
            manifest_path, suite, "L2"
        )
        errors.extend(l2_load_errors)
        errors.extend(
            semantic_model.validate_capability_map(
                semantic_map,
                l2_task_objects,
                rel_to_bench(semantic_map_path),
            )
        )

    # Runtime grading fails closed when /__grade__/expected_answer has no
    # registered answer; surface that at validate time instead.
    expected_registry = load_expected_answer_registry(BENCH_ROOT / "fixtures")
    for task in resolved:
        grader = task.grader or {}
        if (
            grader.get("kind") == "server_side"
            and grader.get("endpoint") == "/__grade__/expected_answer"
            and task.task_id not in expected_registry
        ):
            errors.append(
                f"{task.rel_path}: graded by /__grade__/expected_answer but the "
                f"fixture expected-answer registry has no entry for `{task.task_id}`"
            )

    if requested_tasks:
        found = {task.task_id for task in resolved}
        missing = [task_id for task_id in requested_tasks if task_id not in found]
        if missing:
            errors.append(f"task(s) not found: {', '.join(missing)}")

    return suite, resolved, errors


def resolve_gate_policy(task: ResolvedTask, cli_policy: str | None, suite: dict[str, Any]) -> str:
    policy = cli_policy or task.chrome_gate or task.subset_chrome_gate or suite.get("chrome_gate") or "off"
    if policy not in GATE_POLICIES:
        raise BenchError(f"{task.task_id}: invalid Chrome baseline policy `{policy}`")
    return policy


def score_eligible_for_run(
    suite: dict[str, Any],
    tasks: list[ResolvedTask],
    selected_engines: list[str],
    cli_gate: str | None,
    debug: bool,
    score_mode: str = "baseline_checked",
) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if suite.get("fallback_allowed") is not False:
        reasons.append("suite fallback_allowed is not false")
    if debug:
        reasons.append("--debug run")
    # A baseline-checked score may either include the complete four-engine
    # roster, or compare the complete native-candidate roster with the Chrome
    # oracle explicitly disabled.  The latter is the reproducible v0.4 mode
    # for reusing the already-stable Chrome evidence without rerunning Chrome.
    selected_set = set(selected_engines)
    complete = selected_set == set(ENGINE_ORDER)
    if (
        tasks
        and score_mode != "independent"
        and selected_set == set(NATIVE_CANDIDATES)
        and all(resolve_gate_policy(task, cli_gate, suite) == "off" for task in tasks)
    ):
        complete = True
    if tasks and not complete:
        reasons.append("partial engine set")
    return not reasons, reasons


def count_tasks_by(tasks: list[ResolvedTask], attr: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for task in tasks:
        key = str(getattr(task, attr))
        counts[key] = counts.get(key, 0) + 1
    return counts


def format_count_map(counts: dict[str, int]) -> str:
    if not counts:
        return "none"
    return ", ".join(f"{key}={counts[key]}" for key in sorted(counts))


def format_elapsed(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:d}:{secs:02d}"


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def visible_len(text: str) -> int:
    return len(strip_ansi(text))


def expected_result_rows(tasks: list[ResolvedTask], k_runs: int, selected_engines: list[str]) -> int:
    return len(tasks) * max(0, int(k_runs)) * len(selected_engines)


def gate_policy_counts(tasks: list[ResolvedTask], cli_policy: str | None, suite: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for task in tasks:
        policy = resolve_gate_policy(task, cli_policy, suite)
        counts[policy] = counts.get(policy, 0) + 1
    return counts


def enabled_task_count(manifest_path: pathlib.Path) -> int | None:
    try:
        _, all_tasks = expand_tasks(manifest_path, for_list=True)
    except Exception:
        return None
    return len(all_tasks)


def run_manifest_payload(
    args: argparse.Namespace,
    suite: dict[str, Any],
    manifest_path: pathlib.Path,
    tasks: list[ResolvedTask],
    selected_engines: list[str],
    run_id: str,
    score_eligible: bool,
    score_reasons: list[str],
    fixture_base_url: str | None,
) -> dict[str, Any]:
    gates = [resolve_gate_policy(task, args.chrome_gate, suite) for task in tasks]
    resolved_default = gates[0] if gates and all(gate == gates[0] for gate in gates) else "mixed"
    semantic_map_path, semantic_map, semantic_index = semantic_task_index(
        manifest_path, suite
    )
    semantic_snapshot = None
    if semantic_map_path is not None and semantic_map is not None and any(
        task.layer == "L2" for task in tasks
    ):
        semantic_snapshot = semantic_model.capability_map_snapshot(
            semantic_map,
            [task.task_id for task in tasks],
            path=rel_to_repo(semantic_map_path),
            sha256=semantic_model.sha256_file(semantic_map_path),
        )
    engines: dict[str, Any] = {}
    for engine in ENGINE_ORDER:
        meta = ENGINE_DEFS[engine]
        binary = pathlib.Path(meta["binary"])
        actual_sha256 = sha256_file(binary) if binary.exists() else None
        actual_sha12 = actual_sha256[:12] if actual_sha256 else None
        item = {
            "binary": rel_to_repo(binary),
            "version": meta["version"],
            "sha256": actual_sha256,
            "sha256_12": actual_sha12,
            "expected_sha256": meta.get("sha256"),
            "expected_sha256_12": meta["sha256_12"],
            "cdp_port": meta["cdp_port"],
            "role": meta["role"],
        }
        serve_args = [str(arg) for arg in meta.get("serve_args", ())]
        if serve_args:
            item["serve_args"] = serve_args
        launch_profile_args = {
            str(profile): [str(arg) for arg in args]
            for profile, args in meta.get("launch_profile_args", {}).items()
        }
        if launch_profile_args:
            item["launch_profile_args"] = launch_profile_args
        if engine == "chrome":
            item["mode"] = "headless=new"
        if engine == "moli":
            item["resource_fetch_policy"] = "task_scoped_launch_profile"
        if engine == "obscura":
            item.update(
                {
                    "upstream_tag": meta["upstream_tag"],
                    "upstream_commit": meta["upstream_commit"],
                    "release_archive_sha256": meta["release_archive_sha256"],
                    "network_policy": "loopback_fixture_only",
                    "stealth": False,
                    "persistent_storage": False,
                    "file_access": False,
                }
            )
        engines[engine] = item

    chrome_baseline_policy = {
        "requested": args.chrome_gate,
        "resolved_default": resolved_default,
        "score_eligible": score_eligible,
        "ineligibility_reasons": score_reasons,
    }
    resource_mode = str(getattr(args, "resource_profile", "off") or "off")
    host_telemetry_enabled = str(getattr(args, "host_telemetry", "on") or "on") == "on"
    calibration_baseline = getattr(args, "resource_calibration_baseline", None)

    return {
        "run_id": run_id,
        "started_at": now_iso(),
        "argv": [sanitize_launch_part(part) for part in sys.argv],
        "bench_id": suite.get("bench_id"),
        "bench_version": suite.get("bench_version"),
        "bench_manifest": {
            "path": rel_to_repo(manifest_path),
            "sha256": sha256_file(manifest_path),
        },
        "selected_layers": sorted({task.layer for task in tasks}),
        "enabled_subsets": sorted({task.subset_id for task in tasks}),
        "score_mode": getattr(args, "score_mode", "baseline_checked") or "baseline_checked",
        "scored_engines": list(scored_engines(getattr(args, "score_mode", "baseline_checked") or "baseline_checked")),
        "chrome_baseline_policy": chrome_baseline_policy,
        # Legacy schema key retained for older tooling and existing artifacts.
        "chrome_gate_policy": chrome_baseline_policy,
        "resolved_tasks": [
            task.to_run_manifest(semantic_index.get(task.task_id)) for task in tasks
        ],
        **(
            {"l2_semantic_capability_map": semantic_snapshot}
            if semantic_snapshot is not None
            else {}
        ),
        "selected_engines": selected_engines,
        "k_runs": args.k,
        "seed": args.seed,
        "fallback_allowed": suite.get("fallback_allowed"),
        "score_eligible": score_eligible,
        "engine_set": {
            "name": ACTIVE_ENGINE_SET.get("name", "repository-defaults"),
            "manifest": rel_to_repo(ACTIVE_ENGINE_SET_PATH) if ACTIVE_ENGINE_SET else None,
        },
        "engines": engines,
        "host": resource_metrics.host_provenance(
            str(getattr(args, "provenance_level", "full") or "full")
        ),
        "host_telemetry": {
            "schema": resource_metrics.HOST_TELEMETRY_SCHEMA,
            "enabled": host_telemetry_enabled,
            "sample_interval_s": float(getattr(args, "host_sample_interval_s", 2.0) or 2.0),
            "artifact": "host_telemetry.jsonl" if host_telemetry_enabled else None,
            "scope": "host_scope",
            "affects_functional_score": False,
        },
        "resource_profile": {
            "schema": resource_metrics.ENGINE_RESOURCE_SCHEMA,
            "mode": resource_mode,
            "sample_interval_ms": int(getattr(args, "resource_sample_interval_ms", 250) or 250),
            "engine_scope": "engine root plus all descendants",
            "harness_scope": "reported separately and excluded from engine comparison",
            "fixture_traffic_semantics": "application headers+bodies; grader/event endpoints excluded",
            "control_plane_traffic": "unavailable in portable backend",
            "wire_traffic": "unavailable in portable backend",
            "engine_order": "balanced_rotation"
            if resource_mode in {"baseline", "engine"}
            else "benchmark_default",
            "engine_order_algorithm": (
                "cyclic_task_attempt_seed_offset_v1"
                if resource_mode in {"baseline", "engine"}
                else None
            ),
            "calibration_baseline": str(calibration_baseline) if calibration_baseline else None,
            "max_observer_effect_pct": float(
                getattr(args, "resource_max_observer_effect_pct", 10.0) or 10.0
            ),
            "affects_functional_score": False,
        },
        "site": {
            "base_url": fixture_base_url,
            "site_version": suite.get("site", {}).get("site_version"),
        },
        "runner": {
            "python": sys.version.split()[0],
            "node": node_version(),
            "harness_pins": harness_pins_summary(),
            "source": runner_source_provenance(),
            "fixtures": fixtures_provenance(),
            "compiled_adapters": compiled_adapter_provenance(),
            "jobs": max(1, int(getattr(args, "jobs", 1) or 1)),
            "browser_reuse": "per_worker_process_per_engine" if int(getattr(args, "jobs", 1) or 1) > 1 else "per_run_process_per_engine",
            "artifact_write_order": "artifact_then_results_jsonl",
        },
    }


def harness_pins_summary() -> dict[str, Any]:
    """Pinned + installed harness driver versions for run provenance."""
    summary: dict[str, Any] = {"manifest": rel_to_repo(HARNESS_PINS_PATH) if HARNESS_PINS_PATH.exists() else None}
    if HARNESS_PINS_PATH.exists():
        try:
            pins = effective_harness_pins()
        except Exception:
            return summary
        drivers: dict[str, Any] = {}
        for name, spec in (pins.get("drivers") or {}).items():
            drivers[name] = {
                "pinned": spec.get("version"),
                "installed": installed_driver_version(spec, name),
            }
        summary["drivers"] = drivers
    return summary


def runner_source_provenance() -> dict[str, Any]:
    """Hash the effective runner/harness source, including dirty contents."""

    source_suffixes = {".py", ".js", ".go", ".rb", ".rs"}
    source_names = {
        "Cargo.toml",
        "Cargo.lock",
        "go.mod",
        "go.sum",
        "Gemfile",
        "Gemfile.lock",
    }
    paths = [
        path
        for path in (BENCH_ROOT / "runner").rglob("*")
        if path.is_file()
        and (path.suffix in source_suffixes or path.name in source_names)
        and "target" not in path.parts
        and "__pycache__" not in path.parts
    ]
    digest = hashlib.sha256()
    files: list[dict[str, str]] = []
    for path in sorted(paths):
        relative = path.relative_to(BENCH_ROOT).as_posix()
        file_sha = sha256_file(path)
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(file_sha.encode("ascii"))
        digest.update(b"\n")
        files.append({"path": relative, "sha256": file_sha})
    return {
        "schema": "abb_runner_source/1",
        "tree_sha256": digest.hexdigest(),
        "file_count": len(files),
        "files": files,
    }


def fixtures_provenance() -> dict[str, Any]:
    """Hash the fixture tree so a run proves which fixture bytes it served."""
    digest = hashlib.sha256()
    count = 0
    for path in sorted((BENCH_ROOT / "fixtures").rglob("*")):
        if not path.is_file():
            continue
        digest.update(path.relative_to(BENCH_ROOT).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(sha256_file(path).encode("ascii"))
        digest.update(b"\n")
        count += 1
    return {
        "schema": "abb_fixture_tree/1",
        "tree_sha256": digest.hexdigest(),
        "file_count": count,
    }


COMPILED_ADAPTER_BINARIES = {
    "chromedp": "runner/scripts/adapters/chromedp_adapter/chromedp_adapter",
    "rod": "runner/scripts/adapters/rod_adapter/rod_adapter",
    "chromiumoxide": "runner/scripts/adapters/chromiumoxide_adapter/target/debug/chromiumoxide_adapter",
}


def compiled_adapter_provenance() -> dict[str, Any]:
    """Digest the compiled adapter binaries actually on disk.

    Source pins (go.sum / Cargo.lock) are covered by runner_source_provenance;
    this proves which built artifacts executed, instead of leaving that
    inferred from the pins.
    """
    adapters: dict[str, Any] = {}
    for name, relative in COMPILED_ADAPTER_BINARIES.items():
        path = BENCH_ROOT / relative
        adapters[name] = {
            "path": relative,
            "sha256": sha256_file(path) if path.is_file() else None,
        }
    return {"schema": "abb_compiled_adapters/1", "binaries": adapters}


def rel_to_repo(path: pathlib.Path | str) -> str:
    path = pathlib.Path(path)
    try:
        if path.is_absolute():
            return path.relative_to(REPO_ROOT).as_posix()
        return path.as_posix()
    except ValueError:
        return path.as_posix()


def sanitize_launch_part(part: str) -> str:
    """Strip host fingerprints from one recorded launch-command element.

    Paths under the repo become repo-relative; paths under the system temp
    directory (per-launch profile dirs and the like) become a stable
    placeholder. Everything else passes through unchanged.
    """
    tmp_root = tempfile.gettempdir().rstrip("/") + "/"

    def sanitize_value(value: str) -> str:
        if value.startswith(str(REPO_ROOT)):
            return rel_to_repo(value)
        if value.startswith(tmp_root):
            return "<ephemeral>"
        return value

    if part.startswith("--") and "=" in part:
        flag, _, value = part.partition("=")
        return f"{flag}={sanitize_value(value)}"
    return sanitize_value(part)


def node_version() -> str | None:
    try:
        proc = subprocess.run(["node", "--version"], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)
    except Exception:
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def pinned_binary_version(binary: pathlib.Path, meta: dict[str, Any]) -> str | None:
    argv_template = meta.get("version_argv")
    if not argv_template:
        return None
    argv = [str(part).replace("{binary}", str(binary)) for part in argv_template]
    try:
        proc = subprocess.run(
            argv,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5,
            check=False,
        )
    except Exception:
        return ""
    if proc.returncode != 0:
        return ""
    return (proc.stdout or proc.stderr).strip()


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((LOCAL_HOST, 0))
        return int(sock.getsockname()[1])


def engine_serve_args(
    engine: str,
    launch_profile: str = DEFAULT_LAUNCH_PROFILE,
) -> tuple[str, ...]:
    if launch_profile not in LAUNCH_PROFILES:
        raise BenchError(f"unsupported launch profile: {launch_profile}")
    meta = ENGINE_DEFS[engine]
    base_args = tuple(str(arg) for arg in meta.get("serve_args", ()))
    profile_args = tuple(
        str(arg)
        for arg in meta.get("launch_profile_args", {}).get(launch_profile, ())
    )
    return base_args + profile_args


def serve_engine_launch_command(
    engine: str,
    binary: pathlib.Path,
    port: int,
    launch_profile: str = DEFAULT_LAUNCH_PROFILE,
) -> list[str]:
    """Build the auditable serve command for a non-Chrome engine."""
    return [
        str(binary),
        "serve",
        "--host",
        LOCAL_HOST,
        "--port",
        str(port),
        *engine_serve_args(engine, launch_profile),
    ]


class BrowserManager:
    def __init__(
        self,
        dynamic_ports: bool = False,
        resource_runtime: ResourceRuntime | None = None,
        worker_slot: int = 0,
    ) -> None:
        self.processes: dict[str, BrowserProcess] = {}
        self.dynamic_ports = dynamic_ports
        self.resource_runtime = resource_runtime
        self.worker_slot = worker_slot
        self._profile_dirs: list[pathlib.Path] = []
        self._generations: dict[str, int] = {}
        self._last_task: dict[str, str] = {}
        self._lock = threading.RLock()
        self._closed = False

    def note_task(self, engine: str, task_id: str) -> str | None:
        """Record this worker's task order per engine, returning the predecessor."""
        with self._lock:
            previous = self._last_task.get(engine)
            self._last_task[engine] = task_id
            return previous

    def launch(
        self,
        engine: str,
        launch_profile: str = DEFAULT_LAUNCH_PROFILE,
    ) -> BrowserProcess:
        with self._lock:
            if self._closed:
                raise BenchError("browser manager is closed")
            return self._launch_locked(engine, launch_profile)

    def _launch_locked(self, engine: str, launch_profile: str) -> BrowserProcess:
        desired_serve_args = engine_serve_args(engine, launch_profile)
        if engine in self.processes:
            browser = self.processes[engine]
            proc = browser.process
            alive = (proc is None or proc.poll() is None) and port_is_open(browser.port)
            if alive and browser.serve_args == desired_serve_args:
                return browser
            if alive:
                # A worker owns only one process per engine. Switching task
                # profiles replaces that process so resource measurement never
                # includes a dormant default and all-resources Moli together.
                self._kill_process(proc)
            else:
                # Engines can crash under driver input (a benchmark result for
                # the task that triggered it). Drop the corpse and relaunch so
                # one crash does not cascade connection failures over later
                # tasks on this worker.
                try:
                    if proc is not None and proc.poll() is None:
                        proc.terminate()
                except Exception:
                    pass
            if browser.cgroup is not None:
                browser.cgroup.cleanup()
            del self.processes[engine]
        meta = ENGINE_DEFS[engine]
        binary = pathlib.Path(meta["binary"])
        if not binary.exists():
            raise BenchError(f"{engine}: pinned binary not found: {binary}")
        attempts = 3 if self.dynamic_ports else 1
        last_error: Exception | None = None
        for _ in range(attempts):
            port = find_free_port() if self.dynamic_ports else int(meta["cdp_port"])
            try:
                return self._launch_on_port(engine, binary, port, launch_profile)
            except BenchError as exc:
                last_error = exc
                if "already in use" not in str(exc):
                    raise
        raise last_error if last_error else BenchError(f"{engine}: launch failed")

    def _launch_on_port(
        self,
        engine: str,
        binary: pathlib.Path,
        port: int,
        launch_profile: str,
    ) -> BrowserProcess:
        if port_is_open(port):
            raise BenchError(f"{engine}: port {port} is already in use")
        serve_args = engine_serve_args(engine, launch_profile)
        if engine == "chrome":
            profile_dir = pathlib.Path(tempfile.mkdtemp(prefix=f"abb-chrome-{port}-"))
            self._profile_dirs.append(profile_dir)
            cmd = [
                str(binary),
                "--headless=new",
                "--no-sandbox",
                "--disable-gpu",
                "--no-proxy-server",
                f"--user-data-dir={profile_dir}",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-background-networking",
                "--disable-component-update",
                "--disable-sync",
                "--disable-features=NetworkPrediction,OptimizationHints",
                # Hermetic bench: every non-loopback host resolves to a dead
                # local port so stray Chrome-internal requests fail instantly
                # instead of stalling the first fixture navigation ~25s on a
                # network-blackholed machine (QUIC to clients2.google.com).
                "--host-resolver-rules=MAP * 127.0.0.1:9, EXCLUDE 127.0.0.1, EXCLUDE localhost",
                # No HTTP cache: the first navigation on a fresh profile can
                # deadlock ~25s against its own speculative preconnect on the
                # cache-entry lock (netlog: HTTP_CACHE_ADD_TO_ENTRY), and a
                # benchmark wants uncached fetches anyway.
                "--disable-http-cache",
                f"--remote-debugging-port={port}",
                f"--remote-debugging-address={LOCAL_HOST}",
                "about:blank",
            ]
        else:
            cmd = serve_engine_launch_command(engine, binary, port, launch_profile)

        # Browser output goes to spool files: PIPE would deadlock the engine
        # once the 64 KiB pipe buffer fills (nobody drains it during a run).
        log_dir = pathlib.Path(tempfile.mkdtemp(prefix=f"abb-{engine}-log-"))
        self._profile_dirs.append(log_dir)
        stdout_file = (log_dir / "stdout.log").open("w", encoding="utf-8")
        stderr_file = (log_dir / "stderr.log").open("w", encoding="utf-8")
        env = subprocess_env()
        if engine == "chrome":
            # A hung session DBus makes Chrome's first navigation block on its
            # 25s DBus call timeout (observed on this host). Point both buses
            # at /dev/null so the connect fails instantly.
            env["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=/dev/null"
            env["DBUS_SYSTEM_BUS_ADDRESS"] = "unix:path=/dev/null"
        cgroup: resource_metrics.CgroupV2Group | None = None
        cgroup_error: str | None = None
        cgroup_cpu_baseline: dict[str, int] | None = None
        if self.resource_runtime is not None:
            cgroup, cgroup_error = resource_metrics.CgroupV2Group.create(
                f"{self.resource_runtime.run_id}-{engine}-{port}-{time.time_ns()}"
            )
            if cgroup is not None:
                cgroup_cpu_baseline = cgroup.cpu_stat()
                peak_error = cgroup.reset_memory_peak()
                if peak_error:
                    cgroup_error = peak_error

        launch_started = time.perf_counter()
        proc = subprocess.Popen(
            cmd,
            stdout=stdout_file,
            stderr=stderr_file,
            text=True,
            start_new_session=True,
            env=env,
        )
        stdout_file.close()
        stderr_file.close()
        cgroup_assign_errors: list[str] = []
        cold_sampler: resource_metrics.EngineProcessSampler | None = None
        if cgroup is not None:
            cgroup_assign_errors.extend(cgroup.add_process_tree(proc.pid))
        if self.resource_runtime is not None:
            cold_sampler = resource_metrics.EngineProcessSampler(
                proc.pid,
                cgroup,
                min(50, self.resource_runtime.sample_interval_ms),
                reset_memory_peak=False,
                cgroup_cpu_baseline=cgroup_cpu_baseline,
            )
            cold_sampler.start()
        try:
            version_info = wait_for_json_version(port, proc, stderr_path=log_dir / "stderr.log")
            if cgroup is not None:
                # Children forked before the root was migrated are caught here;
                # future descendants inherit the engine cgroup.
                cgroup_assign_errors.extend(cgroup.add_process_tree(proc.pid))
        except Exception:
            if cold_sampler is not None:
                elapsed_ms = int((time.perf_counter() - launch_started) * 1000)
                cold, _ = cold_sampler.stop(elapsed_ms)
                cold.update(
                    {
                        "phase": "cold_start",
                        "engine": engine,
                        "port": port,
                        "ready_ms": elapsed_ms,
                        "status": "launch_failed",
                        "cgroup_error": cgroup_error,
                        "cgroup_assign_errors": cgroup_assign_errors,
                    }
                )
                self.resource_runtime.register_cold_start(cold)
            self._kill_process(proc)
            if cgroup is not None:
                cgroup.cleanup()
            raise
        self._generations[engine] = self._generations.get(engine, 0) + 1
        # The predecessor is only useful as same-process context. Carrying it
        # across a replaced process would name a task that ran somewhere else.
        self._last_task.pop(engine, None)
        browser = BrowserProcess(
            engine=engine,
            port=port,
            process=proc,
            version_info=version_info,
            binary=binary,
            binary_sha256=sha256_file(binary),
            launch_command=tuple(cmd),
            serve_args=serve_args,
            cgroup=cgroup,
            cgroup_error=cgroup_error,
            worker_slot=self.worker_slot,
            generation=self._generations[engine],
        )
        self._warm_up(browser)
        if cgroup is not None:
            cgroup_assign_errors.extend(cgroup.add_process_tree(proc.pid))
        if cold_sampler is not None:
            ready_ms = int((time.perf_counter() - launch_started) * 1000)
            cold, _ = cold_sampler.stop(ready_ms)
            cold.update(
                {
                    "phase": "cold_start",
                    "engine": engine,
                    "port": port,
                    "ready_ms": ready_ms,
                    "launch_cpu_ms": cold.get("cpu_total_ms"),
                    "launch_user_cpu_ms": cold.get("cpu_user_ms"),
                    "launch_system_cpu_ms": cold.get("cpu_system_ms"),
                    "launch_peak_pss_bytes": cold.get("pss_peak_bytes"),
                    "status": "ready",
                    "cgroup_error": cgroup_error,
                    "cgroup_assign_errors": cgroup_assign_errors,
                }
            )
            browser.cold_start = cold
            self.resource_runtime.register_cold_start(cold)
        self.processes[engine] = browser
        return browser

    @staticmethod
    def _warm_up(browser: BrowserProcess) -> None:
        # First navigation on a cold profile can stall; absorb it here instead
        # of inside the first task attempt.
        try:
            ws_url = create_page_ws(browser)
            with CDPClient(ws_url, pathlib.Path(os.devnull), timeout_s=5.0) as client:
                client.command("Runtime.evaluate", {"expression": "1", "returnByValue": True})
        except Exception:
            pass

    def close_all(self) -> None:
        self._closed = True
        with self._lock:
            for browser in list(self.processes.values()):
                self._kill_process(browser.process)
                if browser.cgroup is not None:
                    browser.cgroup.cleanup()
            self.processes.clear()
            for profile_dir in self._profile_dirs:
                shutil.rmtree(profile_dir, ignore_errors=True)
            self._profile_dirs.clear()

    @staticmethod
    def _kill_process(proc: subprocess.Popen[str]) -> None:
        if proc.poll() is not None:
            return
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except Exception:
            proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except Exception:
                proc.kill()
            proc.wait(timeout=5)


def wait_for_json_version(
    port: int,
    proc: subprocess.Popen[str] | None = None,
    timeout_s: float = 10.0,
    stderr_path: pathlib.Path | None = None,
) -> dict[str, Any]:
    deadline = time.time() + timeout_s
    url = f"http://{LOCAL_HOST}:{port}/json/version"
    last_error: str | None = None
    while time.time() < deadline:
        if proc is not None and proc.poll() is not None:
            stderr = ""
            try:
                if stderr_path is not None and stderr_path.exists():
                    stderr = stderr_path.read_text(encoding="utf-8", errors="replace")[-2000:]
                elif proc.stderr:
                    stderr = proc.stderr.read()
            except Exception:
                pass
            raise BenchError(f"browser process exited before ready on port {port}: {stderr.strip()}")
        try:
            return http_json(url, timeout=1.0)
        except Exception as exc:
            last_error = str(exc)
            time.sleep(0.1)
    raise BenchError(f"CDP ready probe failed for {url}: {last_error}")


def create_page_target(browser: BrowserProcess, target_url: str = "about:blank") -> tuple[str, bool]:
    base = browser.base_url
    for candidate_url in [target_url, "about:blank"]:
        encoded = urllib.parse.quote(candidate_url, safe="")
        for method in ("PUT", "GET"):
            try:
                payload = http_json(f"{base}/json/new?{encoded}", timeout=2.0, method=method)
                ws_url = payload.get("webSocketDebuggerUrl")
                if ws_url:
                    return ws_url, candidate_url == target_url
            except Exception:
                pass
    try:
        targets = http_json(f"{base}/json/list", timeout=2.0)
        if isinstance(targets, list):
            for target in targets:
                if target.get("type") == "page" and target.get("webSocketDebuggerUrl"):
                    return target["webSocketDebuggerUrl"], False
            for target in targets:
                if target.get("webSocketDebuggerUrl"):
                    return target["webSocketDebuggerUrl"], False
    except Exception:
        pass
    ws_url = browser.version_info.get("webSocketDebuggerUrl")
    if ws_url:
        return ws_url, False
    raise BenchError(f"{browser.engine}: cannot resolve a CDP websocket endpoint")


def create_page_ws(browser: BrowserProcess) -> str:
    return create_page_target(browser, "about:blank")[0]


BROWSER_SCOPE_DOMAINS = ("Browser.", "Target.", "Schema.", "SystemInfo.", "Security.")
REMOTE_CDP_IDENTITY_FIELDS = ("product", "protocolVersion", "revision")


def require_remote_cdp_identity(
    identity: Any,
    *,
    label: str,
) -> dict[str, str]:
    """Require the exact non-empty identity fields used for remote attribution."""

    if not isinstance(identity, dict):
        raise BenchError(f"{label} must be a JSON object")
    missing = [
        field
        for field in REMOTE_CDP_IDENTITY_FIELDS
        if not isinstance(identity.get(field), str)
        or not str(identity[field]).strip()
    ]
    if missing:
        raise BenchError(
            f"{label} missing non-empty remote identity field(s): "
            + ", ".join(missing)
        )
    return {field: str(identity[field]) for field in REMOTE_CDP_IDENTITY_FIELDS}


def require_matching_remote_cdp_identity(
    observed: Any,
    expected: Any,
    *,
    label: str,
) -> dict[str, str]:
    """Reject a complete but wrong remote identity before attribution."""

    expected_identity = require_remote_cdp_identity(
        expected,
        label=f"{label} expected identity",
    )
    observed_identity = require_remote_cdp_identity(
        observed,
        label=f"{label} observed identity",
    )
    if observed_identity != expected_identity:
        mismatches = {
            field: {
                "expected": expected_identity[field],
                "observed": observed_identity[field],
            }
            for field in REMOTE_CDP_IDENTITY_FIELDS
            if observed_identity[field] != expected_identity[field]
        }
        raise BenchError(
            f"{label} remote identity mismatch: "
            + json.dumps(mismatches, sort_keys=True)
        )
    return observed_identity


def open_page_session(
    browser: BrowserProcess, cdp_path: pathlib.Path, timeout_s: float
) -> tuple[CDPClient, str | None, str | None]:
    """Open a CDP connection with a fresh page.

    Preferred flow (uniform across engines, and required by Lightpanda, which
    rejects page commands on /json/new endpoints with BrowserContextNotLoaded):
    connect to the browser-level websocket, Target.createTarget, then
    Target.attachToTarget(flatten) and address the page via sessionId.
    Falls back to a flat /json/new page websocket when the Target flow is
    unavailable. Returns (client, session_id, target_id).
    """
    browser_ws = browser.version_info.get("webSocketDebuggerUrl")
    strict_remote = browser.version_info.get("transport") == "remote_cdp"
    expected_identity = (
        require_remote_cdp_identity(
            browser.version_info,
            label="remote CDP preflight",
        )
        if strict_remote
        else None
    )
    if browser_ws:
        client = CDPClient(browser_ws, cdp_path, timeout_s=timeout_s)
        target_id: Any = None
        creation_state = "not_requested"
        try:
            client.connect()
            if strict_remote:
                identity = client.command("Browser.getVersion")
                actual_identity = require_remote_cdp_identity(
                    identity,
                    label="remote CDP task connection",
                )
                assert expected_identity is not None
                for key in REMOTE_CDP_IDENTITY_FIELDS:
                    expected = expected_identity[key]
                    actual = actual_identity[key]
                    if actual != expected:
                        raise BenchError(
                            "remote CDP identity changed on the task connection: "
                            f"{key} expected {expected!r}, got {actual!r}"
                        )
                # Experimental callers persist this same-connection identity
                # in their per-task observations.
                client.remote_identity = identity
            creation_state = "requested"
            try:
                created = client.command(
                    "Target.createTarget", {"url": "about:blank"}
                )
            except CDPCommandError:
                # A normal CDP error envelope proves that createTarget was
                # rejected. Transport/parse failures remain ambiguous because
                # the target may have been created before the response was lost.
                creation_state = "rejected"
                raise
            except Exception:
                creation_state = "ambiguous"
                raise
            target_id = created.get("targetId")
            if target_id:
                creation_state = "created"
                attached = client.command("Target.attachToTarget", {"targetId": target_id, "flatten": True})
                session_id = attached.get("sessionId")
                if session_id:
                    return client, str(session_id), str(target_id)
            else:
                creation_state = "ambiguous"
            if strict_remote:
                raise BenchError(
                    "remote CDP endpoint did not create and attach a fresh target"
                )
            client.close()
        except Exception as exc:
            cleanup_confirmed = creation_state in {"not_requested", "rejected"}
            cleanup_attempts: list[dict[str, Any]] = []
            if strict_remote and target_id:
                for cleanup_attempt in range(1, 3):
                    try:
                        close_result = client.command(
                            "Target.closeTarget", {"targetId": target_id}
                        )
                        confirmed = close_result.get("success") is True
                        cleanup_attempts.append(
                            {
                                "attempt": cleanup_attempt,
                                "success": close_result.get("success"),
                                "confirmed": confirmed,
                            }
                        )
                        if confirmed:
                            cleanup_confirmed = True
                            creation_state = "closed"
                            break
                    except Exception as cleanup_exc:
                        cleanup_attempts.append(
                            {
                                "attempt": cleanup_attempt,
                                "confirmed": False,
                                "error": (
                                    f"{type(cleanup_exc).__name__}: {cleanup_exc}"
                                ),
                            }
                        )
            if target_id and not cleanup_confirmed:
                creation_state = "cleanup_unconfirmed"
            client.close()
            if strict_remote:
                cleanup_metrics = {
                    "cdp_call_count": int(getattr(client, "call_count", 0)),
                    "cdp_error_count": int(getattr(client, "error_count", 0)),
                    "ws_disconnect_count": int(
                        getattr(client, "disconnect_count", 0)
                    ),
                }
                cleanup_observations = {
                    "target_cleanup": {
                        "backend": "Target.closeTarget",
                        "target_id": target_id,
                        "confirmed": cleanup_confirmed,
                        "creation_state": creation_state,
                        "ambiguous_create": creation_state == "ambiguous",
                        "attempts": cleanup_attempts,
                    },
                    "isolation_restored": cleanup_confirmed,
                }
                setattr(exc, "cdp_metrics", cleanup_metrics)
                setattr(exc, "cdp_observations", cleanup_observations)
                setattr(exc, "isolation_restored", cleanup_confirmed)
                if not cleanup_confirmed:
                    if is_cdp_transport_exception(exc):
                        # Cleanup evidence still forces the L1 sequence to
                        # stop, but the original timeout/disconnect type must
                        # reach its transport breaker unchanged.
                        raise
                    cleanup_error = BenchError(
                        "remote page bootstrap target cleanup was not confirmed"
                    )
                    setattr(cleanup_error, "cdp_metrics", cleanup_metrics)
                    setattr(
                        cleanup_error,
                        "cdp_observations",
                        cleanup_observations,
                    )
                    setattr(cleanup_error, "isolation_restored", False)
                    raise cleanup_error from exc
                raise
    if strict_remote:
        raise BenchError(
            "remote CDP probes require one browser-level WebSocket with no fallback"
        )
    client = CDPClient(create_page_ws(browser), cdp_path, timeout_s=timeout_s)
    client.connect()
    return client, None, None


class CDPClient:
    def __init__(self, ws_url: str, cdp_path: pathlib.Path, timeout_s: float = 10.0):
        self.ws_url = ws_url
        self.cdp_path = cdp_path
        self.timeout_s = timeout_s
        self.sock: socket.socket | None = None
        self.next_id = 1
        self.call_count = 0
        self.error_count = 0
        self.disconnect_count = 0
        # Bytes read past the HTTP upgrade terminator belong to the first
        # WebSocket frame and must survive the handshake parser.
        self._recv_buf = bytearray()
        # Buffer for CDP events that arrive while a command response is awaited
        # or while pumping for a specific event; consumed by wait_for_event.
        self.events: list[dict[str, Any]] = []

    def __enter__(self) -> "CDPClient":
        self.connect()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def connect(self) -> None:
        self._recv_buf.clear()
        parsed = urllib.parse.urlparse(self.ws_url)
        if parsed.scheme not in {"ws", "wss"}:
            raise BenchError(
                f"only ws:// and wss:// CDP endpoints are supported by this runner: {self.ws_url}"
            )
        secure = parsed.scheme == "wss"
        port = parsed.port or (443 if secure else 80)
        host = parsed.hostname or LOCAL_HOST
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        try:
            raw_sock = socket.create_connection(
                (host, port), timeout=self.timeout_s
            )
        except TimeoutError as exc:
            raise CDPTransportTimeout("TCP connection timed out") from exc
        if secure:
            try:
                sock = ssl.create_default_context().wrap_socket(
                    raw_sock,
                    server_hostname=host,
                )
            except TimeoutError as exc:
                raw_sock.close()
                raise CDPTransportTimeout("TLS handshake timed out") from exc
            except Exception:
                raw_sock.close()
                raise
        else:
            sock = raw_sock
        try:
            sock.settimeout(self.timeout_s)
            key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
            request = (
                f"GET {path} HTTP/1.1\r\n"
                f"Host: {host}:{port}\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\n"
                "Sec-WebSocket-Version: 13\r\n"
                "\r\n"
            )
            sock.sendall(request.encode("ascii"))
            response = b""
            while b"\r\n\r\n" not in response:
                chunk = sock.recv(4096)
                if not chunk:
                    raise CDPTransportError("websocket handshake closed")
                response += chunk
            header, trailing = response.split(b"\r\n\r\n", 1)
            if b" 101 " not in header.split(b"\r\n", 1)[0]:
                raise CDPTransportError(
                    f"websocket handshake failed: {response[:200]!r}"
                )
        except TimeoutError as exc:
            sock.close()
            raise CDPTransportTimeout("websocket handshake timed out") from exc
        except Exception:
            sock.close()
            raise
        self._recv_buf.extend(trailing)
        self.sock = sock
        self.cdp_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.cdp_path.exists():
            self.cdp_path.write_text("", encoding="utf-8")

    def close(self) -> None:
        if self.sock is None:
            return
        try:
            self._send_frame(b"", opcode=8)
        except Exception:
            pass
        try:
            self.sock.close()
        finally:
            self.sock = None

    def command(self, method: str, params: dict[str, Any] | None = None, session_id: str | None = None) -> dict[str, Any]:
        if self.sock is None:
            raise BenchError("CDP client is not connected")
        msg_id = self.next_id
        self.next_id += 1
        self.call_count += 1
        payload = {"id": msg_id, "method": method}
        if params is not None:
            payload["params"] = params
        if session_id:
            payload["sessionId"] = session_id
        self._trace({"ts": now_iso(), "direction": "send", "id": msg_id, "method": method, "params": params or {}, "sessionId": session_id})
        self._send_frame(json.dumps(payload).encode("utf-8"), opcode=1)
        while True:
            frame = self._recv_frame()
            if frame is None:
                self.disconnect_count += 1
                raise ConnectionError("websocket disconnected")
            data = json.loads(frame.decode("utf-8"))
            if data.get("id") == msg_id:
                if "error" in data:
                    self.error_count += 1
                    self._trace({"ts": now_iso(), "direction": "recv", "id": msg_id, "method": method, "error": data["error"]})
                    raise CDPCommandError(method, data["error"])
                self._trace({"ts": now_iso(), "direction": "recv", "id": msg_id, "method": method, "result": data.get("result", {})})
                return data.get("result", {})
            # Buffer events (frames with a method and no matching id) so a later
            # wait_for_event step can observe events that arrived while an earlier
            # command was in flight (e.g. Fetch.requestPaused during Page.navigate).
            if data.get("method") is not None:
                self.events.append(data)
            self._trace({
                "ts": now_iso(),
                "direction": "event",
                "method": data.get("method"),
                "params": data.get("params", {}),
                "params_present": "params" in data,
                "wire_keys": sorted(data),
                "sessionId": data.get("sessionId"),
            })

    def _event_matches(
        self, data: dict[str, Any], method: str, match: Any, session_id: str | None
    ) -> bool:
        if data.get("method") != method:
            return False
        if session_id is not None and data.get("sessionId") != session_id:
            return False
        return event_params_match(data.get("params", {}), match)

    def _has_decrypted_data_pending(self) -> bool:
        """Return whether a TLS socket already has decrypted bytes buffered.

        ``select`` only observes the underlying file descriptor.  An
        ``SSLSocket`` can have additional frames buffered inside OpenSSL after
        that descriptor has been drained, so callers must check ``pending``
        before waiting for another OS-level readiness notification.
        """
        if self.sock is None:
            return False
        pending = getattr(self.sock, "pending", None)
        if not callable(pending):
            return False
        try:
            return int(pending()) > 0
        except (OSError, ValueError) as exc:
            raise ConnectionError(
                f"checking pending TLS data failed: {exc}"
            ) from exc

    def wait_for_event(
        self,
        method: str,
        match: Any = None,
        session_id: str | None = None,
        timeout_s: float = 5.0,
    ) -> dict[str, Any]:
        """Wait for a CDP event by method (+ optional param subset / session).

        Scans already-buffered events first, then pumps the socket until the
        deadline. Raises TimeoutError if no matching event arrives, and
        ConnectionError if the transport drops. The matched event is consumed
        (removed from the buffer); non-matching events stay buffered for later
        waiters.
        """
        for index, event in enumerate(self.events):
            if self._event_matches(event, method, match, session_id):
                return self.events.pop(index)
        if self.sock is None:
            raise ConnectionError("CDP client is not connected")
        deadline = time.time() + max(0.0, timeout_s)
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            if self._recv_buf or self._has_decrypted_data_pending():
                ready = [self.sock]
            else:
                try:
                    ready, _, _ = select.select(
                        [self.sock], [], [], min(0.2, remaining)
                    )
                except (OSError, ValueError) as exc:
                    raise ConnectionError(
                        f"select on CDP socket failed: {exc}"
                    ) from exc
            if not ready:
                continue
            frame = self._recv_frame()
            if frame is None:
                self.disconnect_count += 1
                raise ConnectionError("websocket disconnected while waiting for event")
            data = json.loads(frame.decode("utf-8"))
            if "method" not in data:
                # Stray command response arriving during a wait; trace and skip.
                self._trace({"ts": now_iso(), "direction": "recv", "id": data.get("id"), "note": "response-during-wait"})
                continue
            self._trace({
                "ts": now_iso(),
                "direction": "event",
                "method": data.get("method"),
                "params": data.get("params", {}),
                "params_present": "params" in data,
                "wire_keys": sorted(data),
                "sessionId": data.get("sessionId"),
            })
            if self._event_matches(data, method, match, session_id):
                return data
            self.events.append(data)
        raise TimeoutError(f"event {method} not observed within {timeout_s}s")

    def pump_pending_events(self, timeout_s: float = 0.25) -> int:
        """Buffer any events already arriving after the last command response.

        A command can return just before its causally-triggered event is
        delivered.  Lifecycle cleanup uses this short bounded pump when no
        download guid has been observed yet, so a following local failure does
        not leave that active download writing to a renamed attempt directory.
        """
        if self.sock is None:
            raise ConnectionError("CDP client is not connected")
        deadline = time.time() + max(0.0, timeout_s)
        buffered = 0
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                return buffered
            if self._recv_buf or self._has_decrypted_data_pending():
                ready = [self.sock]
            else:
                try:
                    ready, _, _ = select.select(
                        [self.sock],
                        [],
                        [],
                        min(0.05, remaining),
                    )
                except (OSError, ValueError) as exc:
                    raise ConnectionError(
                        f"select on CDP socket failed: {exc}"
                    ) from exc
            if not ready:
                continue
            frame = self._recv_frame()
            if frame is None:
                self.disconnect_count += 1
                raise ConnectionError(
                    "websocket disconnected while pumping pending events"
                )
            data = json.loads(frame.decode("utf-8"))
            if data.get("method") is not None:
                self.events.append(data)
                buffered += 1
                self._trace(
                    {
                        "ts": now_iso(),
                        "direction": "event",
                        "method": data.get("method"),
                        "params": data.get("params", {}),
                        "params_present": "params" in data,
                        "wire_keys": sorted(data),
                        "sessionId": data.get("sessionId"),
                        "note": "cleanup-pump",
                    }
                )
            else:
                self._trace(
                    {
                        "ts": now_iso(),
                        "direction": "recv",
                        "id": data.get("id"),
                        "note": "response-during-cleanup-pump",
                    }
                )

    def _trace(self, payload: dict[str, Any]) -> None:
        append_jsonl(self.cdp_path, payload)

    def _send_frame(self, payload: bytes, opcode: int = 1) -> None:
        if self.sock is None:
            raise ConnectionError("socket closed")
        first = 0x80 | opcode
        mask_bit = 0x80
        length = len(payload)
        if length < 126:
            header = struct.pack("!BB", first, mask_bit | length)
        elif length < (1 << 16):
            header = struct.pack("!BBH", first, mask_bit | 126, length)
        else:
            header = struct.pack("!BBQ", first, mask_bit | 127, length)
        mask = secrets.token_bytes(4)
        masked = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
        self.sock.sendall(header + mask + masked)

    def _recv_frame(self) -> bytes | None:
        if self.sock is None:
            return None
        fragments = bytearray()
        fragmented_opcode: int | None = None
        while True:
            header = self._recv_exact(2)
            if not header:
                return None
            first, second = header
            final = bool(first & 0x80)
            opcode = first & 0x0F
            length = second & 0x7F
            masked = bool(second & 0x80)
            if length == 126:
                length = struct.unpack("!H", self._recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._recv_exact(8))[0]
            mask = self._recv_exact(4) if masked else b""
            payload = self._recv_exact(length) if length else b""
            if masked:
                payload = bytes(
                    byte ^ mask[i % 4] for i, byte in enumerate(payload)
                )

            # Control frames can be interleaved with a fragmented data message.
            if opcode in {8, 9, 10}:
                if not final or length > 125:
                    raise ConnectionError("invalid fragmented WebSocket control frame")
                if opcode == 8:
                    return None
                if opcode == 9:
                    self._send_frame(payload, opcode=10)
                continue

            if opcode in {1, 2}:
                if fragmented_opcode is not None:
                    raise ConnectionError(
                        "new WebSocket data frame before fragmented message ended"
                    )
                if final:
                    return payload
                fragmented_opcode = opcode
                fragments.extend(payload)
                continue

            if opcode == 0:
                if fragmented_opcode is None:
                    raise ConnectionError(
                        "unexpected WebSocket continuation frame"
                    )
                fragments.extend(payload)
                if final:
                    return bytes(fragments)
                continue

            # Ignore extension/reserved opcodes while preserving any active
            # fragmented message; the next valid continuation may still finish it.

    def _recv_exact(self, n: int) -> bytes:
        assert self.sock is not None
        chunks = bytearray()
        if self._recv_buf:
            buffered = min(n, len(self._recv_buf))
            chunks.extend(self._recv_buf[:buffered])
            del self._recv_buf[:buffered]
        while len(chunks) < n:
            try:
                chunk = self.sock.recv(n - len(chunks))
            except TimeoutError as exc:
                # A socket deadline is a transport stall. Keep it distinct
                # from the semantic TimeoutError raised by wait_for_event()
                # after a complete stream simply omits the required event.
                raise CDPTransportTimeout("websocket receive timed out") from exc
            if not chunk:
                raise ConnectionError("websocket closed")
            chunks.extend(chunk)
        return bytes(chunks)


FIXTURE_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".png": "image/png",
}

# The fixture stress library includes a minimal WebSocket echo peer, an SSE
# stream, and an immutable cacheable asset. Together they give the raw_cdp driver
# real endpoints to exercise the last CDP Network events that need a live peer:
# webSocketHandshakeResponseReceived / webSocketFrameSent / webSocketFrameReceived
# (echo peer), eventSourceMessageReceived (SSE), and requestServedFromCache (an
# immutable subresource re-served on reload). Verified against pinned Chrome.
_WS_ACCEPT_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

# Immutable JS subresource + a host page that loads it. Navigating to the host
# then reloading serves the subresource from the browser cache, which is what
# Network.requestServedFromCache reports.
_CACHE_ASSET_JS = b"window.__wsd_cache_marker=1;\n"
_CACHE_HOST_HTML = (
    b"<!doctype html><meta charset=utf-8><title>wsd cache host</title>"
    b"<script src=\"/__cache__/immutable.js\"></script><body>wsd cache host</body>"
)
# Slow-document body for the navigation family (/v0_4/slow). The marker value
# is computed at parse time so the answer emerges from the interaction, not
# from a literal the driver could scrape early.
_SLOW_DOC_HTML = (
    b"<!doctype html><meta charset=utf-8><title>slow document</title>"
    b"<body><h1 id=\"slow-title\"></h1>"
    b"<script>document.getElementById('slow-title').textContent = 'slow-' + (9 * 11);</script>"
    b"</body>"
)


def _ws_accept_key(client_key: str) -> str:
    digest = hashlib.sha1((client_key + _WS_ACCEPT_GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def _ws_recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            break
        buf += chunk
    return buf


def _ws_read_frame(sock: socket.socket) -> tuple[int, bytes] | None:
    header = _ws_recv_exact(sock, 2)
    if len(header) < 2:
        return None
    opcode = header[0] & 0x0F
    masked = header[1] & 0x80
    length = header[1] & 0x7F
    if length == 126:
        length = struct.unpack(">H", _ws_recv_exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack(">Q", _ws_recv_exact(sock, 8))[0]
    mask = _ws_recv_exact(sock, 4) if masked else b"\x00\x00\x00\x00"
    payload = bytearray(_ws_recv_exact(sock, length))
    for i in range(len(payload)):
        payload[i] ^= mask[i % 4]
    return opcode, bytes(payload)


def _ws_write_frame(sock: socket.socket, payload: bytes, opcode: int = 0x1) -> None:
    first = 0x80 | opcode
    length = len(payload)
    if length < 126:
        header = struct.pack("!BB", first, length)
    elif length < 65536:
        header = struct.pack("!BBH", first, 126, length)
    else:
        header = struct.pack("!BBQ", first, 127, length)
    sock.sendall(header + payload)


APP_CATALOG: list[tuple[str, int]] = [
    ("widget-a", 3),
    ("widget-b", 5),
    ("widget-c", 7),
    ("gizmo-x", 11),
    ("gizmo-y", 13),
]


def load_expected_answer_registry(
    fixtures_dir: pathlib.Path,
    base: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Merge the shared expected-answer file with per-family fragment files."""
    registry: dict[str, Any] = {}
    if base is not None:
        registry.update(base)
    else:
        expected_path = fixtures_dir / "expected_answers.json"
        if expected_path.exists():
            registry.update(load_json(expected_path))
    for frag in sorted(fixtures_dir.rglob("expected_answers.fragment.json")):
        try:
            registry.update(load_json(frag))
        except Exception as exc:
            print(f"WARNING: skipping malformed fixture fragment {frag}: {exc}", file=sys.stderr)
    return registry


class FixtureServer:
    def __init__(
        self,
        fixtures_dir: pathlib.Path | None = None,
        expected_answers: dict[str, Any] | None = None,
        traffic_tracker: resource_metrics.FixtureTrafficTracker | None = None,
        bind_host: str = LOCAL_HOST,
        bind_port: int = 0,
    ) -> None:
        # Loopback + ephemeral port by default (harness-internal use). The
        # fixture-serve subcommand passes an explicit stable port so an HTTPS
        # tunnel can front the server for remote-endpoint runs.
        self.bind_host = bind_host
        self.bind_port = bind_port
        self.events: dict[str, list[str]] = {}
        # Server-side sessions for the multi-step mini-app:
        # sid -> {"user": str, "cart": {name: qty}}.
        self.app_sessions: dict[str, dict[str, Any]] = {}
        self.server: http.server.ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self.base_url: str | None = None
        self.traffic_tracker = traffic_tracker
        self.fixtures_dir = (fixtures_dir or (BENCH_ROOT / "fixtures")).resolve()
        self.expected_answers = load_expected_answer_registry(
            self.fixtures_dir, base=expected_answers
        )
        routes_path = self.fixtures_dir / "routes.json"
        self.routes: dict[str, str] = load_json(routes_path) if routes_path.exists() else {}
        # Fragment files let independent task families register routes/answers
        # without contending on the shared top-level JSON files.
        for frag in sorted(self.fixtures_dir.rglob("routes.fragment.json")):
            try:
                self.routes.update({str(k): str(v) for k, v in load_json(frag).items()})
            except Exception as exc:
                print(f"WARNING: skipping malformed fixture fragment {frag}: {exc}", file=sys.stderr)

    def read_fixture_file(self, url_path: str) -> tuple[bytes, dict[str, str]] | None:
        if url_path in self.routes:
            rel = self.routes[url_path]
        elif url_path.startswith("/fixtures/"):
            rel = url_path[len("/fixtures/"):]
        else:
            return None
        target = (self.fixtures_dir / rel).resolve()
        if not target.is_relative_to(self.fixtures_dir) or not target.is_file():
            return None
        headers = {"Content-Type": FIXTURE_CONTENT_TYPES.get(target.suffix, "application/octet-stream")}
        sidecar = target.with_name(target.name + ".headers.json")
        if sidecar.exists():
            try:
                headers.update({str(k): str(v) for k, v in load_json(sidecar).items()})
            except Exception:
                pass
        return target.read_bytes(), headers

    def deployment_contract(self) -> dict[str, Any]:
        """Describe the exact fixture implementation and effective content.

        Public-tunnel experiments cannot treat an origin URL as a content
        identity.  This compact, read-only contract lets an external verifier
        bind the remote process to the checked-out runner implementation, the
        merged expected-answer registry, and every registered static route.
        It deliberately exposes hashes rather than grader answers.
        """

        static_routes: list[dict[str, Any]] = []
        for route, source in sorted(self.routes.items()):
            found = self.read_fixture_file(route)
            if found is None:
                raise BenchError(
                    f"registered fixture route cannot be resolved: {route} -> {source}"
                )
            body, headers = found
            static_routes.append(
                {
                    "path": route,
                    "source": source,
                    "status": 200,
                    "size": len(body),
                    "sha256": hashlib.sha256(body).hexdigest(),
                    "headers": {
                        str(name).lower(): str(value)
                        for name, value in sorted(
                            headers.items(),
                            key=lambda item: item[0].lower(),
                        )
                    },
                }
            )
        expected_payload = json.dumps(
            self.expected_answers,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        try:
            fixture_root = self.fixtures_dir.relative_to(BENCH_ROOT).as_posix()
        except ValueError:
            # The endpoint is reachable through a public tunnel in remote
            # experiments; never disclose an arbitrary host filesystem path.
            fixture_root = "<external>"
        return {
            "schema": "abb.fixture_deployment.v1",
            "implementation": {
                "path": rel_to_bench(RUNNER_SOURCE_PATH),
                "sha256": RUNNER_SOURCE_SHA256,
            },
            "fixture_root": fixture_root,
            "expected_answers": {
                "entries": len(self.expected_answers),
                "sha256": hashlib.sha256(expected_payload).hexdigest(),
            },
            "static_routes": static_routes,
        }

    def grade_expected(self, payload: dict[str, Any]) -> dict[str, Any]:
        task_id = str(payload.get("task_id", ""))
        spec = self.expected_answers.get(task_id)
        answer = str(payload.get("answer", ""))
        if not isinstance(spec, dict):
            return {
                "ok": False,
                "checks": [{"name": "expected_answer_registered", "status": "fail", "evidence": f"no expected answer for task `{task_id}`"}],
                "failure": failure_obj("infra", f"expected_answers.json has no entry for `{task_id}`"),
            }
        mode = spec.get("mode", "equals")
        expected = spec.get("expected")
        checks: list[dict[str, Any]] = []
        if mode == "equals":
            ok = answer == str(expected)
            checks.append({"name": "answer_equals_expected", "status": "pass" if ok else "fail", "evidence": f"expected {expected!r}, got {answer!r}"})
        elif mode == "contains_all":
            wanted = [str(item) for item in (expected if isinstance(expected, list) else [expected])]
            missing = [item for item in wanted if item not in answer]
            ok = not missing
            checks.append({"name": "answer_contains_all", "status": "pass" if ok else "fail", "evidence": f"missing={missing!r} answer={answer[:400]!r}"})
        elif mode == "contains":
            ok = str(expected) in answer
            checks.append({"name": "answer_contains", "status": "pass" if ok else "fail", "evidence": f"expected substring {expected!r}, answer={answer[:400]!r}"})
        else:
            ok = False
            checks.append({"name": "expected_mode", "status": "fail", "evidence": f"unknown mode `{mode}`"})
        return {
            "ok": ok,
            "checks": checks,
            "failure": None if ok else failure_obj("cdp_semantic", "answer did not match server-side expectation"),
        }

    def start(self) -> str:
        fixture = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args: Any) -> None:
                return

            def _traffic_begin(self, path: str, request_body: bytes = b"") -> None:
                self._traffic_status = 0
                self._traffic_response_version = self.protocol_version
                self._traffic_response_reason = ""
                self._traffic_response_headers: list[tuple[str, str]] = []
                self._traffic_finished = False
                self._traffic_request = (
                    fixture.traffic_tracker.begin_request(
                        path=path,
                        command=self.command,
                        request_version=self.request_version,
                        headers={str(key): str(value) for key, value in self.headers.items()},
                        request_body=request_body,
                    )
                    if fixture.traffic_tracker is not None
                    else None
                )

            def send_response(self, code: int, message: str | None = None) -> None:
                self._traffic_status = int(code)
                self._traffic_response_reason = str(
                    message
                    if message is not None
                    else self.responses.get(code, ("",))[0]
                )
                super().send_response(code, message)

            def send_header(self, keyword: str, value: str) -> None:
                if hasattr(self, "_traffic_response_headers"):
                    self._traffic_response_headers.append((str(keyword), str(value)))
                super().send_header(keyword, value)

            def _traffic_finish(
                self,
                response_body_bytes: int,
                request_stream_body_bytes: int = 0,
            ) -> None:
                if getattr(self, "_traffic_finished", False):
                    return
                self._traffic_finished = True
                if fixture.traffic_tracker is not None:
                    fixture.traffic_tracker.finish_request(
                        getattr(self, "_traffic_request", None),
                        status=int(getattr(self, "_traffic_status", 0)),
                        response_headers=list(
                            getattr(self, "_traffic_response_headers", [])
                        ),
                        response_body_bytes=max(0, int(response_body_bytes)),
                        request_stream_body_bytes=max(0, int(request_stream_body_bytes)),
                        response_version=str(
                            getattr(self, "_traffic_response_version", "HTTP/1.0")
                        ),
                        response_reason=str(
                            getattr(self, "_traffic_response_reason", "")
                        ),
                    )

            def _finish_response(self, body: bytes = b"") -> None:
                try:
                    self.end_headers()
                    if body:
                        self.wfile.write(body)
                except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                    return
                finally:
                    self._traffic_finish(len(body))

            def do_GET(self) -> None:
                parsed = urllib.parse.urlparse(self.path)
                self._traffic_begin(self.path)
                if parsed.path == "/__fixture__/deployment-contract":
                    body = json.dumps(
                        fixture.deployment_contract(),
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(body)))
                    self._finish_response(body)
                    return
                if parsed.path == "/favicon.ico":
                    self.send_response(204)
                    self.send_header("Content-Length", "0")
                    self._finish_response()
                    return
                if parsed.path == "/storage/indexeddb_inventory":
                    qs = urllib.parse.parse_qs(parsed.query)
                    seed = qs.get("seed", [""])[0]
                    session = qs.get("session", [""])[0]
                    body = fixture.inventory_html(seed, session).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self._finish_response(body)
                    return
                if parsed.path.startswith("/__auth__/"):
                    # HTTP Basic challenge endpoint so Fetch.authRequired /
                    # continueWithAuth can be exercised end-to-end. bench:secret
                    # is an intentionally fixed test credential; the server only
                    # ever binds 127.0.0.1 (see start()).
                    auth = self.headers.get("Authorization", "")
                    if auth == "Basic " + base64.b64encode(b"bench:secret").decode("ascii"):
                        body = b'{"auth":"granted"}'
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                    else:
                        body = b"unauthorized"
                        self.send_response(401)
                        self.send_header("WWW-Authenticate", 'Basic realm="bench"')
                        self.send_header("Content-Type", "text/plain")
                    self.send_header("Content-Length", str(len(body)))
                    self._finish_response(body)
                    return
                if parsed.path == "/__ws__/echo" and (
                    self.headers.get("Upgrade", "").lower() == "websocket"
                ):
                    self._ws_echo()
                    return
                if parsed.path == "/__sse__/messages":
                    self._sse_messages()
                    return
                if parsed.path == "/__cache__/immutable.js":
                    body = _CACHE_ASSET_JS
                    self.send_response(200)
                    self.send_header("Content-Type", "application/javascript; charset=utf-8")
                    # Far-future immutable caching so a reload serves it from the
                    # browser cache (Network.requestServedFromCache).
                    self.send_header("Cache-Control", "public, max-age=31536000, immutable")
                    self.send_header("ETag", '"wsd-immutable-v1"')
                    self.send_header("Content-Length", str(len(body)))
                    self._finish_response(body)
                    return
                if parsed.path == "/__cache__/host":
                    body = _CACHE_HOST_HTML
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(body)))
                    self._finish_response(body)
                    return
                if parsed.path == "/v0_4/redirect/hop":
                    # 302 chain for the navigation family: n>0 hops to n-1;
                    # n<=0 lands on the static landing page.
                    qs = urllib.parse.parse_qs(parsed.query)
                    try:
                        hops = int(qs.get("n", ["0"])[0])
                    except ValueError:
                        hops = 0
                    target = f"/v0_4/redirect/hop?n={hops - 1}" if hops > 0 else "/v0_4/redirect/landing"
                    self.send_response(302)
                    self.send_header("Location", target)
                    self.send_header("Content-Length", "0")
                    self._finish_response()
                    return
                if parsed.path == "/v0_4/echo_headers":
                    echoed = {
                        name.lower(): value
                        for name, value in self.headers.items()
                        if name.lower().startswith("x-abb-") or name.lower() == "authorization"
                    }
                    self._json(echoed)
                    return
                if parsed.path == "/v0_4/net/secure":
                    if self.headers.get("Authorization", "") == "Bearer tok-53":
                        body = b"ok-61"
                        self.send_response(200)
                        self.send_header("Content-Type", "text/plain")
                        self.send_header("Content-Length", str(len(body)))
                        self._finish_response(body)
                        return
                    body = b"denied"
                    self.send_response(401)
                    self.send_header("Content-Type", "text/plain")
                    self.send_header("WWW-Authenticate", "Bearer")
                    self.send_header("Content-Length", str(len(body)))
                    self._finish_response(body)
                    return
                if parsed.path == "/v0_4/app/api/items":
                    if self._app_sid() is None:
                        self._json({"error": "locked"}, status=401)
                        return
                    self._json({"items": [{"name": name, "price": price} for name, price in APP_CATALOG]})
                    return
                if parsed.path == "/v0_4/app/api/cart":
                    sid = self._app_sid()
                    if sid is None:
                        self._json({"error": "locked"}, status=401)
                        return
                    self._json(self._app_cart_view(sid))
                    return
                if parsed.path == "/v0_4/slow":
                    # Slow-document endpoint for the navigation family: hold the
                    # response before serving. The server is threading, so a
                    # sleeping handler does not block graders.
                    qs = urllib.parse.parse_qs(parsed.query)
                    try:
                        delay_ms = min(max(int(qs.get("ms", ["800"])[0]), 0), 5000)
                    except ValueError:
                        delay_ms = 800
                    time.sleep(delay_ms / 1000.0)
                    body = _SLOW_DOC_HTML
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(body)))
                    self._finish_response(body)
                    return
                found = fixture.read_fixture_file(parsed.path)
                if found is not None:
                    body, headers = found
                    self.send_response(200)
                    for key, value in headers.items():
                        self.send_header(key, value)
                    self.send_header("Content-Length", str(len(body)))
                    self._finish_response(body)
                    return
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self._finish_response()

            def _ws_echo(self) -> None:
                # Minimal RFC6455 echo peer: complete the handshake, then echo
                # each text/binary frame with an "echo:" prefix. Bounded by a
                # short socket timeout and a small frame budget so a request
                # thread can never hang the daemon server.
                client_key = self.headers.get("Sec-WebSocket-Key", "")
                if not client_key:
                    self.send_response(400)
                    self.send_header("Content-Length", "0")
                    self._finish_response()
                    return
                self.send_response(101)
                self.send_header("Upgrade", "websocket")
                self.send_header("Connection", "Upgrade")
                self.send_header("Sec-WebSocket-Accept", _ws_accept_key(client_key))
                try:
                    self.end_headers()
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    self._traffic_finish(0)
                    return
                sock = self.connection
                stream_rx = 0
                stream_tx = 0
                try:
                    sock.settimeout(5.0)
                    for _ in range(8):
                        frame = _ws_read_frame(sock)
                        if frame is None:
                            break
                        opcode, payload = frame
                        stream_rx += len(payload)
                        if opcode == 0x8:  # close
                            _ws_write_frame(sock, b"", opcode=0x8)
                            break
                        if opcode in (0x1, 0x2):
                            echoed = b"echo:" + payload
                            _ws_write_frame(sock, echoed, opcode=opcode)
                            stream_tx += len(echoed)
                except (OSError, struct.error):
                    return
                finally:
                    self._traffic_finish(
                        stream_tx,
                        request_stream_body_bytes=stream_rx,
                    )

            def _sse_messages(self) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                sent = 0
                try:
                    self.end_headers()
                    for i in range(3):
                        chunk = f"data: wsd-sse-{i}\n\n".encode("ascii")
                        self.wfile.write(chunk)
                        sent += len(chunk)
                        self.wfile.flush()
                        time.sleep(0.1)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    return
                finally:
                    self._traffic_finish(sent)

            def do_POST(self) -> None:
                parsed = urllib.parse.urlparse(self.path)
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length) if length else b"{}"
                self._traffic_begin(self.path, raw if length else b"")
                try:
                    payload = json.loads(raw.decode("utf-8"))
                except Exception:
                    payload = {}
                if parsed.path == "/__event__/storage_indexeddb_inventory_001":
                    session = str(payload.get("session", ""))
                    event = str(payload.get("event", ""))
                    fixture.events.setdefault(session, []).append(event)
                    self._json({"ok": True})
                    return
                if parsed.path == "/__grade__/storage_indexeddb_inventory_001":
                    self._json(fixture.grade_inventory(payload))
                    return
                if parsed.path == "/__grade__/expected_answer":
                    self._json(fixture.grade_expected(payload))
                    return
                if parsed.path == "/__resource__/echo":
                    # Synthetic calibration endpoint: request bytes are
                    # arbitrary and response bytes have a caller-declared,
                    # bounded size. It is never part of functional scoring.
                    qs = urllib.parse.parse_qs(parsed.query)
                    try:
                        response_size = min(
                            max(int(qs.get("response_bytes", ["0"])[0]), 0),
                            8 * 1024 * 1024,
                        )
                    except ValueError:
                        response_size = 0
                    body_bytes = b"R" * response_size
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Length", str(len(body_bytes)))
                    self._finish_response(body_bytes)
                    return
                if parsed.path == "/v0_4/upload":
                    # Minimal multipart parse: take the part carrying a
                    # filename, respond with an HTML receipt whose text is
                    # the server-computed name:size:sha256[:12].
                    ctype = self.headers.get("Content-Type", "")
                    boundary = None
                    for piece in ctype.split(";"):
                        piece = piece.strip()
                        if piece.startswith("boundary="):
                            boundary = piece[len("boundary="):].strip('"')
                    receipt = "upload-error:no-multipart"
                    if boundary:
                        for part in raw.split(b"--" + boundary.encode()):
                            header_blob, _, body = part.partition(b"\r\n\r\n")
                            if b"filename=" not in header_blob:
                                continue
                            filename = "unknown"
                            for line in header_blob.split(b"\r\n"):
                                if b"filename=" in line:
                                    filename = line.split(b"filename=")[1].strip().strip(b'"').decode("utf-8", "replace")
                            if body.endswith(b"\r\n"):
                                body = body[:-2]
                            digest = hashlib.sha256(body).hexdigest()[:12]
                            receipt = f"uploaded:{filename}:{len(body)}:{digest}"
                            break
                    page = f'<!doctype html><meta charset="utf-8"><div id="up-result">{receipt}</div>'
                    body_bytes = page.encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                    self.send_header("Content-Length", str(len(body_bytes)))
                    self._finish_response(body_bytes)
                    return
                if parsed.path == "/v0_4/app/api/login":
                    user = str(payload.get("user", "")) or "anon"
                    sid = secrets.token_hex(8)
                    fixture.app_sessions[sid] = {"user": user, "cart": {}}
                    self._json({"user": user}, extra_headers={"Set-Cookie": f"abb_app_sid={sid}; Path=/"})
                    return
                if parsed.path == "/v0_4/app/api/logout":
                    sid = self._app_sid()
                    user = fixture.app_sessions.pop(sid, {}).get("user", "none") if sid else "none"
                    self._json(
                        {"ok": f"bye-{user}"},
                        extra_headers={"Set-Cookie": "abb_app_sid=; Path=/; Max-Age=0"},
                    )
                    return
                if parsed.path == "/v0_4/app/api/cart":
                    sid = self._app_sid()
                    if sid is None:
                        self._json({"error": "locked"}, status=401)
                        return
                    item = str(payload.get("item", ""))
                    action = str(payload.get("action", "add"))
                    cart = fixture.app_sessions[sid]["cart"]
                    if item not in dict(APP_CATALOG):
                        self._json({"error": "unknown item"}, status=400)
                        return
                    if action == "add":
                        cart[item] = cart.get(item, 0) + 1
                    elif action == "remove":
                        cart.pop(item, None)
                    self._json({"ok": True})
                    return
                if parsed.path == "/v0_4/app/api/checkout":
                    sid = self._app_sid()
                    if sid is None:
                        self._json({"error": "locked"}, status=401)
                        return
                    view = self._app_cart_view(sid)
                    order = f"ord-{view['total']}-{len(view['items'])}"
                    fixture.app_sessions[sid]["cart"] = {}
                    self._json({"order": order})
                    return
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self._finish_response()

            def _json(self, payload: dict[str, Any], status: int = 200, extra_headers: dict[str, str] | None = None) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                for name, value in (extra_headers or {}).items():
                    self.send_header(name, value)
                self._finish_response(body)

            def _app_sid(self) -> str | None:
                cookie = self.headers.get("Cookie", "") or ""
                for part in cookie.split(";"):
                    name, _, value = part.strip().partition("=")
                    if name == "abb_app_sid" and value in fixture.app_sessions:
                        return value
                return None

            def _app_cart_view(self, sid: str) -> dict[str, Any]:
                cart = fixture.app_sessions[sid]["cart"]
                prices = dict(APP_CATALOG)
                return {
                    "badge": sum(cart.values()),
                    "total": sum(qty * prices[name] for name, qty in cart.items()),
                    "items": [{"name": name, "qty": qty} for name, qty in sorted(cart.items())],
                }

        self.server = http.server.ThreadingHTTPServer((self.bind_host, self.bind_port), Handler)
        port = int(self.server.server_address[1])
        self.base_url = f"http://{self.bind_host}:{port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self.base_url

    def stop(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=2)

    @staticmethod
    def expected_count(seed: str) -> int:
        digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
        return 10 + (int(digest[:2], 16) % 90)

    def inventory_html(self, seed: str, session: str) -> str:
        count = self.expected_count(seed)
        sku = "SKU-" + hashlib.sha256((seed + session).encode("utf-8")).hexdigest()[:8].upper()
        config = json.dumps({"seed": seed, "session": session, "sku": sku, "count": count})
        return f"""<!doctype html>
<html>
<head><meta charset="utf-8"><title>IndexedDB inventory fixture</title></head>
<body>
<h1>Inventory fixture</h1>
<script>
window.__ABB_TASK_CONFIG__ = {config};
async function __abbRecord(eventName) {{
  try {{
    await fetch("/__event__/storage_indexeddb_inventory_001", {{
      method: "POST",
      headers: {{"Content-Type": "application/json"}},
      body: JSON.stringify({{session: window.__ABB_TASK_CONFIG__.session, event: eventName}})
    }});
  }} catch (err) {{}}
}}
function __abbOpenDb(name) {{
  return new Promise((resolve, reject) => {{
    const req = indexedDB.open(name, 1);
    req.onupgradeneeded = () => req.result.createObjectStore("inventory", {{keyPath: "sku"}});
    req.onerror = () => reject(req.error || new Error("indexedDB.open failed"));
    req.onsuccess = () => resolve(req.result);
  }});
}}
function __abbTx(db, mode, fn) {{
  return new Promise((resolve, reject) => {{
    const tx = db.transaction("inventory", mode);
    const store = tx.objectStore("inventory");
    const result = fn(store);
    tx.oncomplete = () => resolve(result);
    tx.onerror = () => reject(tx.error || new Error("transaction failed"));
  }});
}}
window.__ABB_RUN_IDB_TASK__ = async function() {{
  const cfg = window.__ABB_TASK_CONFIG__;
  const db = await __abbOpenDb("abb_inventory_" + cfg.session);
  await __abbTx(db, "readwrite", (store) => store.put({{sku: cfg.sku, count: cfg.count}}));
  await __abbRecord("idb_write");
  const row = await new Promise((resolve, reject) => {{
    const tx = db.transaction("inventory", "readonly");
    const req = tx.objectStore("inventory").get(cfg.sku);
    req.onerror = () => reject(req.error || new Error("get failed"));
    req.onsuccess = () => resolve(req.result);
  }});
  await __abbRecord("idb_read");
  return {{
    answer: String(row && row.count),
    observations: {{
      session: cfg.session,
      indexeddb_write_observed: true,
      indexeddb_read_observed: Boolean(row && row.count === cfg.count)
    }}
  }};
}};
</script>
</body>
</html>
"""

    def grade_inventory(self, payload: dict[str, Any]) -> dict[str, Any]:
        seed = str(payload.get("seed", ""))
        observations = payload.get("observations") or {}
        session = str(payload.get("session") or observations.get("session") or "")
        expected = str(self.expected_count(seed))
        answer = str(payload.get("answer", ""))
        events = self.events.get(session, [])
        claimed_write = bool(observations.get("indexeddb_write_observed"))
        claimed_read = bool(observations.get("indexeddb_read_observed"))
        checks = [
            {
                "name": "answer_matches_seed",
                "status": "pass" if answer == expected else "fail",
                "evidence": f"expected {expected}, got {answer}",
            },
            {
                "name": "indexeddb_write_observed",
                "status": "pass" if "idb_write" in events else "fail",
                "evidence": f"server_events={events}; client_claim={claimed_write}",
            },
            {
                "name": "indexeddb_read_observed",
                "status": "pass" if "idb_read" in events else "fail",
                "evidence": f"server_events={events}; client_claim={claimed_read}",
            },
        ]
        ok = all(check["status"] == "pass" for check in checks)
        return {
            "ok": ok,
            "checks": checks,
            "failure": None
            if ok
            else {
                "class": "cdp_semantic",
                "detail": "IndexedDB answer or server-side trace did not match server-side seed",
                "kernel_workitem": False,
            },
        }


def seed_for_attempt(base_seed: str | None, task: ResolvedTask, attempt: int) -> str:
    source = base_seed or secrets.token_hex(8)
    return hashlib.sha256(f"{source}:{task.task_id}:{attempt}".encode("utf-8")).hexdigest()[:12]


def artifact_paths(run_dir: pathlib.Path, task: ResolvedTask, engine: str, attempt: int) -> tuple[pathlib.Path, pathlib.Path, str]:
    rel = pathlib.Path("artifacts") / task.layer / task.subset_id / task.task_id / engine / str(attempt)
    final = run_dir / rel
    tmp = final.parent / f".{attempt}.tmp-{os.getpid()}-{time.time_ns()}"
    return tmp, final, rel.as_posix()


def ensure_profile_files(artifact_dir: pathlib.Path, profile: str) -> None:
    for name in ARTIFACT_PROFILES[profile]:
        path = artifact_dir / name
        if not path.exists():
            path.write_text("", encoding="utf-8")


def _peek_core_dumped(proc: Any) -> bool | None:
    """Inspect an exited child without reaping it, when the platform allows.

    ``subprocess.Popen.returncode`` preserves an exit code or terminating
    signal but discards the wait-status core-dump bit.  ``waitid(WNOWAIT)``
    lets us retain that evidence before ``poll()`` reaps the engine.  Unknown
    is represented as ``None`` rather than guessed.
    """
    required = ("waitid", "P_PID", "WEXITED", "WNOHANG", "WNOWAIT")
    if not all(hasattr(os, name) for name in required):
        return None
    pid = getattr(proc, "pid", None)
    if not isinstance(pid, int) or pid <= 0:
        return None
    try:
        info = os.waitid(
            os.P_PID,
            pid,
            os.WEXITED | os.WNOHANG | os.WNOWAIT,
        )
    except (ChildProcessError, OSError):
        return None
    if info is None:
        return None
    code = getattr(info, "si_code", None)
    if code == getattr(os, "CLD_DUMPED", object()):
        return True
    if code in {
        getattr(os, "CLD_EXITED", object()),
        getattr(os, "CLD_KILLED", object()),
    }:
        return False
    return None


def process_diagnostic(
    kind: str,
    proc: Any | None = None,
    *,
    returncode: int | None = None,
    state: str | None = None,
) -> dict[str, Any]:
    """Describe process liveness/termination without conflating its owner."""
    core_dumped = _peek_core_dumped(proc) if proc is not None and returncode is None else None
    if returncode is None and proc is not None:
        try:
            returncode = proc.poll()
        except Exception:
            returncode = None
    if state is None:
        state = "running" if returncode is None else "exited"
    signal_number = -returncode if isinstance(returncode, int) and returncode < 0 else None
    signal_name = None
    if signal_number is not None:
        try:
            signal_name = signal.Signals(signal_number).name
        except (ValueError, TypeError):
            pass
    return {
        "kind": kind,
        "state": state,
        "returncode": returncode,
        "signal": signal_number,
        "signal_name": signal_name,
        "core_dumped": core_dumped,
    }


def is_socket_transport_os_error(exc: OSError) -> bool:
    """Whether an otherwise-generic OSError proves a socket transport fault."""
    # urllib wraps fixture/grader HTTP failures in URLError.  Even when its
    # ``reason`` is a socket error, this outer attempt layer cannot prove the
    # failed transport was the browser/client CDP channel.
    if isinstance(exc, urllib.error.URLError):
        return False
    return getattr(exc, "errno", None) in SOCKET_TRANSPORT_ERRNOS


def failure_obj(
    klass: str,
    detail: str,
    kernel_workitem: bool = False,
    *,
    origin: str | None = None,
    process: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if klass not in FAILURE_CLASSES:
        klass = "infra"
    failure = {
        "class": klass,
        "detail": detail,
        "kernel_workitem": kernel_workitem,
    }
    if origin is not None:
        failure["origin"] = origin
    if process is not None:
        failure["process"] = process
    return failure


def promote_observed_engine_exit(
    result: dict[str, Any],
    grader: dict[str, Any],
    browser_process: Any,
    observed_engine_process: dict[str, Any] | None = None,
) -> None:
    """Make an observed engine exit primary while preserving prior evidence."""
    status = str(result.get("status") or "infra")
    failure = result.get("failure")
    if isinstance(failure, dict) and failure.get("origin") == "engine_process":
        return
    # A success-shaped driver payload does not make an engine exit harmless:
    # the reusable browser is part of the attempt contract and must still be
    # alive after the driver returns. Promote that exit to crash as well.
    # A terminal diagnostic may carry one-shot waitid/core evidence, so reuse
    # it.  A previously-running observation must be refreshed after finally/
    # cleanup to catch an engine that exited in that gap.
    engine_process = observed_engine_process
    if not isinstance(engine_process, dict) or engine_process.get("state") != "exited":
        engine_process = process_diagnostic("engine", browser_process)
    if engine_process.get("state") != "exited":
        return

    secondary_failure = dict(failure) if isinstance(failure, dict) else None
    secondary_detail = (
        str(secondary_failure.get("detail") or "")
        if secondary_failure is not None
        else ""
    )
    detail = f"engine process exited while attempt reported {status}"
    if secondary_detail:
        detail += f": {secondary_detail}"
    primary = failure_obj(
        "infra",
        detail,
        origin="engine_process",
        process=engine_process,
    )
    primary["secondary_status"] = status
    if secondary_failure is not None:
        primary["secondary_failure"] = secondary_failure
        if isinstance(secondary_failure.get("process"), dict):
            primary["secondary_process"] = secondary_failure["process"]

    result["status"] = "crash"
    result["failure"] = primary
    grader["ok"] = False
    grader["failure"] = primary


def attempt_base_result(
    run_id: str,
    task: ResolvedTask,
    engine: str,
    attempt: int,
    seed: str,
    gate_payload: dict[str, Any],
    artifact_rel: str,
) -> dict[str, Any]:
    payload = {
        "run_id": run_id,
        "layer": task.layer,
        "subset_id": task.subset_id,
        "task_id": task.task_id,
        "task_version": task.task_version,
        "launch_profile": task.launch_profile,
        "attempt": attempt,
        "engine": engine,
        "seed": seed,
        "driver": task.driver.get("kind"),
        "chrome_gate": gate_payload,
        "status": "infra",
        "score_included": False,
        "failure": failure_obj("infra", "attempt did not complete"),
        "answer": None,
        "duration_ms": 0,
        "cdp_call_count": 0,
        "cdp_error_count": 0,
        "ws_disconnect_count": 0,
        "fallback_used": False,
        "artifact_dir": artifact_rel,
        # Filled in by run_driver_attempt once an engine process is in hand.
        # Rows for attempts that never reached a process (a skipped candidate
        # behind a failed Chrome gate) keep the nulls, so every row carries the
        # key and consumers never have to test for its absence.
        "run_context": {
            "worker_slot": None,
            "browser_pid": None,
            "browser_generation": None,
            "prev_task_id": None,
            "started_monotonic_ms": None,
        },
    }
    if task.layer == "L1":
        payload["evaluation_axis"] = "protocol_driver_compatibility"
    elif task.semantic_capability:
        payload["evaluation_axis"] = "web_platform_workflow_semantic_correctness"
        payload["semantic_capability"] = task.semantic_capability
    return payload


def engine_provenance(browser: BrowserProcess) -> dict[str, Any]:
    """Return the runner-owned evidence that identifies one live engine.

    CDP-compatible products may intentionally report a synthetic Chrome
    product string.  PID, immutable binary digest, launch command, HTTP
    discovery endpoint and browser websocket together make a fallback to a
    different local browser visible in every attempt row.
    """
    binary = browser.binary
    return {
        "engine": browser.engine,
        "pid": getattr(browser.process, "pid", None),
        "binary": rel_to_repo(binary) if binary is not None else None,
        "binary_sha256": browser.binary_sha256,
        # Repo-relative / placeholder form so result rows carry no absolute
        # host paths and no per-launch ephemeral directory names.
        "launch_command": [sanitize_launch_part(part) for part in browser.launch_command],
        "serve_args": list(browser.serve_args),
        "http_endpoint": browser.base_url,
        "browser_ws": browser.version_info.get("webSocketDebuggerUrl"),
        "http_identity": dict(browser.version_info),
    }


def is_unsupported_error(exc: Exception) -> bool:
    text = str(exc).lower()
    # Chrome reports unknown methods as "'X' wasn't found"; other engines use
    # "method not found" / "unknown method" / explicit "unsupported".
    return (
        "method not found" in text
        or "not found" in text
        or "wasn't found" in text
        or "unsupported" in text
        or "unknown method" in text
    )


def event_params_match(params: Any, match: Any) -> bool:
    """Recursive subset match: every key/value in `match` must be present and
    equal in `params`. Dicts recurse; lists compare index-wise; scalars use
    equality. `match=None` matches anything.
    """
    if match is None:
        return True
    if isinstance(match, dict):
        if set(match) == {EVENT_MATCH_ONE_OF}:
            choices = match[EVENT_MATCH_ONE_OF]
            return isinstance(choices, list) and any(event_params_match(params, choice) for choice in choices)
        if not isinstance(params, dict):
            return False
        return all(key in params and event_params_match(params[key], value) for key, value in match.items())
    if isinstance(match, list):
        if not isinstance(params, list) or len(params) != len(match):
            return False
        return all(event_params_match(p, m) for p, m in zip(params, match))
    return params == match


def _saved_path_value(saved: dict[str, Any], name: str, path: str | None) -> Any:
    return lookup_saved_path(saved, f"{name}.{path}" if path else name)


def grade_inline_check(check: dict[str, Any], saved: dict[str, Any]) -> tuple[bool, str]:
    kind = check.get("kind")
    name = check.get("name", "value")
    value = saved.get(name)
    if kind == "event_observed":
        observed = saved.get("__events_observed__") or []
        method = check.get("method")
        # Either an event method was observed, or a named save_result_as landed.
        ok = (method in observed) if method else (saved.get(name) is not None)
        return ok, f"observed_events={observed} method={method!r} name={name!r}"
    if kind == "unsupported_observed":
        unsupported = saved.get("__unsupported__") or {}
        method = check.get("method")
        ok = method in unsupported
        return ok, f"unsupported={ {k: v.get('class') for k, v in unsupported.items()} } method={method!r}"
    if kind == "saved_path_equals":
        resolved = _saved_path_value(saved, name, check.get("path"))
        return resolved == check.get("expected"), f"{name}.{check.get('path')}={resolved!r} expected={check.get('expected')!r}"
    if kind == "saved_path_truthy":
        resolved = _saved_path_value(saved, name, check.get("path"))
        return bool(resolved), f"{name}.{check.get('path')}={resolved!r}"
    if kind == "saved_path_contains":
        resolved = _saved_path_value(saved, name, check.get("path"))
        return isinstance(resolved, str) and str(check.get("expected")) in resolved, f"{name}.{check.get('path')}={resolved!r} must contain {check.get('expected')!r}"
    if kind == "saved_path_one_of":
        resolved = _saved_path_value(saved, name, check.get("path"))
        expected = check.get("expected")
        ok = isinstance(expected, list) and resolved in expected
        return ok, f"{name}.{check.get('path')}={resolved!r} expected one of {expected!r}"
    if kind == "array_length":
        resolved = _saved_path_value(saved, name, check.get("path"))
        length = len(resolved) if isinstance(resolved, (list, str)) else None
        if length is None:
            return False, f"{name}.{check.get('path')} is not an array/string: {type(resolved).__name__}"
        if "expected" in check:
            return length == check["expected"], f"len={length} expected={check['expected']}"
        lo = check.get("min")
        hi = check.get("max")
        ok = (lo is None or length >= lo) and (hi is None or length <= hi)
        return ok, f"len={length} min={lo} max={hi}"
    if kind == "array_contains":
        resolved = _saved_path_value(saved, name, check.get("path"))
        if not isinstance(resolved, list):
            return False, f"{name}.{check.get('path')} is not an array: {type(resolved).__name__}"
        expected = check.get("expected")
        if isinstance(expected, dict):
            ok = any(event_params_match(item, expected) for item in resolved)
        else:
            ok = expected in resolved
        return ok, f"array(len={len(resolved)}) contains {expected!r} -> {ok}"
    if kind == "value_equals":
        return value == check.get("expected"), f"{name}={value!r} expected={check.get('expected')!r}"
    if kind == "value_type":
        return saved.get(f"{name}__type") == check.get("expected"), f"{name}__type={saved.get(f'{name}__type')!r} expected={check.get('expected')!r}"
    if kind == "value_truthy":
        return bool(value), f"{name}={value!r}"
    if kind == "value_contains":
        return isinstance(value, str) and str(check.get("expected")) in value, f"{name}={value!r} must contain {check.get('expected')!r}"
    if kind == "eval_no_exception":
        count = int(saved.get("__eval_exception_count__") or 0)
        return count == 0, f"eval exceptions={count}"
    if kind == "eval_has_exception":
        count = int(saved.get("__eval_exception_count__") or 0)
        return count > 0, f"eval exceptions={count}"
    if kind == "array_match_contains":
        # Locate exactly one entry by identity fields, then assert a nested
        # array on that entry. This avoids pinning array indices against one
        # engine's serialization order.
        resolved = _saved_path_value(saved, name, check.get("path"))
        if not isinstance(resolved, list):
            return False, f"{name}.{check.get('path')} is not an array: {type(resolved).__name__}"
        match = check.get("match")
        matched = [item for item in resolved if event_params_match(item, match)]
        if len(matched) != 1:
            return False, f"identity {match!r} matched {len(matched)} entries (need exactly 1)"
        inner: Any = matched[0]
        contains_path = check.get("contains_path")
        if contains_path:
            for part in str(contains_path).split("."):
                inner = inner.get(part) if isinstance(inner, dict) else None
        if not isinstance(inner, list):
            return False, f"matched entry `{contains_path}` is not an array: {type(inner).__name__}"
        expected = check.get("expected")
        if isinstance(expected, dict):
            ok = any(event_params_match(item, expected) for item in inner)
        else:
            ok = expected in inner
        return ok, (
            f"matched 1 entry by {match!r}; {contains_path or '<entry>'}"
            f"(len={len(inner)}) contains {expected!r} -> {ok}"
        )
    if kind == "saved_body_text_equals":
        # Compare a CDP body envelope ({body, base64Encoded}) by decoded
        # content; either wire representation is a legal answer.
        resolved = _saved_path_value(saved, name, check.get("path")) if check.get("path") else value
        if not isinstance(resolved, dict):
            return False, f"{name} is not a body envelope: {type(resolved).__name__}"
        body = resolved.get("body")
        if not isinstance(body, str):
            return False, f"{name}.body is not a string: {type(body).__name__}"
        if resolved.get("base64Encoded"):
            try:
                text = base64.b64decode(body, validate=True).decode("utf-8")
            except Exception as exc:
                return False, f"base64Encoded body failed to decode: {exc}"
        else:
            text = body
        expected_text = check.get("expected")
        if not isinstance(expected_text, str):
            return False, (
                "saved_body_text_equals: `expected` must be a string, got "
                f"{type(expected_text).__name__}"
            )
        return text == expected_text, (
            f"decoded body={text[:200]!r} expected={expected_text!r} "
            f"(base64Encoded={bool(resolved.get('base64Encoded'))})"
        )
    if kind == "no_error":
        # Reaching the grader means every non-optional step completed without a
        # CDP error; optional-step failures are recorded but do not fail this check.
        return True, "all required steps completed"
    return False, f"unknown inline assertion kind `{kind}`"


def grade_inline(task: ResolvedTask, saved: dict[str, Any]) -> dict[str, Any]:
    # Legacy string forms kept for pre-migration tasks.
    legacy = {
        "value_equals_3": {"kind": "value_equals", "name": "value", "expected": 3},
        "result_type_number": {"kind": "value_type", "name": "value", "expected": "number"},
    }
    checks: list[dict[str, Any]] = []
    for check in task.grader.get("checks", []):
        if isinstance(check, str):
            spec = legacy.get(check)
            if spec is None:
                checks.append({"name": check, "status": "fail", "evidence": "unknown inline assertion"})
                continue
            label = check
        else:
            spec = check
            label = check.get("label") or check.get("kind", "check")
        ok, evidence = grade_inline_check(spec, saved)
        checks.append({"name": label, "status": "pass" if ok else "fail", "evidence": evidence})
    if not checks:
        # A gradeless task must not silently pass.
        checks.append({"name": "checks_declared", "status": "fail", "evidence": "task declares no inline checks"})
    ok = all(check["status"] == "pass" for check in checks)
    return {
        "ok": ok,
        "checks": checks,
        "failure": None
        if ok
        else failure_obj("cdp_semantic", "inline assertions failed"),
    }


def substitute_params(value: Any, replacements: dict[str, str]) -> Any:
    if isinstance(value, str):
        for key, replacement in replacements.items():
            value = value.replace(key, replacement)
        return value
    if isinstance(value, dict):
        return {k: substitute_params(v, replacements) for k, v in value.items()}
    if isinstance(value, list):
        return [substitute_params(item, replacements) for item in value]
    return value


_SAVED_PLACEHOLDER = re.compile(r"^\{saved:([^}]+)\}$")


def lookup_saved_path(saved: dict[str, Any], path: str) -> Any:
    head, _, rest = path.partition(".")
    current: Any = saved.get(f"{head}__raw", saved.get(head))
    for segment in rest.split(".") if rest else []:
        if isinstance(current, dict):
            current = current.get(segment)
        elif isinstance(current, list) and segment.isdigit():
            current = current[int(segment)] if int(segment) < len(current) else None
        else:
            return None
    return current


def substitute_saved(value: Any, saved: dict[str, Any]) -> Any:
    if isinstance(value, str):
        match = _SAVED_PLACEHOLDER.match(value)
        if match:
            return lookup_saved_path(saved, match.group(1))
        return value
    if isinstance(value, dict):
        return {k: substitute_saved(v, saved) for k, v in value.items()}
    if isinstance(value, list):
        return [substitute_saved(item, saved) for item in value]
    return value


DOWNLOAD_BEGIN_EVENTS = {
    "Browser.downloadWillBegin": "Browser",
    "Page.downloadWillBegin": "Page",
}
DOWNLOAD_PROGRESS_EVENTS = {
    "Browser.downloadProgress": "Browser",
    "Page.downloadProgress": "Page",
}
DOWNLOAD_TERMINAL_STATES = {"canceled", "completed"}
DOWNLOAD_BEHAVIOR_METHODS = {
    "Browser.setDownloadBehavior",
    "Page.setDownloadBehavior",
}


def record_download_lifecycle_event(
    event: dict[str, Any],
    download_domains: dict[str, set[str]],
    terminal_downloads: set[str],
) -> None:
    """Record a download event without consuming it from the CDP event queue."""
    method = event.get("method")
    params = event.get("params")
    if not isinstance(params, dict):
        return
    guid = params.get("guid")
    if not isinstance(guid, str) or not guid:
        return
    domain = DOWNLOAD_BEGIN_EVENTS.get(str(method)) or DOWNLOAD_PROGRESS_EVENTS.get(
        str(method)
    )
    if domain:
        download_domains.setdefault(guid, set()).add(domain)
    if (
        method in DOWNLOAD_PROGRESS_EVENTS
        and params.get("state") in DOWNLOAD_TERMINAL_STATES
    ):
        terminal_downloads.add(guid)


def cleanup_raw_cdp_downloads(
    client: CDPClient,
    download_behavior_configs: list[tuple[str, str | None, str | None]],
    download_domains: dict[str, set[str]],
    terminal_downloads: set[str],
) -> list[str]:
    """Best-effort teardown for browser-scoped download state.

    Download behavior persists beyond the task target, and an active download
    may still write into the per-attempt temporary directory after it is
    renamed.  Observe command-buffered lifecycle events, cancel/drain any guid
    that is not terminal, then restore every domain whose configuration command
    was attempted. Recording before the response matters because an engine can
    apply the behavior even if its response times out. Formal local callers
    retain these errors as diagnostics; strict remote callers promote any error
    to unconfirmed isolation and stop their sequence.
    """
    errors: list[str] = []

    def scan_buffered_events() -> None:
        for event in list(client.events):
            if isinstance(event, dict):
                record_download_lifecycle_event(
                    event,
                    download_domains,
                    terminal_downloads,
                )

    scan_buffered_events()
    if download_behavior_configs and not download_domains:
        try:
            client.pump_pending_events(timeout_s=0.25)
        except Exception as exc:
            errors.append(
                f"pending download event pump: {type(exc).__name__}: {exc}"
            )
        scan_buffered_events()
    for guid in sorted(set(download_domains) - terminal_downloads):
        try:
            client.command("Browser.cancelDownload", {"guid": guid})
        except Exception as exc:
            errors.append(
                f"Browser.cancelDownload({guid}): {type(exc).__name__}: {exc}"
            )
        scan_buffered_events()
        if guid in terminal_downloads:
            continue
        domains = list(download_domains.get(guid) or {"Browser"})
        # Prefer the domain that announced this guid. If an engine reports both
        # deprecated Page and Browser events, either matching terminal event is
        # sufficient to prove that writes have stopped.
        for domain in domains:
            try:
                event = client.wait_for_event(
                    f"{domain}.downloadProgress",
                    {
                        "guid": guid,
                        "state": {
                            EVENT_MATCH_ONE_OF: sorted(DOWNLOAD_TERMINAL_STATES)
                        },
                    },
                    timeout_s=2.0,
                )
                record_download_lifecycle_event(
                    event,
                    download_domains,
                    terminal_downloads,
                )
                break
            except Exception as exc:
                errors.append(
                    f"{domain}.downloadProgress({guid}): "
                    f"{type(exc).__name__}: {exc}"
                )

    reset_seen: set[tuple[str, str | None, str | None]] = set()
    for method, configured_session, browser_context_id in reversed(
        download_behavior_configs
    ):
        reset_key = (method, configured_session, browser_context_id)
        if reset_key in reset_seen:
            continue
        reset_seen.add(reset_key)
        params: dict[str, Any] = {"behavior": "default"}
        if method == "Browser.setDownloadBehavior":
            params["eventsEnabled"] = False
            if browser_context_id is not None:
                params["browserContextId"] = browser_context_id
        try:
            client.command(method, params, session_id=configured_session)
        except Exception as exc:
            errors.append(f"{method} reset: {type(exc).__name__}: {exc}")
    return errors


def run_raw_cdp_driver(
    task: ResolvedTask,
    browser: BrowserProcess,
    artifact_dir: pathlib.Path,
    fixture_base_url: str | None = None,
) -> dict[str, Any]:
    cdp_path = artifact_dir / "cdp.jsonl"
    strict_remote = (
        getattr(browser, "version_info", {}).get("transport") == "remote_cdp"
    )
    saved: dict[str, Any] = {}
    answer: Any = None
    fixture_url: str | None = None
    if task.scene.get("kind") == "self_hosted_fixture":
        if not fixture_base_url:
            raise BenchError("self-hosted fixture server is not running")
        fixture_url = fixture_base_url + task.scene.get("url", "/")
    # {fixture_url} in step params must always be a real http URL (e.g.
    # Network.setCookie rejects about:blank), even for about_blank scenes.
    replacement_url = fixture_url or (f"{fixture_base_url}/l1/core" if fixture_base_url else "about:blank")
    replacements = {
        "{fixture_url}": replacement_url,
        "{fixture_path}": str((BENCH_ROOT / "fixtures").resolve()),
        "{artifact_dir}": str(artifact_dir.resolve()),
    }
    timeout_s = task.task.get("timeouts", {}).get("task_ms", 10000) / 1000
    client, session_id, target_id = open_page_session(browser, cdp_path, timeout_s)
    remote_identity = getattr(client, "remote_identity", None)
    if isinstance(remote_identity, dict):
        saved["__remote_identity__"] = dict(remote_identity)
    download_behavior_configs: list[
        tuple[str, str | None, str | None]
    ] = []
    download_domains: dict[str, set[str]] = {}
    terminal_downloads: set[str] = set()
    driver_result: dict[str, Any] | None = None
    primary_error: Exception | None = None
    target_cleanup: dict[str, Any] = {
        "backend": "Target.closeTarget",
        "target_id": target_id,
        "confirmed": target_id is None,
        "attempts": [],
    }
    download_cleanup: dict[str, Any] = {
        "required": False,
        "confirmed": True,
        "errors": [],
    }
    try:
        # Named sessions for step-level addressing. "page" is the default page
        # session materialized by open_page_session; "browser" is the root
        # connection. Steps may capture more via save_session_as (from a
        # Target.attachToTarget result or a Target.attachedToTarget event).
        saved_sessions: dict[str, str | None] = {"page": session_id, "browser": None}

        def resolve_named_session(key: str) -> str | None:
            if key == "browser":
                return None
            if key in saved_sessions:
                return saved_sessions[key]
            raise BenchError(f"raw_cdp step references unknown session `{key}`")

        def step_session(step: dict[str, Any], method: str) -> str | None:
            # Explicit addressing wins; otherwise fall back to the domain-prefix
            # heuristic (browser-scope domains on the root connection).
            if "session" in step:
                return resolve_named_session(str(step["session"]))
            if method.startswith(BROWSER_SCOPE_DOMAINS):
                return None
            return session_id

        def event_session_filter(step: dict[str, Any]) -> str | None:
            # Filter events by session only when a non-browser session is named;
            # browser/root events carry no sessionId and match unfiltered.
            if "session" in step and str(step["session"]) != "browser":
                return resolve_named_session(str(step["session"]))
            return None

        needs_page = task.scene.get("kind") == "about_blank" and any(
            step.get("method", "").startswith("Runtime.") for step in task.driver.get("steps", [])
        )
        if needs_page or fixture_url:
            try:
                client.command("Page.enable", session_id=session_id)
                client.command("Page.navigate", {"url": fixture_url or "about:blank"}, session_id=session_id)
                if fixture_url:
                    # Wait for the fixture document before probing it; a fixed
                    # sleep races the load under parallel workers.
                    deadline = time.time() + 3.0
                    while time.time() < deadline:
                        try:
                            state = client.command(
                                "Runtime.evaluate",
                                {"expression": "document.readyState", "returnByValue": True},
                                session_id=session_id,
                            )
                            if (state.get("result") or {}).get("value") in {"interactive", "complete"}:
                                break
                        except CDPCommandError:
                            break
                        time.sleep(0.1)
                    time.sleep(0.2)
                else:
                    time.sleep(0.2)
            except CDPCommandError:
                # Some L1 probes intentionally target non-Page domains. Keep the
                # actual task step responsible for final unsupported/fail status.
                pass
        for step in task.driver.get("steps", []):
            if "sleep_ms" in step and "method" not in step and "wait_for_event" not in step:
                time.sleep(min(int(step["sleep_ms"]), 5000) / 1000)
                continue

            # Event-wait step: block until a matching CDP event arrives.
            if "wait_for_event" in step:
                method = str(step["wait_for_event"])
                match = substitute_params(substitute_saved(step.get("match"), saved), replacements)
                wait_s = min(int(step.get("timeout_ms", 5000)), 30000) / 1000
                try:
                    event = client.wait_for_event(method, match, event_session_filter(step), wait_s)
                except CDPTransportTimeout:
                    # CDPTransportTimeout is also a TimeoutError. Preserve the
                    # transport classification before handling the healthy
                    # semantic deadline below.
                    raise
                except TimeoutError:
                    if step.get("optional"):
                        saved.setdefault("__event_timeouts__", []).append(method)
                        continue
                    # A required event that never arrives is a semantic gap:
                    # route it through the CDP-error truth table (cdp_semantic).
                    raise CDPCommandError(method, {"message": f"wait_for_event timeout: {method} not observed within {wait_s}s"})
                event_params = event.get("params", {})
                record_download_lifecycle_event(
                    event,
                    download_domains,
                    terminal_downloads,
                )
                saved.setdefault("__events_observed__", []).append(method)
                if "save_result_as" in step:
                    name = step["save_result_as"]
                    saved[name] = event_params
                    saved[f"{name}__raw"] = event_params
                if "save_session_as" in step:
                    session_value = event_params.get("sessionId") or event.get("sessionId")
                    if not session_value:
                        # Registering None would silently reroute later steps to
                        # the browser-level connection; fail loudly instead.
                        raise BenchError(f"wait_for_event {method}: event carries no sessionId to save_session_as")
                    saved_sessions[str(step["save_session_as"])] = session_value
                continue

            params = substitute_params(substitute_saved(step.get("params"), saved), replacements)
            selected_session = step_session(step, step["method"])
            if step["method"] in DOWNLOAD_BEHAVIOR_METHODS:
                # The browser may apply this persistent setting even when its
                # response is lost. Track before the command so finally always
                # has a reset plan.
                download_behavior_configs.append(
                    (
                        step["method"],
                        selected_session,
                        (
                            str(params["browserContextId"])
                            if isinstance(params, dict)
                            and params.get("browserContextId") is not None
                            else None
                        ),
                    )
                )
            try:
                result = client.command(
                    step["method"],
                    params,
                    session_id=selected_session,
                )
            except CDPCommandError as exc:
                if step.get("expect_unsupported"):
                    # Negative/deprecated probe: a clean synchronous rejection is
                    # the expected outcome, not a task failure. Record it and go on.
                    cls = "unsupported" if is_unsupported_error(exc) else "rejected"
                    saved.setdefault("__unsupported__", {})[step["method"]] = {"message": str(exc), "class": cls}
                    continue
                if step.get("optional"):
                    saved.setdefault("__optional_step_errors__", []).append(f"{step['method']}: {exc}")
                    continue
                raise
            if step.get("expect_unsupported"):
                # The command was expected to be rejected but succeeded.
                saved.setdefault("__supported_unexpectedly__", []).append(step["method"])
            if result.get("exceptionDetails"):
                saved["__eval_exception_count__"] = int(saved.get("__eval_exception_count__") or 0) + 1
            if "save_result_as" in step:
                name = step["save_result_as"]
                saved[name] = result
                saved[f"{name}__raw"] = result
            if "save_session_as" in step:
                saved_sessions[str(step["save_session_as"])] = result.get("sessionId")
            if "save_as" in step:
                remote = result.get("result", {})
                if not isinstance(remote, dict):
                    remote = {}
                name = step["save_as"]
                saved[name] = remote.get("value")
                saved[f"{name}__type"] = remote.get("type")
                saved[f"{name}__raw"] = result
                answer = remote.get("value")
        grader = grade_inline(task, saved)
        driver_result = {
            "ok": grader["ok"],
            "answer": answer,
            "observations": saved,
            "grader": grader,
            "metrics": {
                "cdp_call_count": client.call_count,
                "cdp_error_count": client.error_count,
                "ws_disconnect_count": client.disconnect_count,
            },
        }
    except Exception as exc:
        # Preserve transport accounting and partial observations when a raw
        # command/event step raises before the normal driver result is built.
        # Callers still receive the original exception type and classification
        # after cleanup has been attempted and recorded.
        setattr(
            exc,
            "cdp_metrics",
            {
                "cdp_call_count": client.call_count,
                "cdp_error_count": client.error_count,
                "ws_disconnect_count": client.disconnect_count,
            },
        )
        setattr(exc, "cdp_observations", dict(saved))
        primary_error = exc
    finally:
        cleanup_errors = cleanup_raw_cdp_downloads(
            client,
            download_behavior_configs,
            download_domains,
            terminal_downloads,
        )
        download_cleanup = {
            "required": bool(download_behavior_configs or download_domains),
            "confirmed": not cleanup_errors,
            "errors": list(cleanup_errors),
        }
        saved["download_cleanup"] = download_cleanup
        if cleanup_errors:
            saved["__download_cleanup_errors__"] = cleanup_errors
        if target_id:
            for cleanup_attempt in range(1, 3):
                try:
                    close_result = client.command(
                        "Target.closeTarget", {"targetId": target_id}
                    )
                    confirmed = close_result.get("success") is True
                    target_cleanup["attempts"].append(
                        {
                            "attempt": cleanup_attempt,
                            "success": close_result.get("success"),
                            "confirmed": confirmed,
                        }
                    )
                    if confirmed:
                        target_cleanup["confirmed"] = True
                        break
                except Exception as exc:
                    target_cleanup["attempts"].append(
                        {
                            "attempt": cleanup_attempt,
                            "confirmed": False,
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    )
        saved["target_cleanup"] = target_cleanup
        saved["isolation_restored"] = (
            target_cleanup["confirmed"] is True
            and (not strict_remote or download_cleanup["confirmed"] is True)
        )
        client.close()

    metrics = (
        dict(driver_result.get("metrics") or {})
        if driver_result is not None
        else dict(getattr(primary_error, "cdp_metrics", {}) or {})
    )
    cleanup_error: BenchError | None = None
    preserve_transport_error = False
    if strict_remote and (
        target_cleanup["confirmed"] is not True
        or download_cleanup["confirmed"] is not True
    ):
        if driver_result is not None:
            saved["primary_outcome"] = {
                "kind": "driver_result",
                "status": "pass" if driver_result.get("ok") else "fail",
                "ok": driver_result.get("ok"),
                "answer": driver_result.get("answer"),
                "grader": driver_result.get("grader"),
            }
        elif primary_error is not None:
            saved["primary_outcome"] = {
                "kind": "exception",
                "status": "error",
                "error_type": type(primary_error).__name__,
                "error": str(primary_error),
            }
        cleanup_failures = []
        if target_cleanup["confirmed"] is not True:
            cleanup_failures.append("Target.closeTarget unconfirmed")
        if download_cleanup["confirmed"] is not True:
            cleanup_failures.append(
                "download cleanup errors="
                + json.dumps(download_cleanup["errors"], sort_keys=True)
            )
        saved["isolation_restored"] = False
        preserve_transport_error = bool(
            primary_error is not None
            and is_cdp_transport_exception(primary_error)
        )
        if not preserve_transport_error:
            cleanup_error = BenchError(
                "remote raw-CDP isolation cleanup was not explicitly confirmed: "
                + "; ".join(cleanup_failures)
            )
            setattr(cleanup_error, "cdp_metrics", metrics)
            setattr(cleanup_error, "cdp_observations", dict(saved))
            setattr(
                cleanup_error,
                "target_cleanup_confirmed",
                target_cleanup["confirmed"] is True,
            )
            setattr(
                cleanup_error,
                "download_cleanup_confirmed",
                download_cleanup["confirmed"] is True,
            )
            setattr(cleanup_error, "isolation_restored", False)

    if primary_error is not None:
        setattr(primary_error, "cdp_metrics", metrics)
        setattr(primary_error, "cdp_observations", dict(saved))
        if preserve_transport_error:
            setattr(
                primary_error,
                "target_cleanup_confirmed",
                target_cleanup["confirmed"] is True,
            )
            setattr(
                primary_error,
                "download_cleanup_confirmed",
                download_cleanup["confirmed"] is True,
            )
            setattr(primary_error, "isolation_restored", False)
    if cleanup_error is not None:
        if primary_error is not None:
            raise cleanup_error from primary_error
        raise cleanup_error
    if primary_error is not None:
        raise primary_error
    if driver_result is None:
        raise BenchError("raw CDP driver produced no result")
    return driver_result


def browser_cdp_product(browser: BrowserProcess) -> str:
    """CDP-level Browser.getVersion product for the engine under test.

    Captured once per browser process and cached. Used by the framework-driver
    binding gate: the product string reported through the live framework
    transport must equal this reference, or the attempt fails cleanly.
    """
    if browser.cdp_product is None:
        ws_url = browser.version_info.get("webSocketDebuggerUrl")
        if not ws_url:
            browser.cdp_product = ""
            return browser.cdp_product
        try:
            with CDPClient(ws_url, pathlib.Path(os.devnull), timeout_s=5.0) as client:
                version = client.command("Browser.getVersion")
                browser.cdp_product = str(version.get("product") or "")
        except Exception:
            # Transient capture failure: leave the cache empty so the next
            # attempt retries instead of poisoning every later binding gate.
            return ""
    return browser.cdp_product


def run_framework_driver(
    task: ResolvedTask,
    browser: BrowserProcess,
    artifact_dir: pathlib.Path,
    fixture_base_url: str,
    run_id: str,
    engine: str,
    attempt: int,
    seed: str,
) -> dict[str, Any]:
    """Drive the engine with a real pinned framework (Playwright / Puppeteer).

    The scenario (driver.steps) and checks (driver.checks) are passed to
    framework_probe.js, which connects the pinned framework to the engine's
    browser websocket (connectOverCDP / connect). No fallback: the probe
    verifies per attempt that the endpoint and the live framework session both
    identify as the engine under test, and fails cleanly otherwise.
    """
    session = f"{run_id}-{task.task_id}-{engine}-{attempt}-{seed}"
    url_template = task.scene["url"]
    path = url_template.format(seed=urllib.parse.quote(seed), session=urllib.parse.quote(session))
    task_url = fixture_base_url + path
    browser_ws = browser.version_info.get("webSocketDebuggerUrl")
    if not browser_ws:
        raise BenchError(f"{engine}: no browser websocket endpoint for framework driver")
    replacements = {
        "{seed}": seed,
        "{session}": session,
        "{fixture_base_url}": fixture_base_url,
        "{artifact_dir}": str(artifact_dir),
    }
    env = subprocess_env()
    # This flag controls strict remote identity and cleanup behavior inside the
    # probe. Never let an unrelated parent-shell value turn a local benchmark
    # attempt into a remote one.
    env.pop("REMOTE_CDP_IDENTITY_JSON", None)
    env.update(
        {
            "FRAMEWORK": FRAMEWORK_DRIVER_KINDS[task.driver["kind"]],
            "BROWSER_WS": browser_ws,
            "CDP_PORT": str(browser.port),
            "EXPECT_PRODUCT": str(browser.version_info.get("Browser") or ""),
            "EXPECT_UA": str(browser.version_info.get("User-Agent") or ""),
            "EXPECT_PRODUCT_LIVE": browser_cdp_product(browser),
            # Substitute on the object, then serialize: substituting into the
            # serialized text could produce invalid JSON if a replacement value
            # ever contained quotes or backslashes.
            "FW_STEPS": json.dumps(substitute_params(task.driver.get("steps") or [], replacements)),
            "FW_CHECKS": json.dumps(substitute_params(task.driver.get("checks") or [], replacements)),
            "FW_CONNECT_OPTIONS": json.dumps(task.driver.get("connect_options") or {}),
            # The runner kills the probe at task_ms; the probe clamps per-op
            # waits to the remaining budget so an engine that hangs mid-
            # scenario still emits a graded result instead of an infra kill.
            "FW_TASK_TIMEOUT_MS": str(int(task.task.get("timeouts", {}).get("task_ms", 30000))),
            "TASK_URL": task_url,
            "TASK_ID": task.task_id,
            "RUN_ID": run_id,
            "ENGINE": engine,
            "ATTEMPT": str(attempt),
            "SEED": seed,
            "ARTIFACT_DIR": str(artifact_dir),
        }
    )
    if browser.version_info.get("transport") == "remote_cdp":
        env["REMOTE_CDP_IDENTITY_JSON"] = json.dumps(
            {
                "product": (
                    browser.version_info.get("product")
                    or browser.version_info.get("Browser")
                    or ""
                ),
                "protocolVersion": browser.version_info.get("protocolVersion"),
                "revision": browser.version_info.get("revision"),
            }
        )
    output = run_node_driver_process(
        task,
        FRAMEWORK_PROBE_SCRIPT,
        env,
        artifact_dir,
        fixture_base_url,
        run_id,
        engine,
        attempt,
        seed,
        session,
    )
    if browser.version_info.get("transport") == "remote_cdp":
        output = enforce_remote_driver_cleanup(
            output,
            driver_key=FRAMEWORK_DRIVER_KINDS[task.driver["kind"]],
        )
    return output


def run_scenario_adapter_driver(
    task: ResolvedTask,
    browser: BrowserProcess,
    artifact_dir: pathlib.Path,
    fixture_base_url: str,
    run_id: str,
    engine: str,
    attempt: int,
    seed: str,
    runtime_binding: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Drive the engine through a thin-client scenario adapter.

    The adapter (SCENARIO_ADAPTER_KINDS) receives one abb_scenario_adapter/1
    payload on stdin and reports the framework_probe.js result contract on
    stdout — see runner/scripts/adapters/PROTOCOL.md. Same no-fallback rule as
    the framework drivers: the adapter verifies the endpoint identity (HTTP
    /json/version) and the live transport identity through its own protocol
    (for example CDP Browser.getVersion or WebDriver capabilities) per attempt,
    and refuses to run on a mismatch.
    """
    spec = SCENARIO_ADAPTER_KINDS[task.driver["kind"]]
    session = f"{run_id}-{task.task_id}-{engine}-{attempt}-{seed}"
    url_template = task.scene["url"]
    path = url_template.format(seed=urllib.parse.quote(seed), session=urllib.parse.quote(session))
    task_url = fixture_base_url + path
    browser_ws = browser.version_info.get("webSocketDebuggerUrl")
    if not browser_ws:
        raise BenchError(f"{engine}: no browser websocket endpoint for scenario adapter driver")
    replacements = {
        "{seed}": seed,
        "{session}": session,
        "{fixture_base_url}": fixture_base_url,
        "{artifact_dir}": str(artifact_dir),
    }
    if spec["driver_key"] == "selenium":
        if runtime_binding is None:
            raise BenchError(
                f"missing pre-resolved Selenium binding for ({engine}, selenium)"
            )
        expect_product = str(
            browser.version_info.get("Browser")
            or browser.version_info.get("Product")
            or ""
        )
        route_id = (runtime_binding.get("route") or {}).get("route_id")
        if route_id == "native_webdriver":
            expect_product_live = str(
                browser.version_info.get("Browser")
                or browser.version_info.get("Product")
                or ""
            )
        elif route_id == "chromedriver_cdp":
            # Preserve the discovery identity as a fail-closed expectation if
            # the live CDP probe is transiently unavailable.  A blank expected
            # value must never let a blank live observation verify.
            expect_product_live = browser_cdp_product(browser) or expect_product
        else:
            raise BenchError(
                f"unknown pre-resolved Selenium route `{route_id}` for ({engine}, selenium)"
            )
    else:
        # Preserve the legacy payload contract for every other adapter.
        expect_product = str(browser.version_info.get("Browser") or "")
        expect_product_live = browser_cdp_product(browser)
    strict_remote = browser.version_info.get("transport") == "remote_cdp"
    expected_remote_identity = (
        require_remote_cdp_identity(
            browser.version_info,
            label="remote scenario-adapter preflight",
        )
        if strict_remote
        else None
    )
    payload = {
        "protocol": "abb_scenario_adapter/1",
        "driver_kind": task.driver["kind"],
        "driver_key": spec["driver_key"],
        "browser_ws": browser_ws,
        "cdp_port": browser.port,
        "remote_cdp": strict_remote,
        "expected_remote_identity": expected_remote_identity,
        "expect_product": expect_product,
        "expect_ua": str(browser.version_info.get("User-Agent") or ""),
        "expect_product_live": expect_product_live,
        "transport_policy": task.driver.get("transport_policy"),
        "task_url": task_url,
        "steps": substitute_params(task.driver.get("steps") or [], replacements),
        "checks": substitute_params(task.driver.get("checks") or [], replacements),
        "connect_timeout_ms": 15000,
        "action_timeout_ms": 8000,
        # The runner kills the adapter at task_ms; the adapter uses this to
        # fail remaining ops fast instead of being killed mid-run (which would
        # misclassify an engine-capability fail as infra).
        "task_timeout_ms": int(task.task.get("timeouts", {}).get("task_ms", 30000)),
        "artifact_dir": str(artifact_dir),
        "task_id": task.task_id,
        "run_id": run_id,
        "engine": engine,
        "attempt": attempt,
        "seed": seed,
    }
    if spec["driver_key"] == "selenium":
        payload["binding"] = runtime_binding
    argv = scenario_adapter_argv(spec)
    env = subprocess_env()
    if spec.get("env_setup") == "agent_browser":
        configure_agent_browser_attempt_env(
            env, run_id, task.task_id, engine, attempt, seed
        )
        local_ab = BENCH_ROOT / "node_modules" / ".bin" / "agent-browser"
        if "AB_BIN" not in env and local_ab.exists():
            env["AB_BIN"] = str(local_ab)
    try:
        output = run_driver_subprocess(
            task,
            argv,
            env,
            artifact_dir,
            fixture_base_url,
            run_id,
            engine,
            attempt,
            seed,
            session,
            stdin_text=json.dumps(payload),
        )
        if strict_remote:
            assert expected_remote_identity is not None
            output = enforce_remote_scenario_adapter_identity(
                output,
                expected_remote_identity,
                driver_key=str(spec["driver_key"]),
            )
            output = enforce_remote_driver_cleanup(
                output,
                driver_key=str(spec["driver_key"]),
            )
        return output
    finally:
        if spec.get("env_setup") == "agent_browser":
            # The adapter closes its named session itself.  Confirm the whole
            # per-attempt namespace here as well: commands such as download
            # can keep daemon work alive briefly after the adapter returns.
            force_close_agent_browser_attempt(env)
            remove_agent_browser_namespace_state(env)


def enforce_remote_scenario_adapter_identity(
    output: dict[str, Any],
    expected: dict[str, str],
    *,
    driver_key: str,
) -> dict[str, Any]:
    """Fail closed when a remote adapter lacks complete live binding.

    A remote compatibility result is attributable only when the same live
    client connection supplied all three immutable preflight fields. Explicit
    infra/timeout/transport rows remain diagnostic; any otherwise gradable
    output is converted to a binding-unverified infra exclusion. Connection
    evidence is retained so the experimental driver runner can independently
    classify recognized endpoint/network failures as transport outcomes.
    """

    observations = output.get("observations") or {}
    if not isinstance(observations, dict):
        observations = {}
    binding = observations.get("binding") or {}
    if not isinstance(binding, dict):
        binding = {}

    actual = binding.get("actual")
    claimed_expected = binding.get("expected")
    compared_fields = binding.get("compared_fields")
    valid = (
        isinstance(actual, dict)
        and isinstance(claimed_expected, dict)
        and compared_fields == list(REMOTE_CDP_IDENTITY_FIELDS)
        and binding.get("same_connection_as_task") is True
        and binding.get("reconnect_allowed") is False
        and all(
            str(actual.get(field) or "") == expected[field]
            and str(claimed_expected.get(field) or "") == expected[field]
            for field in REMOTE_CDP_IDENTITY_FIELDS
        )
    )
    if valid:
        return output

    reported_status = str(output.get("status") or "").strip().lower()
    derived_status = reported_status or (
        "pass" if output.get("ok") is True else "fail"
    )
    gradable = output.get("ok") is True or derived_status not in {
        "infra",
        "timeout",
        "transport_error",
    }
    if not gradable:
        return output

    rejected_binding = dict(binding)
    rejected_binding.update(
        {
            "verified": False,
            "excluded": True,
            "gate": "remote_full_identity_unverified",
            "required_fields": list(REMOTE_CDP_IDENTITY_FIELDS),
        }
    )
    rejected_observations = dict(observations)
    rejected_observations.update(
        {
            "binding": rejected_binding,
            "failure_class": "binding_unverified",
            "formal_score_eligible": False,
            "rejected_driver_output": output,
        }
    )
    if binding.get("verified") is True:
        detail = (
            f"remote {driver_key} adapter claimed a verified binding without "
            "same-connection product/protocolVersion/revision evidence"
        )
    else:
        detail = (
            f"remote {driver_key} adapter produced a gradable result before "
            "same-connection product/protocolVersion/revision verification"
        )
    return {
        "ok": False,
        "status": "infra",
        "failure": failure_obj("script_error", detail),
        "error": {"class": "script_error", "message": detail},
        "answer": output.get("answer"),
        "observations": rejected_observations,
        "grader": {
            "ok": False,
            "checks": [],
            "failure": failure_obj("script_error", detail),
        },
        "metrics": output.get("metrics") or {},
    }


def enforce_remote_driver_cleanup(
    output: dict[str, Any],
    *,
    driver_key: str,
) -> dict[str, Any]:
    """Reject a remote pass without same-connection cleanup evidence.

    Kitesurf target identifiers are connection-local, so a second diagnostic
    connection cannot prove that an adapter-owned page was removed. Adapters
    must finish cleanup before emitting a successful result and persist the
    acknowledgement observed through their task connection.
    """

    if output.get("ok") is not True:
        return output
    observations = output.get("observations") or {}
    if not isinstance(observations, dict):
        observations = {}
    cleanup = observations.get("target_cleanup")
    valid = (
        isinstance(cleanup, dict)
        and cleanup.get("confirmed") is True
        and cleanup.get("same_connection_as_task") is True
        and observations.get("isolation_restored") is True
    )
    if valid:
        return output

    detail = (
        f"remote {driver_key} result lacks confirmed same-connection "
        "target cleanup"
    )
    rejected_observations = dict(observations)
    rejected_observations.update(
        {
            "target_cleanup": cleanup,
            "isolation_restored": False,
            "cleanup_contract_error": detail,
            "primary_outcome": output,
        }
    )
    return {
        "ok": False,
        "status": "infra",
        "failure": failure_obj("script_error", detail),
        "error": {"class": "script_error", "message": detail},
        "answer": output.get("answer"),
        "observations": rejected_observations,
        "grader": {
            "ok": False,
            "checks": [],
            "failure": failure_obj("script_error", detail),
        },
        "metrics": output.get("metrics") or {},
    }


def browser_page_target_ids(browser: BrowserProcess) -> set[str]:
    browser_ws = browser.version_info.get("webSocketDebuggerUrl")
    if not browser_ws:
        raise BenchError(f"{browser.engine}: no browser websocket for target lifecycle check")
    last_error: Exception | None = None
    for retry in range(3):
        try:
            with CDPClient(
                browser_ws, pathlib.Path(os.devnull), timeout_s=3.0
            ) as client:
                result = client.command("Target.getTargets")
            return {
                str(info["targetId"])
                for info in result.get("targetInfos", [])
                if info.get("targetId")
                and str(info.get("type") or "page") == "page"
            }
        except (ConnectionError, OSError) as exc:
            last_error = exc
            if retry < 2:
                time.sleep(0.05)
    assert last_error is not None
    raise last_error


def cleanup_new_page_targets(
    browser: BrowserProcess,
    before: set[str] | None,
) -> dict[str, Any]:
    diagnostic: dict[str, Any] = {
        "backend": "Target.getTargets/Target.closeTarget",
        "confirmed": False,
        "before": sorted(before) if before is not None else None,
        "after": None,
        "final": None,
        "new_targets": [],
        "closed_targets": [],
        "retry_closed_targets": [],
        "remaining_new_targets": [],
        "errors": [],
    }
    if before is None:
        diagnostic["errors"].append("pre-attempt target snapshot unavailable")
        return diagnostic
    try:
        after = browser_page_target_ids(browser)
        diagnostic["after"] = sorted(after)
    except Exception as exc:
        diagnostic["errors"].append(f"post-attempt target snapshot failed: {exc}")
        return diagnostic
    new_targets = sorted(after - before)
    diagnostic["new_targets"] = new_targets
    browser_ws = browser.version_info.get("webSocketDebuggerUrl")
    if not browser_ws:
        diagnostic["errors"].append("browser websocket unavailable during cleanup")
        return diagnostic
    try:
        with CDPClient(browser_ws, pathlib.Path(os.devnull), timeout_s=3.0) as client:
            for target_id in new_targets:
                try:
                    result = client.command("Target.closeTarget", {"targetId": target_id})
                    if result.get("success") is True:
                        diagnostic["closed_targets"].append(target_id)
                    else:
                        diagnostic["errors"].append(
                            f"{target_id}: Target.closeTarget returned success=false"
                        )
                except Exception as exc:
                    diagnostic["errors"].append(f"{target_id}: {exc}")
    except Exception as exc:
        diagnostic["errors"].append(f"cleanup connection failed: {exc}")
    try:
        final = browser_page_target_ids(browser)
        diagnostic["final"] = sorted(final)
        remaining = final - before
        if remaining:
            # Target.closeTarget acknowledgement can precede disappearance
            # from Target.getTargets.  Give that asynchronous edge one short
            # bounded retry without delaying the common path.
            time.sleep(0.05)
            final = browser_page_target_ids(browser)
            diagnostic["final"] = sorted(final)
            remaining = final - before
        if remaining:
            # A browser may drop the cleanup WebSocket while keeping both the
            # process and target alive. Reconnect once rather than merely
            # proving that the target survived the failed close command.
            try:
                with CDPClient(
                    browser_ws, pathlib.Path(os.devnull), timeout_s=3.0
                ) as retry_client:
                    for target_id in sorted(remaining):
                        try:
                            result = retry_client.command(
                                "Target.closeTarget", {"targetId": target_id}
                            )
                            if result.get("success") is True:
                                if target_id not in diagnostic["closed_targets"]:
                                    diagnostic["closed_targets"].append(target_id)
                                diagnostic["retry_closed_targets"].append(target_id)
                            else:
                                diagnostic["errors"].append(
                                    f"{target_id}: retry Target.closeTarget "
                                    "returned success=false"
                                )
                        except Exception as exc:
                            diagnostic["errors"].append(
                                f"{target_id}: retry failed: {exc}"
                            )
            except Exception as exc:
                diagnostic["errors"].append(
                    f"cleanup retry connection failed: {exc}"
                )
            time.sleep(0.05)
            final = browser_page_target_ids(browser)
            diagnostic["final"] = sorted(final)
            remaining = final - before
        diagnostic["remaining_new_targets"] = sorted(remaining)
        if remaining:
            diagnostic["errors"].append(
                "new page targets remain after cleanup: "
                + ", ".join(sorted(remaining))
            )
        else:
            diagnostic["confirmed"] = True
    except Exception as exc:
        diagnostic["errors"].append(f"post-cleanup target snapshot failed: {exc}")
    return diagnostic


def run_node_cdp_probe_driver(
    task: ResolvedTask,
    browser: BrowserProcess,
    artifact_dir: pathlib.Path,
    fixture_base_url: str,
    run_id: str,
    engine: str,
    attempt: int,
    seed: str,
) -> dict[str, Any]:
    script_rel = task.driver["script"]
    script = BENCH_ROOT / script_rel
    session = f"{run_id}-{task.task_id}-{engine}-{attempt}-{seed}"
    url_template = task.scene["url"]
    path = url_template.format(seed=urllib.parse.quote(seed), session=urllib.parse.quote(session))
    task_url = fixture_base_url + path
    env = subprocess_env()
    configure_agent_browser_attempt_env(
        env, run_id, task.task_id, engine, attempt, seed
    )
    # Pinned repo-local agent-browser (harness_pins.json) wins over whatever is
    # on PATH, so the tool-surface subset is not hostage to global npm state.
    local_ab = BENCH_ROOT / "node_modules" / ".bin" / "agent-browser"
    if "AB_BIN" not in env and local_ab.exists():
        env["AB_BIN"] = str(local_ab)
    # Browser-level ws: scripts bootstrap their own page session (Target.create/
    # attach), which Lightpanda requires. Local engines retain the /json/new
    # compatibility fallback. Remote attempts must verify Browser.getVersion
    # and run the task on this exact connection, with no reconnect fallback.
    browser_ws = browser.version_info.get("webSocketDebuggerUrl") or create_page_ws(browser)
    env.update(
        {
            "BROWSER_WS": browser_ws,
            "CDP_PORT": str(browser.port),
            "TASK_URL": task_url,
            "TASK_URL_PRELOADED": "0",
            "TASK_ID": task.task_id,
            "RUN_ID": run_id,
            "ENGINE": engine,
            "ATTEMPT": str(attempt),
            "SEED": seed,
            "ARTIFACT_DIR": str(artifact_dir),
        }
    )
    extra_env = task.driver.get("env") or {}
    replacements = {
        "{seed}": seed,
        "{session}": session,
        "{fixture_base_url}": fixture_base_url,
        "{artifact_dir}": str(artifact_dir),
    }
    for key, value in extra_env.items():
        text = json.dumps(value) if isinstance(value, (dict, list)) else str(value)
        env[str(key)] = substitute_params(text, replacements)
    # This variable switches both Node probes into strict remote identity and
    # cleanup mode. Clear parent-shell and task-provided values after merging
    # task env so a stale Kitesurf expectation cannot reclassify a formal
    # local Chrome/Moli/Lightpanda/Obscura attempt.
    env.pop("REMOTE_CDP_IDENTITY_JSON", None)
    strict_remote = browser.version_info.get("transport") == "remote_cdp"
    if strict_remote:
        # Apply this after task-provided env so a task cannot replace the
        # runner-owned identity expectation used for attribution.
        env["REMOTE_CDP_IDENTITY_JSON"] = json.dumps(
            {
                "product": (
                    browser.version_info.get("product")
                    or browser.version_info.get("Browser")
                    or ""
                ),
                "protocolVersion": browser.version_info.get("protocolVersion"),
                "revision": browser.version_info.get("revision"),
            }
        )
    before_targets: set[str] | None
    pre_error: str | None = None
    if strict_remote:
        before_targets = None
    else:
        try:
            before_targets = browser_page_target_ids(browser)
        except Exception as exc:
            before_targets = None
            pre_error = str(exc)
    driver_output: dict[str, Any] | None = None
    driver_error: BaseException | None = None
    try:
        driver_output = run_node_driver_process(
            task,
            script,
            env,
            artifact_dir,
            fixture_base_url,
            run_id,
            engine,
            attempt,
            seed,
            session,
        )
    except BaseException as exc:
        driver_error = exc
    finally:
        if script.name == "l1_ab_probe.js":
            # Always confirm closure outside the Node probe.  A successful
            # probe may still leave asynchronous daemon work (notably a
            # download finalizer) that outlives its JavaScript finally block.
            force_close_agent_browser_attempt(env)
        remove_agent_browser_namespace_state(env)
        if strict_remote:
            # Kitesurf's public endpoint exposes a connection-local target
            # namespace: opening a second WebSocket for Target.getTargets can
            # itself produce a different default target. Cross-connection
            # before/after IDs therefore cannot prove cleanup. Require the
            # close acknowledgement emitted on the exact task connection.
            cleanup_source = driver_output
            if cleanup_source is None:
                try:
                    loaded = json.loads(
                        (artifact_dir / "stdout.log").read_text(encoding="utf-8")
                    )
                    cleanup_source = loaded if isinstance(loaded, dict) else None
                except Exception:
                    cleanup_source = None
            source_observations = (
                (cleanup_source or {}).get("observations") or {}
            )
            same_connection_cleanup = (
                source_observations.get("target_cleanup")
                if isinstance(source_observations, dict)
                else None
            )
            diagnostic = {
                "backend": "driver_same_connection_target_cleanup",
                "confirmed": (
                    isinstance(same_connection_cleanup, dict)
                    and same_connection_cleanup.get("confirmed") is True
                ),
                "driver_target_cleanup": same_connection_cleanup,
                "cross_connection_snapshot_skipped": True,
                "errors": [],
            }
            if diagnostic["confirmed"] is not True:
                diagnostic["errors"].append(
                    "driver did not provide confirmed same-connection target cleanup"
                )
            write_json(artifact_dir / "target_cleanup.json", diagnostic)
        else:
            try:
                diagnostic = cleanup_new_page_targets(browser, before_targets)
                if pre_error:
                    diagnostic["errors"].insert(
                        0, f"pre-attempt target snapshot failed: {pre_error}"
                    )
                write_json(artifact_dir / "target_cleanup.json", diagnostic)
            except Exception as exc:
                diagnostic = {
                    "backend": "Target.getTargets/Target.closeTarget",
                    "confirmed": False,
                    "errors": [
                        f"target cleanup guard failed: {type(exc).__name__}: {exc}"
                    ],
                }

    assert isinstance(diagnostic, dict)
    if driver_output is not None:
        observations = driver_output.get("observations") or {}
        if not isinstance(observations, dict):
            observations = {}
        observations = dict(observations)
        observations["outer_target_cleanup"] = diagnostic
        driver_output["observations"] = observations
    elif driver_error is not None and strict_remote:
        try:
            raw_output = json.loads(
                (artifact_dir / "stdout.log").read_text(encoding="utf-8")
            )
        except Exception:
            raw_output = {}
        raw_observations = raw_output.get("observations") or {}
        if not isinstance(raw_observations, dict):
            raw_observations = {}
        exception_observations = {
            **raw_observations,
            "outer_target_cleanup": diagnostic,
            "isolation_restored": diagnostic.get("confirmed") is True,
        }
        setattr(driver_error, "cdp_observations", exception_observations)
        setattr(
            driver_error,
            "isolation_restored",
            diagnostic.get("confirmed") is True,
        )
    if strict_remote and diagnostic.get("confirmed") is not True:
        detail = "remote target cleanup guard could not confirm isolation restoration"
        if driver_error is not None:
            # Preserve the original timeout/transport/process classification.
            # The attached observations still force the caller to stop before
            # another remote attempt starts.
            raise driver_error
        primary: dict[str, Any]
        if driver_output is not None:
            primary = driver_output
        elif driver_error is not None:
            primary = {
                "exception": f"{type(driver_error).__name__}: {driver_error}"
            }
        else:
            primary = {"error": "driver produced no result"}
        return {
            "ok": False,
            "status": "infra",
            "failure": failure_obj("script_error", detail),
            "answer": (driver_output or {}).get("answer"),
            "observations": {
                **((driver_output or {}).get("observations") or {}),
                "outer_target_cleanup": diagnostic,
                "isolation_restored": False,
                "primary_outcome": primary,
            },
            "grader": {
                "ok": False,
                "checks": [],
                "failure": failure_obj("script_error", detail),
            },
            "metrics": (driver_output or {}).get("metrics") or {},
        }
    if driver_error is not None:
        raise driver_error
    if driver_output is None:
        raise BenchError("node CDP probe produced no result")
    return driver_output


def run_node_driver_process(
    task: ResolvedTask,
    script: pathlib.Path,
    env: dict[str, str],
    artifact_dir: pathlib.Path,
    fixture_base_url: str,
    run_id: str,
    engine: str,
    attempt: int,
    seed: str,
    session: str,
) -> dict[str, Any]:
    return run_driver_subprocess(task, ["node", str(script)], env, artifact_dir, fixture_base_url, run_id, engine, attempt, seed, session)


def _terminate_driver_process_group(
    proc: subprocess.Popen[str],
    grace_s: float = 2.0,
) -> tuple[str | bytes | None, str | bytes | None]:
    """Best-effort TERM/KILL cleanup for one detached adapter process group."""
    stdout_text: str | bytes | None = None
    stderr_text: str | bytes | None = None
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except OSError:
        pass
    try:
        return proc.communicate(timeout=grace_s)
    except subprocess.TimeoutExpired as exc:
        stdout_text = exc.stdout
        stderr_text = exc.stderr
    except BaseException:
        # communicate() itself may have been interrupted. Cleanup must still
        # advance to SIGKILL without replacing the caller's original error.
        pass

    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        pass
    try:
        final_stdout, final_stderr = proc.communicate(timeout=grace_s)
        if final_stdout is not None:
            stdout_text = final_stdout
        if final_stderr is not None:
            stderr_text = final_stderr
    except subprocess.TimeoutExpired as exc:
        if exc.stdout is not None:
            stdout_text = exc.stdout
        if exc.stderr is not None:
            stderr_text = exc.stderr
    except BaseException:
        pass
    try:
        proc.wait(timeout=grace_s)
    except BaseException:
        pass
    return stdout_text, stderr_text


@dataclass(frozen=True)
class _DriverProcStat:
    pid: int
    state: str
    pgrp: int
    session: int
    start_ticks: int


@dataclass(frozen=True)
class _DriverProcessIdentity:
    leader_pid: int
    pgid: int
    session: int
    leader_start_ticks: int


def _read_driver_proc_stat(pid: int) -> _DriverProcStat | None:
    """Read the fields needed to identify one Linux process generation."""
    try:
        raw = pathlib.Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        comm_end = raw.rfind(")")
        if comm_end < 0:
            return None
        fields = raw[comm_end + 2 :].split()
        return _DriverProcStat(
            pid=pid,
            state=fields[0],
            pgrp=int(fields[2]),
            session=int(fields[3]),
            start_ticks=int(fields[19]),
        )
    except (OSError, ValueError, IndexError):
        return None


def _capture_driver_process_identity(
    proc: subprocess.Popen[str],
) -> _DriverProcessIdentity | None:
    """Capture the detached adapter session before it can be reaped."""
    if not pathlib.Path("/proc/self/stat").exists():
        return None
    stat = _read_driver_proc_stat(proc.pid)
    if stat is None:
        return None
    try:
        pgid = os.getpgid(proc.pid)
        session_id = os.getsid(proc.pid)
    except OSError:
        return None
    if (
        pgid != stat.pgrp
        or session_id != stat.session
        or proc.pid != pgid
        or proc.pid != session_id
        or pgid in {0, 1, os.getpgrp()}
    ):
        return None
    return _DriverProcessIdentity(
        leader_pid=proc.pid,
        pgid=pgid,
        session=session_id,
        leader_start_ticks=stat.start_ticks,
    )


def _driver_session_members(
    identity: _DriverProcessIdentity,
) -> tuple[_DriverProcStat, ...] | None:
    """Return an anchored session snapshot, or None if identity was lost."""
    leader_before = _read_driver_proc_stat(identity.leader_pid)
    if (
        leader_before is None
        or leader_before.start_ticks != identity.leader_start_ticks
        or leader_before.pgrp != identity.pgid
        or leader_before.session != identity.session
    ):
        return None

    members: list[_DriverProcStat] = []
    try:
        proc_entries = pathlib.Path("/proc").iterdir()
        for entry in proc_entries:
            if not entry.name.isdecimal():
                continue
            stat = _read_driver_proc_stat(int(entry.name))
            if stat is not None and stat.session == identity.session:
                members.append(stat)
    except OSError:
        return None

    leader_after = _read_driver_proc_stat(identity.leader_pid)
    if (
        leader_after is None
        or leader_after.start_ticks != identity.leader_start_ticks
        or leader_after.pgrp != identity.pgid
        or leader_after.session != identity.session
    ):
        return None
    return tuple(sorted(members, key=lambda item: item.pid))


def _signal_driver_session(
    identity: _DriverProcessIdentity,
    signal_number: int,
) -> bool:
    """Signal only the still-anchored adapter session and its descendants."""
    members = _driver_session_members(identity)
    if members is None:
        return False

    if any(
        member.state != "Z" and member.pgrp == identity.pgid
        for member in members
    ):
        try:
            os.killpg(identity.pgid, signal_number)
        except OSError:
            pass

    # A descendant may create another process group while staying in the
    # adapter session. A pidfd plus a second stat check avoids PID-reuse races
    # while covering that case without touching BrowserManager's session.
    for member in members:
        if member.state == "Z":
            continue
        try:
            pidfd = os.pidfd_open(member.pid)
        except (AttributeError, OSError):
            pidfd = None
        if pidfd is None:
            # Never fall back to os.kill(pid): the process can exit and its PID
            # can be reused between the stat check and the signal. Members in
            # the original group were already covered by the anchored killpg;
            # a moved descendant without pidfd support makes verification fail
            # closed instead of risking an unrelated process.
            continue
        try:
            current = _read_driver_proc_stat(member.pid)
            if current == member:
                signal.pidfd_send_signal(pidfd, signal_number)
        except (AttributeError, OSError):
            pass
        finally:
            os.close(pidfd)
    return True


def _driver_session_is_clean(identity: _DriverProcessIdentity) -> bool:
    members = _driver_session_members(identity)
    return (
        members is not None
        and len(members) == 1
        and members[0].pid == identity.leader_pid
        and members[0].state == "Z"
    )


def _driver_leader_is_terminal(identity: _DriverProcessIdentity) -> bool:
    result = os.waitid(
        os.P_PID,
        identity.leader_pid,
        os.WEXITED | os.WNOHANG | os.WNOWAIT,
    )
    return result is not None and result.si_pid == identity.leader_pid


def _wait_driver_without_reaping(
    identity: _DriverProcessIdentity,
    deadline: float,
) -> bool:
    """Wait for terminal state while retaining the leader as a PID anchor."""
    while True:
        if _driver_leader_is_terminal(identity):
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.01, remaining))


class _DriverPipeCapture:
    """Drain adapter pipes without calling Popen.wait()/communicate()."""

    def __init__(
        self,
        proc: subprocess.Popen[str],
        stdin_text: str | None,
    ):
        self._chunks: dict[str, list[bytes]] = {
            "stdout": [],
            "stderr": [],
        }
        self._encodings: dict[str, str] = {}
        self._threads: list[threading.Thread] = []

        for name in ("stdout", "stderr"):
            stream = getattr(proc, name)
            if stream is None:
                continue
            self._encodings[name] = getattr(stream, "encoding", None) or "utf-8"
            thread = threading.Thread(
                target=self._drain,
                args=(name, stream),
                name=f"selenium-adapter-{name}",
                daemon=True,
            )
            thread.start()
            self._threads.append(thread)

        if proc.stdin is not None:
            thread = threading.Thread(
                target=self._write_stdin,
                args=(proc.stdin, stdin_text or ""),
                name="selenium-adapter-stdin",
                daemon=True,
            )
            thread.start()
            self._threads.append(thread)

    def _drain(self, name: str, stream: Any) -> None:
        raw_stream = getattr(stream, "buffer", stream)
        read = getattr(raw_stream, "read1", raw_stream.read)
        try:
            while True:
                chunk = read(65536)
                if not chunk:
                    break
                if isinstance(chunk, str):
                    chunk = chunk.encode(self._encodings[name], errors="replace")
                self._chunks[name].append(chunk)
        except (OSError, ValueError):
            pass
        finally:
            try:
                stream.close()
            except Exception:
                pass

    @staticmethod
    def _write_stdin(stream: Any, stdin_text: str) -> None:
        try:
            stream.write(stdin_text)
            stream.flush()
        except (BrokenPipeError, OSError, ValueError):
            pass
        finally:
            try:
                stream.close()
            except Exception:
                pass

    def finish(self, deadline: float) -> tuple[str, str]:
        for thread in self._threads:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        return tuple(
            b"".join(self._chunks[name]).decode(
                self._encodings.get(name, "utf-8"),
                errors="replace",
            )
            for name in ("stdout", "stderr")
        )  # type: ignore[return-value]


def _cleanup_unidentified_selenium_process_group(
    proc: subprocess.Popen[str],
    hard_deadline: float,
) -> bool:
    """Fail closed if the detached adapter's Linux identity cannot be pinned."""
    group_is_safe = False
    try:
        group_is_safe = (
            os.getpgid(proc.pid) == proc.pid
            and os.getsid(proc.pid) == proc.pid
            and proc.pid not in {0, 1, os.getpgrp()}
        )
    except OSError:
        pass

    if group_is_safe:
        # Popen(start_new_session=True) created this still-unreaped leader, so
        # the numeric group cannot be reused before this one signal.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
    else:
        try:
            pidfd = os.pidfd_open(proc.pid)
        except (AttributeError, OSError):
            pidfd = None
        if pidfd is not None:
            try:
                signal.pidfd_send_signal(pidfd, signal.SIGKILL)
            except (AttributeError, OSError):
                pass
            finally:
                os.close(pidfd)

    while time.monotonic() < hard_deadline:
        try:
            result = os.waitid(
                os.P_PID,
                proc.pid,
                os.WEXITED | os.WNOHANG | os.WNOWAIT,
            )
        except ChildProcessError:
            result = None
            break
        if result is not None and result.si_pid == proc.pid:
            break
        time.sleep(min(0.01, max(0.0, hard_deadline - time.monotonic())))

    try:
        proc.wait(timeout=max(0.0, hard_deadline - time.monotonic()))
    except (OSError, subprocess.TimeoutExpired):
        pass

    if not group_is_safe:
        return proc.returncode is not None
    while time.monotonic() < hard_deadline:
        try:
            os.killpg(proc.pid, 0)
        except ProcessLookupError:
            return proc.returncode is not None
        except PermissionError:
            pass
        except OSError:
            return proc.returncode is not None
        time.sleep(min(0.01, max(0.0, hard_deadline - time.monotonic())))
    return False


def _cleanup_selenium_process_group(
    proc: subprocess.Popen[str],
    identity: _DriverProcessIdentity,
    hard_deadline: float,
    grace_s: float = 2.0,
) -> bool:
    """TERM, escalate, verify the session, and only then reap its leader."""
    if proc.returncode is not None:
        return True

    clean = _driver_session_is_clean(identity)
    if not clean:
        remaining = max(0.0, hard_deadline - time.monotonic())
        kill_reserve = min(0.5, remaining / 2)
        term_deadline = min(
            time.monotonic() + grace_s,
            hard_deadline - kill_reserve,
        )
        _signal_driver_session(identity, signal.SIGTERM)
        while time.monotonic() < term_deadline:
            if _driver_session_is_clean(identity):
                clean = True
                break
            time.sleep(min(0.02, max(0.0, term_deadline - time.monotonic())))

    if not clean:
        _signal_driver_session(identity, signal.SIGKILL)
        while time.monotonic() < hard_deadline:
            if _driver_session_is_clean(identity):
                clean = True
                break
            _signal_driver_session(identity, signal.SIGKILL)
            time.sleep(min(0.02, max(0.0, hard_deadline - time.monotonic())))

    if _driver_leader_is_terminal(identity):
        proc.wait(timeout=max(0.0, hard_deadline - time.monotonic()))
    return clean


def run_driver_subprocess(
    task: ResolvedTask,
    argv: list[str],
    env: dict[str, str],
    artifact_dir: pathlib.Path,
    fixture_base_url: str,
    run_id: str,
    engine: str,
    attempt: int,
    seed: str,
    session: str,
    stdin_text: str | None = None,
) -> dict[str, Any]:
    timeout_s = task.task.get("timeouts", {}).get("task_ms", 30000) / 1000
    hard_kill_s = task.task.get("timeouts", {}).get(
        "hard_kill_ms",
        max(45000, int(timeout_s * 1000)),
    ) / 1000
    started = time.monotonic()
    hard_deadline = started + hard_kill_s
    is_selenium = task.driver.get("kind") == "webdriver_selenium"
    proc = subprocess.Popen(
        argv,
        text=True,
        stdin=subprocess.PIPE if stdin_text is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        start_new_session=True,
    )
    identity = (
        _capture_driver_process_identity(proc)
        if is_selenium
        else None
    )
    logs_written = False
    cleanup_error: BaseException | None = None
    cleanup_complete = True
    if is_selenium:
        capture = _DriverPipeCapture(proc, stdin_text)
        if identity is None:
            try:
                cleanup_complete = (
                    _cleanup_unidentified_selenium_process_group(
                        proc,
                        hard_deadline,
                    )
                )
            except BaseException as exc:
                cleanup_error = exc
                cleanup_complete = False
                if time.monotonic() < hard_deadline:
                    try:
                        cleanup_complete = (
                            _cleanup_unidentified_selenium_process_group(
                                proc,
                                hard_deadline,
                            )
                        )
                    except BaseException:
                        pass
            stdout_text, stderr_text = capture.finish(hard_deadline)
            (artifact_dir / "stdout.log").write_text(
                stdout_text,
                encoding="utf-8",
            )
            (artifact_dir / "stderr.log").write_text(
                stderr_text,
                encoding="utf-8",
            )
            if cleanup_error is not None and not isinstance(
                cleanup_error,
                Exception,
            ):
                raise cleanup_error
            detail = (
                "Selenium adapter process identity could not be captured; "
                "the attempt was aborted before execution"
            )
            if not cleanup_complete:
                detail += " and cleanup could not be confirmed"
            return {
                "ok": False,
                "status": "infra",
                "failure": failure_obj("script_error", detail),
                "answer": None,
                "observations": {
                    "selenium_cleanup": {
                        "confirmed": cleanup_complete,
                        "diagnostic": (
                            str(cleanup_error)
                            if cleanup_error is not None
                            else None
                        ),
                    }
                },
                "grader": {
                    "ok": False,
                    "checks": [],
                    "failure": failure_obj(
                        "script_error",
                        "Selenium adapter containment setup failed",
                    ),
                },
                "metrics": {
                    "cdp_call_count": 0,
                    "cdp_error_count": 0,
                    "ws_disconnect_count": 0,
                },
            }

        original_error: BaseException | None = None
        timed_out = False
        try:
            task_deadline = min(started + timeout_s, hard_deadline)
            timed_out = not _wait_driver_without_reaping(
                identity,
                task_deadline,
            )
        except BaseException as exc:
            original_error = exc

        cleanup_complete = False
        try:
            cleanup_complete = _cleanup_selenium_process_group(
                proc,
                identity,
                hard_deadline,
            )
        except BaseException as exc:
            cleanup_error = exc
            if time.monotonic() < hard_deadline:
                try:
                    cleanup_complete = _cleanup_selenium_process_group(
                        proc,
                        identity,
                        hard_deadline,
                    )
                except BaseException:
                    pass

        stdout_text, stderr_text = capture.finish(hard_deadline)
        (artifact_dir / "stdout.log").write_text(
            stdout_text,
            encoding="utf-8",
        )
        (artifact_dir / "stderr.log").write_text(
            stderr_text,
            encoding="utf-8",
        )
        logs_written = True
        if original_error is not None:
            if (
                cleanup_error is not None or not cleanup_complete
            ) and hasattr(original_error, "add_note"):
                original_error.add_note(
                    "Selenium cleanup: "
                    + (
                        str(cleanup_error)
                        if cleanup_error is not None
                        else "process-group cleanup was not confirmed"
                    )
                )
            raise original_error
        if timed_out:
            timeout_error = subprocess.TimeoutExpired(
                argv,
                timeout_s,
                output=stdout_text,
                stderr=stderr_text,
            )
            timeout_error.process_diagnostic = process_diagnostic(
                "driver",
                proc,
                state="timed_out",
            )
            if (
                cleanup_error is not None or not cleanup_complete
            ) and hasattr(timeout_error, "add_note"):
                timeout_error.add_note(
                    "Selenium cleanup: "
                    + (
                        str(cleanup_error)
                        if cleanup_error is not None
                        else "process-group cleanup was not confirmed"
                    )
                )
            raise timeout_error from None
        if cleanup_error is not None and not isinstance(
            cleanup_error,
            Exception,
        ):
            raise cleanup_error
    else:
        try:
            stdout_text, stderr_text = proc.communicate(
                input=stdin_text,
                timeout=timeout_s,
            )
        except subprocess.TimeoutExpired as exc:
            try:
                stdout_text, stderr_text = _terminate_driver_process_group(proc)
            except BaseException:
                stdout_text = stderr_text = None
            timeout_error = subprocess.TimeoutExpired(
                argv,
                timeout_s,
                output=stdout_text if stdout_text is not None else exc.stdout,
                stderr=stderr_text if stderr_text is not None else exc.stderr,
            )
            timeout_error.process_diagnostic = process_diagnostic(
                "driver",
                proc,
                state="timed_out",
            )
            raise timeout_error from None
        except BaseException:
            try:
                _terminate_driver_process_group(proc)
            except BaseException:
                pass
            raise
    returncode = proc.returncode
    if not logs_written:
        (artifact_dir / "stdout.log").write_text(stdout_text, encoding="utf-8")
        (artifact_dir / "stderr.log").write_text(stderr_text, encoding="utf-8")
    cleanup_diagnostic = None
    if is_selenium and (cleanup_error is not None or not cleanup_complete):
        cleanup_diagnostic = {
            "confirmed": cleanup_complete,
            "diagnostic": (
                str(cleanup_error)
                if cleanup_error is not None
                else "process-group cleanup was not confirmed"
            ),
        }
    if returncode is not None and returncode != 0:
        try:
            nonzero_output = json.loads(stdout_text.strip() or "{}")
        except json.JSONDecodeError:
            nonzero_output = {}
        if not isinstance(nonzero_output, dict):
            nonzero_output = {}
        nonzero_observations = nonzero_output.get("observations") or {}
        if not isinstance(nonzero_observations, dict):
            nonzero_observations = {}
        if cleanup_diagnostic is not None:
            nonzero_observations = dict(nonzero_observations)
            nonzero_observations["selenium_cleanup"] = cleanup_diagnostic
        return {
            "ok": False,
            "status": "infra",
            "failure": failure_obj(
                "script_error",
                f"script exited with code {returncode}",
                origin="driver_process",
                process=process_diagnostic("driver", returncode=returncode),
            ),
            "answer": nonzero_output.get("answer"),
            "observations": nonzero_observations,
            "grader": {
                "ok": False,
                "checks": [],
                "failure": failure_obj(
                    "script_error",
                    "script process failed",
                    origin="driver_process",
                    process=process_diagnostic("driver", returncode=returncode),
                ),
            },
            "metrics": nonzero_output.get("metrics") or {
                "cdp_call_count": 0,
                "cdp_error_count": 0,
                "ws_disconnect_count": 0,
            },
        }
    if is_selenium and returncode is None:
        return {
            "ok": False,
            "status": "infra",
            "failure": failure_obj(
                "script_error",
                "Selenium adapter process-group cleanup was not confirmed",
            ),
            "answer": None,
            "observations": {
                "selenium_cleanup": cleanup_diagnostic
                or {
                    "confirmed": False,
                    "diagnostic": "adapter return code is unavailable",
                }
            },
            "grader": {
                "ok": False,
                "checks": [],
                "failure": failure_obj(
                    "script_error",
                    "Selenium adapter cleanup failed",
                ),
            },
            "metrics": {
                "cdp_call_count": 0,
                "cdp_error_count": 0,
                "ws_disconnect_count": 0,
            },
        }
    try:
        output = json.loads(stdout_text.strip() or "{}")
    except json.JSONDecodeError as exc:
        return {
            "ok": False,
            "status": "infra",
            "failure": failure_obj("script_error", f"script stdout is not a single JSON object: {exc}"),
            "answer": None,
            "observations": (
                {"selenium_cleanup": cleanup_diagnostic}
                if cleanup_diagnostic is not None
                else {}
            ),
            "grader": {"ok": False, "checks": [], "failure": failure_obj("script_error", "invalid script stdout")},
            "metrics": {"cdp_call_count": 0, "cdp_error_count": 0, "ws_disconnect_count": 0},
        }
    if is_selenium and not isinstance(output, dict):
        return {
            "ok": False,
            "status": "infra",
            "failure": failure_obj(
                "script_error",
                "script stdout JSON must be an object",
            ),
            "answer": None,
            "observations": (
                {"selenium_cleanup": cleanup_diagnostic}
                if cleanup_diagnostic is not None
                else {}
            ),
            "grader": {
                "ok": False,
                "checks": [],
                "failure": failure_obj(
                    "script_error",
                    "invalid script stdout",
                ),
            },
            "metrics": {
                "cdp_call_count": 0,
                "cdp_error_count": 0,
                "ws_disconnect_count": 0,
            },
        }
    metrics = output.get("metrics") or {}
    if not output.get("ok"):
        err = output.get("error") or {}
        klass = "engine_unsupported" if err.get("class") == "engine_unsupported" else "script_error"
        status = "unsupported" if klass == "engine_unsupported" else "infra"
        failure_observations = output.get("observations") or {}
        if is_selenium and not isinstance(failure_observations, dict):
            failure_observations = {}
        if cleanup_diagnostic is not None:
            failure_observations = dict(failure_observations)
            failure_observations["selenium_cleanup"] = cleanup_diagnostic
        return {
            "ok": False,
            "status": status,
            "failure": failure_obj(klass, str(err.get("message") or "script reported failure")),
            "answer": output.get("answer"),
            "observations": failure_observations,
            "grader": {"ok": False, "checks": [], "failure": failure_obj(klass, str(err.get("message") or "script reported failure"))},
            "metrics": metrics,
        }
    if is_selenium and not cleanup_complete:
        cleanup_observations = output.get("observations") or {}
        if not isinstance(cleanup_observations, dict):
            cleanup_observations = {}
        cleanup_observations = dict(cleanup_observations)
        cleanup_observations["selenium_cleanup"] = cleanup_diagnostic
        return {
            "ok": False,
            "status": "infra",
            "failure": failure_obj(
                "script_error",
                "Selenium adapter process-group cleanup was not confirmed",
            ),
            "answer": output.get("answer"),
            "observations": cleanup_observations,
            "grader": {
                "ok": False,
                "checks": [],
                "failure": failure_obj(
                    "script_error",
                    "Selenium adapter cleanup failed",
                ),
            },
            "metrics": metrics,
        }

    observations = output.get("observations") or {}
    if task.grader.get("kind") == "server_side":
        grader_payload = {
            "run_id": run_id,
            "task_id": task.task_id,
            "attempt": attempt,
            "engine": engine,
            "seed": seed,
            "session": session,
            "answer": output.get("answer"),
            "observations": observations,
        }
        grader = http_json(fixture_base_url + task.grader["endpoint"], timeout=5.0, method="POST", data=grader_payload)
        # L2 workflow boundary: the server grades the final answer, the
        # driver-declared checks (e.g. step_ok) prove the required steps
        # actually completed — both gates are part of the L2 verdict. L1
        # scenario rows keep their historical server-grade-only semantics.
        driver_checks = [
            check
            for check in (observations.get("checks") or [])
            if isinstance(check, dict)
        ] if task.layer == "L2" else []
        if driver_checks:
            grader = dict(grader) if isinstance(grader, dict) else {"ok": False, "checks": []}
            grader["checks"] = list(grader.get("checks") or []) + driver_checks
            if any(check.get("status") != "pass" for check in driver_checks):
                grader["ok"] = False
                if not grader.get("failure"):
                    grader["failure"] = failure_obj(
                        str(observations.get("failure_class") or "cdp_semantic"),
                        "driver-side required-step checks failed",
                    )
    else:
        # Script-graded (inline_assertions): the driver script already ran its
        # checks and reports them under observations.checks.
        checks = observations.get("checks") or []
        graded_ok = bool(checks) and all(check.get("status") == "pass" for check in checks)
        grader = {
            "ok": graded_ok,
            "checks": checks,
            "failure": None
            if graded_ok
            else failure_obj(
                str(observations.get("failure_class") or "cdp_semantic"),
                "script-reported checks failed" if checks else "script reported no checks",
            ),
        }
    return {
        "ok": bool(grader.get("ok")),
        "answer": output.get("answer"),
        "observations": observations,
        "grader": grader,
        "metrics": metrics,
    }


def run_driver_attempt(
    run_dir: pathlib.Path,
    results_path: pathlib.Path,
    run_id: str,
    task: ResolvedTask,
    engine: str,
    attempt: int,
    seed: str,
    browser: BrowserProcess,
    gate_payload: dict[str, Any],
    score_eligible: bool,
    fixture_base_url: str | None,
    score_mode: str = "baseline_checked",
    resource_runtime: ResourceRuntime | None = None,
    fixture_server: FixtureServer | None = None,
    scenario_binding: dict[str, Any] | None = None,
) -> dict[str, Any]:
    tmp_dir, final_dir, artifact_rel = artifact_paths(run_dir, task, engine, attempt)
    if final_dir.exists():
        raise BenchError(f"artifact directory already exists; refusing to overwrite: {final_dir}")
    tmp_dir.mkdir(parents=True, exist_ok=False)
    driver_kind = task.driver.get("kind")
    unavailable_reason = (
        str(scenario_binding.get("unavailable_reason") or "")
        if isinstance(scenario_binding, dict)
        else ""
    )
    # Raw-CDP attempts close the target they own, and node probes have their
    # own lifecycle guard in run_node_cdp_probe_driver.  Frameworks and
    # scenario adapters need an outer guard because a hard subprocess timeout
    # prevents their in-process finally blocks from running.
    guard_outer_targets = (
        not unavailable_reason
        and (
            driver_kind in FRAMEWORK_DRIVER_KINDS
            or driver_kind in SCENARIO_ADAPTER_KINDS
        )
    )
    outer_before_targets: set[str] | None = None
    outer_target_pre_error: str | None = None
    if guard_outer_targets:
        try:
            outer_before_targets = browser_page_target_ids(browser)
        except Exception as exc:
            outer_target_pre_error = str(exc)
    resource_collection_started = time.perf_counter()
    resource_sampler: resource_metrics.EngineProcessSampler | None = None
    resource_sampler_error: str | None = None
    traffic_token: str | None = None
    traffic_payload: dict[str, Any] | None = None
    if resource_runtime is not None:
        session = f"{run_id}-{task.task_id}-{engine}-{attempt}-{seed}"
        traffic_token = f"{session}-{time.time_ns()}"
        resource_runtime.traffic.begin_attempt(traffic_token, session)
        try:
            resource_sampler = resource_metrics.EngineProcessSampler(
                browser.process.pid,
                browser.cgroup,
                resource_runtime.sample_interval_ms,
            )
            resource_sampler.start()
        except Exception as exc:
            # Resource collection is deliberately non-fatal to the functional
            # attempt; the missing measurement is explicit in the result.
            resource_sampler = None
            resource_sampler_error = f"{type(exc).__name__}: {exc}"
    # Monotonic: a wall-clock step mid-run (NTP) would otherwise corrupt both the
    # duration and the attempt ordering the run_context below is recorded for.
    start = time.monotonic()
    result = attempt_base_result(run_id, task, engine, attempt, seed, gate_payload, artifact_rel)
    result["engine_provenance"] = engine_provenance(browser)
    # Enough to replay a load-only non-pass: which worker and which engine
    # process it ran on, what that worker ran immediately before on the same
    # process, and where the attempt sits on the run's timeline. The end of the
    # attempt is `started_monotonic_ms + duration_ms`, which every exit path
    # already fills in.
    result["run_context"] = {
        "worker_slot": browser.worker_slot,
        "browser_pid": browser.process.pid if browser.process is not None else None,
        "browser_generation": browser.generation,
        "prev_task_id": browser.prev_task_id,
        "started_monotonic_ms": int(start * 1000),
    }
    grader = {"ok": False, "checks": [], "failure": failure_obj("infra", "driver did not run")}
    stdout_text = ""
    stderr_text = ""
    observed_engine_process: dict[str, Any] | None = None
    caught_exception: BaseException | None = None
    try:
        if unavailable_reason:
            incompatibility = failure_obj(
                "engine_unsupported",
                unavailable_reason,
                origin="binding_catalog",
            )
            binding_observation = dict(scenario_binding or {})
            binding_observation["verified"] = False
            result["binding"] = binding_observation
            driver_out = {
                "ok": False,
                "status": "unsupported",
                "failure": incompatibility,
                "answer": None,
                "observations": {
                    "binding": binding_observation,
                    "failure_class": "engine_unsupported",
                },
                "grader": {
                    "ok": False,
                    "checks": [],
                    "failure": incompatibility,
                },
                "metrics": {
                    "cdp_call_count": 0,
                    "cdp_error_count": 0,
                    "ws_disconnect_count": 0,
                },
            }
            stdout_text = json.dumps(driver_out, sort_keys=True) + "\n"
        elif driver_kind == "raw_cdp":
            driver_out = run_raw_cdp_driver(task, browser, tmp_dir, fixture_base_url)
            stdout_text = "raw_cdp completed\n"
        elif driver_kind == "node_cdp_probe":
            if not fixture_base_url:
                raise BenchError("self-hosted fixture server is not running")
            driver_out = run_node_cdp_probe_driver(task, browser, tmp_dir, fixture_base_url, run_id, engine, attempt, seed)
        elif driver_kind in FRAMEWORK_DRIVER_KINDS:
            if not fixture_base_url:
                raise BenchError("self-hosted fixture server is not running")
            driver_out = run_framework_driver(task, browser, tmp_dir, fixture_base_url, run_id, engine, attempt, seed)
        elif driver_kind in SCENARIO_ADAPTER_KINDS:
            if not fixture_base_url:
                raise BenchError("self-hosted fixture server is not running")
            driver_out = run_scenario_adapter_driver(
                task,
                browser,
                tmp_dir,
                fixture_base_url,
                run_id,
                engine,
                attempt,
                seed,
                runtime_binding=scenario_binding,
            )
        else:
            driver_out = {
                "ok": False,
                "status": "infra",
                "failure": failure_obj("infra", f"driver {driver_kind} is not implemented"),
                "answer": None,
                "observations": {},
                "grader": {"ok": False, "checks": [], "failure": failure_obj("infra", f"driver {driver_kind} is not implemented")},
                "metrics": {},
            }
        grader = driver_out.get("grader") or grader
        failure = driver_out.get("failure")
        status = driver_out.get("status")
        if not status:
            status = "pass" if driver_out.get("ok") else "fail"
        if status == "fail" and grader.get("failure"):
            failure = grader.get("failure")
        elif status == "fail" and not failure:
            failure = failure_obj("cdp_semantic", "grader checks failed")
        elif status == "pass":
            failure = None
        metrics = driver_out.get("metrics") or {}
        result.update(
            {
                "status": status,
                "failure": failure,
                "answer": driver_out.get("answer"),
                "duration_ms": int((time.monotonic() - start) * 1000),
                "cdp_call_count": int(metrics.get("cdp_call_count") or 0),
                "cdp_error_count": int(metrics.get("cdp_error_count") or 0),
                "ws_disconnect_count": int(metrics.get("ws_disconnect_count") or 0),
            }
        )
    except subprocess.TimeoutExpired as exc:
        caught_exception = exc
        stdout_text = exc.stdout or ""
        stderr_text = exc.stderr or ""
        engine_process = process_diagnostic("engine", browser.process)
        observed_engine_process = engine_process
        engine_exited = engine_process["state"] == "exited"
        timeout_process = getattr(
            exc,
            "process_diagnostic",
            process_diagnostic("driver", state="timed_out"),
        )
        result.update(
            {
                "status": "crash" if engine_exited else "timeout",
                "failure": failure_obj(
                    "infra",
                    (
                        f"engine exited while task waited for the driver subprocess: {exc}"
                        if engine_exited
                        else f"task timed out after {exc.timeout}s"
                    ),
                    origin="engine_process" if engine_exited else "task_timeout",
                    process=engine_process if engine_exited else timeout_process,
                ),
                "duration_ms": int((time.monotonic() - start) * 1000),
            }
        )
        grader = {"ok": False, "checks": [], "failure": result["failure"]}
    except TimeoutError as exc:
        caught_exception = exc
        # socket.timeout is an alias/subclass of TimeoutError and must be
        # handled before OSError below.  A command/task deadline while the
        # engine process remains alive is a timeout, not a browser crash.
        engine_process = process_diagnostic("engine", browser.process)
        observed_engine_process = engine_process
        engine_exited = engine_process["state"] == "exited"
        detail = str(exc) or type(exc).__name__
        result.update(
            {
                "status": "crash" if engine_exited else "timeout",
                "failure": failure_obj(
                    "infra",
                    (
                        f"engine exited while the task was waiting: {detail}"
                        if engine_exited
                        else f"task timed out: {detail}"
                    ),
                    origin="engine_process" if engine_exited else "task_timeout",
                    process=engine_process,
                ),
                "duration_ms": int((time.monotonic() - start) * 1000),
            }
        )
        grader = {"ok": False, "checks": [], "failure": result["failure"]}
    except CDPCommandError as exc:
        caught_exception = exc
        status = "unsupported" if is_unsupported_error(exc) else "fail"
        klass = "engine_unsupported" if status == "unsupported" else "cdp_semantic"
        result.update(
            {
                "status": status,
                "failure": failure_obj(klass, str(exc), kernel_workitem=(engine == "moli" and klass != "engine_unsupported")),
                "duration_ms": int((time.monotonic() - start) * 1000),
            }
        )
        grader = {"ok": False, "checks": [], "failure": result["failure"]}
    except ConnectionError as exc:
        caught_exception = exc
        engine_process = process_diagnostic("engine", browser.process)
        observed_engine_process = engine_process
        engine_exited = engine_process["state"] == "exited"
        result.update(
            {
                "status": "crash",
                "failure": failure_obj(
                    "infra",
                    str(exc),
                    origin="engine_process" if engine_exited else "client_transport",
                    process=engine_process,
                ),
                "duration_ms": int((time.monotonic() - start) * 1000),
            }
        )
        grader = {"ok": False, "checks": [], "failure": result["failure"]}
    except OSError as exc:
        caught_exception = exc
        detail = str(exc) or type(exc).__name__
        engine_process = process_diagnostic("engine", browser.process)
        observed_engine_process = engine_process
        if (
            not isinstance(exc, urllib.error.URLError)
            and ETIMEDOUT_ERRNO is not None
            and getattr(exc, "errno", None) == ETIMEDOUT_ERRNO
        ):
            result.update(
                {
                    "status": "timeout",
                    "failure": failure_obj(
                        "infra",
                        f"task timed out: {detail}",
                        origin="task_timeout",
                        process=engine_process,
                    ),
                    "duration_ms": int((time.monotonic() - start) * 1000),
                }
            )
        elif is_socket_transport_os_error(exc):
            result.update(
                {
                    "status": "crash",
                    "failure": failure_obj(
                        "infra",
                        detail,
                        origin="client_transport",
                        process=engine_process,
                    ),
                    "duration_ms": int((time.monotonic() - start) * 1000),
                }
            )
        else:
            # File/artifact/fixture/grader I/O and generic urllib failures do
            # not prove anything about the client/browser transport.
            result.update(
                {
                    "status": "infra",
                    "failure": failure_obj("infra", detail),
                    "duration_ms": int((time.monotonic() - start) * 1000),
                }
            )
        grader = {"ok": False, "checks": [], "failure": result["failure"]}
    except Exception as exc:
        caught_exception = exc
        result.update(
            {
                "status": "infra",
                "failure": failure_obj("infra", str(exc)),
                "duration_ms": int((time.monotonic() - start) * 1000),
            }
        )
        grader = {"ok": False, "checks": [], "failure": result["failure"]}
    finally:
        exception_metrics = getattr(caught_exception, "cdp_metrics", None)
        if isinstance(exception_metrics, dict):
            result.update(
                {
                    "cdp_call_count": int(
                        exception_metrics.get("cdp_call_count") or 0
                    ),
                    "cdp_error_count": int(
                        exception_metrics.get("cdp_error_count") or 0
                    ),
                    "ws_disconnect_count": int(
                        exception_metrics.get("ws_disconnect_count") or 0
                    ),
                }
            )
        # On the subprocess-timeout kill path the captured streams can still
        # be bytes; a bytes payload must not crash the whole run.
        if isinstance(stdout_text, bytes):
            stdout_text = stdout_text.decode("utf-8", errors="replace")
        if isinstance(stderr_text, bytes):
            stderr_text = stderr_text.decode("utf-8", errors="replace")
        if stdout_text and not (tmp_dir / "stdout.log").exists():
            (tmp_dir / "stdout.log").write_text(stdout_text, encoding="utf-8")
        if stderr_text and not (tmp_dir / "stderr.log").exists():
            (tmp_dir / "stderr.log").write_text(stderr_text, encoding="utf-8")
        if guard_outer_targets:
            try:
                target_diagnostic = cleanup_new_page_targets(
                    browser, outer_before_targets
                )
                if outer_target_pre_error:
                    target_diagnostic["errors"].insert(
                        0,
                        "pre-attempt target snapshot failed: "
                        + outer_target_pre_error,
                    )
                write_json(
                    tmp_dir / "target_cleanup.json", target_diagnostic
                )
            except Exception:
                # Lifecycle diagnostics are deliberately non-fatal and must
                # not hide the functional attempt result.
                pass

    if resource_runtime is not None:
        traffic_events: list[dict[str, Any]] = []
        if traffic_token is not None:
            traffic_payload = resource_runtime.traffic.end_attempt(traffic_token)
            traffic_events = list(traffic_payload.pop("events", []))
        if resource_sampler is not None:
            try:
                resource_payload, process_samples = resource_sampler.stop(
                    int(result.get("duration_ms") or 0)
                )
            except Exception as exc:
                resource_sampler_error = f"{type(exc).__name__}: {exc}"
                resource_payload = {
                    "schema": resource_metrics.ENGINE_RESOURCE_SCHEMA,
                    "scope": "engine_scope",
                    "measurement_backend": {
                        "cpu": "unavailable",
                        "memory_pss": "unavailable",
                        "memory_accounting": "unavailable",
                    },
                    "quality_flags": ["sampler_failed"],
                    "unavailable": {"sampler": resource_sampler_error},
                }
                process_samples = []
        else:
            resource_payload = {
                "schema": resource_metrics.ENGINE_RESOURCE_SCHEMA,
                "scope": "engine_scope",
                "measurement_backend": {
                    "cpu": "unavailable",
                    "memory_pss": "unavailable",
                    "memory_accounting": "unavailable",
                },
                "cpu_total_ms": None,
                "cpu_user_ms": None,
                "cpu_system_ms": None,
                "avg_cores": None,
                "pss_baseline_bytes": None,
                "pss_peak_bytes": None,
                "pss_end_bytes": None,
                "pss_peak_delta_bytes": None,
                "process_count_peak": None,
                "sample_interval_ms": resource_runtime.sample_interval_ms,
                "samples_seen": 0,
                "sampler_cpu_ms": None,
                "quality_flags": ["sampler_unavailable"],
                "unavailable": {
                    "sampler": resource_sampler_error or "sampler did not start"
                },
            }
            process_samples = []
        resource_payload["fixture_traffic"] = traffic_payload or {
            "schema": resource_metrics.FIXTURE_TRAFFIC_SCHEMA,
            "available": False,
            "reason": "fixture traffic tracker unavailable",
        }
        resource_payload["control_plane_traffic"] = {
            "available": False,
            "reason": "portable control-plane byte backend is not implemented",
        }
        resource_payload["wire_traffic"] = {
            "available": False,
            "reason": "optional privileged wire-byte backend is not enabled",
        }
        resource_payload["collection_wall_ms"] = round(
            (time.perf_counter() - resource_collection_started) * 1000,
            3,
        )
        resource_payload["engine_cgroup_error"] = browser.cgroup_error
        result["resource"] = resource_payload
        resource_metrics.write_resource_samples(
            tmp_dir / "resource.jsonl",
            process_samples,
            traffic_events,
        )

    # Resource collection is part of the attempt lifecycle and may be the
    # first place an engine exit becomes observable.  Recheck only after it
    # completes, while reusing any earlier terminal diagnostic so one-shot
    # waitid/core-dump evidence is not lost.
    promote_observed_engine_exit(
        result,
        grader,
        browser.process,
        observed_engine_process,
    )

    result["score_included"] = should_include_score(result, task, engine, score_eligible, score_mode)
    if engine == "chrome" and result.get("chrome_gate", {}).get("chrome_attempt_ref"):
        result["chrome_gate"]["status"] = result["status"]
    write_json(tmp_dir / "grader.json", grader)
    write_json(tmp_dir / "run.json", result)
    ensure_profile_files(tmp_dir, task.artifact_profile)
    final_dir.parent.mkdir(parents=True, exist_ok=True)
    tmp_dir.replace(final_dir)
    append_result(results_path, result)
    return result


def run_gate_skip_attempt(
    run_dir: pathlib.Path,
    results_path: pathlib.Path,
    run_id: str,
    task: ResolvedTask,
    engine: str,
    attempt: int,
    seed: str,
    gate_payload: dict[str, Any],
) -> dict[str, Any]:
    tmp_dir, final_dir, artifact_rel = artifact_paths(run_dir, task, engine, attempt)
    if final_dir.exists():
        raise BenchError(f"artifact directory already exists; refusing to overwrite: {final_dir}")
    tmp_dir.mkdir(parents=True, exist_ok=False)
    result = attempt_base_result(run_id, task, engine, attempt, seed, gate_payload, artifact_rel)
    result.update(
        {
            "status": "chrome_gate_fail",
            "score_included": False,
            "failure": failure_obj("infra", "Chrome baseline did not pass; candidate engine was not executed"),
            "duration_ms": 0,
        }
    )
    grader = {
        "ok": False,
        "checks": [],
        "failure": result["failure"],
        "note": "candidate attempt skipped because Chrome baseline failed",
    }
    write_json(tmp_dir / "grader.json", grader)
    write_json(tmp_dir / "run.json", result)
    ensure_profile_files(tmp_dir, task.artifact_profile)
    final_dir.parent.mkdir(parents=True, exist_ok=True)
    tmp_dir.replace(final_dir)
    append_result(results_path, result)
    return result


def scored_engines(score_mode: str) -> tuple[str, ...]:
    # Default: only native candidates are scored; Chrome may still run as a
    # selected engine or an explicitly requested gate.
    # Independent: every engine in the current roster is scored on its own
    # merits, with no Chrome oracle.
    return ENGINE_ORDER if score_mode == "independent" else NATIVE_CANDIDATES


def display_status(status: str) -> str:
    if status == "chrome_gate_fail":
        return "chrome_baseline_fail"
    return status


def should_include_score(
    result: dict[str, Any],
    task: ResolvedTask,
    engine: str,
    score_eligible: bool,
    score_mode: str = "baseline_checked",
) -> bool:
    if not score_eligible:
        return False
    if engine not in scored_engines(score_mode):
        return False
    if result["status"] in {"chrome_gate_fail", "infra"}:
        return False
    if result.get("fallback_used"):
        return False
    semantic_role = (task.semantic_capability or {}).get("role")
    if semantic_role in {"driver_cross_check", "diagnostic"}:
        return False
    # Diagnostic tasks are evidence for every engine, score units for none. A
    # task the Chrome oracle cannot validate must not remain scored only for
    # candidate engines.
    if "purpose.diagnostic" in (task.tags or []):
        return False
    return True


class RunReporter:
    def __init__(
        self,
        total_rows: int,
        *,
        quiet: bool = False,
        progress: bool = True,
        color_mode: str = "auto",
        stream: Any | None = None,
    ) -> None:
        self.total_rows = max(0, int(total_rows))
        self.quiet = quiet
        self.progress = progress
        self.color_mode = color_mode if color_mode in COLOR_MODES else "auto"
        self.stream = stream or sys.stdout
        self.interactive = bool(progress and hasattr(self.stream, "isatty") and self.stream.isatty())
        self.color = self._resolve_color()
        self.started_at = time.time()
        self.completed_rows = 0
        self.status_counts: dict[str, int] = {}
        self._last_line_len = 0
        self._log_interval = max(1, self.total_rows // 20) if self.total_rows else 1
        self._next_log_at = self._log_interval

    def _resolve_color(self) -> bool:
        if self.color_mode == "always":
            return True
        if self.color_mode == "never" or os.environ.get("NO_COLOR"):
            return False
        return bool(hasattr(self.stream, "isatty") and self.stream.isatty())

    def paint(self, text: str, color: str) -> str:
        if not self.color:
            return text
        code = ANSI_COLORS.get(color)
        return f"{code}{text}{ANSI_RESET}" if code else text

    def paint_status(self, text: str, status: str) -> str:
        return self.paint(text, STATUS_COLORS.get(status, "bold"))

    def write_digest(self, lines: list[str]) -> None:
        if self.quiet:
            return
        for index, line in enumerate(lines):
            if index == 0:
                line = self.paint(line, "bold")
            elif index == 1:
                line = self.paint(line, "dim")
            elif line.startswith(("run_id", "bench", "manifest", "dataset", "subsets", "engines", "matrix", "baseline", "score", "fixtures", "artifacts")):
                head, sep, tail = line.partition(" ")
                line = self.paint(head, "cyan") + sep + tail
            print(line, file=self.stream, flush=True)

    def phase(self, message: str) -> None:
        if self.quiet:
            return
        self._clear_progress_line()
        print(f"{self.paint('>', 'blue')} {message}", file=self.stream, flush=True)

    def advance(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        self.completed_rows += len(rows)
        for row in rows:
            status = str(row.get("status") or "infra")
            self.status_counts[status] = self.status_counts.get(status, 0) + 1
        if self.quiet or not self.progress:
            return
        if self.interactive:
            self._render_progress_line()
            return
        if self.completed_rows >= self._next_log_at or self.completed_rows >= self.total_rows:
            print(f"progress {self._progress_text()}", file=self.stream, flush=True)
            while self._next_log_at <= self.completed_rows:
                self._next_log_at += self._log_interval

    def finish(self) -> None:
        if self.quiet:
            return
        if self.progress and self.interactive:
            self._render_progress_line()
            print(file=self.stream, flush=True)
            self._last_line_len = 0
        elapsed = format_elapsed(time.time() - self.started_at)
        label = self.paint("completed", "green") if self.completed_rows == self.total_rows else self.paint("completed", "yellow")
        print(
            f"{label} {self.completed_rows}/{self.total_rows} result rows in {elapsed}; {self._stats_text()}",
            file=self.stream,
            flush=True,
        )

    def _clear_progress_line(self) -> None:
        if self.interactive and self._last_line_len:
            self.stream.write("\r" + (" " * self._last_line_len) + "\r")
            self.stream.flush()
            self._last_line_len = 0

    def _render_progress_line(self) -> None:
        line = self._progress_text()
        width = shutil.get_terminal_size((100, 20)).columns
        if visible_len(line) > width:
            line = self._progress_text(compact=True)
        if visible_len(line) > width:
            line = strip_ansi(line)[: max(0, width - 1)]
        line_len = visible_len(line)
        padding = " " * max(0, self._last_line_len - line_len)
        self.stream.write("\r" + line + padding)
        self.stream.flush()
        self._last_line_len = line_len

    def _progress_text(self, compact: bool = False) -> str:
        total = max(1, self.total_rows)
        pct = (self.completed_rows / total) * 100
        elapsed = format_elapsed(time.time() - self.started_at)
        if compact:
            return f"{self.completed_rows}/{self.total_rows} {pct:5.1f}% | {self._stats_text(compact=True)} | {elapsed}"
        width = shutil.get_terminal_size((100, 20)).columns
        stats = self._stats_text(compact=True)
        suffix = f" {self.completed_rows}/{self.total_rows} {pct:5.1f}% | {stats} | {elapsed}"
        bar_width = max(10, min(34, width - visible_len(suffix) - 4))
        filled = int(bar_width * min(self.completed_rows, total) / total)
        bar = self.paint("#" * filled, "green") + self.paint("-" * (bar_width - filled), "dim")
        return f"[{bar}]{suffix}"

    def _stats_text(self, compact: bool = False) -> str:
        parts: list[str] = []
        for status in PROGRESS_STATUS_ORDER:
            count = self.status_counts.get(status, 0)
            if not count:
                continue
            label = PROGRESS_STATUS_LABELS.get(status, status)
            if compact:
                label = {
                    "unsupported": "unsup",
                    "timeout": "tout",
                    "chrome_gate_fail": "baseline_skip",
                }.get(status, label)
            parts.append(self.paint_status(f"{label}={count}", status))
        for status, count in sorted(self.status_counts.items()):
            if status not in PROGRESS_STATUS_ORDER and count:
                parts.append(self.paint(f"{status}={count}", "yellow"))
        return " ".join(parts) if parts else self.paint("running", "dim")


def build_run_digest_lines(
    *,
    suite: dict[str, Any],
    manifest_path: pathlib.Path,
    tasks: list[ResolvedTask],
    selected_engines: list[str],
    run_id: str,
    run_dir: pathlib.Path,
    k_runs: int,
    jobs: int,
    score_mode: str,
    score_eligible: bool,
    score_reasons: list[str],
    chrome_gate: str | None,
    fixture_base_url: str | None,
    resource_profile: str = "off",
    host_telemetry_enabled: bool = True,
) -> list[str]:
    total_enabled = enabled_task_count(manifest_path)
    selected_count = len(tasks)
    enabled_suffix = f" / enabled={total_enabled}" if total_enabled is not None else ""
    score_text = "eligible" if score_eligible else "not eligible: " + ("; ".join(score_reasons) or "unspecified")
    host_telemetry_mode = "on" if host_telemetry_enabled else "off"
    return [
        "Agent Browser Bench run",
        "-----------------------",
        f"run_id      {run_id}",
        f"bench       {suite.get('bench_id')} @ {suite.get('bench_version')}",
        f"manifest    {rel_to_repo(manifest_path)} sha12={sha256_file(manifest_path)[:12]}",
        f"dataset     selected_tasks={selected_count}{enabled_suffix}; layers {format_count_map(count_tasks_by(tasks, 'layer'))}",
        f"subsets     {format_count_map(count_tasks_by(tasks, 'subset_id'))}",
        f"engines     {', '.join(selected_engines)}",
        f"matrix      k={k_runs} x engines={len(selected_engines)} -> {expected_result_rows(tasks, k_runs, selected_engines)} result rows; jobs={jobs}",
        f"baseline    {format_count_map(gate_policy_counts(tasks, chrome_gate, suite))}; score_mode={score_mode}",
        f"score       {score_text}",
        f"fixtures    {fixture_base_url or 'not required'}",
        f"resources   host={host_telemetry_mode}; engine_profile={resource_profile}",
        f"artifacts   {rel_to_repo(run_dir)}",
    ]


def run_required_gate(
    run_dir: pathlib.Path,
    results_path: pathlib.Path,
    run_id: str,
    task: ResolvedTask,
    attempt: int,
    seed: str,
    chrome_browser: BrowserProcess,
    score_eligible: bool,
    fixture_base_url: str | None,
    resource_runtime: ResourceRuntime | None = None,
    fixture_server: FixtureServer | None = None,
    scenario_binding: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    ref = f"chrome:{task.task_id}:{attempt}"
    gate_payload = {"required": True, "status": "running", "chrome_attempt_ref": ref}
    chrome_result = run_driver_attempt(
        run_dir,
        results_path,
        run_id,
        task,
        "chrome",
        attempt,
        seed,
        chrome_browser,
        gate_payload,
        score_eligible=False,
        fixture_base_url=fixture_base_url,
        resource_runtime=resource_runtime,
        fixture_server=fixture_server,
        scenario_binding=scenario_binding,
    )
    return {"required": True, "status": chrome_result["status"], "chrome_attempt_ref": ref}, chrome_result


def run_attempts(args: argparse.Namespace, suite: dict[str, Any], tasks: list[ResolvedTask]) -> pathlib.Path:
    selected_engines = parse_engines(args.engines)
    jobs = max(1, int(getattr(args, "jobs", 1) or 1))
    score_mode = getattr(args, "score_mode", "baseline_checked") or "baseline_checked"
    # In independent mode there is no Chrome oracle: all engines just run and are
    # scored on their own, so the gate machinery is disabled entirely.
    gating_active = score_mode != "independent"
    required_gate_needed = gating_active and any(resolve_gate_policy(task, args.chrome_gate, suite) == "required" for task in tasks)
    best_effort_gate_needed = gating_active and any(resolve_gate_policy(task, args.chrome_gate, suite) == "best_effort" for task in tasks)
    if required_gate_needed and "chrome" not in selected_engines:
        raise BenchError("chrome must be selected when Chrome baseline check is required")

    browser_engines = set(selected_engines)
    if required_gate_needed or (best_effort_gate_needed and "chrome" in selected_engines):
        browser_engines.add("chrome")
    selenium_bindings = resolve_selenium_runtime_bindings(
        tasks,
        [engine for engine in ENGINE_ORDER if engine in browser_engines],
    )
    unavailable_bindings = resolve_unavailable_runtime_bindings(
        tasks,
        [engine for engine in ENGINE_ORDER if engine in browser_engines],
    )

    score_eligible, score_reasons = score_eligible_for_run(
        suite, tasks, selected_engines, args.chrome_gate, args.debug, score_mode
    )
    requested_run_id = args.run_id or getattr(args, "label", None)
    out_root = resolve_path(args.out, DEFAULT_RUNS_DIR) if args.out else DEFAULT_RUNS_DIR
    run_id, run_dir = reserve_run_dir(out_root, requested_run_id, getattr(args, "run_id_conflict", "suffix"))
    results_path = run_dir / "results.jsonl"
    manifest_path = resolve_path(args.manifest, DEFAULT_MANIFEST) if args.manifest else DEFAULT_MANIFEST
    resource_mode = str(getattr(args, "resource_profile", "off") or "off")
    resource_enabled = resource_mode == "engine"
    resource_balanced_order = resource_mode in {"baseline", "engine"}
    resource_runtime = (
        ResourceRuntime(
            run_dir,
            run_id,
            int(getattr(args, "resource_sample_interval_ms", 250) or 250),
        )
        if resource_enabled
        else None
    )

    fixture_server: FixtureServer | None = None
    fixture_base_url: str | None = None
    if resource_enabled or any(task.scene.get("kind") == "self_hosted_fixture" for task in tasks):
        fixture_server = FixtureServer(
            traffic_tracker=resource_runtime.traffic if resource_runtime is not None else None
        )
        fixture_base_url = fixture_server.start()

    run_manifest = run_manifest_payload(
        args,
        suite,
        manifest_path,
        tasks,
        selected_engines,
        run_id,
        score_eligible,
        score_reasons,
        fixture_base_url,
    )
    write_json(run_dir / "run_manifest.json", run_manifest)
    host_sampler: resource_metrics.HostTelemetrySampler | None = None
    host_sampler_error: str | None = None
    if str(getattr(args, "host_telemetry", "on") or "on") == "on":
        try:
            host_sampler = resource_metrics.HostTelemetrySampler(
                run_dir / "host_telemetry.jsonl",
                float(getattr(args, "host_sample_interval_s", 2.0) or 2.0),
                os.getpid(),
            )
            host_sampler.start()
        except Exception as exc:
            host_sampler = None
            host_sampler_error = f"{type(exc).__name__}: {exc}"

    reporter = RunReporter(
        expected_result_rows(tasks, args.k, selected_engines),
        quiet=bool(getattr(args, "quiet", False)),
        progress=not bool(getattr(args, "no_progress", False)),
        color_mode=getattr(args, "color", "auto") or "auto",
    )
    reporter.write_digest(
        build_run_digest_lines(
            suite=suite,
            manifest_path=manifest_path,
            tasks=tasks,
            selected_engines=selected_engines,
            run_id=run_id,
            run_dir=run_dir,
            k_runs=args.k,
            jobs=jobs,
            score_mode=score_mode,
            score_eligible=score_eligible,
            score_reasons=score_reasons,
            chrome_gate=args.chrome_gate,
            fixture_base_url=fixture_base_url,
            resource_profile=str(getattr(args, "resource_profile", "off") or "off"),
            host_telemetry_enabled=bool(
                (run_manifest.get("host_telemetry") or {}).get("enabled")
            ),
        )
    )

    managers: list[BrowserManager] = []
    managers_lock = threading.Lock()
    thread_state = threading.local()

    class EngineHandles:
        """Task-aware view over a worker's BrowserManager.

        Resolving an engine re-checks process liveness and applies the task's
        launch profile. A profile change replaces the worker's current engine
        process; a crash is likewise relaunched for the next task instead of
        cascading connection failures over the rest of the queue.
        """

        def __init__(
            self,
            manager: BrowserManager,
            engines: list[str],
            initial_task: ResolvedTask,
        ) -> None:
            self.manager = manager
            for engine in engines:
                manager.launch(engine, initial_task.launch_profile)

        def for_task(self, engine: str, task: ResolvedTask) -> BrowserProcess:
            browser = self.manager.launch(engine, task.launch_profile)
            browser.prev_task_id = self.manager.note_task(engine, task.task_id)
            return browser

    def make_browsers(
        dynamic_ports: bool,
        initial_task: ResolvedTask,
    ) -> "EngineHandles":
        if shutdown_event.is_set():
            raise BenchError("run interrupted")
        with managers_lock:
            # Slot assignment and registration share one critical section so two
            # workers starting at once cannot claim the same number.
            local_manager = BrowserManager(
                dynamic_ports=dynamic_ports,
                resource_runtime=resource_runtime,
                worker_slot=len(managers) + 1,
            )
            managers.append(local_manager)
        return EngineHandles(
            local_manager,
            [engine for engine in ENGINE_ORDER if engine in browser_engines],
            initial_task,
        )

    task_order_index = {
        task.task_id: index for index, task in enumerate(tasks)
    }
    seed_rotation = int.from_bytes(
        hashlib.sha256(str(args.seed or "").encode("utf-8")).digest()[:4],
        "big",
    )

    def resource_engine_order(task: ResolvedTask, attempt: int) -> list[str]:
        ordered = list(selected_engines)
        if not resource_balanced_order or score_mode != "independent" or len(ordered) < 2:
            return ordered
        # Consecutive task-attempts rotate exactly. Across N attempts, each
        # engine occupies every ordinal position either floor(N/E) or
        # ceil(N/E) times; the seed only chooses the first rotation.
        position = task_order_index[task.task_id] * int(args.k) + int(attempt) - 1
        offset = (seed_rotation + position) % len(ordered)
        return ordered[offset:] + ordered[:offset]

    def run_one(task: ResolvedTask, attempt: int, browsers: EngineHandles) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        seed = seed_for_attempt(args.seed, task, attempt)
        gate_result: dict[str, Any]

        def scenario_binding_for(engine: str) -> dict[str, Any] | None:
            driver_kind = str(task.driver.get("kind") or "")
            driver_id = CATALOG_DRIVER_BY_TASK_KIND.get(driver_kind)
            if driver_id is None:
                return None
            unavailable = unavailable_bindings.get((engine, driver_id))
            if unavailable is not None:
                return unavailable
            if driver_id != "selenium":
                return None
            try:
                return selenium_bindings[engine]
            except KeyError as exc:
                raise BenchError(
                    f"missing pre-resolved Selenium binding for ({engine}, selenium)"
                ) from exc

        if not gating_active:
            # Independent mode: no Chrome oracle. Every selected engine runs and
            # is scored on its own merits (Chrome included, via should_include_score).
            gate_result = {"required": False, "status": "independent", "chrome_attempt_ref": None}
            engines_to_run = resource_engine_order(task, attempt)
            for engine in engines_to_run:
                rows.append(
                    run_driver_attempt(
                        run_dir,
                        results_path,
                        run_id,
                        task,
                        engine,
                        attempt,
                        seed,
                        browsers.for_task(engine, task),
                        gate_result,
                        score_eligible=score_eligible,
                        fixture_base_url=fixture_base_url,
                        score_mode=score_mode,
                        resource_runtime=resource_runtime,
                        fixture_server=fixture_server,
                        scenario_binding=scenario_binding_for(engine),
                    )
                )
            return rows
        gate = resolve_gate_policy(task, args.chrome_gate, suite)
        if gate == "required":
            gate_result, chrome_row = run_required_gate(
                run_dir,
                results_path,
                run_id,
                task,
                attempt,
                seed,
                browsers.for_task("chrome", task),
                score_eligible,
                fixture_base_url,
                resource_runtime=resource_runtime,
                fixture_server=fixture_server,
                scenario_binding=scenario_binding_for("chrome"),
            )
            rows.append(chrome_row)
            if gate_result["status"] != "pass":
                for engine in selected_engines:
                    if engine == "chrome":
                        continue
                    rows.append(run_gate_skip_attempt(run_dir, results_path, run_id, task, engine, attempt, seed, gate_result))
                return rows
            engines_to_run = [engine for engine in selected_engines if engine != "chrome"]
        elif gate == "best_effort":
            if "chrome" in selected_engines:
                ref = f"chrome:{task.task_id}:{attempt}"
                gate_payload = {"required": False, "status": "best_effort", "chrome_attempt_ref": ref}
                chrome_result = run_driver_attempt(
                    run_dir,
                    results_path,
                    run_id,
                    task,
                    "chrome",
                    attempt,
                    seed,
                    browsers.for_task("chrome", task),
                    gate_payload,
                    score_eligible=False,
                    fixture_base_url=fixture_base_url,
                    resource_runtime=resource_runtime,
                    fixture_server=fixture_server,
                    scenario_binding=scenario_binding_for("chrome"),
                )
                rows.append(chrome_result)
                gate_result = {"required": False, "status": chrome_result["status"], "chrome_attempt_ref": ref}
            else:
                gate_result = {"required": False, "status": "not_run", "chrome_attempt_ref": None}
            engines_to_run = [engine for engine in selected_engines if engine != "chrome"]
        else:
            gate_result = {"required": False, "status": gate, "chrome_attempt_ref": None}
            engines_to_run = selected_engines

        for engine in engines_to_run:
            rows.append(
                run_driver_attempt(
                    run_dir,
                    results_path,
                    run_id,
                    task,
                    engine,
                    attempt,
                    seed,
                    browsers.for_task(engine, task),
                    gate_result,
                    score_eligible=score_eligible,
                    fixture_base_url=fixture_base_url,
                    score_mode=score_mode,
                    resource_runtime=resource_runtime,
                    fixture_server=fixture_server,
                    scenario_binding=scenario_binding_for(engine),
                )
            )
        return rows

    shutdown_event = threading.Event()
    previous_signal_handlers: dict[int, Any] = {}

    def interrupt_run(signum: int, _frame: Any) -> None:
        shutdown_event.set()
        raise KeyboardInterrupt(f"run interrupted by signal {signum}")

    if threading.current_thread() is threading.main_thread():
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous_signal_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, interrupt_run)

    run_completed = False
    try:
        if jobs == 1:
            reporter.phase(f"Launching browsers: {', '.join(engine for engine in ENGINE_ORDER if engine in browser_engines)}")
            browsers = make_browsers(dynamic_ports=False, initial_task=tasks[0])
            reporter.phase(f"Running {len(tasks) * args.k} task attempts")
            for task in tasks:
                for attempt in range(1, args.k + 1):
                    reporter.advance(run_one(task, attempt, browsers))
        else:
            items = [(task, attempt) for task in tasks for attempt in range(1, args.k + 1)]
            reporter.phase(f"Running {len(items)} task attempts with {jobs} workers")

            def worker(item: tuple[ResolvedTask, int]) -> list[dict[str, Any]]:
                if shutdown_event.is_set():
                    raise BenchError("run interrupted")
                if not hasattr(thread_state, "browsers"):
                    # Each worker owns its browser processes on ephemeral ports:
                    # full process/port/profile isolation between parallel lanes.
                    thread_state.browsers = make_browsers(
                        dynamic_ports=True,
                        initial_task=item[0],
                    )
                return run_one(item[0], item[1], thread_state.browsers)

            errors: list[str] = []
            pool = concurrent.futures.ThreadPoolExecutor(max_workers=jobs)
            futures: dict[
                concurrent.futures.Future[list[dict[str, Any]]],
                tuple[ResolvedTask, int],
            ] = {}
            try:
                futures = {pool.submit(worker, item): item for item in items}
                for future in concurrent.futures.as_completed(futures):
                    item = futures[future]
                    try:
                        reporter.advance(future.result())
                    except Exception as exc:
                        errors.append(f"{item[0].task_id}#{item[1]}: {exc}")
            except BaseException:
                shutdown_event.set()
                for future in futures:
                    future.cancel()
                pool.shutdown(wait=False, cancel_futures=True)
                raise
            else:
                pool.shutdown(wait=True)
            if errors:
                reporter.phase(f"Failed after {reporter.completed_rows}/{reporter.total_rows} result rows")
                raise BenchError("parallel run failed for some attempts:\n" + "\n".join(errors[:10]))
        reporter.finish()
        run_completed = True
    finally:
        for local_manager in managers:
            local_manager.close_all()
        if fixture_server:
            fixture_server.stop()
        if host_sampler is not None:
            try:
                host_summary = host_sampler.stop()
            except Exception as exc:
                host_summary = {
                    "polluted": True,
                    "flags": ["host_telemetry_stop_failed"],
                    "error": f"{type(exc).__name__}: {exc}",
                }
        else:
            host_summary = {
                "polluted": bool(host_sampler_error),
                "flags": ["host_telemetry_unavailable"] if host_sampler_error else [],
                "error": host_sampler_error,
                "sample_count": 0,
            }
        write_json(run_dir / "host_summary.json", host_summary)
        run_manifest["completed_at"] = now_iso()
        run_manifest["completion_status"] = (
            "completed" if run_completed else "interrupted"
        )
        run_manifest["completed_result_rows"] = reporter.completed_rows
        run_manifest["expected_result_rows"] = reporter.total_rows
        run_manifest["host_telemetry"]["summary"] = host_summary
        if resource_runtime is not None:
            run_manifest["resource_profile"]["cold_start_artifact"] = "cold_start.jsonl"
            run_manifest["resource_profile"]["cold_start_samples"] = len(
                resource_runtime.cold_starts
            )
        write_json(run_dir / "run_manifest.json", run_manifest)
        for signum, previous in previous_signal_handlers.items():
            signal.signal(signum, previous)

    return run_dir


def command_doctor(args: argparse.Namespace) -> int:
    print("engine,binary,version,sha12,port,launch")
    manager = BrowserManager()
    ok = True
    try:
        for engine in ENGINE_ORDER:
            meta = ENGINE_DEFS[engine]
            binary = pathlib.Path(meta["binary"])
            sha12 = ""
            launch_status = "not_run"
            if not binary.exists():
                print(
                    f"{engine},{rel_to_repo(binary)},{meta['version']},missing,"
                    f"{meta['cdp_port']},missing_binary"
                )
                ok = False
                continue
            actual_sha256 = sha256_file(binary)
            sha12 = actual_sha256[:12]
            validation_errors: list[str] = []
            if sha12 != meta["sha256_12"]:
                ok = False
                validation_errors.append(f"sha_mismatch_expected_{meta['sha256_12']}")
            if meta.get("sha256") and actual_sha256 != meta["sha256"]:
                ok = False
                validation_errors.append("full_sha_mismatch")
            actual_version = pinned_binary_version(binary, meta)
            if actual_version is not None and actual_version != meta["version"]:
                ok = False
                validation_errors.append(
                    "version_mismatch_expected_"
                    + re.sub(r"[^0-9A-Za-z._-]+", "_", str(meta["version"]))
                )
            if port_is_open(int(meta["cdp_port"])):
                print(
                    f"{engine},{rel_to_repo(binary)},{meta['version']},{sha12},"
                    f"{meta['cdp_port']},port_in_use"
                )
                ok = False
                continue
            try:
                browser = manager.launch(engine)
                product = browser.version_info.get("Browser") or browser.version_info.get("Product") or "unknown"
                launch_status = f"ready:{product}"
            except Exception as exc:
                ok = False
                launch_status = f"launch_failed:{exc}"
            if validation_errors:
                launch_status += "|" + "|".join(validation_errors)
            print(
                f"{engine},{rel_to_repo(binary)},{meta['version']},{sha12},"
                f"{meta['cdp_port']},{launch_status}"
            )
        node = node_version()
        print(f"node,{node or 'missing'}")
        if not check_harness_pins(emit=True):
            ok = False
        if not check_protocol_pin(emit=True):
            ok = False
        if not check_language_runtimes(emit=True):
            ok = False
        if not check_compiled_adapters(emit=True):
            ok = False
    finally:
        manager.close_all()
    if ok:
        print("OK")
        return 0
    return 1


def compiled_adapter_binaries() -> dict[str, pathlib.Path]:
    """Adapters launched as a pre-built binary, keyed by driver_key.

    Detected by shape rather than by a second hand-maintained list: an argv
    whose program is derived from the adapter directory ("{script}/...") is a
    compiled artifact, while ["node"] / ["python3"] / ["ruby"] pass the script
    as an argument to an interpreter that is already on PATH.
    """
    found: dict[str, pathlib.Path] = {}
    for kind, spec in SCENARIO_ADAPTER_KINDS.items():
        argv0 = spec["argv"][0]
        if "{script}" not in argv0:
            continue
        driver_key = spec["driver_key"]
        if driver_key in found:
            # Silently keeping the last one would drop a binary from the doctor
            # check without failing anything, which is the exact class of quiet
            # gap this check exists to close.
            raise BenchError(f"duplicate driver_key `{driver_key}` among compiled adapters (kind `{kind}`)")
        found[driver_key] = pathlib.Path(argv0.replace("{script}", str(BENCH_ROOT / spec["script"])))
    return found


def compiled_adapter_source_mtime(binary: pathlib.Path) -> float:
    """Newest mtime among an adapter's checked-in sources.

    The adapter root is the directory holding its build manifest, which is the
    binary's parent for the Go columns and two levels up for the Cargo target
    directory. Build outputs are gitignored, so a checkout only ever moves the
    source mtimes forward, which is what makes the comparison meaningful.
    """
    root = binary.parent
    for candidate in (binary.parent, *binary.parents):
        if any((candidate / name).exists() for name in ("go.mod", "Cargo.toml")):
            root = candidate
            break
    newest = 0.0
    for path in root.rglob("*"):
        if path.is_dir() or "target" in path.parts or path == binary:
            continue
        if path.suffix in {".go", ".rs", ".mod", ".sum", ".toml", ".lock"}:
            newest = max(newest, path.stat().st_mtime)
    return newest


def check_compiled_adapters(emit: bool = False) -> bool:
    """Verify adapters that run as a pre-built binary are built, and current.

    These have no way to report "not built" through the stdin/stdout contract:
    the missing binary surfaces as a task timeout on every single attempt, which
    reads as an engine failure. Catch it here, where it can name the build
    command instead.

    A *stale* binary is worse than a missing one: it runs, and every task using
    an op added since the last build fails as though the engine could not do it.
    That is a silently wrong benchmark result, so an out-of-date build is an
    error here rather than a warning.
    """
    ok = True
    for driver_key, binary in sorted(compiled_adapter_binaries().items()):
        hint = COMPILED_ADAPTER_BUILD_HINTS.get(driver_key, "see runner/scripts/adapters")
        if not binary.exists():
            ok = False
            status = f"missing_build,run: {hint}"
        elif binary.stat().st_mtime < compiled_adapter_source_mtime(binary):
            ok = False
            status = f"stale_build,sources are newer than the binary,run: {hint}"
        else:
            status = "ok"
        if emit:
            print(f"compiled_adapter,{driver_key},{rel_to_repo(binary)},{status}")
    return ok


def check_protocol_pin(emit: bool = False) -> bool:
    """Verify the pinned devtools-protocol JSON is present at the pinned version."""
    if not HARNESS_PINS_PATH.exists():
        return True
    try:
        pins = load_json(HARNESS_PINS_PATH)
    except Exception:
        return True
    spec = (pins.get("protocols") or {}).get("devtools-protocol")
    if not spec:
        return True
    expected = str(spec.get("version"))
    installed = installed_npm_version("devtools-protocol")
    json_dir = BENCH_ROOT / str(spec.get("json_dir") or "node_modules/devtools-protocol/json")
    files_ok = all((json_dir / name).exists() for name in ("browser_protocol.json", "js_protocol.json"))
    if installed == expected and files_ok:
        status = "ok"
    elif installed is None or not files_ok:
        status = "missing_run_npm_ci"
        return _emit_pin("protocol_pin", "devtools-protocol", expected, status, emit) and False
    else:
        status = f"version_mismatch_installed_{installed}"
        return _emit_pin("protocol_pin", "devtools-protocol", expected, status, emit) and False
    _emit_pin("protocol_pin", "devtools-protocol", expected, status, emit)
    return True


def _emit_pin(kind: str, name: str, expected: str, status: str, emit: bool) -> bool:
    if emit:
        print(f"{kind},{name},{expected},{status}")
    return True


def _parse_version(text: str) -> tuple[int, ...]:
    nums = re.findall(r"\d+", text or "")
    return tuple(int(n) for n in nums[:3]) if nums else ()


def check_language_runtimes(emit: bool = False) -> bool:
    """Verify language runtimes declared in harness_pins.json are present.

    A missing runtime does not fail doctor by itself for optional adapters; but
    runtimes declared in language_runtimes are ones the landed drivers require,
    so a missing/old one is reported and fails the check.
    """
    if not HARNESS_PINS_PATH.exists():
        return True
    try:
        pins = load_json(HARNESS_PINS_PATH)
    except Exception:
        return True
    runtimes = pins.get("language_runtimes") or {}
    ok = True
    for name, spec in runtimes.items():
        if name.startswith("_"):
            continue
        cmd = str(spec.get("verify_cmd") or f"{name} --version")
        min_version = str(spec.get("min_version") or "")
        try:
            out = subprocess.run(cmd.split(), capture_output=True, text=True, timeout=15)
            raw = (out.stdout or out.stderr).strip()
        except Exception:
            raw = ""
        if not raw:
            status = "missing"
            ok = False
        elif min_version and _parse_version(raw) < _parse_version(min_version):
            status = f"too_old_min_{min_version}"
            ok = False
        else:
            status = "ok"
        if emit:
            found = _parse_version(raw)
            found_s = ".".join(str(n) for n in found) if found else "none"
            print(f"language_runtime,{name},{found_s},{status}")
    return ok


def installed_npm_version(package: str) -> str | None:
    pkg_json = BENCH_ROOT / "node_modules" / package / "package.json"
    if not pkg_json.exists():
        return None
    try:
        return str(load_json(pkg_json).get("version"))
    except Exception:
        return None


def installed_pip_version(package: str) -> str | None:
    """Version of a pip package in the interpreter running this harness.

    The runner and the python scenario adapters share one `python3`, so
    importlib.metadata in-process is the honest check (the bare `pip` on this
    host belongs to a different, older interpreter).
    """
    try:
        from importlib.metadata import version

        return str(version(package))
    except Exception:
        return None


def installed_go_module_version(go_mod_file: str, module: str) -> str | None:
    """Version of a Go module pinned in a committed go.mod require block.

    Go adapters vendor their pin in-repo (go.mod + go.sum); `go run` can only
    build exactly that version, so reading go.mod IS the installed check.
    """
    path = BENCH_ROOT / go_mod_file
    if not path.exists():
        return None
    try:
        for line in path.read_text().splitlines():
            parts = line.strip().split()
            if len(parts) >= 2 and parts[0] == module:
                return parts[1]
            if len(parts) >= 3 and parts[0] == "require" and parts[1] == module:
                return parts[2]
    except Exception:
        return None
    return None


def installed_cargo_version(cargo_lock_file: str, package: str) -> str | None:
    """Version of a crate pinned in a committed Cargo.lock.

    Rust adapters vendor their pin in-repo (Cargo.toml + Cargo.lock); cargo
    can only build exactly that version, so reading Cargo.lock IS the
    installed check.
    """
    path = BENCH_ROOT / cargo_lock_file
    if not path.exists():
        return None
    try:
        current_name = None
        for line in path.read_text().splitlines():
            line = line.strip()
            if line.startswith("name = "):
                current_name = line.split('"')[1] if '"' in line else None
            elif line.startswith("version = ") and current_name == package:
                return line.split('"')[1] if '"' in line else None
    except Exception:
        return None
    return None


def installed_gem_version(package: str) -> str | None:
    """Version of a Ruby gem visible to the host `ruby` (the interpreter the
    ferrum adapter runs under), via a short subprocess probe."""
    try:
        out = subprocess.run(
            ["ruby", "-rrubygems", "-e", f'puts Gem::Specification.find_by_name("{package}").version'],
            capture_output=True, text=True, timeout=20,
        )
        version = (out.stdout or "").strip()
        return version or None
    except Exception:
        return None


def installed_binary_version(spec: dict[str, Any]) -> str | None:
    """Pinned-binary check: the declared version counts as installed iff the
    file exists and its sha256 prefix matches the pin (chromedriver)."""
    path = BENCH_ROOT / str(spec.get("binary_path") or "")
    if not path.exists():
        return None
    try:
        digest = sha256_file(path)[:12]
    except Exception:
        return None
    if digest != str(spec.get("sha256_12") or ""):
        return f"sha_mismatch_{digest}"
    return str(spec.get("version"))


def installed_driver_version(spec: dict[str, Any], name: str) -> str | None:
    """Installed version for one harness_pins driver entry (npm/pip/go/cargo/gem/binary)."""
    if spec.get("pip_package"):
        return installed_pip_version(str(spec["pip_package"]))
    if spec.get("go_module"):
        return installed_go_module_version(str(spec.get("go_mod_file") or ""), str(spec["go_module"]))
    if spec.get("cargo_package"):
        return installed_cargo_version(str(spec.get("cargo_lock_file") or ""), str(spec["cargo_package"]))
    if spec.get("gem_package"):
        return installed_gem_version(str(spec["gem_package"]))
    if spec.get("binary_path"):
        return installed_binary_version(spec)
    return installed_npm_version(str(spec.get("npm_package") or name))


def check_harness_pins(emit: bool = False) -> bool:
    """Verify the pinned harness drivers (harness_pins.json) are installed.

    The pins live outside build_artifacts/ on purpose: build_artifacts is for
    engines under test, harness_pins.json is for the driver side (Playwright,
    Puppeteer, agent-browser).
    """
    ok = True
    if not HARNESS_PINS_PATH.exists():
        if emit:
            print("harness_pins,missing,harness_pins.json not found")
        return False
    try:
        pins = effective_harness_pins()
    except Exception as exc:
        if emit:
            print(f"harness_pins,invalid,{exc}")
        return False
    for name, spec in (pins.get("drivers") or {}).items():
        expected = str(spec.get("version"))
        installed = installed_driver_version(spec, name)
        if installed == expected:
            status = "ok"
        elif installed is None:
            if spec.get("pip_package"):
                status = "missing_run_pip_install"
            elif spec.get("go_module"):
                status = "missing_go_mod_pin"
            elif spec.get("cargo_package"):
                status = "missing_cargo_lock_pin"
            elif spec.get("gem_package"):
                status = "missing_run_gem_install"
            elif spec.get("binary_path"):
                status = "missing_pinned_binary"
            else:
                status = "missing_run_npm_ci"
            ok = False
        else:
            status = f"version_mismatch_installed_{installed}"
            ok = False
        if emit:
            print(f"harness_pin,{name},{expected},{status}")
    return ok


def command_coverage(args: argparse.Namespace) -> int:
    """Regenerate the CDP stable-surface coverage matrix.

    Writes generated/cdp_coverage.{md,json} and prints a summary. With
    --check, exits non-zero when the acceptance bar is unmet so it can gate CI.
    The bar is gap=0, no waiver-ledger errors, and a live waiver set matching
    coverage.FROZEN_WAIVERS member-for-member.
    """
    from runner import coverage as cov

    result = cov.build_coverage()
    if not getattr(args, "no_write", False):
        cov.write_reports(result)
    print(
        f"cdp_coverage protocol={result.protocol_version} pin={result.protocol_revision} "
        f"stable={result.stable_total} tested={len(result.tested)} "
        f"waived={len(result.waived)} gap={len(result.gap)} "
        f"waiver={result.waiver_fraction*100:.2f}%"
    )
    if result.unmapped_features:
        for feature in result.unmapped_features:
            print(f"WARNING: cdp feature `{feature}` maps to no protocol member", file=sys.stderr)
    for err in result.waiver_errors:
        print(f"ERROR: {err}", file=sys.stderr)

    added = result.waivers_added
    missing = result.waivers_missing
    for member_id in added:
        print(
            f"ERROR: waiver `{member_id}` is not in the frozen set; "
            "update the waiver policy and frozen whitelist together",
            file=sys.stderr,
        )
    for member_id in missing:
        print(
            f"ERROR: frozen waiver `{member_id}` is missing from the ledger",
            file=sys.stderr,
        )
    if getattr(args, "check", False):
        blocked = bool(result.gap) or bool(result.waiver_errors) or bool(added) or bool(missing)
        if blocked:
            print(
                f"coverage check FAILED: gap={len(result.gap)} "
                f"waiver_errors={len(result.waiver_errors)} "
                f"waivers_added={len(added)} waivers_missing={len(missing)}",
                file=sys.stderr,
            )
            return 1
        print(
            f"coverage check OK: gap=0, {len(result.waived)} waivers match the "
            f"frozen set, all justified"
        )
    return 0


def command_scenarios(args: argparse.Namespace) -> int:
    """Expand scenario specs into per-driver task files.

    Default: regenerate all bindings and report what changed. With --check: exit
    non-zero if any generated task file is missing, stale, or orphaned, so CI and
    `validate` can gate on the specs and generated tasks being in lockstep.
    """
    from runner import scenario as sc

    specs = sc.load_scenarios()
    spec_errors: list[str] = []
    for spec in specs:
        spec_errors.extend(sc.validate_scenario(spec))
    for err in spec_errors:
        print(f"ERROR: {err}", file=sys.stderr)

    if getattr(args, "check", False):
        in_sync, diffs = sc.check_sync(specs)
        for diff in diffs:
            print(f"ERROR: {diff}", file=sys.stderr)
        if spec_errors or not in_sync:
            print(
                f"scenarios check FAILED: spec_errors={len(spec_errors)} out_of_sync={len(diffs)} "
                f"(run `python3 -m runner.run scenarios` to regenerate)",
                file=sys.stderr,
            )
            return 1
        planned = sc.planned_outputs(specs)
        bindings = sum(1 for path in planned if path.name.endswith(".json") and path.name.startswith("sc_"))
        print(f"scenarios check OK: {len(specs)} specs -> {bindings} bindings in sync")
        return 0

    if spec_errors:
        print("refusing to generate: fix the scenario spec errors above", file=sys.stderr)
        return 1
    written, removed = sc.generate(specs)
    planned = sc.planned_outputs(specs)
    bindings = sum(1 for path in planned if path.name.endswith(".json") and path.name.startswith("sc_"))
    print(f"scenarios: {len(specs)} specs -> {bindings} bindings; wrote {len(written)}, removed {len(removed)}")
    for path in written:
        print(f"  wrote {path.relative_to(sc.BENCH_ROOT)}")
    for path in removed:
        print(f"  removed {path.relative_to(sc.BENCH_ROOT)}")
    return 0


def scenario_sync_errors() -> list[str]:
    """Return scenario spec + generated-file-sync errors for `validate`, or []."""
    try:
        from runner import scenario as sc

        specs = sc.load_scenarios()
        errors: list[str] = []
        for spec in specs:
            errors.extend(sc.validate_scenario(spec))
        _, diffs = sc.check_sync(specs)
        errors.extend(diffs)
        return errors
    except Exception as exc:  # never let scenario checking crash validate
        return [f"scenario check unavailable: {exc}"]


def coverage_gap_summary() -> str | None:
    """Return a one-line coverage warning for `validate`, or None on error."""
    try:
        from runner import coverage as cov

        result = cov.build_coverage()
    except Exception as exc:  # never let coverage break validate
        return f"cdp coverage unavailable: {exc}"
    if not result.gap and not result.waiver_errors:
        return None
    parts = [f"cdp coverage gap={len(result.gap)}"]
    if result.waiver_errors:
        parts.append(f"waiver_errors={len(result.waiver_errors)}")
    parts.append("(run `python3 -m runner.run coverage` for the ledger)")
    return " ".join(parts)


def command_validate(args: argparse.Namespace) -> int:
    manifest_path = resolve_path(args.manifest, DEFAULT_MANIFEST)
    suite, tasks, errors = validate_manifest(manifest_path, args.subset, args.task, args.layer)
    # Scenario specs and their generated per-driver tasks must be in lockstep.
    # Only enforce for a full-suite validate; a --subset/--task run is a narrow
    # slice and should not fail on unrelated scenario drift.
    if not args.layer and not args.subset and not args.task:
        errors = list(errors) + scenario_sync_errors()
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    by_subset: dict[str, int] = {}
    for task in tasks:
        by_subset[task.subset_id] = by_subset.get(task.subset_id, 0) + 1
    print(f"OK manifest={rel_to_repo(manifest_path)} tasks={len(tasks)} subsets={len(by_subset)}")
    for subset_id, count in sorted(by_subset.items()):
        print(f"{subset_id}: {count}")
    if not args.layer and not args.subset and not args.task:
        warning = coverage_gap_summary()
        if warning:
            print(f"WARNING: {warning}", file=sys.stderr)
    return 0


def command_list(args: argparse.Namespace) -> int:
    manifest_path = resolve_path(args.manifest, DEFAULT_MANIFEST)
    suite, tasks = expand_tasks(manifest_path, args.subset, args.task, args.feature, args.tag, args.layer, for_list=True)
    _semantic_path, _semantic_map, semantic_index = semantic_task_index(
        manifest_path, suite
    )
    if args.kind == "subsets":
        rows = []
        counts: dict[str, int] = {}
        for task in tasks:
            counts[task.subset_id] = counts.get(task.subset_id, 0) + 1
        for subset_id, subset in subset_index(suite).items():
            if args.subset and subset_id not in args.subset:
                continue
            if args.layer and subset.get("_layer_id") not in args.layer:
                continue
            rows.append(
                {
                    "layer": subset.get("_layer_id"),
                    "subset_id": subset_id,
                    "driver": subset.get("driver"),
                    "chrome_baseline": subset.get("chrome_gate", "off"),
                    "chrome_gate": subset.get("chrome_gate", "off"),
                    "task_count": counts.get(subset_id, 0),
                }
            )
    else:
        rows = [
            {
                "layer": task.layer,
                "subset_id": task.subset_id,
                "task_id": task.task_id,
                "description": task.description,
                "features": task.features,
                "tags": task.tags,
                "chrome_baseline": resolve_gate_policy(task, None, suite),
                "chrome_gate": resolve_gate_policy(task, None, suite),
                "task_version": task.task_version,
                "driver": task.driver.get("kind"),
                "path": task.rel_path,
                "evaluation_axis": (
                    "protocol_driver_compatibility"
                    if task.layer == "L1"
                    else "web_platform_workflow_semantic_correctness"
                ),
                "semantic_capability": semantic_index.get(task.task_id),
            }
            for task in tasks
        ]
    if args.json:
        print(json.dumps(rows, indent=2, sort_keys=True))
    else:
        for row in rows:
            print(" ".join(f"{key}={value}" for key, value in row.items()))
    return 0


def command_run(args: argparse.Namespace) -> int:
    manifest_path = resolve_path(args.manifest, DEFAULT_MANIFEST)
    suite, tasks = expand_tasks(manifest_path, args.subset, args.task, args.feature, args.tag, args.layer)
    if not tasks:
        raise BenchError("resolved task set is empty")
    _, _, errors = validate_manifest(manifest_path, args.subset, args.task, args.layer)
    if errors:
        raise BenchError("validate failed before run:\n" + "\n".join(errors))
    selected_engines = parse_engines(args.engines)
    score_mode = getattr(args, "score_mode", "baseline_checked") or "baseline_checked"
    gating_active = score_mode != "independent"
    if gating_active and any(resolve_gate_policy(task, args.chrome_gate, suite) == "required" for task in tasks) and "chrome" not in selected_engines:
        raise BenchError("chrome must be selected when Chrome baseline check is required")
    score_eligible, score_reasons = score_eligible_for_run(
        suite, tasks, selected_engines, args.chrome_gate, args.debug, score_mode
    )
    if args.dry_run:
        _semantic_path, _semantic_map, semantic_index = semantic_task_index(
            manifest_path, suite
        )
        chrome_baseline_policy = {
            "requested": args.chrome_gate,
            "score_eligible": score_eligible,
            "ineligibility_reasons": score_reasons,
        }
        payload = {
            "manifest": rel_to_repo(manifest_path),
            "selected_layers": sorted({task.layer for task in tasks}),
            "tasks": [
                task.to_run_manifest(semantic_index.get(task.task_id))
                for task in tasks
            ],
            "engines": selected_engines,
            "k_runs": args.k,
            "expected_result_rows": expected_result_rows(tasks, args.k, selected_engines),
            "color": args.color,
            "run_id_conflict": args.run_id_conflict,
            "auto_report": bool(getattr(args, "report", True)),
            "score_mode": score_mode,
            "scored_engines": list(scored_engines(score_mode)),
            "chrome_baseline_policy": chrome_baseline_policy,
            "chrome_gate_policy": chrome_baseline_policy,
            "host_telemetry": {
                "enabled": str(getattr(args, "host_telemetry", "on")) == "on",
                "sample_interval_s": float(
                    getattr(args, "host_sample_interval_s", 2.0) or 2.0
                ),
            },
            "resource_profile": {
                "mode": str(getattr(args, "resource_profile", "off") or "off"),
                "sample_interval_ms": int(
                    getattr(args, "resource_sample_interval_ms", 250) or 250
                ),
                "calibration_baseline": getattr(
                    args, "resource_calibration_baseline", None
                ),
            },
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    run_dir = run_attempts(args, suite, tasks)
    if getattr(args, "report", True):
        generate_report_files(run_dir)
    print(f"run_dir={run_dir}")
    return 0


def summarize_results(run_manifest: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    layers: dict[str, Any] = {
        "L1": {"by_subset": {}, "by_engine": {}},
        "L2": {"by_subset": {}, "by_engine": {}},
    }
    failure_classes: dict[str, int] = {}
    failure_origins: dict[str, int] = {}
    chrome_gate = {"passed_tasks": 0, "failed_tasks": 0}

    for row in rows:
        layer = row["layer"]
        if layer not in layers:
            continue
        subset = row["subset_id"]
        engine = row["engine"]
        layers[layer]["by_subset"].setdefault(subset, {"total": 0, "pass": 0})
        layers[layer]["by_engine"].setdefault(engine, {"total": 0, "pass": 0})
        layers[layer]["by_subset"][subset]["total"] += 1
        layers[layer]["by_engine"][engine]["total"] += 1
        if row["status"] == "pass":
            layers[layer]["by_subset"][subset]["pass"] += 1
            layers[layer]["by_engine"][engine]["pass"] += 1
        failure = row.get("failure")
        if failure:
            klass = failure.get("class", "infra")
            failure_classes[klass] = failure_classes.get(klass, 0) + 1
            origin = failure.get("origin")
            if origin:
                failure_origins[origin] = failure_origins.get(origin, 0) + 1
        if engine == "chrome" and row.get("chrome_gate", {}).get("required"):
            if row["status"] == "pass":
                chrome_gate["passed_tasks"] += 1
            else:
                chrome_gate["failed_tasks"] += 1

    grouped: dict[tuple[str, int], dict[str, str]] = {}
    for row in rows:
        if row["layer"] != "L2" or row["engine"] not in NATIVE_CANDIDATES:
            continue
        if not row.get("score_included") and row["status"] == "infra":
            continue
        grouped.setdefault((row["task_id"], int(row["attempt"])), {})[row["engine"]] = row["status"]

    selected_candidates = [
        engine
        for engine in NATIVE_CANDIDATES
        if engine in (run_manifest.get("selected_engines") or [])
    ]
    candidate_pairwise: dict[str, dict[str, Any]] = {}
    for left_idx, left in enumerate(selected_candidates):
        for right in selected_candidates[left_idx + 1 :]:
            counts = {
                "left_only": 0,
                "right_only": 0,
                "both_pass": 0,
                "both_fail": 0,
                "missing": 0,
            }
            for statuses in grouped.values():
                if left not in statuses or right not in statuses:
                    counts["missing"] += 1
                    continue
                left_pass = statuses[left] == "pass"
                right_pass = statuses[right] == "pass"
                if left_pass and right_pass:
                    counts["both_pass"] += 1
                elif left_pass:
                    counts["left_only"] += 1
                elif right_pass:
                    counts["right_only"] += 1
                else:
                    counts["both_fail"] += 1
            candidate_pairwise[f"{left}__{right}"] = {
                "left": left,
                "right": right,
                **counts,
            }
    layers["L2"]["candidate_pairwise"] = candidate_pairwise

    for layer_payload in layers.values():
        if not isinstance(layer_payload, dict):
            continue
        for section in ("by_subset", "by_engine"):
            for stats in layer_payload.get(section, {}).values():
                total = stats["total"]
                stats["pass_rate"] = stats["pass"] / total if total else 0

    semantic_correctness = semantic_model.summarize_semantic_results(
        run_manifest, rows
    )
    layers["L2"]["semantic_correctness"] = semantic_correctness
    evaluation_axes = {
        "protocol_driver_compatibility": {
            "source_layer": "L1",
            "unit": "task_attempt",
            "question": "Can this protocol/driver route connect, act, and produce verifiable evidence?",
            "by_subset": layers["L1"]["by_subset"],
            "by_engine": layers["L1"]["by_engine"],
        },
        "web_platform_workflow_semantic_correctness": {
            "source_layer": "L2",
            **semantic_correctness,
        },
    }

    return {
        "run_id": run_manifest.get("run_id"),
        "bench_id": run_manifest.get("bench_id") or "agent_browser_bench",
        "bench_version": run_manifest.get("bench_version") or "unknown",
        "score_eligible": bool(run_manifest.get("score_eligible")),
        "layers": layers,
        "evaluation_axes": evaluation_axes,
        "chrome_baseline": chrome_gate,
        "chrome_gate": chrome_gate,
        "failure_classes": failure_classes,
        "failure_origins": failure_origins,
        "provenance": {"run_manifest": "run_manifest.json"},
    }


def write_scorecard(run_dir: pathlib.Path, run_manifest: dict[str, Any], rows: list[dict[str, Any]], scores: dict[str, Any]) -> None:
    lines: list[str] = []
    lines.append(f"# Scorecard: {run_manifest.get('run_id')}")
    lines.append("")
    lines.append("## Run summary")
    lines.append("")
    lines.append(f"- score_eligible: `{run_manifest.get('score_eligible')}`")
    lines.append(f"- enabled_subsets: `{', '.join(run_manifest.get('enabled_subsets', []))}`")
    lines.append(f"- attempts: `{len(rows)}`")
    lines.append("")
    lines.append("| engine | binary | version | sha12 |")
    lines.append("|---|---|---|---|")
    for engine, meta in run_manifest.get("engines", {}).items():
        lines.append(f"| {engine} | `{meta.get('binary')}` | {meta.get('version')} | `{meta.get('sha256_12')}` |")
    lines.append("")
    lines.append("## Chrome baseline check")
    lines.append("")
    lines.append("| task | attempt | status |")
    lines.append("|---|---:|---|")
    for row in rows:
        if row["engine"] == "chrome" and row.get("chrome_gate", {}).get("required"):
            lines.append(f"| {row['task_id']} | {row['attempt']} | {display_status(row['status'])} |")
    lines.append("")
    lines.append("## Evaluation boundary")
    lines.append("")
    lines.append(
        "- L1 reports protocol/driver compatibility at task-attempt granularity."
    )
    lines.append(
        "- L2 headline rates report semantic capabilities; correlated probes must all pass, "
        "but count as one capability-attempt. Driver cross-checks and diagnostics are separate evidence."
    )
    lines.append("")
    lines.append("## L1 protocol / driver compatibility evidence")
    lines.append("")
    append_status_table(lines, [row for row in rows if row["layer"] == "L1"])
    lines.append("")
    semantic = scores["layers"]["L2"].get("semantic_correctness") or {}
    if semantic.get("available"):
        lines.append("## L2 semantic correctness (capability-level)")
        lines.append("")
        map_meta = semantic.get("map") or {}
        lines.append(
            f"- map: `{map_meta.get('path')}` sha12=`{str(map_meta.get('sha256') or '')[:12]}`"
        )
        lines.append(f"- score unit: `{semantic.get('unit')}`")
        lines.append(f"- aggregation: {semantic.get('aggregation')}")
        lines.append("")
        lines.append(
            "| engine | capability attempts | passed | pass rate | scored attempts | scored passed | scored rate | missing |"
        )
        lines.append("|---|---:|---:|---:|---:|---:|---:|---:|")
        for engine, stats in semantic.get("by_engine", {}).items():
            lines.append(
                f"| {engine} | {stats['total']} | {stats['pass']} | {stats['pass_rate']:.2%} | "
                f"{stats['scored_total']} | {stats['scored_pass']} | "
                f"{stats['scored_pass_rate']:.2%} | {stats['missing']} |"
            )
        lines.append("")
        lines.append("### Capability inventory and verdicts")
        lines.append("")
        lines.append(
            "| capability | category | observable | semantic probes | cross-checks | diagnostics | complete selection | engine verdicts |"
        )
        lines.append("|---|---|---|---:|---:|---:|---|---|")
        for capability_id, capability in semantic.get("capabilities", {}).items():
            verdict_parts = []
            for engine, stats in capability.get("by_engine", {}).items():
                verdict_parts.append(f"{engine}={stats['pass']}/{stats['total']}")
            lines.append(
                f"| `{capability_id}` | {capability.get('category', '')} | "
                f"{capability.get('observable', '')} | "
                f"{len(capability.get('semantic_probe_task_ids', []))} | "
                f"{len(capability.get('driver_cross_check_task_ids', []))} | "
                f"{len(capability.get('diagnostic_task_ids', []))} | "
                f"{capability.get('complete_selection')} | {', '.join(verdict_parts)} |"
            )
        lines.append("")

        semantic_roles = {
            str(task.get("task_id")): (task.get("semantic_capability") or {}).get("role")
            for task in run_manifest.get("resolved_tasks", [])
            if isinstance(task, dict)
        }
        cross_check_rows = [
            row
            for row in rows
            if semantic_roles.get(str(row.get("task_id"))) == "driver_cross_check"
        ]
        if cross_check_rows:
            lines.append("## L2 representative driver cross-checks (not semantic-score units)")
            lines.append("")
            append_status_table(lines, cross_check_rows)
            lines.append("")
        diagnostic_rows = [
            row
            for row in rows
            if semantic_roles.get(str(row.get("task_id"))) == "diagnostic"
        ]
        if diagnostic_rows:
            lines.append("## L2 diagnostics (not semantic-score units)")
            lines.append("")
            append_status_table(lines, diagnostic_rows)
            lines.append("")

        semantic_pairwise = semantic.get("pairwise") or {}
        if semantic_pairwise:
            lines.append("## L2 semantic capability pairwise")
            lines.append("")
            lines.append(
                "| pair | left only | right only | both pass | both fail | missing |"
            )
            lines.append("|---|---:|---:|---:|---:|---:|")
            for payload in semantic_pairwise.values():
                lines.append(
                    f"| {payload['left']} vs {payload['right']} | "
                    f"{payload['left_only']} | {payload['right_only']} | "
                    f"{payload['both_pass']} | {payload['both_fail']} | "
                    f"{payload['missing']} |"
                )
            lines.append("")

    lines.append("## L2 task-level evidence (non-headline)")
    lines.append("")
    append_status_table(lines, [row for row in rows if row["layer"] == "L2"])
    lines.append("")
    lines.append("## Failure classes")
    lines.append("")
    lines.append("| class | count |")
    lines.append("|---|---:|")
    for key, value in sorted(scores.get("failure_classes", {}).items()):
        lines.append(f"| {key} | {value} |")
    lines.append("")
    if scores.get("failure_origins"):
        lines.append("## Failure origins")
        lines.append("")
        lines.append("| origin | count |")
        lines.append("|---|---:|")
        for key, value in sorted(scores["failure_origins"].items()):
            lines.append(f"| {key} | {value} |")
        lines.append("")
    negative_task_ids = {
        task.get("task_id")
        for task in run_manifest.get("resolved_tasks", [])
        if "purpose.negative" in (task.get("tags") or [])
    }
    negative = [row for row in rows if row.get("task_id") in negative_task_ids]
    if negative:
        lines.append("## Negative tasks")
        lines.append("")
        append_status_table(lines, negative)
        lines.append("")
    (run_dir / "scorecard.md").write_text("\n".join(lines), encoding="utf-8")


def append_status_table(lines: list[str], rows: list[dict[str, Any]]) -> None:
    lines.append("| layer | subset | task | engine | attempt | status | failure.class | failure.origin |")
    lines.append("|---|---|---|---|---:|---|---|---|")
    for row in rows:
        failure = row.get("failure") or {}
        lines.append(
            f"| {row['layer']} | {row['subset_id']} | {row['task_id']} | {row['engine']} | "
            f"{row['attempt']} | {display_status(row['status'])} | {failure.get('class', '')} | "
            f"{failure.get('origin', '')} |"
        )




def generate_report_files(run_dir: pathlib.Path, emit: bool = True) -> None:
    if not run_dir.exists():
        raise BenchError(f"run directory not found: {run_dir}")
    run_manifest = load_json(run_dir / "run_manifest.json")
    rows = read_jsonl(run_dir / "results.jsonl")
    scores = summarize_results(run_manifest, rows)
    write_json(run_dir / "scores.json", scores)
    write_scorecard(run_dir, run_manifest, rows, scores)
    resource_profile = run_manifest.get("resource_profile") or {}
    if resource_profile.get("mode") == "engine" or any(row.get("resource") for row in rows):
        host_summary = (
            load_json(run_dir / "host_summary.json")
            if (run_dir / "host_summary.json").exists()
            else (run_manifest.get("host_telemetry") or {}).get("summary")
        )
        calibration = None
        baseline_ref = resource_profile.get("calibration_baseline")
        if baseline_ref:
            baseline_dir = pathlib.Path(str(baseline_ref))
            if not baseline_dir.is_absolute():
                baseline_dir = REPO_ROOT / baseline_dir
            baseline_results = baseline_dir / "results.jsonl"
            baseline_manifest_path = baseline_dir / "run_manifest.json"
            if baseline_results.exists() and baseline_manifest_path.exists():
                calibration = resource_metrics.duration_calibration(
                    rows,
                    read_jsonl(baseline_results),
                    float(resource_profile.get("max_observer_effect_pct") or 10.0),
                    profiled_manifest=run_manifest,
                    baseline_manifest=load_json(baseline_manifest_path),
                )
                calibration["baseline_run"] = rel_to_repo(baseline_dir)
            else:
                calibration = {
                    "acceptable": False,
                    "error": (
                        "baseline results or manifest not found: "
                        f"{baseline_results}, {baseline_manifest_path}"
                    ),
                    "baseline_run": rel_to_repo(baseline_dir),
                }
        cold_starts = (
            read_jsonl(run_dir / "cold_start.jsonl")
            if (run_dir / "cold_start.jsonl").exists()
            else []
        )
        resource_summary = resource_metrics.summarize_resources(
            run_manifest,
            rows,
            host_summary,
            calibration,
            cold_starts,
        )
        write_json(run_dir / "resource_summary.json", resource_summary)
        resource_metrics.write_resource_card(
            run_dir / "resource-card.md",
            resource_summary,
        )
    if emit:
        print(f"wrote {run_dir / 'scores.json'}")
        print(f"wrote {run_dir / 'scorecard.md'}")
        if (run_dir / "resource_summary.json").exists():
            print(f"wrote {run_dir / 'resource_summary.json'}")
            print(f"wrote {run_dir / 'resource-card.md'}")


def command_report(args: argparse.Namespace) -> int:
    run_dir = resolve_path(args.run, pathlib.Path())
    generate_report_files(run_dir)
    return 0


def command_fixture_serve(args: argparse.Namespace) -> int:
    """Serve the fixture tree standalone for a tunnel-fronted deployment.

    This is the dynamic-origin half of the Kitesurf fixture contract: the
    operator runs this server, fronts it with an HTTPS tunnel, then verifies
    the deployment with `tools/kitesurf_dynamic_fixture.py verify <url>`
    before any recipe run. The server itself stays loopback-only unless
    --host is set explicitly.
    """
    server = FixtureServer(bind_host=args.host, bind_port=args.port)
    base_url = server.start()
    print(f"fixture server listening on {base_url}")
    print("routes: static fixture tree + dynamic probes (ws/sse/auth/cart/graders)")
    print("verify a fronting deployment with:")
    print(f"  python3 tools/kitesurf_dynamic_fixture.py verify <public-base-url>")
    print("press Ctrl-C to stop")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("stopping")
    finally:
        server.stop()
    return 0


def command_inspect(args: argparse.Namespace) -> int:
    run_dir = resolve_path(args.run, pathlib.Path())
    rows = read_jsonl(run_dir / "results.jsonl")
    wanted_attempt = int(args.attempt)
    matches = [
        row
        for row in rows
        if row["task_id"] == args.task and row["engine"] == args.engine and int(row["attempt"]) == wanted_attempt
    ]
    if not matches:
        raise BenchError(f"attempt not found: task={args.task} engine={args.engine} attempt={wanted_attempt}")
    row = matches[0]
    artifact_dir = run_dir / row["artifact_dir"]
    grader = load_json(artifact_dir / "grader.json") if (artifact_dir / "grader.json").exists() else {}
    cdp_errors = tail_jsonl_matching(artifact_dir / "cdp.jsonl", lambda item: "error" in item or item.get("method") == "Inspector.detached")
    payload = {
        "status": row["status"],
        "failure": row.get("failure"),
        "grader": grader,
        "cdp_errors": cdp_errors[-5:],
        "artifact_dir": str(artifact_dir),
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"status={payload['status']}")
        print(f"failure={json.dumps(payload['failure'], sort_keys=True)}")
        print("grader_checks=" + json.dumps(grader.get("checks", []), sort_keys=True))
        print("cdp_errors=" + json.dumps(payload["cdp_errors"], sort_keys=True))
        print(f"artifact_dir={artifact_dir}")
    return 0


def tail_jsonl_matching(path: pathlib.Path, predicate: Any) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    matches: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            try:
                item = json.loads(line)
            except Exception:
                continue
            if predicate(item):
                matches.append(item)
    return matches[-20:]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m runner.run", description="Agent Browser Bench CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="check pinned binaries, ports, and CDP launch")
    doctor.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    doctor.set_defaults(func=command_doctor)

    validate = sub.add_parser("validate", help="validate suite and task manifests")
    validate.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    validate.add_argument("--layer", action="append")
    validate.add_argument("--subset", action="append")
    validate.add_argument("--task", action="append")
    validate.set_defaults(func=command_validate)

    coverage = sub.add_parser("coverage", help="regenerate the CDP stable-surface coverage matrix")
    coverage.add_argument("--check", action="store_true", help="exit non-zero unless gap=0 and the waiver set matches the frozen whitelist")
    coverage.add_argument("--no-write", action="store_true", help="print the summary without rewriting generated/*")
    coverage.set_defaults(func=command_coverage)

    scenarios = sub.add_parser("scenarios", help="expand scenario specs into per-driver tasks")
    scenarios.add_argument("--check", action="store_true", help="exit non-zero unless every generated task file is in sync with its scenario spec")
    scenarios.set_defaults(func=command_scenarios)

    list_cmd = sub.add_parser("list", help="list subsets or tasks")
    list_cmd.add_argument("kind", nargs="?", choices=["subsets", "tasks"], default="tasks")
    list_cmd.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    list_cmd.add_argument("--subset", action="append")
    list_cmd.add_argument("--task", action="append")
    list_cmd.add_argument("--feature", action="append")
    list_cmd.add_argument("--tag", action="append")
    list_cmd.add_argument("--layer", action="append")
    list_cmd.add_argument("--json", action="store_true")
    list_cmd.set_defaults(func=command_list)

    run = sub.add_parser("run", help="execute resolved tasks and write run artifacts")
    run.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    run.add_argument("--layer", action="append")
    run.add_argument("--subset", action="append")
    run.add_argument("--task", action="append")
    run.add_argument("--feature", action="append")
    run.add_argument("--tag", action="append")
    run.add_argument("--engines", default="chrome,moli,lightpanda,obscura")
    run.add_argument("--jobs", type=int, default=1, help="parallel task workers; each worker owns isolated browser processes on ephemeral ports")
    run.add_argument(
        "--k",
        type=int,
        default=None,
        help=(
            "attempts per task and engine; defaults to manifest.default_k_runs "
            f"({DEFAULT_K_RUNS} in the default manifest). Use k=3 only for an "
            "official release evidence run"
        ),
    )
    run.add_argument("--seed")
    run.add_argument(
        "--host-telemetry",
        choices=["on", "off"],
        default="on",
        help="record low-frequency host load/memory/swap/PSI telemetry (default: on)",
    )
    run.add_argument(
        "--host-sample-interval-s",
        type=float,
        default=2.0,
        help="host telemetry interval in seconds (minimum 0.25)",
    )
    run.add_argument(
        "--resource-profile",
        choices=["off", "baseline", "engine"],
        default="off",
        help="off: normal order; baseline: balanced order without profiler; "
        "engine: balanced order with CPU/PSS/fixture-traffic profiling",
    )
    run.add_argument(
        "--resource-sample-interval-ms",
        type=int,
        default=250,
        help="engine process-tree PSS sample interval (minimum 10 ms)",
    )
    run.add_argument(
        "--resource-calibration-baseline",
        help="profiler-off run directory with the same corpus/seed/k for observer-effect calibration",
    )
    run.add_argument(
        "--resource-max-observer-effect-pct",
        type=float,
        default=10.0,
        help="maximum median task and collection-wall overhead for resource comparison eligibility",
    )
    run.add_argument("--run-id", help="explicit run directory name (used verbatim; no timestamp suffix). Defaults to a UTC stamp when omitted")
    run.add_argument(
        "--provenance-level",
        choices=["full", "minimal"],
        default="full",
        help="host detail recorded in run_manifest: full, or minimal (kernel/platform/cpu_count only; "
        "drops cgroup path, CPU affinity and governor details for release runs)",
    )
    run.add_argument(
        "--run-id-conflict",
        choices=sorted(RUN_ID_CONFLICT_MODES),
        default="suffix",
        help="suffix: append _002, _003 on same-minute conflicts (default); error: refuse an existing final run id",
    )
    run.add_argument("--label", help="optional run id prefix when --run-id is omitted")
    run.add_argument("--out")
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--no-report", dest="report", action="store_false", help="skip automatic scorecard generation after run")
    run.set_defaults(report=True)
    run.add_argument("--no-progress", action="store_true", help="print the run digest but disable live progress updates")
    run.add_argument("--quiet", action="store_true", help="suppress digest and progress output; final run_dir line is still printed")
    run.add_argument("--color", choices=sorted(COLOR_MODES), default="auto", help="color terminal status output: auto, always, or never")
    run.add_argument("--chrome-gate", dest="chrome_gate", choices=sorted(GATE_POLICIES), help=argparse.SUPPRESS)
    run.add_argument(
        "--chrome-baseline",
        dest="chrome_gate",
        choices=sorted(GATE_POLICIES),
        help="Chrome baseline check policy for case robustness: off, best_effort, or required",
    )
    run.add_argument(
        "--score-mode",
        choices=["baseline_checked", "independent"],
        default="baseline_checked",
        help="baseline_checked: apply Chrome baseline policies, but only moli+lightpanda+obscura are scored (default). "
        "independent: every selected engine runs and is scored on its own.",
    )
    run.add_argument("--debug", action="store_true")
    run.set_defaults(func=command_run)

    fixture_serve = sub.add_parser(
        "fixture-serve",
        help="serve the fixture tree standalone (for a tunnel-fronted Kitesurf deployment)",
    )
    fixture_serve.add_argument("--host", default="127.0.0.1", help="bind address (default loopback)")
    fixture_serve.add_argument("--port", type=int, default=8907, help="stable port for the tunnel (default 8907)")
    fixture_serve.set_defaults(func=command_fixture_serve)

    report = sub.add_parser("report", help="generate scores and scorecard from an existing run")
    report.add_argument("--run", required=True)
    report.add_argument("--format", default="md,json")
    report.set_defaults(func=command_report)

    inspect = sub.add_parser("inspect", help="show one attempt summary")
    inspect.add_argument("--run", required=True)
    inspect.add_argument("--task", required=True)
    inspect.add_argument("--engine", required=True, choices=ENGINE_ORDER)
    inspect.add_argument("--attempt", type=int, required=True)
    inspect.add_argument("--json", action="store_true")
    inspect.set_defaults(func=command_inspect)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "k", None) is None:
        try:
            suite = load_suite(resolve_path(getattr(args, "manifest", None), DEFAULT_MANIFEST))
            args.k = int(suite.get("default_k_runs", DEFAULT_K_RUNS))
        except Exception:
            args.k = DEFAULT_K_RUNS
    try:
        return int(args.func(args))
    except BenchError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
