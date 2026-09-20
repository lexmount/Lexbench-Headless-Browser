"""Fixed-mode reports must count complete actual executions, including regressions."""
import hashlib
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from summarize_moli_fixed_layout import summarize_fixed_sources

SHA = 'a' * 64
CONTRACT = {'task_ids': ['one', 'two'], 'attempts_per_task': 3,
            'bench_manifest_sha256': 'b' * 64, 'task_ids_sha256': 'c' * 64,
            'seed': 'fixed', 'score_mode': 'independent'}


def run(root, name, task, statuses, layout=True):
    path = root / name
    path.mkdir()
    manifest = {'run_id': name, 'completion_status': 'completed',
                'selected_engines': ['moli'], 'k_runs': 3, 'completed_result_rows': len(statuses),
                'bench_manifest': {'sha256': CONTRACT['bench_manifest_sha256']},
                'seed': 'fixed', 'score_mode': 'independent',
                'engines': {'moli': {'sha256': SHA, 'version': 'moli 1.1.9'}},
                'runner': {'fixtures': {'tree_sha256': 'fixtures'}, 'harness_pins': {}},
                'resolved_tasks': [{'task_id': task, 'sha256': hashlib.sha256(task.encode()).hexdigest()}],
                'moli_layout_policy': {'try_layout': False}}
    (path / 'run_manifest.json').write_text(json.dumps(manifest))
    rows = [{'engine': 'moli', 'task_id': task, 'attempt': i, 'status': status,
             'engine_provenance': {'binary_sha256': SHA, 'layout_enabled': layout},
             'run_id': name, 'failure': None if status == 'pass' else {'detail': 'assertion failed'}}
            for i, status in enumerate(statuses, 1)]
    (path / 'results.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in rows))
    return path


def test_supplement_combines_disjoint_tasks_and_keeps_failed_execution(tmp_path):
    a = run(tmp_path, 'retained', 'one', ['pass'] * 3)
    b = run(tmp_path, 'supplement', 'two', ['pass', 'fail', 'pass'])
    result = summarize_fixed_sources([(a, 'results'), (b, 'results')], CONTRACT, 'on')
    assert result['population']['calls'] == 6
    assert result['outcomes']['passed_cases'] == 1
    assert result['outcomes']['success_rate_pct'] == 50
    assert result['cases'][1]['passes'] == 2
    assert result['cases'][1]['attempts'][1]['failure']['detail'] == 'assertion failed'


@pytest.mark.parametrize('problem', ['missing_task', 'duplicate_task', 'wrong_layout', 'wrong_binary', 'incomplete'])
def test_rejects_incomplete_or_mixed_execution_evidence(tmp_path, problem):
    a = run(tmp_path, 'first', 'one', ['pass'] * 3)
    b = run(tmp_path, 'second', 'two', ['pass'] * 3, layout=problem != 'wrong_layout')
    sources = [(a, 'results'), (b, 'results')]
    if problem == 'missing_task':
        sources.pop()
    elif problem == 'duplicate_task':
        sources.append((a, 'results'))
    elif problem == 'wrong_binary':
        p = b / 'results.jsonl'
        p.write_text(p.read_text().replace(SHA, 'd' * 64))
    elif problem == 'incomplete':
        p = b / 'run_manifest.json'
        p.write_text(p.read_text().replace('"completed"', '"running"'))
    with pytest.raises(ValueError):
        summarize_fixed_sources(sources, CONTRACT, 'on')
