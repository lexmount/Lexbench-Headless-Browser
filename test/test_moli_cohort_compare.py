"""The version comparator must reject changes outside the Moli binary."""

import copy
import json

import pytest


from tools import compare_moli_cohort as comparator
from tools.compare_moli_cohort import first_difference, normalized_conditions, normalized_manifest


def manifest():
    return {
        "run_id": "a", "started_at": "now", "site": {"base_url": "http://127.0.0.1:123"},
        "engine_set": {"name": "a", "manifest": "build_artifacts/active-set.json"},
        "engines": {"moli": {"version": "1", "sha256": "aaa", "expected_sha256": "aaa", "binary": "build_artifacts/moli/bin/moli"}},
        "runner": {"source": {"tree_sha256": "source"}, "fixtures": {"tree_sha256": "fixtures"}, "jobs": 8},
        "host": {"platform": "macOS"},
        "resolved_tasks": [{"task_id": "one", "version": "1"}],
    }


def test_only_moli_version_and_local_run_fields_may_change():
    left = manifest()
    right = copy.deepcopy(left)
    right["run_id"] = "b"
    right["site"]["base_url"] = "http://127.0.0.1:456"
    right["engine_set"]["name"] = "b"
    right["engines"]["moli"].update(version="2", sha256="bbb", expected_sha256="bbb")
    assert first_difference(normalized_manifest(left), normalized_manifest(right)) is None


def test_runner_fixture_task_host_and_jobs_changes_are_rejected():
    left = manifest()
    changes = (
        ("runner", "source", "tree_sha256"),
        ("runner", "fixtures", "tree_sha256"),
        ("host", "platform"),
        ("runner", "jobs"),
        ("resolved_tasks", 0, "version"),
    )
    for path in changes:
        right = copy.deepcopy(left)
        target = right
        for part in path[:-1]:
            target = target[part]
        target[path[-1]] = "changed"
        assert first_difference(normalized_manifest(left), normalized_manifest(right))


def test_candidate_commit_is_binary_identity_not_a_run_condition():
    left = {
        "run_id": "base", "moli_sha256": "aaa", "moli_version": "1.1.8",
        "moli_commit": None, "chromedriver_sha256": "driver", "profile_sha256": "profile",
    }
    right = {
        **left, "run_id": "candidate", "moli_sha256": "bbb", "moli_version": "1.1.9",
        "moli_commit": "7" * 40,
    }
    assert normalized_conditions(left) == normalized_conditions(right)
    right["chromedriver_sha256"] = "other"
    assert normalized_conditions(left) != normalized_conditions(right)


@pytest.fixture
def complete_run(tmp_path, monkeypatch):
    profile = {"attempts_per_task": 3, "bench_manifest_sha256": "tasks"}
    monkeypatch.setattr(comparator, "frozen_tasks", lambda: (profile, ["one"]))
    run = manifest()
    run.update(completion_status="completed", completed_result_rows=3,
               bench_manifest={"sha256": "tasks"}, selected_engines=["moli"], k_runs=3)
    rows = [{"run_id": "a", "engine": "moli", "task_id": "one", "attempt": i,
             "engine_provenance": {"binary_sha256": "aaa"}}
            for i in range(1, 4)]
    (tmp_path / "run_manifest.json").write_text(json.dumps(run))
    (tmp_path / "results.jsonl").write_text("\n".join(map(json.dumps, rows)))
    return tmp_path, rows


def test_complete_run_accepts_matching_physical_results(complete_run):
    path, rows = complete_run
    assert comparator.load_run(path)[1] == rows


@pytest.mark.parametrize("field,value", [
    ("engine", "chrome"), ("run_id", "other"),
    ("engine_provenance", {"binary_sha256": "other"}),
    ("engine_provenance", {}),
])
def test_complete_run_rejects_foreign_or_unbound_physical_results(complete_run, field, value):
    path, rows = complete_run
    rows[1][field] = value
    (path / "results.jsonl").write_text("\n".join(map(json.dumps, rows)))
    with pytest.raises(ValueError, match="physical result identity"):
        comparator.load_run(path)
