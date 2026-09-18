"""Choose layout from an explicit allowlist of layout-independent contracts.

The policy never reads measured outcomes. New features, commands, scripts,
scenes, or grading contracts require layout until independently classified.
"""

from __future__ import annotations

from typing import Any


POLICY_ID = "contract_layout_v2"
# Protocol metadata reads require neither page geometry nor script execution.
LAYOUT_INDEPENDENT_OPERATIONS = {
    "Browser.getVersion": "cdp.browser.get_version",
    "Schema.getDomains": "cdp.schema.get_domains",
}
_STEP_FIELDS = frozenset({"method", "params", "optional", "save_as", "session"})


def choose_layout(task: dict[str, Any]) -> str:
    """Use mock layout only for a completely recognized metadata-read contract."""
    driver = task.get("driver")
    if not isinstance(driver, dict) or driver.get("kind") != "raw_cdp":
        return "on"
    if set(driver) != {"kind", "steps"}:
        return "on"
    if task.get("scene") != {"kind": "about_blank"}:
        return "on"
    if task.get("grader") != {"kind": "inline_assertions", "checks": [{"kind": "no_error"}]}:
        return "on"
    features = task.get("features")
    if not isinstance(features, list) or not features or any(not isinstance(item, str) for item in features):
        return "on"
    steps = driver["steps"]
    if not isinstance(steps, list) or not steps:
        return "on"
    recognized_features: set[str] = set()
    for step in steps:
        if not isinstance(step, dict) or not set(step).issubset(_STEP_FIELDS):
            return "on"
        method = step.get("method")
        if not isinstance(method, str) or method not in LAYOUT_INDEPENDENT_OPERATIONS:
            return "on"
        if step.get("params", {}) != {} or step.get("session", "page") not in ("page", "browser"):
            return "on"
        if "optional" in step and not isinstance(step["optional"], bool):
            return "on"
        if "save_as" in step and not isinstance(step["save_as"], str):
            return "on"
        recognized_features.add(LAYOUT_INDEPENDENT_OPERATIONS[method])
    return "off" if set(features) == recognized_features else "on"
