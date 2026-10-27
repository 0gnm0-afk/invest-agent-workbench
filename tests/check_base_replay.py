"""Verify additive base-watch behavior against a saved watch-only run."""
import csv,json,sys,hashlib
from pathlib import Path
from collections import Counter
from PIL import Image
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import market.daily as daily, dashboard_data, screening.priority as priority_review
from unittest.mock import patch
import socket
from check_momentum_replay import core,Links
load=lambda p:json.loads(p.read_text(encoding='utf-8-sig'))
if __name__=='__main__':
    out=Path(sys.argv[1]).resolve();before=Path(sys.argv[2]).resolve()
    r=load(out/'priority-review.json');old=load(before/'priority-review.json')
    entries={(e['market'],e['symbol']):e for g in r['markets'].values() for e in g['entries']}
    prior={(e['market'],e['symbol']):e for g in old['markets'].values() for e in g['entries']}
    assert prior.keys()<=entries.keys()
    for key,e in prior.items():
        n=entries[key];assert e['phase']==n['phase'] and e['entry_signal']==n['entry_signal']
        assert not e['selected'] or n['selected']
        for route,s in e['route_signals'].items():assert n['route_signals'][route]==s
    for m,g in old['markets'].items():
        for route in ('pdf_momentum','sperandeo'):assert g[route]==r['markets'][m][route]
    obs={(x['market'],x['symbol']):x for x in r['observations']}
    for o in old['observations']:
        n=obs[(o['market'],o['symbol'])]
        for k in ('main_candidate','sperandeo_candidate','active_events'):assert o.get(k)==n.get(k)
        assert o.get('signals')==[s for s in n.get('signals',[]) if s['route']!='base_watch']
    assert load(out/'screen-base.json')==load(before/'screen-base.json')
    assert core(load(out/'sectors/signals.json'))==core(load(before/'sectors/signals.json'))
    with (out/'candidates.csv').open(encoding='utf-8-sig',newline='') as f:rows=list(csv.DictReader(f))
    assert len(rows)==len(entries)==r['candidate_count']
    for row in rows:
        e=entries[(row['market'],row['symbol'])];assert row['phase']==e['phase']
        b=e['route_signals'].get('base_watch')
        if b:
            assert row['base_pattern']==b['kind'] and float(row['range_reward_risk'])==b['range_reward_risk']
            assert b['phase']=='watch' and not b['entry_pass'] and b['entry_zone'] is None
            assert b['known_at']<e['as_of'] and b['structure_stop']<b['latest_close']
    charts=load(out/'priority-charts.json')
    assert {(x['market'],x['symbol']) for x in charts['rows']}=={k for k,e in entries.items() if e['selected']}
    for x in charts['rows']:
        assert x['state']=='available'
        with Image.open(out/x['image']) as im:im.verify()
    links=0
    for file in out.rglob('*.html'):
        parser=Links();parser.feed(file.read_text(encoding='utf-8'))
        for rel in parser.links:
            if rel.startswith(('http:','https:','#')):continue
            dest=(file.parent/rel).resolve();assert dest.is_relative_to(out) and dest.is_file(),(file,rel)
            links+=1
    manifest=load(out/'manifest.json')
    daily.check_hashes(manifest['input_hashes']);daily.check_hashes(manifest['code_hashes'])
    daily.check_hashes({str(out/p):h for p,h in manifest['output_hashes'].items()})
    snapshot=load(Path(load(out/'run.json')['market_source'])/'snapshot.json')
    with patch.object(socket.socket,'connect',side_effect=AssertionError('offline_network_forbidden')):
        replayed=priority_review.build(load(out/'screen.json'),snapshot,load(out/'config.json')['priority_review'])
    assert replayed==r
    del snapshot,replayed
    # Read-only adapter; does not invoke account_view or refresh balances.
    view=dashboard_data.saved_market(out.parents[2]);assert not view['errors']
    assert len(view['entries'])==len(entries)
    print(json.dumps({'passed':True,'old_candidates_preserved':len(prior),'candidate_count':len(entries),
       'markets':{m:{'count':g['candidate_count'],'phases':g['phase_counts'],'base_count':g['base_watch']['candidate_count'],
       'base_types':dict(Counter(e['route_signals']['base_watch']['kind'] for e in g['entries'] if 'base_watch' in e['route_signals'])),
       'selected':g['selected_count']} for m,g in r['markets'].items()},'charts':len(charts['rows']),
       'links':links,'dashboard_adapter_count':len(view['entries']),'code_input_output_hashes':True,'all_priority_calculations_replayed':True},ensure_ascii=False))
