#!/usr/bin/env python3
"""Check all task labels; --write classifies new/changed task contracts."""
from pathlib import Path
import argparse
import collections
import json
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from runner.run import validate_manifest
from runner.moli_layout_policy import DEFAULT_REGISTRY, classify_contract, load_registry

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--write',action='store_true')
    args=parser.parse_args()
    _,tasks,errors=validate_manifest(ROOT/'manifest.json')
    if errors:raise ValueError(errors)
    old=load_registry() if DEFAULT_REGISTRY.exists() else {}
    entries=[]
    for task in sorted(tasks,key=lambda task:task.task_id):
        entry=old.get(task.task_id)
        if entry and entry['task_sha256']==task.sha256:
            entries.append(entry)
        else:
            state,reason=classify_contract(task.task)
            entries.append({'task_id':task.task_id,'task_sha256':task.sha256,'requirement':state,'basis':'unclassified' if state=='unknown' else 'contract','reason':reason})
    if args.write:
        DEFAULT_REGISTRY.write_text(json.dumps({'schema':'moli_layout_requirements/1','tasks':entries},indent=2)+'\n')
    elif {e['task_id']:e for e in entries}!=old:
        raise ValueError('layout registry coverage/hash drift; use --write and review classification changes')
    print(json.dumps({'tasks':len(entries),'requirements':dict(collections.Counter(e['requirement'] for e in entries))}))
if __name__=='__main__':main()
