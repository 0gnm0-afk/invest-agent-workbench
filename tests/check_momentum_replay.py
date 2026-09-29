"""Read-only audit of a saved PDF screening run against a preserved baseline."""
import copy,csv,hashlib,json,sys
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit,unquote

class Links(HTMLParser):
    def __init__(self):super().__init__();self.links=[]
    def handle_starttag(self,tag,attrs):
        self.links += [v for k,v in attrs if k in ('href','src') and v]

def load(p):return json.loads(p.read_text(encoding='utf-8-sig'))
def core(value):
    value=copy.deepcopy(value);value.pop('classification_applied_at',None)
    for sector in value['sectors']:
        sector.pop('candidates',None)
        for row in sector['leaders']+sector['weak_contributors']:
            for key in ('origins','chart','chart_status'):row.pop(key,None)
    return value

if __name__=='__main__':
    out=Path(sys.argv[1]).resolve();old=Path(sys.argv[2]).resolve()
    review=load(out/'priority-review.json');run=load(out/'run.json')
    assert review['schema']==3 and review['taver_state']=='disabled'
    assert not (out/'taver-scan.html').exists()
    assert load(out/'screen-base.json')==load(old/'screen-base.json')
    before=load(old/'screen.json');after=load(out/'screen.json')
    for screen in (before,after):
        for row in screen['rows']:row.get('sector_context',{}).pop('classification_applied_at',None)
    assert before==after
    assert core(load(out/'sectors/signals.json'))==core(load(old/'sectors/signals.json'))
    entries={(e['market'],e['symbol']) for g in review['markets'].values() for e in g['entries']}
    with (out/'candidates.csv').open(encoding='utf-8-sig',newline='') as f:rows=list(csv.DictReader(f))
    assert len(rows)==len(entries)==review['candidate_count']==run['candidate_count']
    assert {(e['market'],e['symbol']) for e in rows}==entries
    linked={(s['market'],e['symbol']) for s in load(out/'sectors/signals.json')['sectors'] for e in s['candidates']}
    assert linked<=entries
    manifest=load(out/'manifest.json')
    for relative,digest in manifest['output_hashes'].items():assert hashlib.sha256((out/relative).read_bytes()).hexdigest()==digest
    links=0
    for page in out.rglob('*.html'):
        parser=Links();parser.feed(page.read_text(encoding='utf-8'))
        for href in parser.links:
            u=urlsplit(href)
            if u.scheme or u.netloc or not u.path:continue
            dest=(page.parent/unquote(u.path)).resolve()
            assert dest.is_relative_to(out) and dest.is_file(),(page,href)
            links+=1
    assert '<h2>조사 순서</h2>' not in (out/'report.html').read_text(encoding='utf-8')
    assert all('taver' not in e['origins'] for g in review['markets'].values() for e in g['entries'])
    counts={}
    for market in ('KR','US'):
        obs=[o for o in review['observations'] if o['market']==market]
        available=[o for o in obs if o.get('features',{}).get('state')=='available']
        counts[market]={'rows':len(obs),'states':dict(Counter(o['state'] for o in obs)),
          'rs80':sum(o['rs_percentile']>=80 for o in available),'template_pass':sum(o['features']['template_pass'] for o in available),
          'common_and_template':sum(all(o['common_checks'].values()) and o['features']['template_pass'] for o in available),
          'events':dict(Counter(e['kind'] for o in available for e in o['events']))}
    print(json.dumps({'checks':'passed','output_hashes':len(manifest['output_hashes']),'local_html_links':links,
      'active_candidates':len(entries),'baseline_screen_identical':True,'sector_values_history_leaders_identical':True,
      'counts':counts,'raw_state':run['state']},ensure_ascii=False))
