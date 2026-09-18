"""Retry failed cases once as a complete k-attempt layout-on batch."""
from __future__ import annotations
import collections
import copy
import hashlib
from pathlib import Path

POLICY_ID = "failed_case_layout_rerun_v1"

def policy(mode: str, try_layout: bool = False) -> dict:
    if mode not in {"off", "on"}:
        raise ValueError("layout mode must be off or on")
    return {"policy_id": POLICY_ID, "initial_layout": mode, "try_layout": try_layout,
            "retry_layout": "on" if mode == "off" and try_layout else None,
            "retry_scope": "failed_cases_after_complete_run", "retry_attempts": "same_k",
            "final_result": "all_pass_rerun_replaces_original_case"}

def groups(rows):
    result = collections.defaultdict(list)
    for row in rows:
        if row["engine"] == "moli":
            result[row["task_id"]].append(row)
    return result

def failed_cases(rows, k):
    selected = set()
    for task_id, attempts in groups(rows).items():
        if len(attempts) != k or {row["attempt"] for row in attempts} != set(range(1,k+1)):
            raise ValueError("incomplete original case")
        # A mandatory Chrome gate rejection is not an executed Moli failure.
        if any(row["status"] == "chrome_gate_fail" for row in attempts):
            continue
        if not all(row["status"] == "pass" for row in attempts):
            selected.add(task_id)
    return selected

def pass_count(rows):
    return sum(all(row["status"] == "pass" for row in attempts) for attempts in groups(rows).values())

def replace_cases(initial, retries, k, run_dir: Path, run_id: str):
    selected = failed_cases(initial,k)
    expected = {(task_id, attempt) for task_id in selected for attempt in range(1,k+1)}
    replacement = {(row["task_id"],row["attempt"]):row for row in retries}
    if len(replacement) != len(retries) or set(replacement) != expected:
        raise ValueError("incomplete or unexpected layout rerun")
    originals={(row["task_id"],row["attempt"]):row for row in initial if row["engine"]=="moli"}
    for key,new in replacement.items():
        if new["engine"] != "moli" or new["seed"] != originals[key]["seed"] or new.get("engine_provenance",{}).get("layout_enabled") is not True:
            raise ValueError("layout rerun input or launch mismatch")
    recovered={task_id for task_id,rows in groups(retries).items() if all(row["status"]=="pass" for row in rows)}
    result=[]
    for old in initial:
        key=(old["task_id"],old["attempt"])
        if old["engine"] != "moli" or old["task_id"] not in recovered:
            result.append(old)
            continue
        new=copy.deepcopy(replacement[key])
        if new["engine"] != "moli" or new["seed"] != old["seed"] or new.get("engine_provenance",{}).get("layout_enabled") is not True:
            raise ValueError("layout rerun input or launch mismatch")
        new["run_id"]=run_id
        new["layout_retry"]={
            "policy_id":POLICY_ID,
            "original_artifact_dir":old["artifact_dir"],
            "original_run_sha256":hashlib.sha256((run_dir/old["artifact_dir"]/"run.json").read_bytes()).hexdigest(),
            "retry_run_sha256":hashlib.sha256((run_dir/new["artifact_dir"]/"run.json").read_bytes()).hexdigest(),
            "original_status":old["status"],
            "total_execution_duration_ms":old["duration_ms"]+new["duration_ms"],
        }
        result.append(new)
    return result


def verify(run_dir: Path, manifest: dict, final_rows: list) -> None:
    if (manifest.get("moli_layout_policy") or {}).get("retry_layout") == "on" and manifest.get("completion_status") != "completed":
        raise ValueError("layout recovery run is incomplete")
    receipt=manifest.get("layout_retry")
    if not receipt:
        return
    import json
    matrices=[]
    for label in ("initial", "retry"):
        name=receipt[label+"_results"]
        path=run_dir/name
        if Path(name).name != name or path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest()!=receipt[label+"_results_sha256"]:
            raise ValueError("layout evidence matrix hash mismatch")
        matrices.append([json.loads(line) for line in path.read_text().splitlines()])
    if hashlib.sha256((run_dir/'results.jsonl').read_bytes()).hexdigest()!=receipt['final_results_sha256']:
        raise ValueError("final matrix hash mismatch")
    if replace_cases(*matrices,int(manifest['k_runs']),run_dir,manifest['run_id']) != final_rows:
        raise ValueError("final matrix does not match complete successful reruns")
