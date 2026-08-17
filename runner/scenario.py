"""Scenario / adapter expansion for the driver matrix.

Hand-authoring one task per driver does not scale to 13 drivers. This module
formalizes v0.3's "Playwright task + Puppeteer mirror" pattern into a single
driver-agnostic *scenario spec* that is expanded to one concrete task per bound
driver.

* A **scenario spec** (`tasks/scenarios/<id>.scenario.json`) declares a
  fixture scene, a sequence of driver-agnostic *ops* (the vocabulary that
  `runner/scripts/framework_probe.js` already interprets identically for
  Playwright and Puppeteer), driver-agnostic checks, and the roster of driver
  keys it binds to.
* A **binding** is the mechanical mapping of that spec onto one driver: the
  generator swaps in the driver's `driver.kind` and emits a normal task file
  under the driver's subset directory. Adding a driver is adding a column to
  `DRIVER_REGISTRY`, never re-authoring the scenarios.

Generated task files carry a `_generated_from` marker and are verified in sync
with their specs by `python3 -m runner.run scenarios --check` (and by
`validate`). Humans edit only the spec; the generated files are derived.
"""
from __future__ import annotations

import json
import pathlib
from typing import Any

from runner.launch_profiles import DEFAULT_LAUNCH_PROFILE, LAUNCH_PROFILES

BENCH_ROOT = pathlib.Path(__file__).resolve().parent.parent
SCENARIOS_DIR = BENCH_ROOT / "tasks" / "scenarios"
SCENARIO_SUFFIX = ".scenario.json"
GENERATED_MARKER = "_generated_from"
# Server-side observation grading: every scenario declares an
# `expected_answer`; the generator emits a server_side grader per binding and
# registers the expectation in this generated fragment, which the fixture
# server merges into its expected-answer registry at startup.
EXPECTED_FRAGMENT = pathlib.Path("fixtures/v0_4/scenarios/expected_answers.fragment.json")
GRADE_ENDPOINT = "/__grade__/expected_answer"

# One column per bound driver. `feature_prefix` names the fw.* namespace; `kind`
# is the driver.kind the runner dispatches on; `dir`/`subset`/`layer` place the
# generated task in the manifest's glob-discovered tree. New drivers (chromedp,
# rod, pydoll, ...) are added here as their adapters and runtimes land.
DRIVER_REGISTRY: dict[str, dict[str, Any]] = {
    "playwright": {
        "kind": "framework_playwright",
        "suffix": "pw",
        "subset": "l1.playwright",
        "dir": "tasks/L1/playwright",
        "layer": "L1",
        "feature_prefix": "fw.playwright",
    },
    "puppeteer": {
        "kind": "framework_puppeteer",
        "suffix": "pp",
        "subset": "l1.puppeteer",
        "dir": "tasks/L1/puppeteer",
        "layer": "L1",
        "feature_prefix": "fw.puppeteer",
    },
    "chrome_remote_interface": {
        "kind": "thin_chrome_remote_interface",
        "suffix": "cri",
        "subset": "l1.chrome_remote_interface",
        "dir": "tasks/L1/chrome_remote_interface",
        "layer": "L1",
        "feature_prefix": "fw.chrome_remote_interface",
    },
    "cdp_use": {
        "kind": "thin_cdp_use",
        "suffix": "cdpu",
        "subset": "l1.cdp_use",
        "dir": "tasks/L1/cdp_use",
        "layer": "L1",
        "feature_prefix": "fw.cdp_use",
    },
    "pydoll": {
        "kind": "thin_pydoll",
        "suffix": "pyd",
        "subset": "l1.pydoll",
        "dir": "tasks/L1/pydoll",
        "layer": "L1",
        "feature_prefix": "fw.pydoll",
    },
    "stagehand": {
        "kind": "framework_stagehand",
        "suffix": "sh",
        "subset": "l1.stagehand",
        "dir": "tasks/L1/stagehand",
        "layer": "L1",
        "feature_prefix": "fw.stagehand",
    },
    "chrome_devtools_mcp": {
        "kind": "mcp_chrome_devtools",
        "suffix": "mcp",
        "subset": "l1.chrome_devtools_mcp",
        "dir": "tasks/L1/chrome_devtools_mcp",
        "layer": "L1",
        "feature_prefix": "fw.chrome_devtools_mcp",
    },
    "chromedp": {
        "kind": "thin_chromedp",
        "suffix": "chromedp",
        "subset": "l1.chromedp",
        "dir": "tasks/L1/chromedp",
        "layer": "L1",
        "feature_prefix": "fw.chromedp",
    },
    "rod": {
        "kind": "thin_rod",
        "suffix": "rod",
        "subset": "l1.rod",
        "dir": "tasks/L1/rod",
        "layer": "L1",
        "feature_prefix": "fw.rod",
    },
    "agent_browser": {
        "kind": "tool_agent_browser",
        "suffix": "ab",
        "subset": "l1.agent_browser_scenarios",
        "dir": "tasks/L1/agent_browser_scenarios",
        "layer": "L1",
        "feature_prefix": "fw.agent_browser",
    },
    "ferrum": {
        "kind": "thin_ferrum",
        "suffix": "fr",
        "subset": "l1.ferrum",
        "dir": "tasks/L1/ferrum",
        "layer": "L1",
        "feature_prefix": "fw.ferrum",
    },
    "selenium": {
        "kind": "webdriver_selenium",
        "suffix": "se",
        "subset": "l1.selenium",
        "dir": "tasks/L1/selenium",
        "layer": "L1",
        "feature_prefix": "fw.selenium",
        "transport_policy": "engine_native",
        "task_version": 2,
    },
    "chromiumoxide": {
        "kind": "thin_chromiumoxide",
        "suffix": "oxide",
        "subset": "l1.chromiumoxide",
        "dir": "tasks/L1/chromiumoxide",
        "layer": "L1",
        "feature_prefix": "fw.chromiumoxide",
    },
}

ARTIFACT_PROFILE_BY_LAYER = {"L1": "l1_standard", "L2": "l2_standard"}
DEFAULT_TIMEOUTS = {"hard_kill_ms": 45000, "task_ms": 30000}


def load_scenarios(scenarios_dir: pathlib.Path = SCENARIOS_DIR) -> list[dict[str, Any]]:
    """Load every scenario spec, sorted by scenario_id for deterministic output."""
    specs: list[dict[str, Any]] = []
    if not scenarios_dir.exists():
        return specs
    for path in sorted(scenarios_dir.glob(f"*{SCENARIO_SUFFIX}")):
        spec = json.loads(path.read_text())
        spec["_path"] = path
        specs.append(spec)
    specs.sort(key=lambda s: str(s.get("scenario_id", "")))
    return specs


def validate_scenario(spec: dict[str, Any]) -> list[str]:
    """Schema-check one scenario spec. Returns a list of human-readable errors."""
    errors: list[str] = []
    sid = spec.get("scenario_id")
    where = f"scenario `{sid or spec.get('_path')}`"
    if not isinstance(sid, str) or not sid:
        errors.append(f"{where}: scenario_id must be a non-empty string")
    elif spec.get("_path") and spec["_path"].name != f"{sid}{SCENARIO_SUFFIX}":
        errors.append(
            f"{where}: filename `{spec['_path'].name}` does not match scenario_id "
            f"(expected `{sid}{SCENARIO_SUFFIX}`)"
        )
    for field in ("source_reference", "migration_notes", "expected_status_claim"):
        if field in spec:
            errors.append(f"{where}: obsolete field `{field}` is not allowed")
    for field in ("family", "title", "description"):
        if not isinstance(spec.get(field), str) or not spec.get(field, "").strip():
            errors.append(f"{where}: `{field}` must be a non-empty string")
    scene = spec.get("scene")
    if not isinstance(scene, dict) or not isinstance(scene.get("url"), str):
        errors.append(f"{where}: scene must be an object with a string `url`")
    layer = spec.get("layer", "L1")
    if layer not in ARTIFACT_PROFILE_BY_LAYER:
        errors.append(f"{where}: layer `{layer}` must be one of {sorted(ARTIFACT_PROFILE_BY_LAYER)}")
    launch_profile = spec.get("launch_profile", DEFAULT_LAUNCH_PROFILE)
    if launch_profile not in LAUNCH_PROFILES:
        errors.append(
            f"{where}: launch_profile `{launch_profile}` must be one of "
            f"{sorted(LAUNCH_PROFILES)}"
        )
    steps = spec.get("steps")
    if not isinstance(steps, list) or not steps:
        errors.append(f"{where}: steps must be a non-empty list")
    else:
        for idx, step in enumerate(steps):
            if not isinstance(step, dict) or not isinstance(step.get("op"), str) or not step.get("op"):
                errors.append(f"{where}: steps[{idx}] must be an object with a string `op`")
    checks = spec.get("checks")
    if checks is not None and not isinstance(checks, list):
        errors.append(f"{where}: checks must be a list when present")
    drivers = spec.get("drivers")
    if not isinstance(drivers, list) or not drivers:
        errors.append(f"{where}: drivers must be a non-empty list of driver keys")
    else:
        for key in drivers:
            if key not in DRIVER_REGISTRY:
                errors.append(
                    f"{where}: driver `{key}` is not in DRIVER_REGISTRY "
                    f"(known: {', '.join(sorted(DRIVER_REGISTRY))})"
                )
    anchors = spec.get("cdp_anchors", [])
    if not isinstance(anchors, list) or not all(isinstance(a, str) for a in anchors):
        errors.append(f"{where}: cdp_anchors must be a list of strings when present")
    expected = spec.get("expected_answer")
    if expected is None:
        errors.append(f"{where}: expected_answer is required for server-side observation grading")
    elif not isinstance(expected, dict) or not isinstance(expected.get("expected"), str) or expected.get("mode", "equals") not in ("equals", "contains", "contains_all"):
        errors.append(f"{where}: expected_answer must be {{mode: equals|contains|contains_all, expected: str}}")
    else:
        save_targets = [s.get("save_as") for s in steps or [] if isinstance(s, dict)]
        if "answer" not in save_targets:
            errors.append(f"{where}: expected_answer requires a step with save_as `answer`")
    skips = spec.get("driver_skips", {})
    if not isinstance(skips, dict) or not all(isinstance(v, str) and v.strip() for v in skips.values()):
        errors.append(f"{where}: driver_skips must map driver keys to non-empty reason strings")
    else:
        bound = set(drivers) if isinstance(drivers, list) else set()
        overlap = bound & set(skips)
        if overlap:
            errors.append(f"{where}: drivers {sorted(overlap)} are both bound and skipped")
        unknown_skips = set(skips) - set(DRIVER_REGISTRY)
        if unknown_skips:
            errors.append(f"{where}: driver_skips reference unknown drivers {sorted(unknown_skips)}")
        missing = set(DRIVER_REGISTRY) - bound - set(skips)
        if isinstance(drivers, list) and missing:
            errors.append(
                f"{where}: every landed driver needs a binding or an explicit skip-with-reason; "
                f"missing: {', '.join(sorted(missing))}"
            )
    return errors


def _unique_ops(steps: list[dict[str, Any]]) -> list[str]:
    seen: list[str] = []
    for step in steps:
        op = step.get("op")
        if isinstance(op, str) and op and op not in seen:
            seen.append(op)
    return seen


def expand_scenario(spec: dict[str, Any], driver_key: str) -> dict[str, Any]:
    """Expand a scenario spec into a concrete task dict for one driver."""
    reg = DRIVER_REGISTRY[driver_key]
    sid = spec["scenario_id"]
    steps = spec["steps"]
    layer = spec.get("layer", "L1")
    features = [f"{reg['feature_prefix']}.op.{op}" for op in _unique_ops(steps)]
    features += list(spec.get("cdp_anchors", []))
    diagnostic = bool(spec.get("diagnostic"))
    tags = [f"family.{spec['family']}", "version.v0_4"] + list(spec.get("extra_tags", []))
    if diagnostic:
        # Observation-surface diagnostics are graded
        # best-effort against Chrome instead of gating, and tagged for the
        # reporting layer to keep them out of headline pass-rates.
        tags.append("purpose.diagnostic")
    spec_rel = f"tasks/scenarios/{sid}{SCENARIO_SUFFIX}"
    driver = {
        "kind": reg["kind"],
        "steps": steps,
        "checks": spec.get("checks", []),
    }
    if transport_policy := reg.get("transport_policy"):
        driver["transport_policy"] = transport_policy
    task = {
        GENERATED_MARKER: spec_rel,
        "artifact_profile": ARTIFACT_PROFILE_BY_LAYER[layer],
        "chrome_gate": "best_effort" if diagnostic else "required",
        "description": spec["description"],
        "driver": driver,
        "features": features,
        "grader": {"checks": ["answer_matches_expected"], "endpoint": GRADE_ENDPOINT, "kind": "server_side"},
        "layer": layer,
        "scene": spec["scene"],
        "subset_id": reg["subset"],
        "tags": tags,
        "task_id": f"sc_{sid}__{reg['suffix']}",
        "task_version": int(reg.get("task_version", spec.get("task_version", 1))),
        "timeouts": spec.get("timeouts", DEFAULT_TIMEOUTS),
        "title": f"{spec['title']} ({driver_key})",
    }
    if "connect_options" in spec:
        task["driver"]["connect_options"] = spec["connect_options"]
    if "launch_profile" in spec:
        task["launch_profile"] = spec["launch_profile"]
    return task


def render_task_json(task: dict[str, Any]) -> str:
    """Canonical on-disk form: sorted keys, 2-space indent, trailing newline."""
    return json.dumps(task, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def generated_path(spec: dict[str, Any], driver_key: str) -> pathlib.Path:
    reg = DRIVER_REGISTRY[driver_key]
    return BENCH_ROOT / reg["dir"] / f"sc_{spec['scenario_id']}__{reg['suffix']}.json"


def expected_fragment_content(specs: list[dict[str, Any]]) -> str:
    """Canonical content of the generated expected-answer fragment."""
    entries: dict[str, Any] = {
        "_comment": "GENERATED from tasks/scenarios/*.scenario.json by `python3 -m runner.run scenarios` — do not edit; the fixture server merges this into its server-side expected-answer registry."
    }
    for spec in specs:
        expected = spec.get("expected_answer")
        if not isinstance(expected, dict):
            continue
        for driver_key in spec.get("drivers", []):
            if driver_key not in DRIVER_REGISTRY:
                continue
            task_id = f"sc_{spec['scenario_id']}__{DRIVER_REGISTRY[driver_key]['suffix']}"
            entries[task_id] = {"mode": expected.get("mode", "equals"), "expected": expected["expected"]}
    return json.dumps(entries, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def planned_outputs(specs: list[dict[str, Any]]) -> dict[pathlib.Path, str]:
    """Map every generated file path to its canonical content for the roster."""
    out: dict[pathlib.Path, str] = {}
    for spec in specs:
        for driver_key in spec.get("drivers", []):
            if driver_key not in DRIVER_REGISTRY:
                continue
            out[generated_path(spec, driver_key)] = render_task_json(expand_scenario(spec, driver_key))
    out[BENCH_ROOT / EXPECTED_FRAGMENT] = expected_fragment_content(specs)
    return out


def existing_generated_files() -> set[pathlib.Path]:
    """All committed task files that carry the generated marker."""
    found: set[pathlib.Path] = set()
    for reg in DRIVER_REGISTRY.values():
        for path in (BENCH_ROOT / reg["dir"]).glob("sc_*.json"):
            try:
                if GENERATED_MARKER in json.loads(path.read_text()):
                    found.add(path)
            except Exception:
                continue
    return found


def check_sync(specs: list[dict[str, Any]] | None = None) -> tuple[bool, list[str]]:
    """Return (in_sync, diffs). A diff is any missing/stale/orphaned generated file."""
    specs = specs if specs is not None else load_scenarios()
    planned = planned_outputs(specs)
    diffs: list[str] = []
    for path, content in sorted(planned.items(), key=lambda kv: str(kv[0])):
        rel = path.relative_to(BENCH_ROOT)
        if not path.exists():
            diffs.append(f"missing generated file: {rel}")
        elif path.read_text() != content:
            diffs.append(f"stale generated file (spec changed): {rel}")
    for path in sorted(existing_generated_files()):
        if path not in planned:
            diffs.append(f"orphaned generated file (no scenario): {path.relative_to(BENCH_ROOT)}")
    return (not diffs), diffs


def generate(specs: list[dict[str, Any]] | None = None) -> tuple[list[pathlib.Path], list[pathlib.Path]]:
    """Write all planned files; remove orphaned generated files. Returns (written, removed)."""
    specs = specs if specs is not None else load_scenarios()
    planned = planned_outputs(specs)
    written: list[pathlib.Path] = []
    for path, content in sorted(planned.items(), key=lambda kv: str(kv[0])):
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists() or path.read_text() != content:
            path.write_text(content)
            written.append(path)
    removed: list[pathlib.Path] = []
    for path in sorted(existing_generated_files()):
        if path not in planned:
            path.unlink()
            removed.append(path)
    return written, removed
