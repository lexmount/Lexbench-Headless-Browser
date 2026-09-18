"""Select complete successful layout reruns and preserve both evidence matrices."""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
import copy
import hashlib
import json
from pathlib import Path
from typing import Any

Row = dict[str, Any]
POLICY_ID = "failed_case_layout_rerun_v1"


def policy(mode: str, try_layout: bool = False) -> Row:
    if mode not in {"off", "on"}:
        raise ValueError("layout mode must be off or on")
    return {
        "policy_id": POLICY_ID,
        "initial_layout": mode,
        "try_layout": try_layout,
        "retry_layout": "on" if mode == "off" and try_layout else None,
        "retry_scope": "failed_cases_after_complete_run",
        "retry_attempts": "same_k",
        "final_result": "all_pass_rerun_replaces_original_case",
    }


def groups(rows: list[Row]) -> dict[str, list[Row]]:
    by_task = defaultdict(list)
    for row in rows:
        if row["engine"] == "moli":
            by_task[row["task_id"]].append(row)
    return dict(by_task)


def failed_cases(rows: list[Row], k: int) -> set[str]:
    selected = set()
    for task_id, attempts in groups(rows).items():
        if len(attempts) != k or {row["attempt"] for row in attempts} != set(range(1, k + 1)):
            raise ValueError(f"incomplete original case: {task_id}")
        # A Chrome gate rejection is not an executed Moli failure.
        if any(row["status"] == "chrome_gate_fail" for row in attempts):
            continue
        if not all(row["status"] == "pass" for row in attempts):
            selected.add(task_id)
    return selected


def _passed_cases(rows: list[Row]) -> set[str]:
    return {
        task_id for task_id, attempts in groups(rows).items()
        if all(row["status"] == "pass" for row in attempts)
    }


def pass_count(rows: list[Row]) -> int:
    return len(_passed_cases(rows))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_rows(path: Path) -> list[Row]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def replace_cases(
    initial: list[Row], retries: list[Row], k: int, run_dir: Path, run_id: str,
) -> list[Row]:
    """Keep original row order; replace all k rows only for recovered cases."""
    selected = failed_cases(initial, k)
    expected = {(task_id, attempt) for task_id in selected for attempt in range(1, k + 1)}
    replacement = {(row["task_id"], row["attempt"]): row for row in retries}
    if len(replacement) != len(retries) or set(replacement) != expected:
        raise ValueError("incomplete or unexpected layout rerun")

    originals = {
        (row["task_id"], row["attempt"]): row
        for row in initial if row["engine"] == "moli"
    }
    input_fields = ("engine", "seed", "task_version", "subset_id", "layer", "driver", "launch_profile")
    for key, new in replacement.items():
        old = originals[key]
        provenance = new.get("engine_provenance", {})
        same_inputs = all(new.get(field) == old.get(field) for field in input_fields)
        same_binary = (
            provenance.get("binary_sha256")
            == old.get("engine_provenance", {}).get("binary_sha256")
        )
        if not same_inputs or not same_binary or provenance.get("layout_enabled") is not True:
            raise ValueError(f"layout rerun input or launch mismatch: {key}")

    recovered = _passed_cases(retries)
    result = []
    for old in initial:
        if old["engine"] != "moli" or old["task_id"] not in recovered:
            result.append(old)
            continue
        new = copy.deepcopy(replacement[(old["task_id"], old["attempt"])])
        new["run_id"] = run_id
        new["layout_retry"] = {
            "policy_id": POLICY_ID,
            "original_artifact_dir": old["artifact_dir"],
            "original_run_sha256": _sha256(run_dir / old["artifact_dir"] / "run.json"),
            "retry_run_sha256": _sha256(run_dir / new["artifact_dir"] / "run.json"),
            "original_status": old["status"],
            "total_execution_duration_ms": old["duration_ms"] + new["duration_ms"],
        }
        result.append(new)
    return result


def rerun_failed_cases(
    run_dir: Path,
    run_id: str,
    k: int,
    execute: Callable[[Row], Row],
    notify: Callable[[str], None],
) -> Row | None:
    """Run the selected batch; publish the final matrix only when it is complete.

    The callback owns browser lifecycle and writes each physical retry result.
    An exception leaves the original matrix intact and the retry evidence on disk.
    """
    results_path = run_dir / "results.jsonl"
    initial = _read_rows(results_path)
    selected = failed_cases(initial, k)
    if not selected:
        return None

    initial_path = run_dir / "initial_results.jsonl"
    retry_path = run_dir / "layout_retry_results.jsonl"
    if initial_path.exists() or retry_path.exists():
        raise ValueError("layout rerun evidence already exists")
    initial_path.write_bytes(results_path.read_bytes())
    notify(f"Retrying {len(selected)} failed Moli cases with layout on: {k} attempts each")
    candidates = [row for row in initial if row["engine"] == "moli" and row["task_id"] in selected]
    candidates.sort(key=lambda row: (row["task_id"], row["attempt"]))
    retries = [execute(row) for row in candidates]
    if _read_rows(retry_path) != retries:
        raise ValueError("persisted layout rerun differs from executed results")
    final = replace_cases(initial, retries, k, run_dir, run_id)

    temporary = run_dir / ".final_results.jsonl"
    temporary.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in final),
        encoding="utf-8",
    )
    receipt = {
        "initial_results": initial_path.name,
        "initial_results_sha256": _sha256(initial_path),
        "retry_results": retry_path.name,
        "retry_results_sha256": _sha256(retry_path),
        "final_results_sha256": _sha256(temporary),
        "retried_cases": sorted(selected),
        "extra_executions": len(retries),
        "extra_execution_duration_ms": sum(row["duration_ms"] for row in retries),
        "recovered_cases": sorted(_passed_cases(retries)),
    }
    temporary.replace(results_path)
    notify(f"Final Moli cases: {pass_count(final)}/{len(groups(final))} passed")
    return receipt


def verify(run_dir: Path, manifest: Row, final_rows: list[Row]) -> None:
    enabled = (manifest.get("moli_layout_policy") or {}).get("retry_layout") == "on"
    if enabled and manifest.get("completion_status") != "completed":
        raise ValueError("layout recovery run is incomplete")
    receipt = manifest.get("layout_retry")
    if not receipt:
        has_reruns = (run_dir / "initial_results.jsonl").exists() or any(
            row.get("layout_retry") or (
                row["engine"] == "moli"
                and row.get("engine_provenance", {}).get("layout_enabled")
            )
            for row in final_rows
        )
        if enabled and has_reruns:
            raise ValueError("layout recovery receipt is missing")
        return

    matrices = []
    for label in ("initial", "retry"):
        name = receipt[label + "_results"]
        path = run_dir / name
        if (
            Path(name).name != name or path.is_symlink()
            or _sha256(path) != receipt[label + "_results_sha256"]
        ):
            raise ValueError("layout evidence matrix hash mismatch")
        matrices.append(_read_rows(path))
    if _sha256(run_dir / "results.jsonl") != receipt["final_results_sha256"]:
        raise ValueError("final matrix hash mismatch")
    if replace_cases(*matrices, int(manifest["k_runs"]), run_dir, manifest["run_id"]) != final_rows:
        raise ValueError("final matrix does not match complete successful reruns")
