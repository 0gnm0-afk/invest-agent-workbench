"""Resolve specific vendor discrepancies against12 public Naver daily bars."""
import argparse,ast,hashlib,json,sys,urllib.request
from pathlib import Path
def run(out):
 sys.path.insert(0,str(out/'dependencies'));import pandas as pd,numpy as np
 data=pd.read_parquet(out/'raw/kr-marcap/marcap-2026.parquet',columns=['Code','Date','Open','High','Low','Close','Volume'])
 folder=out/'raw/corroboration';folder.mkdir(exist_ok=True);checks=[]
 for code in ('005930','000660','035420'):
  url=f'https://api.finance.naver.com/siseJson.naver?symbol={code}&requestType=1&startTime=20260301&endTime=20260306&timeframe=day';path=folder/(code+'.txt')
  if not path.exists():
   with urllib.request.urlopen(urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0','Referer':'https://finance.naver.com/'}),timeout=20) as r: path.write_bytes(r.read())
  rows=ast.literal_eval(path.read_text(encoding='utf-8-sig').strip())[1:]
  for row in rows:
   date=pd.to_datetime(row[0],format='%Y%m%d');one=data[(data.Code==code)&(data.Date==date)];assert len(one)==1
   values=one[['Open','High','Low','Close','Volume']].iloc[0].to_numpy(float);expected=np.asarray(row[1:6],float)
   assert np.allclose(values,expected,rtol=1e-12,atol=0),(code,row[0],values,expected)
   checks.append({'symbol':code,'date':str(date.date()),'fields':5,'status':'MATCH'})
  print('Naver vs marcap',code,len(rows),'bars MATCH',flush=True)
 result={'status':'PASS','symbols':3,'bars':len(checks),'values':5*len(checks),'checks':checks,
         'sources':[{'path':str(folder/(c+'.txt')),'sha256':hashlib.sha256((folder/(c+'.txt')).read_bytes()).hexdigest()} for c in ('005930','000660','035420')],
         'scope':'Specific discrepant2026 dates only; not a full-market guarantee or a rewrite of original vendor prices.'}
 (out/'kr_quote_corroboration.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--out',type=Path,required=True);a=p.parse_args();run(a.out)
