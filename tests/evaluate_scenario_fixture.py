"""Evaluate only the isolated browser fixture and render alerts without delivery."""
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from scenarios.monitor import ScenarioStore
from alerts.delivery import scenario_message

root=Path(__file__).resolve().parents[1]
folder=root/'runtime/scenario-monitor-verification'
ready=json.loads((folder/'fixture-ready.json').read_text())
database=Path(ready['database']).resolve()
assert database.is_relative_to(folder.resolve())
packet=json.loads((folder/'fixture-input.json').read_text())
store=ScenarioStore(database)
before=store.read()
assert before['monitors'] and all(m['test_record'] for m in before['monitors'])
value=store.update(packet['accounts'],{'US:SYNTH':packet['stock']},packet['targets'],datetime.now(timezone.utc),packet['sessions'])
repeat=store.update(packet['accounts'],{'US:SYNTH':packet['stock']},packet['targets'],datetime.now(timezone.utc),packet['sessions'])
assert len(repeat['events'])==len(value['events'])
result=dict(synthetic=True,real_delivery=False,monitors=len(value['monitors']),events=len(value['events']),duplicate_repeat=len(repeat['events']),
    readings=[{k:m.get(k) for k in ('id','purpose','status','issue','latest','conversion')} for m in value['monitors']],
    messages=[scenario_message(e) for e in value['events']])
(folder/'browser-flow-evidence.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(result,ensure_ascii=False,indent=2))
