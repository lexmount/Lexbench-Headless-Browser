"""Pre-call Moli layout choice from the frozen task contract.

The policy does not inspect results. Unknown driver shapes fail closed to
on-demand layout, because the primary objective is task completion.
"""

from __future__ import annotations

import json
import re
from typing import Any


POLICY_ID = "contract_layout_v1"
CONTRACT_INSPECTABLE_DRIVERS = frozenset({"raw_cdp", "node_cdp_probe"})
LAYOUT_FEATURE = re.compile(
    r"(?:^|\.)(?:layout|geometry|input|mouse|touch|scroll|screenshot|"
    r"hit_test|box_model|content_quads|computed_style|intersection|paint|pdf|"
    r"highlight|overlay|window_bounds|contents_size)(?:\.|$)",
    re.IGNORECASE,
)
LAYOUT_OPERATION = re.compile(
    r"getBoundingClientRect|elementFromPoint|elementsFromPoint|"
    r"(?:offset|client|scroll)(?:Width|Height|Top|Left)|"
    r"dispatchMouseEvent|dispatchTouchEvent|captureScreenshot|printToPDF|"
    r"getBoxModel|getContentQuads|getLayoutMetrics|scrollIntoView|"
    r"highlight|setWindowBounds|getWindowBounds|setContentsSize|"
    r"setDefaultBackgroundColorOverride|setEmulatedVisionDeficiency|setEmulatedOSTextScale",
    re.IGNORECASE,
)


def choose_layout(task: dict[str, Any]) -> str:
    """Return ``on`` or ``off`` before any attempt is executed."""
    driver = task.get("driver")
    if not isinstance(driver, dict) or driver.get("kind") not in CONTRACT_INSPECTABLE_DRIVERS:
        return "on"
    features = task.get("features")
    if not isinstance(features, list) or not features or any(not isinstance(item, str) for item in features):
        return "on"
    if any(item.startswith("tool.agent_browser.") for item in features):
        return "on"
    if any(LAYOUT_FEATURE.search(item) for item in features):
        return "on"
    if LAYOUT_OPERATION.search(json.dumps(driver, ensure_ascii=False, sort_keys=True)):
        return "on"
    return "off"
