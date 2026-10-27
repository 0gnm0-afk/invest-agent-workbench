"""Open the browser-created isolated database in a fresh server process."""
from copy import deepcopy
import json
from pathlib import Path
import sys
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from dashboard import handler_for,ThreadingHTTPServer
from market.history.chart import aggregate
root=Path(__file__).resolve().parents[1];folder=root/'runtime/scenario-monitor-verification'
ready=json.loads((folder/'fixture-ready.json').read_text());packet=json.loads((folder/'fixture-input.json').read_text())
db=Path(ready['database']).resolve();assert db.is_relative_to(folder.resolve())
stock=packet['stock'];days=[b['date'] for b in stock['bars']]
def chart(s,f):
    return {**deepcopy(s),'timeframe':f,'bars':aggregate(s['bars'],f),'history':dict(state='complete',kind='synthetic',
        first=days[0],last=days[-1],daily_count=len(days),can_retry=False,collected_at='synthetic fixture')}
hist=SimpleNamespace(saved=SimpleNamespace(stocks={},markets={}),view=chart)
fin=SimpleNamespace(read=lambda k:dict(state='unavailable',result=None))
data=dict(accounts=packet['accounts'],market=dict(state='saved',session_dates=packet['targets'],entries=[],sectors=[],candidate_count=0,
    entry_candidate_count=0,quality=dict(received=1,requested=1,failed=0),errors=[],limitations=[],source='synthetic_fixture'))
server=ThreadingHTTPServer(('127.0.0.1',0),handler_for(data,{'US:SYNTH':stock},root/'dashboard','fixture-browser',db,
    analysis_capabilities={'collaboration':'fixture-collab','risk':'fixture-risk'},analysis_dependencies={'histories':hist,'financials':fin},offline=True))
ready['url']='http://127.0.0.1:'+str(server.server_port)
(folder/'fixture-ready.json').write_text(json.dumps(ready),encoding='utf-8')
print(json.dumps(ready),flush=True)
try:server.serve_forever()
finally:server.server_close()
