from pathlib import Path
import json,hashlib,tarfile,collections
import argparse
r=Path(__file__).resolve().parents[1]
parser=argparse.ArgumentParser(description='Import the published 0.1.1 off-pass layout baseline; preserve newer paired decisions.')
parser.add_argument('archive',type=Path)
args=parser.parse_args()
archive=args.archive;archive_sha=hashlib.sha256(archive.read_bytes()).hexdigest();assert archive_sha=='3052461b458581c8da620d55c3741d18dc50693d78c7a6ebeffb2241251f12f9'
with tarfile.open(archive) as tar:raw=tar.extractfile('four_engine_full_20260812/results.jsonl').read()
raw_sha=hashlib.sha256(raw).hexdigest()
m=json.loads((r/'docs/evidence/four_engine_full_20260812/run_manifest.json').read_text());tasks={x['task_id']:x for x in m['resolved_tasks']};binary=m['engines']['moli']['sha256'];by=collections.defaultdict(list)
for line in raw.splitlines():
    x=json.loads(line)
    if x['engine']=='moli':
        assert x['engine_provenance']['binary_sha256']==binary
        assert not any(a=='--layout' or a.startswith('--layout=') or a=='-l' for a in x['engine_provenance']['launch_command'])
        by[x['task_id']].append(x)
assert len(by)==1928 and all(len(v)==3 and {x['attempt'] for x in v}=={1,2,3} for v in by.values())
passed={k for k,v in by.items() if all(x['status']=='pass' and not x['fallback_used'] for x in v)};assert len(passed)==1556
p=r/'config/moli_layout_requirements.json';registry=json.loads(p.read_text());changed=[];excluded=[];newer=[]
for e in registry['tasks']:
    tid=e['task_id']
    if tid not in passed:continue
    old=tasks[tid];current=json.loads((r/old['path']).read_text())
    # Historical public task files have revised prose. Match the declared task
    # identity/version and execution family, retaining BOTH original hashes.
    if not (current['task_id']==tid and all(x['task_version']==current['task_version'] for x in by[tid]) and current['driver']['kind']==old['driver'] and current['grader']['kind']==old['grader'] and current['scene']['kind']==old['scene'] and current['features']==old['features']):excluded.append(tid);continue
    e['historical_off_pass']={'run_id':m['run_id'],'task_version':current['task_version'],'task_sha256':old['sha256'],'binding':'task_id_version_and_execution_family','moli_version':'0.1.1','moli_sha256':binary,'layout':'off','passes':3,'attempts':3,'evidence_sha256':raw_sha,'archive_sha256':archive_sha}
    if e['basis']=='paired_evidence' and e['requirement'] in {'required','not_required'}:newer.append(tid);continue
    if e['basis']=='paired_evidence':e['latest_comparison']={k:e[k] for k in ('requirement','reason','moli_sha256','evidence_sha256')}
    e.update(requirement='not_required',basis='off_pass_evidence',reason='historical_off_pass',moli_sha256=binary,evidence_sha256=raw_sha);changed.append(tid)
p.write_text(json.dumps(registry,indent=2)+'\n')
print(json.dumps({'historical_passed':len(passed),'imported':len(changed),'newer_preserved':len(newer),'excluded':excluded,'requirements':dict(collections.Counter(e['requirement'] for e in registry['tasks']))}))
