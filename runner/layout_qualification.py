"""Pre-run paired qualification. Exploration never enters the scored matrix."""
from __future__ import annotations
import copy
import hashlib
import json
from pathlib import Path
import uuid
if __package__:
    from . import moli_layout_policy as policy
else:
    import moli_layout_policy as policy


def tree_digest(root: Path) -> str:
    if not root.is_dir() or root.is_symlink():
        raise ValueError('missing or unsafe qualification artifacts')
    digest = hashlib.sha256()
    for path in sorted(root.rglob('*')):
        if path.is_symlink():
            raise ValueError('symlink in qualification evidence')
        if path.is_file():
            digest.update(path.relative_to(root).as_posix().encode())
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def qualify(runner, args, suite, tasks, assignments, registry_path):
    unknown = [t for t in tasks if assignments[t.task_id]['requirement'] == 'unknown']
    if not unknown:
        return assignments, None
    # A pair is safe only in benchmark-controlled scenes. Never replay a live
    # external task just because an operation failed.
    if any(t.scene.get('kind') not in {'about_blank', 'self_hosted_fixture'} for t in unknown):
        raise runner.BenchError('automatic layout qualification requires resettable benchmark scenes')
    binary = Path(runner.ENGINE_DEFS['moli']['binary'])
    def identity():
        return {'binary_sha256':runner.sha256_file(binary),
                'runner_sha256':runner.runner_source_provenance()['tree_sha256'],
                'fixtures_sha256':runner.fixtures_provenance()['tree_sha256'],
                'registry_sha256':runner.sha256_file(registry_path),
                'manifest_sha256':runner.sha256_file(runner.resolve_path(args.manifest, runner.DEFAULT_MANIFEST)),
                'package_lock_sha256':runner.sha256_file(runner.REPO_ROOT/'package-lock.json'),
                'driver_pins_sha256':runner.sha256_file(runner.REPO_ROOT/'harness_pins.json'),
                'tasks':{t.task_id:runner.sha256_file(t.path) for t in tasks}}
    before = identity()
    if any(before['tasks'][t.task_id] != t.sha256 for t in tasks):
        raise runner.BenchError('task changed before layout qualification')
    out = runner.resolve_path(args.out, runner.DEFAULT_RUNS_DIR) if args.out else runner.DEFAULT_RUNS_DIR
    root = out / ('layout-qualification-' + uuid.uuid4().hex[:16])
    root.mkdir(parents=True, exist_ok=False)
    protocol = {'schema':'moli_layout_qualification/1','identity':before,'k':3,'seed':args.seed,
                'task_ids':[t.task_id for t in unknown],'order':['off','on'],
                'selection':'off if all off attempts pass; otherwise mode with more passes; ties off; unstable or both-failing remains unknown'}
    runner.write_json(root/'protocol.json', protocol)
    by_mode = {}; physical_calls = 0; duration = 0; evidence = []
    prior = runner.ENGINE_DEFS['moli'].get('serve_args')
    try:
        for mode in ('off','on'):
            if identity() != before:
                raise runner.BenchError('input drift before layout qualification round')
            q = copy.copy(args)
            q.moli_layout = mode; q.engines = 'moli'; q.k = 3; q.jobs = 1
            q.chrome_gate = 'off'; q.score_mode = 'independent'; q.resource_profile = 'baseline'
            q.resource_calibration_baseline = None; q.out = str(root); q.run_id = mode
            q.run_id_conflict = 'error'; q.report = False
            q._layout_assignments = {t.task_id:{**assignments[t.task_id],'layout':mode} for t in unknown}
            q._layout_qualification = None; q._layout_fresh_attempts = True
            runner.ENGINE_DEFS['moli']['serve_args'] = ('--layout',) if mode == 'on' else ()
            directory = runner.run_attempts(q, suite, unknown)
            manifest = json.loads((directory/'run_manifest.json').read_text())
            rows = [json.loads(line) for line in (directory/'results.jsonl').read_text().splitlines()]
            expected = {(t.task_id,n) for t in unknown for n in range(1,4)}
            index = {(r['task_id'],r['attempt']):r for r in rows}
            if manifest['completion_status'] != 'completed' or len(rows) != len(expected) or set(index) != expected:
                raise runner.BenchError('incomplete layout qualification matrix')
            if any(r['engine'] != 'moli' or r['engine_provenance']['layout_enabled'] != (mode == 'on') for r in rows):
                raise runner.BenchError('incorrect layout qualification launch')
            if manifest['engines']['moli']['sha256'] != before['binary_sha256']:
                raise runner.BenchError('binary drift during layout qualification')
            for row in rows:
                folder = directory / row['artifact_dir']
                if not folder.resolve().is_relative_to(directory.resolve()) or not all((folder/name).is_file() for name in ('grader.json','run.json')):
                    raise runner.BenchError('missing qualification attempt artifacts')
            by_mode[mode] = index
            physical_calls += len(rows); duration += sum(r['duration_ms'] for r in rows)
            evidence.append({'mode':mode,'run_id':manifest['run_id'],
                             'results_sha256':runner.sha256_file(directory/'results.jsonl'),
                             'manifest_sha256':runner.sha256_file(directory/'run_manifest.json'),
                             'artifact_tree_sha256':tree_digest(directory/'artifacts')})
    finally:
        if prior is None:runner.ENGINE_DEFS['moli'].pop('serve_args',None)
        else:runner.ENGINE_DEFS['moli']['serve_args'] = prior
    if before != identity():
        raise runner.BenchError('input drift during layout qualification')
    if any(by_mode['off'][key]['seed'] != by_mode['on'][key]['seed'] for key in by_mode['off']):
        raise runner.BenchError('unpaired layout qualification seeds')
    decided = copy.deepcopy(assignments)
    for task in unknown:
        verdicts = {mode:[by_mode[mode][(task.task_id,n)]['status'] for n in range(1,4)] for mode in ('off','on')}
        result = policy.compare_outcomes(verdicts['off'],verdicts['on'])
        decided[task.task_id].update(result,reason='paired_qualification')
    receipt = {'schema':'moli_layout_qualification_result/1','protocol_sha256':runner.sha256_file(root/'protocol.json'),
               'identity':before,'evidence':evidence,'assignments':[decided[t.task_id] for t in unknown],
               'physical_calls':physical_calls,'total_driver_duration_ms':duration,'complete':True}
    runner.write_json(root/'qualification.json',receipt)
    registry = json.loads(registry_path.read_text())
    entries = {entry['task_id']:entry for entry in registry['tasks']}
    for task in unknown:
        result = decided[task.task_id]
        entry = entries.setdefault(task.task_id, {'task_id':task.task_id})
        entry.update(task_sha256=task.sha256, requirement=result['requirement'], basis='paired_evidence', reason='paired_qualification',
                     moli_sha256=before['binary_sha256'], evidence_sha256=runner.sha256_file(root/'qualification.json'))
    registry['tasks'] = [entries[key] for key in sorted(entries)]
    runner.write_json(root/'requirements.json',registry)
    reference = {'requirements':runner.rel_to_repo(root/'requirements.json'), 'requirements_sha256':runner.sha256_file(root/'requirements.json'), 'directory' :runner.rel_to_repo(root),'receipt_sha256':runner.sha256_file(root/'qualification.json'),
                 'physical_calls':physical_calls,'total_driver_duration_ms':duration,
                 'unresolved_tasks':sum(decided[t.task_id]['requirement']=='unknown' for t in unknown)}
    return decided, reference
