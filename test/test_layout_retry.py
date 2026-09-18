from pathlib import Path
import copy
import json
import pytest
from runner import layout_retry, run


def batch(tmp_path, task, statuses, layout=False):
    rows=[]
    for attempt,status in enumerate(statuses,1):
        artifact=f"{task}/{attempt}/{'on' if layout else 'off'}"
        row=dict(task_id=task,engine="moli",attempt=attempt,status=status,seed=f"seed-{attempt}",duration_ms=10,artifact_dir=artifact,run_id="physical",engine_provenance={"layout_enabled":layout})
        p=tmp_path/artifact/'run.json';p.parent.mkdir(parents=True);p.write_text(json.dumps(row))
        rows.append(row)
    return rows


def test_successful_whole_batch_only_replaces_failed_case(tmp_path):
    original=batch(tmp_path,'pass',['pass']*3)+batch(tmp_path,'recover',['fail','pass','fail'])+batch(tmp_path,'still-fail',['fail']*3)
    retry=batch(tmp_path,'recover',['pass']*3,True)+batch(tmp_path,'still-fail',['pass','fail','pass'],True)
    final=layout_retry.replace_cases(original,retry,3,tmp_path,'logical')
    assert len(final)==9
    assert final[:3]==original[:3]
    assert final[6:]==original[6:]
    assert all(row['status']=='pass' and row['run_id']=='logical' for row in final[3:6])
    assert layout_retry.pass_count(original)==1
    assert layout_retry.pass_count(final)==2
    assert all(row['layout_retry']['total_execution_duration_ms']==20 for row in final[3:6])


@pytest.mark.parametrize('mutation',['missing','duplicate','wrong-seed','layout-off'])
def test_incomplete_or_wrong_rerun_cannot_replace_results(tmp_path,mutation):
    original=batch(tmp_path,'task',['fail']*3);retry=batch(tmp_path,'task',['pass']*3,True)
    if mutation=='missing':retry.pop()
    elif mutation=='duplicate':retry.append(copy.deepcopy(retry[0]))
    elif mutation=='wrong-seed':retry[0]['seed']='different'
    else:retry[0]['engine_provenance']['layout_enabled']=False
    with pytest.raises(ValueError):layout_retry.replace_cases(original,retry,3,tmp_path,'logical')


def test_gate_skip_and_pass_are_not_retried(tmp_path):
    rows=batch(tmp_path,'pass',['pass']*3)+batch(tmp_path,'gate',['chrome_gate_fail']*3)
    assert layout_retry.failed_cases(rows,3)==set()


def test_cli_defaults_and_opt_in():
    parser=run.build_parser();args=parser.parse_args(['run'])
    assert args.moli_layout=='off' and args.try_layout is False
    assert parser.parse_args(['run','--try-layout']).try_layout is True
    assert layout_retry.policy('on',True)['retry_layout'] is None
    with pytest.raises(SystemExit):parser.parse_args(['run','--moli-layout','auto'])


def test_recovery_metadata_is_not_a_version_comparison_control():
    import sys
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
    from compare_moli_cohort import normalized_manifest
    base={'engines':{'moli':{'layout_mode':'off'}},'moli_layout_policy':layout_retry.policy('off',True)}
    candidate=copy.deepcopy(base);candidate['layout_retry']={'retried_cases':['one']}
    assert normalized_manifest(base)==normalized_manifest(candidate)
    candidate['moli_layout_policy']=layout_retry.policy('off',False)
    assert normalized_manifest(base)!=normalized_manifest(candidate)


def test_interrupted_recovery_cannot_generate_final_report(tmp_path):
    manifest={'moli_layout_policy':layout_retry.policy('off',True),'completion_status':'interrupted'}
    with pytest.raises(ValueError,match='incomplete'):layout_retry.verify(tmp_path,manifest,[])
