"""The version comparator must reject changes outside the Moli binary."""

import copy
from pathlib import Path
import sys


TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))
from compare_moli_cohort import first_difference, normalized_manifest  # noqa: E402


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

def test_fixed_layout_ignores_annotation_drift_but_not_launch_drift():
    left=manifest();left['engines']['moli']['layout_mode']='on'
    left['moli_layout_policy']={'policy_id':'task_layout_v3','qualification':None,'assignments_sha256':'first','assignments':[{'task_id':'one','task_sha256':'frozen','layout':'on','requirement':'required','reason':'evidence'}]}
    right=copy.deepcopy(left);right['moli_layout_policy']['assignments_sha256']='second'
    right['moli_layout_policy']['assignments'][0].update(requirement='unknown',reason='different_binary')
    assert first_difference(normalized_manifest(left),normalized_manifest(right)) is None
    right['moli_layout_policy']['assignments'][0]['layout']='off'
    assert first_difference(normalized_manifest(left),normalized_manifest(right))
