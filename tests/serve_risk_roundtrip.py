"""Full dashboard + real temporary collaboration DB, synthetic account only."""
import json
import os
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_risk_roundtrip import Roundtrip, ROOT
from dashboard import handler_for, ThreadingHTTPServer


def main():
    fixture=Roundtrip();fixture.setUp()
    fixture.save(fixture.calculate())
    data=fixture.f.data
    data.update(generated_at=fixture.clock())
    data['market'].update(entries=[],sectors=[],session_dates={},quality={},errors=[],candidate_count=0,
                         entry_candidate_count=0,focus={'items':[]})
    data['accounts'].update(accounts=data['accounts']['scenario_input']['accounts'], as_of=data['accounts']['scenario_input']['as_of'], query_state='not_requested',
                            source_mode='historical_saved',attempted_at=None,limitations=[],stale=False)
    server=ThreadingHTTPServer(('127.0.0.1',0),handler_for(data,{'US:SYNTH':fixture.f.stock},ROOT/'dashboard',
        'synthetic-browser',fixture.f.store.path,analysis_dependencies={'histories':fixture.f.hist,'financials':fixture.f.fin},
        risk_options={'clock':fixture.clock,'allow_test_records':True}))
    print(json.dumps({'url':'http://127.0.0.1:'+str(server.server_port),'root':str(fixture.f.root),
                      'analysis_key':fixture.f.cid,'pid':os.getpid()}),flush=True)
    try:server.serve_forever()
    finally:server.server_close();fixture.tearDown()


if __name__=='__main__':main()
