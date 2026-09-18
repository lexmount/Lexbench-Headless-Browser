"""Three-state task requirements, separate from off/on/auto execution modes."""
from __future__ import annotations
import hashlib
import json
import re
from pathlib import Path
from typing import Any

POLICY_ID = "task_layout_v3"
REQUIREMENTS = frozenset({"required", "not_required", "unknown"})
DEFAULT_REGISTRY = Path(__file__).resolve().parents[1] / "config/moli_layout_requirements.json"
LAYOUT_INDEPENDENT_OPERATIONS = {"Browser.getVersion": "cdp.browser.get_version", "Schema.getDomains": "cdp.schema.get_domains"}
# Commands whose defined operation consumes actual page geometry. Optional
# probes do not establish a requirement for the enclosing task.
GEOMETRY_METHODS = frozenset({"Input.dispatchMouseEvent", "Input.dispatchTouchEvent", "Input.synthesizeScrollGesture", "Input.synthesizePinchGesture", "Input.synthesizeTapGesture", "DOM.getNodeForLocation", "DOM.getBoxModel", "DOM.getContentQuads", "Page.getLayoutMetrics", "Page.captureScreenshot", "Page.printToPDF"})
GEOMETRY_JS = re.compile(r"\.(?:getBoundingClientRect|getClientRects|elementFromPoint|elementsFromPoint|scrollIntoView)\s*\(|\.(?:offsetWidth|offsetHeight|offsetTop|offsetLeft|clientWidth|clientHeight|scrollWidth|scrollHeight)\b")
_STEP_FIELDS = frozenset({"method", "params", "optional", "save_as", "session"})

def classify_contract(task: dict[str, Any]) -> tuple[str, str]:
    driver = task.get("driver")
    if not isinstance(driver, dict) or driver.get("kind") != "raw_cdp":
        return "unknown", "opaque_driver"
    steps = driver.get("steps")
    if not isinstance(steps, list) or not steps or any(not isinstance(s, dict) for s in steps):
        return "unknown", "unrecognized_steps"
    for step in steps:
        if step.get("optional"):
            continue
        if step.get("method") in GEOMETRY_METHODS:
            return "required", "geometry_operation:" + step["method"]
        params = step.get("params", {})
        if step.get("method") in {"Runtime.evaluate", "Runtime.callFunctionOn"} and isinstance(params, dict):
            script = params.get("expression", params.get("functionDeclaration", ""))
            # A script can contain a dead branch or a quoted command. Without
            # parsing its complete data/control flow, keep it unclassified.
            if isinstance(script, str) and GEOMETRY_JS.search(script):
                return "unknown", "script_geometry_requires_comparison"
    if set(driver) != {"kind", "steps"} or task.get("scene") != {"kind": "about_blank"} or task.get("grader") != {"kind": "inline_assertions", "checks": [{"kind": "no_error"}]}:
        return "unknown", "unclassified_contract"
    features = task.get("features")
    if not isinstance(features, list) or not features or any(not isinstance(f, str) for f in features):
        return "unknown", "unclassified_features"
    recognized = set()
    for step in steps:
        method = step.get("method")
        if not set(step).issubset(_STEP_FIELDS) or not isinstance(method, str) or method not in LAYOUT_INDEPENDENT_OPERATIONS:
            return "unknown", "unclassified_operation"
        if step.get("params", {}) != {} or step.get("session", "page") not in ("page", "browser"):
            return "unknown", "unclassified_parameters"
        if "optional" in step and not isinstance(step["optional"], bool):
            return "unknown", "unclassified_parameters"
        if "save_as" in step and not isinstance(step["save_as"], str):
            return "unknown", "unclassified_parameters"
        recognized.add(LAYOUT_INDEPENDENT_OPERATIONS[method])
    return ("not_required", "protocol_metadata") if set(features) == recognized else ("unknown", "unclassified_features")

def load_registry(path: Path = DEFAULT_REGISTRY) -> dict[str, dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema") != "moli_layout_requirements/1":
        raise ValueError("invalid layout registry schema")
    entries = payload.get("tasks", [])
    if not isinstance(entries, list):
        raise ValueError("invalid layout registry tasks")
    by_id = {}
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("requirement") not in REQUIREMENTS or not isinstance(entry.get("task_id"), str) or not re.fullmatch(r"[0-9a-f]{64}", str(entry.get("task_sha256", ""))):
            raise ValueError("invalid layout requirement entry")
        if entry["task_id"] in by_id:
            raise ValueError("duplicate layout requirement")
        if entry.get("basis") not in {"contract", "paired_evidence", "unclassified"}:
            raise ValueError("invalid layout requirement basis")
        if entry["basis"] == "paired_evidence" and not re.fullmatch(r"[0-9a-f]{64}", str(entry.get("moli_sha256", ""))):
            raise ValueError("unbound empirical layout requirement")
        if entry["basis"] == "unclassified" and entry["requirement"] != "unknown":
            raise ValueError("unclassified requirement must remain unknown")
        if entry["basis"] == "paired_evidence" and not re.fullmatch(r"[0-9a-f]{64}", str(entry.get("evidence_sha256", ""))):
            raise ValueError("missing empirical evidence identity")
        by_id[entry["task_id"]] = entry
    return by_id

def requirement(task: dict, task_sha256: str, binary_sha256: str | None, registry: dict[str, dict]) -> dict:
    entry = registry.get(task["task_id"])
    if entry is None or entry["task_sha256"] != task_sha256:
        return {"requirement": "unknown", "reason": "missing_or_changed_task"}
    if entry["basis"] == "paired_evidence":
        if entry["moli_sha256"] != binary_sha256:
            inferred, reason = classify_contract(task)
            if inferred == "required":
                return {"requirement": inferred, "reason": reason}
            return {"requirement": "unknown", "reason": "different_binary"}
    elif entry["basis"] == "contract":
        current, _ = classify_contract(task)
        if current != entry["requirement"]:
            return {"requirement": "unknown", "reason": "changed_contract_rule"}
    return {"requirement": entry["requirement"], "reason": entry.get("reason", entry["basis"])}

def choose_layout(task: dict[str, Any]) -> str:
    """Conservative contract preview; auto resolves unknowns before scored calls."""
    state, _ = classify_contract(task)
    return "off" if state == "not_required" else "on"

def compare_outcomes(off: list[str], on: list[str], k: int = 3) -> dict:
    if k < 3 or len(off) != k or len(on) != k:
        raise ValueError("layout qualification requires complete equal cohorts, at least three attempts")
    valid = {"pass", "fail", "unsupported", "timeout", "crash", "infra", "chrome_gate_fail"}
    if any(x not in valid for x in off + on):
        raise ValueError("invalid qualification verdict")
    a, b = off.count("pass"), on.count("pass")
    # Environment errors and partial stability cannot establish necessity.
    if "infra" in off + on or "chrome_gate_fail" in off + on:
        state = "unknown"
    elif a == k:
        state = "not_required"
    elif a == 0 and b == k:
        state = "required"
    else:
        state = "unknown"
    return {"requirement": state, "layout": "on" if b > a else "off", "off_passes": a, "on_passes": b, "k": k}

def assignments(tasks, binary_sha256, registry):
    result = {}
    for task in tasks:
        entry = requirement(task.task, task.sha256, binary_sha256, registry)
        result[task.task_id] = {"task_id": task.task_id, "task_sha256": task.sha256, **entry, "layout": "on" if entry["requirement"] == "required" else "off"}
    return result
