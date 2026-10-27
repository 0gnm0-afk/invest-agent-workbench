"""Pin public KRX-derived annual source files, not a KRX login workaround."""

if not __package__:
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import argparse,hashlib,json,re,urllib.request
from datetime import datetime,timezone
from pathlib import Path

def run(out,commit=None):
 folder=out/'raw/kr-marcap';folder.mkdir(parents=True,exist_ok=True)
 if (folder/'commit.txt').exists():
  existing=(folder/'commit.txt').read_text().strip()
  assert commit is None or commit==existing,'Do not mix source commits in an existing study'
  commit=existing
 if commit is None:
  req=urllib.request.Request('https://api.github.com/repos/FinanceData/marcap/commits/master',headers={'User-Agent':'InvestWorkbench-research'})
  with urllib.request.urlopen(req,timeout=30) as r: commit=json.load(r)['sha']
 assert re.fullmatch('[0-9a-f]{40}',commit)
 (folder/'commit.txt').write_text(commit,encoding='ascii');rows=[]
 for year in range(2015,2027):
  name=f'marcap-{year}.parquet';path=folder/name;url=f'https://raw.githubusercontent.com/FinanceData/marcap/{commit}/data/{name}'
  if not path.exists():
   with urllib.request.urlopen(url,timeout=60) as r: raw=r.read(64000001)
   assert len(raw)<=64000000 and raw[:4]==b'PAR1' and raw[-4:]==b'PAR1'
   path.write_bytes(raw)
  rows.append({'year':year,'url':url,'bytes':path.stat().st_size,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
  print('KR source',year,'ready',flush=True)
 (out/'kr_acquisition.json').write_text(json.dumps({'commit':commit,'at':datetime.now(timezone.utc).isoformat(),'files':rows},indent=2),encoding='utf-8')

if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--out',type=Path,required=True);p.add_argument('--commit');a=p.parse_args();run(a.out,a.commit)
