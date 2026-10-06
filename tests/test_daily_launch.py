import hashlib
import http.client
import json
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from daily_launch import DailyLaunch, plan_for, capped_sessions

NOW=datetime(2026,9,30,8,tzinfo=timezone.utc)

class Calendar(unittest.TestCase):
    def test_kst_previous_date_even_after_kr_close(self):
        with patch('daily_launch.completed_sessions',return_value=['2026-09-28','2026-09-29','2026-09-30']):
            plan=plan_for(NOW)
        self.assertEqual(plan['day'],'2026-09-30');self.assertEqual(plan['targets'],{'KR':'2026-09-29','US':'2026-09-29'})
    def test_us_not_closed_and_different_holidays(self):
        with patch('daily_launch.completed_sessions',side_effect=lambda market,now: {'KR':['2026-09-23'],'US':['2026-09-24']}[market]):
            plan=plan_for(datetime(2026,9,26,0,tzinfo=timezone.utc))
        self.assertEqual(plan['targets'],{'KR':'2026-09-23','US':'2026-09-24'})
    def test_kst_midnight_not_utc_day_and_invalid(self):
        with patch('daily_launch.completed_sessions',return_value=['2026-09-29']):
            self.assertEqual(plan_for(datetime(2026,9,29,15,tzinfo=timezone.utc))['day'],'2026-09-30')
        with self.assertRaises(ValueError):plan_for(datetime(2026,9,30))
        with self.assertRaises(ValueError):capped_sessions(['2026-09-30'],'2026-09-29')

class Launch(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.calls=[]
        self.targets={'KR':'2026-09-29','US':'2026-09-29'}
        self.plan=lambda now:{'day':now.astimezone(__import__('zoneinfo').ZoneInfo('Asia/Seoul')).date().isoformat(),'cutoff':'2026-09-29','evaluated_at':now.isoformat(),'targets':self.targets}
        self.gate=DailyLaunch(self.root,self.runner,self.plan)
    def tearDown(self):self.temp.cleanup()
    def result(self,name,state='succeeded',targets=None,screen=True):
        folder=self.root/'runtime'/name;folder.mkdir(parents=True,exist_ok=True)
        run={'output':str(folder),'state':state,'stages':{'report':'completed'},'market_source':str(folder/'source')}
        payload={'run.json':run}
        if screen:payload['priority-review.json']={'session_dates':targets or self.targets}
        hashes={}
        for file,value in payload.items():
            body=json.dumps(value).encode();(folder/file).write_bytes(body);hashes[file]=hashlib.sha256(body).hexdigest()
        (folder/'manifest.json').write_text(json.dumps({'output_hashes':hashes}))
        (self.root/'runtime/latest.json').write_text(json.dumps({'output':str(folder)}))
        return run
    def runner(self,plan,progress):
        self.calls.append(plan);progress({'stage':'stock_screen'});return self.result('run-'+str(len(self.calls)))
    def test_receipt_retries_temporary_windows_file_lock(self):
        from daily_launch import atomic_write
        with patch('daily_launch.atomic_write',side_effect=[PermissionError(),None]) as writer:
            self.gate.save({'day':'2026-09-30','state':'running'})
        self.assertEqual(writer.call_count,2)

    def test_first_executes_second_and_new_gate_reuse(self):
        a=self.gate.once(NOW);b=self.gate.once(NOW);c=DailyLaunch(self.root,self.runner,self.plan).once(NOW)
        self.assertFalse(a['reused']);self.assertTrue(b['reused']);self.assertTrue(c['reused']);self.assertEqual(len(self.calls),1)
    def test_next_kst_day_runs_once(self):
        self.gate.once(NOW);self.gate.once(datetime(2026,9,30,16,tzinfo=timezone.utc));self.assertEqual(len(self.calls),2)
    def test_explicit_retry_only_archives_receipt(self):
        self.gate.once(NOW);self.gate.once(NOW,retry=True);self.assertEqual(len(self.calls),2);self.assertEqual(len(list((self.gate.folder/'attempts').glob('*.json'))),1)
    def test_partial_is_retained_without_rerun(self):
        self.gate.runner=lambda p,c:self.result('partial','partial')
        self.assertEqual(self.gate.once(NOW)['state'],'partial');self.assertTrue(self.gate.once(NOW)['reused'])
    def test_auth_failure_keeps_old_display_and_is_not_retried(self):
        old=self.result('old')
        def failed(plan,progress):
            self.calls.append(plan);result=self.result('failed','failed',screen=False);result['error']='MarketReadError: market_authentication_http_403';return result
        self.gate.runner=failed;a=self.gate.once(NOW);b=self.gate.once(NOW)
        self.assertEqual(a['display_run'],old['output']);self.assertEqual(a['error'],'market_authentication_http_403');self.assertTrue(self.gate.public(a)['display_stale']);self.assertEqual(len(self.calls),1)
        self.assertEqual(DailyLaunch(self.root).display_run(),old['output'])
    def test_ip_denial_survives_receipt_without_automatic_retry(self):
        old=self.result('old')
        def failed(plan,progress):
            self.calls.append(plan);result=self.result('failed','failed',screen=False)
            result['error']='MarketReadError: market_authentication_http_403_ip_not_allowed'
            return result
        self.gate.runner=failed;a=self.gate.once(NOW);b=self.gate.once(NOW)
        self.assertEqual(a['display_run'],old['output'])
        self.assertEqual(a['error'],'market_authentication_http_403_ip_not_allowed')
        self.assertEqual(b['error'],a['error']);self.assertTrue(b['reused'])
        self.assertTrue(self.gate.public(a)['display_stale']);self.assertEqual(len(self.calls),1)
    def test_basis_refresh_error_survives_receipt(self):
        self.result('old')
        def failed(plan,progress):
            result=self.result('failed','failed',screen=False)
            result['error']='ValueError: basis_document_changed_review_required'
            return result
        self.gate.runner=failed
        self.assertEqual(self.gate.once(NOW)['error'],'basis_document_changed_review_required')

    def test_interrupted_receipt_never_reruns_automatically(self):
        self.gate.save({'day':'2026-09-30','state':'running','display_run':None})
        a=self.gate.once(NOW);self.assertEqual(a['state'],'interrupted');self.assertEqual(self.calls,[])
    def test_concurrent_launches_only_one_owner(self):
        entered=threading.Event();release=threading.Event();out=[]
        def slow(plan,progress):entered.set();release.wait(5);return self.runner(plan,progress)
        self.gate.runner=slow
        thread=threading.Thread(target=lambda:out.append(self.gate.once(NOW)));thread.start();self.assertTrue(entered.wait(5))
        try:
            second=DailyLaunch(self.root,self.runner,self.plan).once(NOW);self.assertTrue(second['following'])
        finally:release.set();thread.join(5)
        self.assertEqual(len(self.calls),1);self.assertEqual(out[0]['state'],'succeeded')
    def test_wrong_target_cannot_claim_success(self):
        self.gate.runner=lambda p,c:self.result('stale',targets={'KR':'2026-09-23','US':'2026-09-24'})
        a=self.gate.once(NOW);self.assertEqual(a['state'],'failed');self.assertEqual(a['error'],'target_sessions_not_reached')
    def test_missing_screen_cannot_claim_success(self):
        self.gate.runner=lambda p,c:self.result('missing',screen=False)
        self.assertEqual(self.gate.once(NOW)['error'],'screening_result_missing')
    def test_hash_tamper_and_outside_run_rejected(self):
        result=self.result('ok');(Path(result['output'])/'run.json').write_text('{}')
        self.assertIsNone(self.gate.usable_run(result['output']))
        with self.assertRaises(ValueError):self.gate.checked_run(self.root.parent)
    def test_runner_exception_is_durable_and_private(self):
        self.gate.runner=lambda p,c:(_ for _ in ()).throw(ValueError('SECRET'))
        a=self.gate.once(NOW);self.assertEqual(a['state'],'failed');self.assertNotIn('SECRET',json.dumps(a));self.assertTrue(self.gate.once(NOW)['reused'])
    def test_daily_pipeline_prior_seed_not_shadowed_by_latest_file(self):
        import daily_market
        prior=self.result('prior')
        with patch.object(daily_market,'ROOT',self.root), patch.object(daily_market,'code_hashes',return_value={}), patch.object(daily_market,'seed_check',return_value=({},{})), patch.object(daily_market,'collect',side_effect=ValueError('stop_before_network')) as collector:
            daily_market.run({'engine':str(self.root)},session_cutoff='2026-09-29',evaluated_at=NOW.isoformat(),prior_result=prior['output'])
        self.assertEqual(collector.call_args.kwargs['prior_source'],prior['market_source'])
        self.assertEqual(collector.call_args.kwargs['session_cutoff'],'2026-09-29')

    def test_worker_process_passes_cutoff_and_prior_run(self):
        self.gate.accounts=False
        old=self.result('old');new=self.result('new')
        folder=self.root/'scripts';folder.mkdir()
        program='import json,sys\nfrom pathlib import Path\nPath("args.json").write_text(json.dumps(sys.argv))\nprint('+repr(json.dumps({'stage':'collection','output':new['output']}))+',flush=True)\nprint('+repr(json.dumps(new))+',flush=True)\n'
        (folder/'daily_market.py').write_text(program)
        self.gate.folder.mkdir(parents=True,exist_ok=True)
        progress=[];got=self.gate.execute({**self.plan(NOW),'prior_run':old['output']},progress.append)
        args=json.loads((self.root/'args.json').read_text());self.assertIn('--session-cutoff',args);self.assertIn('--evaluated-at',args);self.assertIn(old['output'],args);self.assertEqual(got,{**new,"trailing_state":"unavailable"});self.assertEqual(progress[0]['stage'],'collection')
        with patch('trailing_warnings.refresh_saved',return_value={'events':[]}) as refresh:
            got=self.gate.execute({**self.plan(NOW),'prior_run':old['output']},lambda _:None)
        self.assertEqual(got['trailing_state'],'evaluated')
        refresh.assert_called_once_with(self.root,new['output'],self.targets)
        self.gate.accounts=True
        order=[]
        with patch('dashboard_data.refresh_account_snapshot',side_effect=lambda root: (order.append('account') or {'query_state':'received'})) as account, patch('trailing_warnings.refresh_saved',side_effect=lambda *a:order.append('risk')):
            got=self.gate.execute({**self.plan(NOW),'prior_run':old['output']},lambda _:None)
        self.assertEqual(order,['account','risk']);account.assert_called_once_with(self.root)
        self.assertEqual(got['account_state'],'received')
        with patch('dashboard_data.refresh_account_snapshot',side_effect=OSError('test')), patch('trailing_warnings.refresh_saved') as warning:
            got=self.gate.execute({**self.plan(NOW),'prior_run':old['output']},lambda _:None)
        self.assertEqual(got['account_state'],'failed');self.assertEqual(got['trailing_state'],'unavailable');warning.assert_not_called()


class DashboardViews(unittest.TestCase):
    def test_published_view_and_update_status_are_read_only(self):
        from dashboard import handler_for, ThreadingHTTPServer
        with tempfile.TemporaryDirectory() as tmp:
            holder={'view':({'generation':1},{'KR:A':{'bars':[1]}})}
            server=ThreadingHTTPServer(('127.0.0.1',0),handler_for({}, {},Path(tmp),'token',Path(tmp)/'workspace.sqlite3',views=holder,daily_status=lambda:{'enabled':True,'state':'partial','view_ready':True}))
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            def get(path):
                conn=http.client.HTTPConnection('127.0.0.1',server.server_port);conn.request('GET',path);response=conn.getresponse();result=json.loads(response.read());conn.close();return result
            try:
                self.assertEqual(get('/api/home')['generation'],1)
                holder['view']=({'generation':2},{'KR:A':{'bars':[2]}})
                self.assertEqual(get('/api/home')['generation'],2)
                self.assertEqual(get('/api/stock/KR:A')['bars'],[2])
                self.assertEqual(get('/api/daily-update')['state'],'partial')
            finally:server.shutdown();server.server_close();thread.join()

if __name__=='__main__':unittest.main()
