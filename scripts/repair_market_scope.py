"""Derive an immutable active market scope from a verified saved run; no network."""
import argparse,json,shutil,socket
from datetime import datetime,timezone
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4
import market_scope
from market_collect import audit,write_json
import sector_observation as so

ROOT=Path(__file__).resolve().parents[1]

def derive(saved_run, config):
    saved=Path(saved_run).resolve();manifest=so.load(saved/'manifest.json')
    so.checked(saved/'run.json',manifest['output_hashes']['run.json'],{})
    run=so.load(saved/'run.json');source=Path(run['market_source'])
    so.checked(source/'manifest.json',manifest['input_hashes'][str(source/'manifest.json')],{})
    source_manifest=so.load(source/'manifest.json')
    for name,expected in source_manifest.items():
        path=(source/name).resolve()
        if not path.is_relative_to(source.resolve()):raise ValueError('source_path_outside_root')
        so.checked(path,expected,{})
    policy=market_scope.load();snapshot=market_scope.apply_snapshot(so.load(source/'snapshot.json'),policy)
    snapshot['classification_refresh']=config.get('classification_refresh')
    output=ROOT/'runtime/scope-repair'/(datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')+'-'+uuid4().hex[:8])
    output.mkdir(parents=True)
    if run.get('basis_rechecks'):
        receipt=Path(run['basis_rechecks']).resolve();raw=so.load(receipt)
        folder=output/'basis';folder.mkdir()
        so.checked(receipt,manifest['input_hashes'][str(receipt)],{})
        shutil.copyfile(receipt,folder/'rechecks.json')
        for document in raw['documents']:
            path=(receipt.parent/document['file']).resolve()
            if not path.is_relative_to(receipt.parent.resolve()):raise ValueError('basis_path_outside_root')
            so.checked(path,document['sha256'],{})
            shutil.copyfile(path,folder/document['file'])
    summary=audit(snapshot)
    write_json(output/'snapshot.json',snapshot);write_json(output/'summary.json',summary)
    write_json(output/'derivation.json',{'source_run':str(saved),'source_manifest_sha256':so.sha(source/'manifest.json'),
        'source_snapshot_sha256':source_manifest['snapshot.json'],'policy_sha256':policy['sha256'],
        'excluded':len(snapshot['scope_exclusions']),'network_calls':0})
    write_json(output/'manifest.json',{str(p.relative_to(output)):so.sha(p) for p in output.rglob('*') if p.is_file()})
    return {'source':str(output),'state':summary['state'],'requested':len(snapshot['universe']),
            'received':len(snapshot['series']),'unrecovered':len(snapshot['errors']),'excluded':len(snapshot['scope_exclusions'])}

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--run',required=True,type=Path);args=parser.parse_args()
    with patch.object(socket.socket,'connect',side_effect=AssertionError('offline_network_forbidden')):
        result=derive(args.run,so.load(ROOT/'config.json'))
    print(json.dumps(result,ensure_ascii=True))
