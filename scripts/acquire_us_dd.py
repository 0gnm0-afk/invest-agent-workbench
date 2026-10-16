"""Public SEC share history and split metadata, rate-bounded, resumable."""
import argparse,json,sys,time,urllib.request,urllib.error,urllib.parse,threading,hashlib
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor,as_completed
from datetime import datetime,timezone
WB=Path(__file__).resolve().parents[1] if Path(__file__).parent.name=='scripts' else Path('<user-home>/Desktop/workspace/invest-agent-workbench')
sys.path.insert(0,str(WB/'scripts'))
from saved_us_history import SavedHistory
from financial_sources import settings

gates={'sec':threading.Lock(),'yahoo':threading.Lock()}; next_times={'sec':0.,'yahoo':0.}
def fetch(url,path,ua,rate=None):
 if path.exists(): return json.loads(path.read_bytes())
 for attempt in range(2):
  host='sec' if 'sec.gov/' in url else 'yahoo'; rate=.16 if host=='sec' else .24
  with gates[host]:
   delay=max(0,next_times[host]-time.monotonic())
   if delay: time.sleep(delay)
   next_times[host]=time.monotonic()+rate
  try:
   with urllib.request.urlopen(urllib.request.Request(url,headers={'User-Agent':ua,'Accept':'application/json'}),timeout=25) as r: raw=r.read(5000001)
   if len(raw)>5000000: raise ValueError('response_too_large')
   value=json.loads(raw); path.parent.mkdir(parents=True,exist_ok=True); path.write_bytes(raw);return value
  except urllib.error.HTTPError as e:
   if e.code in (429,500,502,503,504) and attempt==0: time.sleep(2);continue
   raise ValueError('HTTP_'+str(e.code)) from None
  except (OSError,TimeoutError):
   if attempt==0: continue
   raise ValueError('network_error') from None

def run(out):
 out.mkdir(parents=True,exist_ok=True); raw=out/'raw/us';raw.mkdir(parents=True,exist_ok=True)
 ua=settings().get('SEC_USER_AGENT'); assert ua and '@' in ua,'SEC research contact required'
 tickers=fetch('https://www.sec.gov/files/company_tickers.json',raw/'tickers.json',ua)
 h=SavedHistory(WB/'runtime/us-history/active.json','US'); assert not h.error
 snap=json.loads((WB/'runtime/2026-10-16/214015-live-c82b02fc/adr-market/snapshot.json').read_bytes())
 listed={r['symbol']:r for r in snap['series'] if r['market']=='US'}
 lookup={v['ticker'].replace('-','.'):v for v in tickers.values()}
 found=[];excluded=[];ciks={}
 for symbol,l in h.listings.items():
  k=symbol.replace('-','.'); t=lookup.get(k)
  typ=l.get('securityType') or listed.get(symbol,{}).get('security_type')
  if typ!='STOCK': excluded.append({'symbol':symbol,'reason':'non_native_or_unverified_share_units'});continue
  if t is None: excluded.append({'symbol':symbol,'reason':'no_SEC_exact_ticker'});continue
  found.append({'symbol':symbol,'cik':str(t['cik_str']).zfill(10),'title':t['title']}); ciks.setdefault(t['cik_str'],[]).append(symbol)
 multi={s for names in ciks.values() if len(names)>1 for s in names}
 chosen=[r for r in found if r['symbol'] not in multi]
 excluded += [{'symbol':s,'reason':'multiple_listed_common_tickers_same_CIK'} for s in sorted(multi)]
 population=out/'us_population.json'
 if population.exists():
  prior=json.loads(population.read_bytes());chosen=prior['eligible']
 else:
  prior={'eligible':chosen,'excluded':excluded,'raw_count':len(h.listings)}
 prior.setdefault('source_identity',{'history_pointer':str(h.pointer),'pointer_sha256':hashlib.sha256(h.pointer.read_bytes()).hexdigest(),
                 'raw_universe_sha256':hashlib.sha256((h.root/'universe.json').read_bytes()).hexdigest(),
                 'SEC_tickers_sha256':hashlib.sha256((raw/'tickers.json').read_bytes()).hexdigest()})
 population.write_text(json.dumps(prior,ensure_ascii=False,indent=2),encoding='utf-8')
 def worker(r):
  folder=raw/r['symbol']; folder.mkdir(exist_ok=True)
  try:
   shares=fetch(f'https://data.sec.gov/api/xbrl/companyconcept/CIK{r["cik"]}/dei/EntityCommonStockSharesOutstanding.json',folder/'shares.json',ua)
   vals=shares.get('units',{}).get('shares',[])
   if not vals: return {**r,'status':'no_point_shares'}
   url=f'https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(r["symbol"].replace(".","-"))}?period1=1420070400&period2=1792108800&interval=1d&events=splits'
   chart=fetch(url,folder/'chart_daily.json','Mozilla/5.0 InvestWorkbenchResearch')
   assert chart['chart']['result'][0]['meta']['symbol'].replace('-','.')==r['symbol'].replace('-','.')
   return {**r,'status':'ready','share_rows':len(vals)}
  except Exception as e: return {**r,'status':'failed','error':str(e) if type(e)==ValueError else type(e).__name__}
 results=[]
 with ThreadPoolExecutor(max_workers=8) as pool:
  futures={pool.submit(worker,r):r for r in chosen}
  for f in as_completed(futures):
   results.append(f.result())
   if len(results)%50==0:
    (out/'us_acquisition.json').write_text(json.dumps(results,ensure_ascii=False),encoding='utf-8')
    print('US acquired',len(results),'/',len(chosen),'ready',sum(r['status']=='ready' for r in results),flush=True)
 (out/'us_acquisition.json').write_text(json.dumps(sorted(results,key=lambda r:r['symbol']),ensure_ascii=False,indent=2),encoding='utf-8')
 print('US acquisition complete',len(results),flush=True)

if __name__=='__main__':
 p=argparse.ArgumentParser(); p.add_argument('--out',type=Path,required=True);a=p.parse_args();run(a.out)
