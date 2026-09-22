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


@pytest.mark.parametrize('requested', ['release-1_1_9', 'Release__119', 'x' * 80])
def test_run_paths_and_receipts_use_the_runner_id(tmp_path, monkeypatch, requested):
    from runner.run import compact_run_id
    binary = tmp_path / 'moli'
    binary.write_text('binary')
    binary.chmod(0o755)
    driver = tmp_path / 'build_artifacts/chromedriver/bin/chromedriver'
    driver.parent.mkdir(parents=True)
    driver.write_text('driver')
    driver.chmod(0o755)
    profile_file = tmp_path / 'profile.json'
    profile_file.write_text('{}')
    profile = dict(task_ids_sha256='tasks', bench_manifest_sha256='bench',
                   score_mode='independent', chrome_baseline='off', seed='test',
                   attempts_per_task=3, jobs=1, host_telemetry='off',
                   resource_profile='off', provenance_level='minimal')
    monkeypatch.setattr(cohort, 'ROOT', tmp_path)
    monkeypatch.setattr(cohort, 'PROFILE', profile_file)
    monkeypatch.setattr(cohort, 'frozen_tasks', lambda: (profile, ['one']))
    monkeypatch.setattr(cohort.subprocess, 'check_output',
                        lambda cmd, **kw: 'ChromeDriver 153.0' if cmd[0] == str(driver) else 'moli 1.1.9')
    invoked = []

    def execute(command, **kwargs):
        requested_id = command[command.index('--run-id') + 1]
        invoked.append(requested_id)
        run_dir = tmp_path / 'runs' / compact_run_id(requested_id)
        run_dir.mkdir()
        manifest = dict(completion_status='completed', completed_result_rows=3,
                        engines={'moli': dict(sha256=cohort.file_sha256(binary),
                                              layout_mode='off', serve_args=[])})
        (run_dir / 'run_manifest.json').write_text(json.dumps(manifest))

    monkeypatch.setattr(cohort.subprocess, 'run', execute)
    monkeypatch.setattr(cohort.sys, 'argv', ['run_moli_cohort.py', str(binary), requested])
    cohort.main()
    canonical = compact_run_id(requested)
    assert invoked == [canonical]
    conditions = json.loads((tmp_path / 'runs' / f'{canonical}.conditions.json').read_text())
    assert conditions['run_id'] == canonical
    with pytest.raises(ValueError, match='run already exists'):
        cohort.main()
    assert invoked == [canonical]
