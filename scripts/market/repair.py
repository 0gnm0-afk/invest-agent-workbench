"""Derive an immutable active market scope from a verified saved run; no network."""

if not __package__:
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import atomic_io as artifact_io
import argparse,json,socket
from datetime import datetime,timezone
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4
import market.scope as market_scope
from market.collect import audit
from market.price_files import PriceFiles
from atomic_io import write_json

from workbench_paths import ROOT

def derive(saved_run, config):
    saved=Path(saved_run).resolve();manifest=artifact_io.load(saved/'manifest.json')
    artifact_io.checked(saved/'run.json',manifest['output_hashes']['run.json'],{})
    run=artifact_io.load(saved/'run.json');source=Path(run['market_source'])
    artifact_io.checked(source/'manifest.json',manifest['input_hashes'][str(source/'manifest.json')],{})
    source_manifest=artifact_io.load(source/'manifest.json')
    for name,expected in source_manifest.items():
        path=(source/name).resolve()
        if not path.is_relative_to(source.resolve()):raise ValueError('source_path_outside_root')
        artifact_io.checked(path,expected,{})
    policy=market_scope.load()
    snapshot=market_scope.apply_snapshot(PriceFiles().market_snapshot(source/'snapshot.json',source_manifest['snapshot.json']),policy)
    snapshot['classification_refresh']=config.get('classification_refresh')
    snapshot['user_classification']=config.get('user_classification')
    output=ROOT/'runtime/scope-repair'/(datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')+'-'+uuid4().hex[:8])
    output.mkdir(parents=True)
    if run.get('basis_rechecks'):
        receipt=Path(run['basis_rechecks']).resolve();raw=artifact_io.load(receipt)
        folder=output/'basis';folder.mkdir()
        artifact_io.checked(receipt,manifest['input_hashes'][str(receipt)],{})
        (folder/'rechecks.json').write_bytes(artifact_io.read_bytes(receipt))
        for document in raw['documents']:
            path=(receipt.parent/document['file']).resolve()
            if not path.is_relative_to(receipt.parent.resolve()):raise ValueError('basis_path_outside_root')
            artifact_io.checked(path,document['sha256'],{})
            (folder/document['file']).write_bytes(artifact_io.read_bytes(path))
    summary=audit(snapshot)
    write_json(output/'snapshot.json',snapshot);write_json(output/'summary.json',summary)
    write_json(output/'derivation.json',{'source_run':str(saved),'source_manifest_sha256':artifact_io.sha(source/'manifest.json'),
        'source_snapshot_sha256':source_manifest['snapshot.json'],'policy_sha256':policy['sha256'],
        'excluded':len(snapshot['scope_exclusions']),'network_calls':0})
    write_json(output/'manifest.json',{str(p.relative_to(output)):artifact_io.sha(p) for p in output.rglob('*') if p.is_file()})
    return {'source':str(output),'state':summary['state'],'requested':len(snapshot['universe']),
            'received':len(snapshot['series']),'unrecovered':len(snapshot['errors']),'excluded':len(snapshot['scope_exclusions'])}

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--run',required=True,type=Path);args=parser.parse_args()
    with patch.object(socket.socket,'connect',side_effect=AssertionError('offline_network_forbidden')):
        result=derive(args.run,artifact_io.load(ROOT/'config.json'))
    print(json.dumps(result,ensure_ascii=True))
