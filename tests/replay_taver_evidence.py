"""Offline stored-data evidence; no collection or historical membership claims."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import socket
import sys
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import daily_market
from invest_agent.trading.taver_structure import analyze
from invest_agent.trading.market import usable_tail
from taver_review import draw_structure


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--run',type=Path,required=True);parser.add_argument('--prior',type=Path,required=True)
    args=parser.parse_args();load=lambda p:json.loads(p.read_text(encoding='utf-8'))
    current=load(args.run/'screen-base.json');prior=load(args.prior/'screen-base.json')
    assert current==prior, 'existing_sperandeo_changed'
    review=load(args.run/'priority-review.json');charts=load(args.run/'priority-charts.json')
    source=Path(load(args.run/'run.json')['market_source']);snap=load(source/'snapshot.json')
    evidence={'network_calls':0,'screen_base_exact_equal':True,'sperandeo_candidate_count':current['candidate_count'],
        'source':str(source),'source_sha256':hashlib.sha256((source/'snapshot.json').read_bytes()).hexdigest(),
        'run':str(args.run),'as_of':review['session_dates'],'markets':{},'cases':[],
        'observation_states':dict(Counter(o['state'] for o in review['taver_observations'])),
        'validation_scope':'shape/independence/persistence only, not profitability; historical case universe membership not tested'}
    for m,g in review['markets'].items():
        a={e['symbol'] for e in g['sperandeo']['entries'] if e['selected']}; b={e['symbol'] for e in g['taver']['entries'] if e['selected']}
        assert len(a)<=10 and len(b)<=10 and len(a|b)==g['selected_count']
        assert len({e['symbol'] for e in g['entries']})==len(g['entries'])
        only=[e['symbol'] for e in g['taver']['entries'] if not e['sperandeo_candidate']]
        evidence['markets'][m]={'sperandeo':g['sperandeo']['candidate_count'],'taver':g['taver']['candidate_count'],
            'overlap':len(g['overlap_symbols']),'taver_only':len(only),'selected_sperandeo':sorted(a),'selected_taver':sorted(b),
            'selected_unique':len(a|b),'selected_taver_only':[e['symbol'] for e in g['entries'] if e['selected'] and e['origins']==['taver']]}
        assert only and evidence['markets'][m]['selected_taver_only']
    from PIL import Image
    assert charts['failed']==0
    for c in charts['rows']:
        with Image.open(args.run/c['image']) as img:img.verify()
        if c.get('structure_image'):
            with Image.open(args.run/c['structure_image']) as img:img.verify()
        metadata=load(args.run/c['metadata']);assert metadata['entry']['symbol']==c['symbol']
        page=(args.run/c['path']).read_text(encoding='utf-8')
        assert '후보 경로:' in page and '테이버' in page
    evidence['charts']={k:charts[k] for k in ['requested','available','failed']}
    folder=Path(__file__).resolve().parents[1]/'docs'/'taver-cases-2026-09-29';folder.mkdir(exist_ok=True)
    dates={'EME':'2026-09-22','CEG':'2026-09-22','HUBB':'2026-09-21','WMT':'2026-09-23','CAT':'2026-09-22','AMZN':'2026-09-18','ETN':'2026-09-21','FSS':'2026-09-22','META':'2026-09-18','DLTR':'2026-09-23'}
    for item in snap['series']:
        symbol=item['symbol']
        if symbol not in dates:continue
        bars=usable_tail(item['bars'],snap['sessions'][item['market']]);cutoff=dates[symbol]
        result=analyze(bars,review['config']['taver_structure'],as_of=cutoff)
        prefix=[b for b in bars if b['date']<=cutoff]
        assert result==analyze(prefix,review['config']['taver_structure'])
        latest=next(o for o in review['taver_observations'] if o['market']==item['market'] and o['symbol']==symbol)
        draw_structure(prefix,folder/(symbol+'.png'),result,symbol+' | stored completed daily, not video intraday')
        evidence['cases'].append({'symbol':symbol,'video_day':cutoff,'cutoff_equals_prefix':True,'video_day_result':result,'latest_result':latest})
    (folder/'evidence.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    print(json.dumps({k:v for k,v in evidence.items() if k!='cases'},ensure_ascii=False))
    for c in evidence['cases']:
        r=c['video_day_result']; p=r.get('primary',{})
        print(c['symbol'],r['candidate'],r.get('patterns'),r.get('warnings'),{k:round(p[k].get('distance_atr',999),2) for k in ['channel','support']} if p else {})

if __name__=='__main__':
    with patch.object(socket.socket,'connect',side_effect=AssertionError('network_forbidden')):main()
