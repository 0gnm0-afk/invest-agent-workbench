"""Interactive proof uses synthetic company/account evidence and an isolated database."""
from copy import deepcopy
from datetime import date, timedelta, datetime, timezone
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parent))
from test_collaboration import CollaborationTests, ROOT
from web.server import handler_for, ThreadingHTTPServer
from market.history.chart import aggregate


def main():
    import argparse
    parser=argparse.ArgumentParser()
    parser.add_argument('--database',type=Path,help='Resume an existing isolated synthetic fixture database.')
    args=parser.parse_args()
    fixture=CollaborationTests();fixture.setUp()
    days=[date(2026,6,1)+timedelta(days=i) for i in range(135)]
    days=[d.isoformat() for d in days if d.weekday()<5]
    fixture.bars=[dict(date=d,open=10+i*.01,high=10.3+i*.01,low=9.7+i*.01,
                       close=10.1+i*.01,volume=10000+i*50) for i,d in enumerate(days)]
    fixture.stock.update(bars=fixture.bars,name='합성 검증 종목',provider='synthetic fixture')
    fixture.accounts.update(as_of=datetime.now(timezone.utc).isoformat(),query_state='received',stale=False,source_mode='synthetic')
    fixture.accounts['accounts'][0].update(alias='synthetic',holdings_complete=True,cash_complete=True,cash={'USD':'1000'},positions=[])
    fixture.data['market'].update(state='saved',session_dates={'US':days[-1]},entries=[],sectors=[],candidate_count=0,
        entry_candidate_count=0,quality=dict(received=1,requested=1,failed=0),errors=[],limitations=[],source='synthetic_fixture')
    fixture.data.update(as_of=datetime.now(timezone.utc).isoformat(),generated_at=datetime.now(timezone.utc).isoformat())
    def chart(stock,frame):
        return {**deepcopy(stock),'timeframe':frame,'bars':aggregate(stock['bars'],frame),
                'history':dict(state='complete',kind='synthetic',first=days[0],last=days[-1],
                               daily_count=len(days),can_retry=False,collected_at='synthetic fixture')}
    fixture.hist.view=chart
    snap=fixture.s.capture(fixture.s.case(fixture.cid));fixture.sid=snap['id']
    fixture.call('record',dict(snapshot_id=fixture.sid,kind='report',title='합성 시나리오 검증 전용',
        body='실제 기업이 아닌 합성 검증 자료. 가격은 완만하게 상승, 사업 실적·촉매는 확인되지 않았다. '
             '시나리오 가격은 가정이며 확률이나 투자 권고를 만들지 않는다. 기존 사용자 작도는 없다.',author='llm'))
    database=args.database or fixture.store.path
    server=ThreadingHTTPServer(('127.0.0.1',0),handler_for(fixture.data,{'US:SYNTH':fixture.stock},ROOT/'dashboard',
        'fixture-browser',database,analysis_capabilities={'collaboration':'fixture-collab','risk':'fixture-risk'},
        analysis_dependencies={'histories':fixture.hist,'financials':fixture.fin},offline=True))
    ready={'url':'http://127.0.0.1:'+str(server.server_port),'database':str(database),
           'analysis_key':fixture.cid,'pid':__import__('os').getpid()}
    output=ROOT/'runtime/trade-cycle-verification';output.mkdir(parents=True,exist_ok=True)
    (output/'fixture-ready.json').write_text(json.dumps(ready),encoding='utf-8')
    print(json.dumps(ready),flush=True)
    try:server.serve_forever()
    finally:server.server_close();fixture.tearDown()


if __name__=='__main__':main()
