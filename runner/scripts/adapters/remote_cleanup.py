"""Shared result contract for remote adapter-owned target cleanup."""

from __future__ import annotations

from typing import Any


def apply_cleanup_contract(
    outcome: dict[str, Any],
    cleanup: dict[str, Any],
    *,
    label: str,
) -> dict[str, Any]:
    observations = {
        **(outcome.get("observations") or {}),
        "target_cleanup": cleanup,
        "isolation_restored": cleanup.get("confirmed") is True,
    }
    if cleanup.get("confirmed") is True:
        return {**outcome, "observations": observations}
    return {
        "ok": False,
        "status": "infra",
        "error": {
            "class": "script_error",
            "message": f"{label} target cleanup was not confirmed: {cleanup!r}",
        },
        "answer": outcome.get("answer"),
        "observations": {**observations, "primary_outcome": outcome},
        "metrics": outcome.get("metrics") or {},
    }
