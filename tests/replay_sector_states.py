"""Saved-data checks independent of daily Verify; no browser or network access."""
import collections
import json
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse
from bs4 import BeautifulSoup

root=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(root/'scripts'))
import sector_signals as signals
import sector_observation as sector

run=Path(sys.argv[1])
prior=root/'runtime/2026-09-29/075820-review-fb717041'
load=sector.load
states=load(run/'sectors/signals.json')
snapshot=load(Path(load(run/'run.json')['market_source'])/'snapshot.json')
groups=load(run/'sectors/groups.json');obs=load(run/'sectors/observation.json')
prefixes=[]
for offset in (1,10,30):
    date=snapshot['sessions']['US'][-offset-1]
    clipped=signals.build(snapshot,groups,obs,states['config'],as_of=date)
    for s in clipped['sectors']:
        expected=next(x for x in states['sectors'] if (x['market'],x['group_code'])==(s['market'],s['group_code']))
        last=next(x for x in expected['history'] if x['date']==s['current']['date'])
        assert s['current']==last, (date,s['group_name'])
    prefixes.append({'cutoff':date,'sectors_matched':len(clipped['sectors'])})
preserved={}
old_observation=load(prior/'sectors/observation.json')
assert {k:v for k,v in obs.items() if k!='applied_at'}=={k:v for k,v in old_observation.items() if k!='applied_at'},'base_sector_metrics_changed'
for name in ('screen-base.json','candidates.csv','priority-review.json'):
    assert (prior/name).read_bytes()==(run/name).read_bytes(),name
    preserved[name]=sector.sha(run/name)
assert [dict(r,sector_context=None) for r in load(prior/'screen.json')['rows']]==[dict(r,sector_context=None) for r in load(run/'screen.json')['rows']]
links=0; pages=0; cards=0; charts=0
for path in [run/'report.html',*sorted((run/'sectors').glob('*.html'))]:
    soup=BeautifulSoup(path.read_text(encoding='utf-8'),'html.parser')
    pages+=1; cards+=len(soup.select('[data-state]'));charts+=len(soup.select('svg'))
    for a in soup.select('a[href]'):
        url=urlparse(a['href'])
        if url.scheme or not url.path:continue
        target=(path.parent/unquote(url.path)).resolve()
        assert target.is_relative_to(run.resolve()) and target.is_file(),(path,a['href'])
        links+=1
dashboard=BeautifulSoup((run/'sectors/observation.html').read_text(encoding='utf-8'),'html.parser')
assert len(dashboard.select('[data-state]'))==21
test_fixture={'script':dashboard.script.string,
    'filters':[x['data-filter'] for x in dashboard.select('[data-filter]')],
    'sections':[[{'state':x['data-state'],'changed':x['data-changed'],'concentrated':x['data-concentrated']}
        for x in s.select('[data-state]')] for s in dashboard.select('section')]}
fixture_root=root/'runtime/sector-state-validation-20260929'
fixture_root.mkdir(parents=True,exist_ok=True)
(fixture_root/'filter-fixture.json').write_text(json.dumps(test_fixture),encoding='utf-8')
evidence={'run':str(run),'prefix_replay':prefixes,'preserved':preserved,'html_pages':pages,
    'base_sector_metrics_unchanged':True,
    'local_links':links,'cards_including_report':cards,'inline_charts':charts,
    'current_states':dict(collections.Counter(s['current']['state'] for s in states['sectors'])),
    'historical_states':dict(collections.Counter(m['state'] for s in states['sectors'] for m in s['history'])),
    'concentrated_sectors':sum(s['current'].get('concentrated',False) for s in states['sectors']),
    'current_examples':[{k:s[k] for k in ('market','group_name','current')} for s in states['sectors'] if s['current']['state']!='mixed'],
    'browser_validation':'not_run_local_file_protocol_blocked; static links and isolated filter unit test only'}
(root/'docs/sector-state-evidence-2026-09-29.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({k:v for k,v in evidence.items() if k!='current_examples'},ensure_ascii=False,indent=2))
