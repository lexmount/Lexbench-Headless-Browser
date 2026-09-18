"""The historical failure cohort is selected solely from Moli outcomes."""
import json

import pytest

from tools import run_moli_cohort as cohort


def write_cohort(tmp_path, monkeypatch, selected):
    (tmp_path / "manifest.json").write_text("{}\n")
    task_file = tmp_path / "task-ids.txt"
    task_file.write_text("\n".join(selected) + "\n")
    source = tmp_path / "runs" / "historical" / "results.jsonl"
    source.parent.mkdir(parents=True)
    rows = []
    for task, statuses in {
        "failed": ["fail", "fail", "fail"],
        "mixed": ["pass", "fail", "pass"],
        "passed": ["pass", "pass", "pass"],
    }.items():
        rows.extend({"engine": "moli", "task_id": task, "status": status} for status in statuses)
    rows.extend({"engine": "chrome", "task_id": "failed", "status": "fail"} for _ in range(3))
    source.write_text("".join(json.dumps(row) + "\n" for row in rows))
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({
        "task_ids_file": task_file.name,
        "task_ids_sha256": cohort.file_sha256(task_file),
        "task_count": len(selected),
        "bench_manifest_sha256": cohort.file_sha256(tmp_path / "manifest.json"),
        "source_run_id": "historical",
        "source_results_sha256": cohort.file_sha256(source),
        "source_attempts_per_task": 3,
    }))
    monkeypatch.setattr(cohort, "ROOT", tmp_path)
    monkeypatch.setattr(cohort, "PROFILE", profile)


def test_cohort_includes_mixed_moli_outcomes_and_chrome_failures(tmp_path, monkeypatch):
    write_cohort(tmp_path, monkeypatch, ["failed", "mixed"])
    assert cohort.frozen_tasks()[1] == ["failed", "mixed"]


@pytest.mark.parametrize("selected", [["mixed"], ["failed"], ["failed", "mixed", "passed"]])
def test_cohort_rejects_missing_failures_and_added_passes(tmp_path, monkeypatch, selected):
    write_cohort(tmp_path, monkeypatch, selected)
    with pytest.raises(ValueError, match="all historical Moli cases"):
        cohort.frozen_tasks()


def test_checked_in_cohort_keeps_all_372_failures():
    profile, task_ids = cohort.frozen_tasks()
    assert profile["chrome_baseline"] == "off"
    assert profile["task_count"] == 372
    assert "pw_raw_browser_getbrowsercommandline" in task_ids
    assert "pw_raw_schema_getdomains" in task_ids
