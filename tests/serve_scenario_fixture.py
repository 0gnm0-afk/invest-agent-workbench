"""Full dashboard with explicitly synthetic saved records; no provider or sender IO."""
import json
from datetime import date, timedelta, datetime, timezone
from pathlib import Path
import sys
from copy import deepcopy

sys.path.insert(0,str(Path(__file__).resolve().parent))
from test_collaboration import CollaborationTests, ROOT
from dashboard import handler_for, ThreadingHTTPServer
from chart_history import aggregate
from drawings.store import defaults


def main():
    fixture=CollaborationTests();fixture.setUp()
    days=[date(2026,8,1)+timedelta(days=i) for i in range(68)]
    days=[d.isoformat() for d in days if d.weekday()<5]
    fixture.bars=[dict(date=d,open=100,high=110,low=96,close=108,volume=1000) for d in days]
    fixture.stock.update(bars=fixture.bars,name='합성 종목 · 기능 검증 전용',provider='synthetic fixture')
    fixture.accounts.update(as_of=datetime.now(timezone.utc).isoformat(),query_state='received',stale=False,source_mode='synthetic')
    fixture.accounts['accounts'][0]['holdings_complete']=True
    fixture.accounts['accounts'][0]['positions'][0]['isin_code']='US0000000001'
    fixture.data['market'].update(state='saved',session_dates={'US':days[-1]},entries=[],sectors=[],candidate_count=0,entry_candidate_count=0,
        quality=dict(received=1,requested=1,failed=0),errors=[],limitations=[],source='synthetic_fixture')
    fixture.data.update(as_of=datetime.now(timezone.utc).isoformat(),generated_at=datetime.now(timezone.utc).isoformat())
    def chart(stock,frame):
        return {**deepcopy(stock),'timeframe':frame,'bars':aggregate(stock['bars'],frame),'history':dict(state='complete',kind='synthetic',
            first=days[0],last=days[-1],daily_count=len(days),can_retry=False,collected_at='synthetic fixture')}
    fixture.hist.view=chart
    state=defaults()
    state['drawings']=[dict(id='fixture_box',kind='box',points=[dict(time=days[2],price=90),dict(time=days[-2],price=110)],color='#2271b2',width=2,extend=False),
        dict(id='fixture_channel',kind='channel',points=[dict(time=days[2],price=80),dict(time=days[-2],price=95),dict(time=days[2],price=100)],color='#218775',width=2,extend=True),
        dict(id='fixture_rr',kind='riskreward',points=[dict(time=days[2],price=100),dict(time=days[-2],price=90),dict(time=days[-2],price=120)],color='#dd9500',width=2,extend=False)]
    fixture.store.shared('US:SYNTH',revision=0,state=state)
    snap=fixture.s.capture(fixture.s.case(fixture.cid));fixture.sid=snap['id']
    fixture.call('record',dict(snapshot_id=fixture.sid,kind='report',title='합성 정성 보고서 · 검증 전용',body='[business] 합성 자료로 기록 재개를 검증합니다.\n[risk] 실제 투자 판단이 아닙니다.\n[new] 새 세션에서 이 보고서를 다시 읽을 수 있습니다.\n[tsr] 실제 수치나 사용자 답을 생성하지 않았습니다.',author='llm'))
    fixture.discussion()
    server=ThreadingHTTPServer(('127.0.0.1',0),handler_for(fixture.data,{'US:SYNTH':fixture.stock},ROOT/'dashboard','fixture-browser',fixture.store.path,
        analysis_capabilities={'collaboration':'fixture-collab','risk':'fixture-risk'},analysis_dependencies={'histories':fixture.hist,'financials':fixture.fin},offline=True))
    ready=dict(url='http://127.0.0.1:'+str(server.server_port),analysis_key=fixture.cid,isolated_root=str(fixture.root),database=str(fixture.store.path))
    output=ROOT/'runtime/scenario-monitor-verification';output.mkdir(parents=True,exist_ok=True)
    (output/'fixture-ready.json').write_text(json.dumps(ready),encoding='utf-8')
    (output/'fixture-input.json').write_text(json.dumps(dict(accounts=fixture.accounts,stock=fixture.stock,targets={'US':days[-1]},sessions={'US':days})),encoding='utf-8')
    print(json.dumps(ready),flush=True)
    try:server.serve_forever()
    finally:server.server_close();fixture.tearDown()


if __name__=='__main__':main()
