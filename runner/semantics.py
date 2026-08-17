"""L2 semantic-capability contracts and capability-level reporting.

L1 rows answer whether a protocol/driver route is usable.  L2 rows are probe
evidence for Web Platform or workflow semantics, but several rows can exercise
the same underlying capability (test vectors, historical duplicates, or a
representative second driver).  This module keeps that distinction explicit:

* every active L2 task belongs to exactly one semantic capability;
* ``semantic_probe`` rows jointly produce one capability verdict per attempt;
* ``driver_cross_check`` and ``diagnostic`` rows remain visible evidence but do
  not multiply the semantic-score denominator.

The checked-in map is data, not runner policy.  Runs snapshot the relevant map
entries into ``run_manifest.json`` so reports remain reproducible after the
repository evolves.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import re
from collections import defaultdict
from typing import Any, Iterable


MAP_SCHEMA_VERSION = 1
PROBE_ROLES = {"semantic_probe", "driver_cross_check", "diagnostic"}
OBSERVABLES = {
    "dom_state",
    "storage_state",
    "network_server_state",
    "workflow_result",
}
CAPABILITY_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


def load_capability_map(path: pathlib.Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError("semantic capability map must be a JSON object")
    return payload


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def capability_map_reference(
    manifest_path: pathlib.Path,
    suite: dict[str, Any],
) -> tuple[pathlib.Path | None, bool]:
    """Resolve the optional L2 map path and whether this manifest requires it."""
    for layer in suite.get("layers", []):
        if not isinstance(layer, dict) or layer.get("layer_id") != "L2":
            continue
        required = bool(layer.get("semantic_capability_map_required"))
        value = layer.get("semantic_capability_map")
        if value is None:
            return None, required
        if not isinstance(value, str) or not value.strip():
            raise ValueError(
                "L2 `semantic_capability_map` must be a non-empty string path, "
                f"got {value!r}"
            )
        path = pathlib.Path(value)
        if not path.is_absolute():
            path = manifest_path.parent / path
        return path.resolve(), required
    return None, False


def capability_task_index(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return task_id -> capability/probe metadata for a validated map."""
    result: dict[str, dict[str, Any]] = {}
    for capability in payload.get("capabilities", []):
        if not isinstance(capability, dict):
            continue
        for probe in capability.get("probes", []):
            if not isinstance(probe, dict) or not isinstance(probe.get("task_id"), str):
                continue
            result[probe["task_id"]] = {
                "capability_id": capability.get("capability_id"),
                "title": capability.get("title"),
                "category": capability.get("category"),
                "observable": capability.get("observable"),
                "role": probe.get("role"),
                "claim": probe.get("claim"),
            }
    return result


def validate_capability_map(
    payload: dict[str, Any],
    l2_tasks: dict[str, dict[str, Any]],
    rel_path: str,
) -> list[str]:
    """Validate schema, task coverage, roles, and the L2 grading boundary."""
    errors: list[str] = []
    if payload.get("schema_version") != MAP_SCHEMA_VERSION:
        errors.append(
            f"{rel_path}: schema_version must be {MAP_SCHEMA_VERSION}"
        )
    if payload.get("layer") != "L2":
        errors.append(f"{rel_path}: layer must be `L2`")
    if payload.get("score_unit") != "semantic_capability_attempt":
        errors.append(
            f"{rel_path}: score_unit must be `semantic_capability_attempt`"
        )
    if payload.get("evaluation_axis") != "web_platform_workflow_semantic_correctness":
        errors.append(
            f"{rel_path}: evaluation_axis must be "
            "`web_platform_workflow_semantic_correctness`"
        )
    if not isinstance(payload.get("aggregation"), str) or not payload[
        "aggregation"
    ].strip():
        errors.append(f"{rel_path}: aggregation must be a non-empty string")
    probe_roles = payload.get("probe_roles")
    if not isinstance(probe_roles, dict) or set(probe_roles) != PROBE_ROLES:
        errors.append(
            f"{rel_path}: probe_roles must define exactly "
            f"{', '.join(sorted(PROBE_ROLES))}"
        )
    elif not all(isinstance(value, str) and value.strip() for value in probe_roles.values()):
        errors.append(f"{rel_path}: probe_roles descriptions must be non-empty strings")

    capabilities = payload.get("capabilities")
    if not isinstance(capabilities, list) or not capabilities:
        return errors + [f"{rel_path}: capabilities must be a non-empty list"]

    seen_capabilities: set[str] = set()
    seen_tasks: dict[str, str] = {}
    for index, capability in enumerate(capabilities):
        where = f"{rel_path}: capabilities[{index}]"
        if not isinstance(capability, dict):
            errors.append(f"{where} must be an object")
            continue
        capability_id = capability.get("capability_id")
        if not isinstance(capability_id, str) or not CAPABILITY_ID_RE.fullmatch(
            capability_id
        ):
            errors.append(f"{where}.capability_id must be a normalized identifier")
            capability_id = f"<invalid-{index}>"
        elif capability_id in seen_capabilities:
            errors.append(f"{rel_path}: duplicate capability_id `{capability_id}`")
        else:
            seen_capabilities.add(capability_id)
        for field in ("title", "category", "description"):
            if not isinstance(capability.get(field), str) or not capability[field].strip():
                errors.append(f"{where}.{field} must be a non-empty string")
        if capability.get("observable") not in OBSERVABLES:
            errors.append(
                f"{where}.observable must be one of {', '.join(sorted(OBSERVABLES))}"
            )

        probes = capability.get("probes")
        if not isinstance(probes, list) or not probes:
            errors.append(f"{where}.probes must be a non-empty list")
            continue
        semantic_count = 0
        cross_check_count = 0
        for probe_index, probe in enumerate(probes):
            probe_where = f"{where}.probes[{probe_index}]"
            if not isinstance(probe, dict):
                errors.append(f"{probe_where} must be an object")
                continue
            task_id = probe.get("task_id")
            role = probe.get("role")
            claim = probe.get("claim")
            if not isinstance(task_id, str) or not task_id:
                errors.append(f"{probe_where}.task_id must be a non-empty string")
                continue
            if task_id in seen_tasks:
                errors.append(
                    f"{rel_path}: task `{task_id}` is assigned to both "
                    f"`{seen_tasks[task_id]}` and `{capability_id}`"
                )
            else:
                seen_tasks[task_id] = capability_id
            if role not in PROBE_ROLES:
                errors.append(
                    f"{probe_where}.role must be one of {', '.join(sorted(PROBE_ROLES))}"
                )
            elif role == "semantic_probe":
                semantic_count += 1
            elif role == "driver_cross_check":
                cross_check_count += 1
            if not isinstance(claim, str) or not claim.strip():
                errors.append(f"{probe_where}.claim must be a non-empty string")

            task = l2_tasks.get(task_id)
            if task is None:
                errors.append(f"{probe_where} references unknown active L2 task `{task_id}`")
                continue
            tags = task.get("tags") or []
            is_diagnostic = "purpose.diagnostic" in tags
            if is_diagnostic and role != "diagnostic":
                errors.append(
                    f"{probe_where}: purpose.diagnostic task must use role `diagnostic`"
                )
            if role == "diagnostic" and not is_diagnostic:
                errors.append(
                    f"{probe_where}: role `diagnostic` requires tag `purpose.diagnostic`"
                )
            if role in {"semantic_probe", "driver_cross_check"}:
                grader_kind = (task.get("grader") or {}).get("kind")
                if grader_kind != "server_side":
                    errors.append(
                        f"{probe_where}: scored/cross-check L2 task must use a "
                        f"server_side grader, got `{grader_kind}`"
                    )
        if cross_check_count and not semantic_count:
            errors.append(
                f"{where}: driver_cross_check probes require at least one semantic_probe"
            )

    mapped = set(seen_tasks)
    active = set(l2_tasks)
    for task_id in sorted(active - mapped):
        errors.append(f"{rel_path}: active L2 task `{task_id}` has no capability assignment")
    for task_id in sorted(mapped - active):
        errors.append(f"{rel_path}: capability map references non-L2 task `{task_id}`")
    return errors


def capability_map_snapshot(
    payload: dict[str, Any],
    selected_task_ids: Iterable[str],
    *,
    path: str,
    sha256: str,
) -> dict[str, Any]:
    """Snapshot map entries touched by this run while retaining full probe sets."""
    selected = set(selected_task_ids)
    capabilities = []
    for capability in payload.get("capabilities", []):
        probes = capability.get("probes", []) if isinstance(capability, dict) else []
        if any(probe.get("task_id") in selected for probe in probes if isinstance(probe, dict)):
            item = dict(capability)
            item["selected_task_ids"] = [
                probe["task_id"]
                for probe in probes
                if isinstance(probe, dict) and probe.get("task_id") in selected
            ]
            capabilities.append(item)
    return {
        "schema_version": payload.get("schema_version"),
        "path": path,
        "sha256": sha256,
        "evaluation_axis": payload.get("evaluation_axis"),
        "score_unit": payload.get("score_unit"),
        "aggregation": payload.get("aggregation"),
        "probe_roles": payload.get("probe_roles"),
        "capabilities": capabilities,
    }


def _empty_stats() -> dict[str, Any]:
    return {
        "total": 0,
        "pass": 0,
        "pass_rate": 0.0,
        "scored_total": 0,
        "scored_pass": 0,
        "scored_pass_rate": 0.0,
        "missing": 0,
    }


def _finalize_stats(stats: dict[str, Any]) -> None:
    stats["pass_rate"] = stats["pass"] / stats["total"] if stats["total"] else 0.0
    stats["scored_pass_rate"] = (
        stats["scored_pass"] / stats["scored_total"]
        if stats["scored_total"]
        else 0.0
    )


def _summarize_evidence_rows(
    rows: list[dict[str, Any]],
    task_ids: set[str],
    resolved_by_task: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    by_engine: dict[str, dict[str, Any]] = {}
    by_driver: dict[str, dict[str, Any]] = {}
    for row in rows:
        task_id = str(row.get("task_id") or "")
        if task_id not in task_ids:
            continue
        engine = str(row.get("engine") or "unknown")
        driver = str((resolved_by_task.get(task_id) or {}).get("driver") or "unknown")
        for bucket, key in ((by_engine, engine), (by_driver, driver)):
            stats = bucket.setdefault(key, {"total": 0, "pass": 0, "pass_rate": 0.0})
            stats["total"] += 1
            if row.get("status") == "pass":
                stats["pass"] += 1
    for bucket in (by_engine, by_driver):
        for stats in bucket.values():
            stats["pass_rate"] = stats["pass"] / stats["total"] if stats["total"] else 0.0
    return {
        "task_count": len(task_ids),
        "row_count": sum(stats["total"] for stats in by_engine.values()),
        "by_engine": by_engine,
        "by_driver": by_driver,
    }


def summarize_semantic_results(
    run_manifest: dict[str, Any],
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """Collapse L2 probe rows into one all-probes-pass capability verdict."""
    snapshot = run_manifest.get("l2_semantic_capability_map")
    if not isinstance(snapshot, dict):
        return {
            "available": False,
            "reason": "run manifest has no L2 semantic capability snapshot",
            "by_engine": {},
            "capabilities": {},
            "driver_cross_checks": {"task_count": 0, "row_count": 0, "by_engine": {}, "by_driver": {}},
            "diagnostics": {"task_count": 0, "row_count": 0, "by_engine": {}, "by_driver": {}},
            "pairwise": {},
        }

    resolved_tasks = run_manifest.get("resolved_tasks") or []
    resolved_by_task = {
        str(task.get("task_id")): task
        for task in resolved_tasks
        if isinstance(task, dict) and task.get("task_id")
    }
    selected = set(resolved_by_task)
    row_index: dict[tuple[str, str, int], dict[str, Any]] = {}
    for row in rows:
        if row.get("layer") != "L2":
            continue
        row_index[(str(row.get("task_id")), str(row.get("engine")), int(row.get("attempt") or 0))] = row

    by_engine: dict[str, dict[str, Any]] = {}
    capability_results: dict[str, Any] = {}
    verdicts: dict[tuple[str, int], dict[str, bool]] = defaultdict(dict)
    cross_check_ids: set[str] = set()
    diagnostic_ids: set[str] = set()
    semantic_capability_count = 0
    complete_selection_count = 0

    for capability in snapshot.get("capabilities", []):
        if not isinstance(capability, dict):
            continue
        capability_id = str(capability.get("capability_id") or "")
        probes = [probe for probe in capability.get("probes", []) if isinstance(probe, dict)]
        semantic_ids = {
            str(probe.get("task_id"))
            for probe in probes
            if probe.get("role") == "semantic_probe"
        }
        cross_check_ids.update(
            str(probe.get("task_id"))
            for probe in probes
            if probe.get("role") == "driver_cross_check"
        )
        diagnostic_ids.update(
            str(probe.get("task_id"))
            for probe in probes
            if probe.get("role") == "diagnostic"
        )
        complete_selection = bool(semantic_ids) and semantic_ids.issubset(selected)
        if semantic_ids:
            semantic_capability_count += 1
        if complete_selection:
            complete_selection_count += 1

        keys = {
            (engine, attempt)
            for task_id, engine, attempt in row_index
            if task_id in semantic_ids
        }
        cap_by_engine: dict[str, dict[str, Any]] = {}
        for engine, attempt in sorted(keys):
            stats = by_engine.setdefault(engine, _empty_stats())
            cap_stats = cap_by_engine.setdefault(engine, _empty_stats())
            matching = [row_index.get((task_id, engine, attempt)) for task_id in semantic_ids]
            if not complete_selection or any(row is None for row in matching):
                stats["missing"] += 1
                cap_stats["missing"] += 1
                continue
            evidence = [row for row in matching if row is not None]
            passed = all(row.get("status") == "pass" for row in evidence)
            score_included = all(bool(row.get("score_included")) for row in evidence)
            for target in (stats, cap_stats):
                target["total"] += 1
                target["pass"] += int(passed)
                if score_included:
                    target["scored_total"] += 1
                    target["scored_pass"] += int(passed)
            verdicts[(capability_id, attempt)][engine] = passed

        for stats in cap_by_engine.values():
            _finalize_stats(stats)
        capability_results[capability_id] = {
            "title": capability.get("title"),
            "category": capability.get("category"),
            "observable": capability.get("observable"),
            "semantic_probe_task_ids": sorted(semantic_ids),
            "driver_cross_check_task_ids": sorted(
                str(probe.get("task_id"))
                for probe in probes
                if probe.get("role") == "driver_cross_check"
            ),
            "diagnostic_task_ids": sorted(
                str(probe.get("task_id"))
                for probe in probes
                if probe.get("role") == "diagnostic"
            ),
            "complete_selection": complete_selection,
            "by_engine": cap_by_engine,
        }

    for stats in by_engine.values():
        _finalize_stats(stats)

    selected_candidates = [
        engine
        for engine in (run_manifest.get("selected_engines") or [])
        if engine != "chrome"
    ]
    pairwise: dict[str, dict[str, Any]] = {}
    for left_index, left in enumerate(selected_candidates):
        for right in selected_candidates[left_index + 1 :]:
            counts = {
                "left": left,
                "right": right,
                "left_only": 0,
                "right_only": 0,
                "both_pass": 0,
                "both_fail": 0,
                "missing": 0,
            }
            for statuses in verdicts.values():
                if left not in statuses or right not in statuses:
                    counts["missing"] += 1
                elif statuses[left] and statuses[right]:
                    counts["both_pass"] += 1
                elif statuses[left]:
                    counts["left_only"] += 1
                elif statuses[right]:
                    counts["right_only"] += 1
                else:
                    counts["both_fail"] += 1
            pairwise[f"{left}__{right}"] = counts

    return {
        "available": True,
        "unit": snapshot.get("score_unit"),
        "aggregation": snapshot.get("aggregation"),
        "map": {
            "path": snapshot.get("path"),
            "sha256": snapshot.get("sha256"),
            "schema_version": snapshot.get("schema_version"),
        },
        "semantic_capabilities": semantic_capability_count,
        "complete_selection_capabilities": complete_selection_count,
        "by_engine": by_engine,
        "capabilities": capability_results,
        "driver_cross_checks": _summarize_evidence_rows(
            rows, cross_check_ids & selected, resolved_by_task
        ),
        "diagnostics": _summarize_evidence_rows(
            rows, diagnostic_ids & selected, resolved_by_task
        ),
        "pairwise": pairwise,
    }
