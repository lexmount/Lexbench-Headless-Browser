from pathlib import Path
import copy
import json
import pytest
from runner import moli_layout_policy as policy
from runner import run

ROOT=Path(__file__).resolve().parents[1]

@pytest.mark.parametrize('off,on,state,mode',[
    (['pass']*3,['pass']*3,'not_required','off'),
    (['pass']*3,['fail']*3,'not_required','off'),
    (['fail']*3,['pass']*3,'required','on'),
    (['unsupported']*3,['pass']*3,'required','on'),
    (['fail']*3,['fail']*3,'unknown','off'),
    (['pass','fail','pass'],['pass']*3,'unknown','on'),
    (['pass','fail','pass'],['fail']*3,'unknown','off'),
    (['infra']*3,['pass']*3,'unknown','on'),
    (['pass']*3,['infra']*3,'unknown','off'),
])
def test_qualification_truth_table(off,on,state,mode):
    result=policy.compare_outcomes(off,on)
    assert result['requirement']==state and result['layout']==mode

@pytest.mark.parametrize('off,on,k', [(['pass']*2,['pass']*3,3),(['pass'],['pass'],1),(['bogus']*3,['pass']*3,3)])
def test_partial_or_invalid_evidence_rejected(off,on,k):
    with pytest.raises(ValueError):policy.compare_outcomes(off,on,k)

def test_all_registered_cases_have_hash_bound_three_state_labels():
    _,tasks,errors=run.validate_manifest(ROOT/'manifest.json');assert not errors
    registry=policy.load_registry()
    assert len(tasks)==len(registry)==1928
    assert set(registry)=={t.task_id for t in tasks}
    assert set(e['requirement'] for e in registry.values())==policy.REQUIREMENTS
    assert all(registry[t.task_id]['task_sha256']==t.sha256 for t in tasks)

def test_off_success_survives_binary_upgrade_but_not_task_change():
    _,tasks,_=run.validate_manifest(ROOT/'manifest.json');registry=policy.load_registry()
    t=next(t for t in tasks if registry[t.task_id]['basis']=='paired_evidence' and registry[t.task_id]['requirement']=='not_required')
    e=registry[t.task_id]
    assert policy.requirement(t.task,t.sha256,e['moli_sha256'],registry)['requirement']=='not_required'
    assert policy.requirement(t.task,t.sha256,'different',registry)['requirement']=='not_required'
    assert policy.requirement(t.task,'changed',e['moli_sha256'],registry)['requirement']=='unknown'

def test_invalid_registry_values_and_duplicates_fail(tmp_path):
    data=json.loads(policy.DEFAULT_REGISTRY.read_text());p=tmp_path/'requirements.json'
    for mutate in [lambda d:d['tasks'][0].update(requirement='off'),lambda d:d['tasks'].append(d['tasks'][0]),lambda d:d['tasks'][0].update(task_sha256='wrong')]:
        d=copy.deepcopy(data);mutate(d);p.write_text(json.dumps(d))
        with pytest.raises(ValueError):policy.load_registry(p)

def test_default_off_and_explicit_modes():
    parser=run.build_parser()
    assert parser.parse_args(['run']).moli_layout=='off'
    for mode in ('off','on','auto'):assert parser.parse_args(['run','--moli-layout',mode]).moli_layout==mode

def test_optional_geometry_probe_does_not_force_layout():
    task={'driver':{'kind':'raw_cdp','steps':[{'method':'Input.dispatchMouseEvent','optional':True}]}}
    assert policy.classify_contract(task)[0]=='unknown'

def test_framework_names_do_not_prove_layout_requirement():
    assert policy.classify_contract({'driver':{'kind':'playwright','op':'click'}})[0]=='unknown'

@pytest.fixture
def qualification_environment(tmp_path,monkeypatch):
    from runner import layout_qualification
    _,tasks,errors=run.validate_manifest(ROOT/'manifest.json');assert not errors
    tasks=[next(t for t in tasks if t.task_id=='v2_diag_gbcr_fixed')]
    binary=tmp_path/'moli';binary.write_bytes(b'fixed-binary')
    monkeypatch.setitem(run.ENGINE_DEFS,'moli',{**run.ENGINE_DEFS['moli'],'binary':binary,'serve_args':()})
    args=run.build_parser().parse_args(['run','--engines','moli','--moli-layout','auto','--out',str(tmp_path/'runs'),'--seed','same-seed'])
    assigned={t.task_id:{'task_id':t.task_id,'task_sha256':t.sha256,'requirement':'unknown','layout':'off','reason':'test'} for t in tasks}
    stages=[]; options={'off':['fail']*3,'on':['pass']*3}
    def fake_attempts(q,suite,chosen):
        assert q.k==3 and q.jobs==1 and q.score_mode=='independent' and q._layout_fresh_attempts
        assert run.ENGINE_DEFS['moli']['serve_args']==(('--layout',) if q.moli_layout=='on' else ())
        stages.append(q.moli_layout)
        directory=Path(q.out)/q.run_id;directory.mkdir()
        rows=[]
        for task in chosen:
            for n,status in enumerate(options[q.moli_layout],1):
                folder=directory/'artifacts'/task.task_id/str(n);folder.mkdir(parents=True)
                (folder/'grader.json').write_text('{}');(folder/'run.json').write_text('{}')
                rows.append({'task_id':task.task_id,'attempt':n,'engine':'moli','engine_provenance':{'layout_enabled':q.moli_layout=='on'},'seed':str(n),'status':status,'duration_ms':7,'artifact_dir':str(folder.relative_to(directory))})
        run.write_json(directory/'run_manifest.json',{'run_id':q.run_id,'completion_status':'completed','engines':{'moli':{'sha256':run.sha256_file(binary)}}})
        (directory/'results.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in rows))
        if options.get('drift') and q.moli_layout=='off':binary.write_bytes(b'changed')
        return directory
    monkeypatch.setattr(run,'run_attempts',fake_attempts)
    return layout_qualification,args,tasks,assigned,stages,options,binary

def test_qualification_freezes_mode_and_emits_reusable_registry(qualification_environment):
    module,args,tasks,assigned,stages,options,binary=qualification_environment
    selected,reference=module.qualify(run,args,{},tasks,assigned,policy.DEFAULT_REGISTRY)
    assert stages==['off','on'] and assigned[tasks[0].task_id]['requirement']=='unknown'
    assert selected[tasks[0].task_id]['requirement']=='required' and selected[tasks[0].task_id]['layout']=='on'
    assert reference['physical_calls']==6 and reference['total_driver_duration_ms']==42
    registry=policy.load_registry(Path(reference['requirements']))
    assert policy.requirement(tasks[0].task,tasks[0].sha256,run.sha256_file(binary),registry)['requirement']=='required'
    assert run.ENGINE_DEFS['moli']['serve_args']==()

def test_partial_qualification_cannot_publish_assignments(qualification_environment):
    module,args,tasks,assigned,stages,options,_=qualification_environment
    options['off']=['pass']*2
    with pytest.raises(run.BenchError,match='incomplete'):module.qualify(run,args,{},tasks,assigned,policy.DEFAULT_REGISTRY)
    assert not list(Path(args.out).rglob('qualification.json'))
    assert run.ENGINE_DEFS['moli']['serve_args']==()

def test_input_drift_rejected(qualification_environment):
    module,args,tasks,assigned,stages,options,_=qualification_environment
    options['drift']=True
    with pytest.raises(run.BenchError,match='drift'):module.qualify(run,args,{},tasks,assigned,policy.DEFAULT_REGISTRY)
    assert not list(Path(args.out).rglob('qualification.json'))

def test_known_requirement_does_not_make_extra_calls(qualification_environment):
    module,args,tasks,assigned,stages,_,_=qualification_environment
    assigned[tasks[0].task_id].update(requirement='required',layout='on')
    selected,reference=module.qualify(run,args,{},tasks,assigned,policy.DEFAULT_REGISTRY)
    assert not stages and reference is None and selected==assigned

def test_qualification_updates_missing_and_stale_task_bindings(qualification_environment,tmp_path):
    module,args,tasks,assigned,_,_,binary=qualification_environment
    registry=tmp_path/'registry.json'
    registry.write_text(json.dumps({'schema':'moli_layout_requirements/1','tasks':[]}))
    _,reference=module.qualify(run,args,{},tasks,assigned,registry)
    data=policy.load_registry(Path(reference['requirements']))
    assert data[tasks[0].task_id]['task_sha256']==tasks[0].sha256
    assert policy.requirement(tasks[0].task,tasks[0].sha256,run.sha256_file(binary),data)['requirement']=='required'

def test_auto_without_seed_freezes_one_before_any_calls(capsys):
    args=run.build_parser().parse_args(['run','--task','pw_raw_browser_getversion','--engines','moli','--moli-layout','auto','--k','3','--dry-run'])
    assert args.seed is None
    assert run.command_run(args)==0
    payload=json.loads(capsys.readouterr().out)
    assert len(args.seed)==32 and payload['seed']==args.seed
    assert payload['moli_layout_qualification_calls']==0


def test_historical_off_success_overrides_geometry_guess():
    task={'task_id':'coordinate','driver':{'kind':'raw_cdp','steps':[{'method':'Input.dispatchMouseEvent'}]}}
    entry={'task_sha256':'same','requirement':'not_required','basis':'off_pass_evidence','moli_sha256':'old','reason':'historical_off_pass'}
    assert policy.classify_contract(task)[0]=='required'
    assert policy.requirement(task,'same','new',{'coordinate':entry})['requirement']=='not_required'
    assert policy.requirement(task,'changed','new',{'coordinate':entry})['requirement']=='unknown'


def test_required_evidence_does_not_become_cross_version_success():
    task={'task_id':'opaque','driver':{'kind':'playwright'}}
    entry={'task_sha256':'same','requirement':'required','basis':'paired_evidence','moli_sha256':'old'}
    assert policy.requirement(task,'same','new',{'opaque':entry})['requirement']=='unknown'
