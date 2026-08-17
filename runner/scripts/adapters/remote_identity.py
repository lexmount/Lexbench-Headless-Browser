"""Shared fail-closed identity contract for Python scenario adapters."""

from __future__ import annotations

from typing import Any


IDENTITY_FIELDS = ("product", "protocolVersion", "revision")


def normalize_identity(value: Any) -> dict[str, str]:
    source = value if isinstance(value, dict) else {}
    return {
        "product": str(source.get("product") or source.get("Browser") or ""),
        "protocolVersion": str(
            source.get("protocolVersion") or source.get("Protocol-Version") or ""
        ),
        "revision": str(source.get("revision") or ""),
    }


def require_remote_identity(value: Any, label: str) -> dict[str, str]:
    identity = normalize_identity(value)
    missing = [field for field in IDENTITY_FIELDS if not identity[field]]
    if missing:
        raise ValueError(
            f"{label} is missing required field(s): {', '.join(missing)}"
        )
    return identity


def compare_remote_identity(
    expected: dict[str, str], actual_value: Any
) -> dict[str, Any]:
    actual = normalize_identity(actual_value)
    mismatches = [
        field for field in IDENTITY_FIELDS if actual[field] != expected[field]
    ]
    return {
        "transport": "remote_cdp",
        "expected": expected,
        "actual": actual,
        "compared_fields": list(IDENTITY_FIELDS),
        "mismatches": mismatches,
        "verified": not mismatches,
        "same_connection_as_task": True,
        "reconnect_allowed": False,
    }
