"""Persist proven dual-provider absence; classification refresh never narrows scope."""
import json
from pathlib import Path
from invest_agent.trading.runner import atomic_write
from invest_agent.trading.store import instance_lock
ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT/'runtime/market-scope-auto/records.json'


def read_registry():
    if not REGISTRY.exists():
        return [], b''
    raw = REGISTRY.read_bytes()
    data = json.loads(raw)
    if data.get('schema') != 1:
        raise ValueError('invalid_automatic_scope_registry')
    return data['records'], raw


def remember(records, policy):
    rule = policy.get('dual_provider') or {}
    if not rule.get('enabled'):
        return 0
    items = [r for r in records if r.get('dual_provider_rule') == rule.get('rule_id')
             and r.get('reason') == 'dual_provider_data_unavailable' and r.get('isin_code')]
    if not items:
        return 0
    with instance_lock(REGISTRY.parent):
        prior, _ = read_registry()
        keys = {(r['market'],r['symbol'],r.get('isin_code')) for r in prior}
        added = []
        for r in items:
            key = r['market'],r['symbol'],r['isin_code']
            if key not in keys:
                keys.add(key); added.append(r)
        if added:
            atomic_write(REGISTRY,json.dumps({'schema':1,'records':prior+added},ensure_ascii=False,indent=2))
        return len(added)


def refresh_classification_source(source, reference, output):
    """A review may refresh classification without changing any market observations."""
    import sector_observation as so
    from market_collect import write_json
    source,output=Path(source).resolve(),Path(output).resolve()
    snapshot=so.load(source/'snapshot.json')
    if snapshot.get('classification_refresh')==reference:
        return source
    output.mkdir(parents=True,exist_ok=False)
    snapshot['classification_refresh']=reference
    write_json(output/'snapshot.json',snapshot)
    write_json(output/'summary.json',so.load(source/'summary.json'))
    write_json(output/'classification-derivation.json',{'source':str(source),
        'source_manifest_sha256':so.sha(source/'manifest.json'),'source_snapshot_sha256':so.sha(source/'snapshot.json')})
    write_json(output/'manifest.json',{p.name:so.sha(p) for p in output.iterdir() if p.is_file()})
    return output
