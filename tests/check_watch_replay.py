"""Saved watch-result invariants beyond the standard full replay verifier."""
import csv,hashlib,json,sys
from pathlib import Path
from PIL import Image
from check_momentum_replay import core,Links
load=lambda p:json.loads(p.read_text(encoding='utf-8-sig'))
if __name__=='__main__':
    out=Path(sys.argv[1]).resolve();before=Path(sys.argv[2]).resolve();r=load(out/'priority-review.json');old=load(before/'priority-review.json')
    assert r['schema']==4 and r['taver_state']=='disabled'
    a={(o['market'],o['symbol']):o for o in old['observations']}
    for o in r['observations']:
        b=a[(o['market'],o['symbol'])]
        for k in ('main_candidate','sperandeo_candidate','active_events'):assert o.get(k)==b.get(k),(o['symbol'],k)
    assert load(out/'screen-base.json')==load(before/'screen-base.json')
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
    import sector_signals
    snapshot=load(Path(load(out/'run.json')['market_source'])/'snapshot.json')
    baseline=sector_signals.build(snapshot,load(before/'sectors/groups.json'),load(before/'sectors/observation.json'),load(out/'config.json').get('sector_states'))
    sector_signals.attach_candidates(baseline,r,load(before/'sectors/groups.json'),load(out/'priority-charts.json'))
    assert core(baseline)==core(load(before/'sectors/signals.json'))
    entries={(e['market'],e['symbol']):e for g in r['markets'].values() for e in g['entries']}
    assert len(entries)==r['candidate_count']
    with (out/'candidates.csv').open(encoding='utf-8-sig',newline='') as f:csvrows=list(csv.DictReader(f))
    assert len(csvrows)==len(entries)
    for row in csvrows:assert row['phase']==entries[(row['market'],row['symbol'])]['phase']
    charts=load(out/'priority-charts.json');selected={k for k,e in entries.items() if e['selected']}
    assert {(x['market'],x['symbol']) for x in charts['rows']}==selected
    for x in charts['rows']:
        assert x['state']=='available',x
        with Image.open(out/x['image']) as im:im.verify()
        meta=load(out/x['metadata'])['entry'];s=meta['entry_signal']
        if meta['phase']!='entry':assert s['breakout_date'] is None and s['entry_zone'] is None and not s['entry_pass']
    links=0
    for file in out.rglob('*.html'):
        h=Links();h.feed(file.read_text(encoding='utf-8'))
        for rel in h.links:
            if rel.startswith(('http:','https:','#')):continue
            dest=(file.parent/rel).resolve();assert dest.is_relative_to(out) and dest.is_file(),(file,rel)
            links+=1
    page=(out/'report.html').read_text(encoding='utf-8');assert '<h1>스크리너' in page and '섹터 변화 먼저 보기' not in page
    for rel,digest in load(out/'manifest.json')['output_hashes'].items():assert hashlib.sha256((out/rel).read_bytes()).hexdigest()==digest
    print(json.dumps({'checked':'passed','phase_counts':{m:g['phase_counts'] for m,g in r['markets'].items()},'candidates':len(entries),'charts_decoded':len(charts['rows']),'links':links,'strict_entries_unchanged':True,'sector_values_identical_at_original_classification_date':True,'current_classification_applied_at':load(out/'sectors/signals.json')['classification_applied_at']},ensure_ascii=False))
