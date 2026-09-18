#!/usr/bin/env python3
"""Run the current Moli failure cohort with one supplied binary."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "benchmarks/moli-0.1.1-failure-cohort.json"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def frozen_tasks() -> tuple[dict, list[str]]:
    profile = json.loads(PROFILE.read_text(encoding="utf-8"))
    task_file = ROOT / profile["task_ids_file"]
    if file_sha256(task_file) != profile["task_ids_sha256"]:
        raise ValueError("frozen task list hash changed")
    task_ids = task_file.read_text(encoding="utf-8").splitlines()
    if len(task_ids) != profile["task_count"] or task_ids != sorted(set(task_ids)):
        raise ValueError("frozen task list has missing, duplicate or unordered IDs")
    if file_sha256(ROOT / "manifest.json") != profile["bench_manifest_sha256"]:
        raise ValueError("benchmark dataset changed; make a new baseline profile")
    source = ROOT / "runs" / profile["source_run_id"] / "results.jsonl"
    if source.is_file():
        if file_sha256(source) != profile["source_results_sha256"]:
            raise ValueError("historical selection evidence changed")
        historical_moli = {}
        for line in source.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            if row["engine"] == "moli":
                historical_moli.setdefault(row["task_id"], []).append(row["status"])
        attempts = profile["source_attempts_per_task"]
        if not historical_moli or any(len(statuses) != attempts for statuses in historical_moli.values()):
            raise ValueError("historical Moli attempt coverage is incomplete")
        expected_ids = sorted(
            task_id for task_id, statuses in historical_moli.items()
            if any(status != "pass" for status in statuses)
        )
        if task_ids != expected_ids:
            raise ValueError("cohort must contain all historical Moli cases that did not pass every attempt")
    return profile, task_ids


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("moli_binary", type=Path, help="absolute path to the version under test")
    parser.add_argument("run_id", help="new result directory name")
    parser.add_argument("--try-layout", action="store_true", help="Rerun failed cases with layout on for the same k attempts; replace only all-pass reruns")
    parser.add_argument("--moli-layout", choices=("on", "off"), default="off")
    args = parser.parse_args()
    if not args.moli_binary.is_absolute() or not args.moli_binary.is_file() or not os.access(args.moli_binary, os.X_OK):
        parser.error("Moli must be an executable absolute path")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.run_id):
        parser.error("run_id must contain only letters, digits, underscore or hyphen")
    profile, task_ids = frozen_tasks()
    binary = args.moli_binary.resolve()
    version = subprocess.check_output([str(binary), "--version"], text=True).strip()
    binary_sha = file_sha256(binary)
    driver = ROOT / "build_artifacts/chromedriver/bin/chromedriver"
    if not driver.is_file() or not os.access(driver, os.X_OK):
        raise ValueError("pinned ChromeDriver is missing")
    driver_version = subprocess.check_output([str(driver), "--version"], text=True).split()[1]
    driver_sha = file_sha256(driver)
    run_dir = ROOT / "runs" / args.run_id
    receipt = ROOT / "runs" / f"{args.run_id}.conditions.json"
    if run_dir.exists() or receipt.exists():
        raise ValueError(f"run already exists: {run_dir}")

    lock_path = ROOT / "runs/.moli-cohort.lock"
    lock_path.parent.mkdir(exist_ok=True)
    with lock_path.open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if run_dir.exists() or receipt.exists():
            raise ValueError(f"run already exists: {run_dir}")
        link = ROOT / "build_artifacts/moli/bin/moli"
        link.parent.mkdir(parents=True, exist_ok=True)
        temporary_link = link.with_name("moli.next")
        temporary_link.unlink(missing_ok=True)
        temporary_link.symlink_to(binary)
        temporary_link.replace(link)
        active_set = ROOT / "build_artifacts/active-set.json"
        payload = {
            "name": args.run_id,
            "engines": {"moli": {
                "binary": str(link), "version": version,
                "sha256": binary_sha, "sha256_12": binary_sha[:12],
            }},
            "harness_drivers": {"chromedriver": {
                "version": driver_version, "sha256_12": driver_sha[:12],
            }},
        }
        temporary_set = active_set.with_suffix(".json.next")
        temporary_set.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        temporary_set.replace(active_set)
        conditions = {
            "schema": "lexbench_moli_cohort_conditions/1",
            "run_id": args.run_id,
            "profile_sha256": file_sha256(PROFILE),
            "task_ids_sha256": profile["task_ids_sha256"],
            "bench_manifest_sha256": profile["bench_manifest_sha256"],
            "chromedriver_sha256": driver_sha,
            "chromedriver_version": driver_version,
            "moli_sha256": binary_sha,
            "moli_version": version,
            "moli_layout": args.moli_layout,
            "try_layout": args.try_layout,
        }
        receipt.write_text(json.dumps(conditions, indent=2) + "\n", encoding="utf-8")
        env = dict(os.environ)
        env["PYTHONPATH"] = str(ROOT)
        env["AGENT_BROWSER_SOCKET_DIR"] = "/tmp/ab"
        Path(env["AGENT_BROWSER_SOCKET_DIR"]).mkdir(exist_ok=True)
        ruby = Path("/opt/homebrew/opt/ruby/bin")
        gems = Path.home() / ".browser-eval/toolchains/lexbench-gems"
        if ruby.is_dir() and gems.is_dir():
            env["PATH"] = f"{ruby}:{env['PATH']}"
            env["GEM_HOME"] = str(gems)
        command = [
            sys.executable, "-m", "runner.run", "run",
            *(part for task_id in task_ids for part in ("--task", task_id)),
            "--engines", "moli", "--score-mode", profile["score_mode"],
            "--moli-layout", args.moli_layout,
            "--chrome-baseline", profile["chrome_baseline"],
            "--seed", profile["seed"],
            "--k", str(profile["attempts_per_task"]),
            "--jobs", str(profile["jobs"]),
            "--host-telemetry", profile["host_telemetry"],
            "--resource-profile", profile["resource_profile"],
            "--run-id", args.run_id, "--run-id-conflict", "error",
            "--provenance-level", profile["provenance_level"], "--no-progress",
        ]
        print(f"Moli {version} sha256={binary_sha}; {len(task_ids)} tasks × {profile['attempts_per_task']} attempts", flush=True)
        if args.try_layout:
            command.append("--try-layout")
        subprocess.run(command, cwd=ROOT, env=env, check=True)
        if file_sha256(binary) != binary_sha or file_sha256(driver) != driver_sha:
            raise ValueError("Moli or ChromeDriver binary changed during the run")
    manifest = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
    if (manifest.get("completion_status") != "completed"
            or manifest.get("completed_result_rows") != len(task_ids) * profile["attempts_per_task"]
            or manifest["engines"]["moli"]["sha256"] != binary_sha):
        raise ValueError("run did not produce the complete pinned Moli matrix")
    if manifest["engines"]["moli"].get("layout_mode") != args.moli_layout:
        raise ValueError("Moli layout mode differs from the declared run")
    has_global_layout = "--layout" in manifest["engines"]["moli"].get("serve_args", [])
    if has_global_layout != (args.moli_layout == "on"):
        raise ValueError("Moli global layout flag differs from the declared run")
    if (manifest.get("moli_layout_policy") or {}).get("try_layout", False) != args.try_layout:
        raise ValueError("Moli retry policy differs from the declared run")
    print(f"Complete cohort: {run_dir}")


if __name__ == "__main__":
    main()
