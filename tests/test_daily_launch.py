import hashlib
import http.client
import json
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch
from contextlib import contextmanager, ExitStack
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from launch.daily import DailyLaunch, plan_for, capped_sessions
from market.collection_receipts import collection_receipt, ROLE

NOW=datetime(2026,9,30,8,tzinfo=timezone.utc)

class Calendar(unittest.TestCase):
    def test_kst_previous_date_even_after_kr_close(self):
        with patch('launch.daily.completed_sessions',return_value=['2026-09-28','2026-09-29','2026-09-30']):
            plan=plan_for(NOW)
        self.assertEqual(plan['day'],'2026-09-30');self.assertEqual(plan['targets'],{'KR':'2026-09-29','US':'2026-09-29'})
    def test_us_not_closed_and_different_holidays(self):
        with patch('launch.daily.completed_sessions',side_effect=lambda market,now: {'KR':['2026-09-23'],'US':['2026-09-24']}[market]):
            plan=plan_for(datetime(2026,9,26,0,tzinfo=timezone.utc))
        self.assertEqual(plan['targets'],{'KR':'2026-09-23','US':'2026-09-24'})
    def test_kst_midnight_not_utc_day_and_invalid(self):
        with patch('launch.daily.completed_sessions',return_value=['2026-09-29']):
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
        # screen is a legacy fixture argument: False now means no published C9.
        folder=self.root/'runtime'/name;folder.mkdir(parents=True,exist_ok=True)
        run={'output':str(folder),'state':state,'stages':{'report':'completed'},'market_source':str(folder/'source')}
        payload={'run.json':run}
        if screen:
            run.update(mode='live', data_times={'session_dates':targets or self.targets},
                       quality={'markets':{m:{'requested':2,'received':1 if state=='partial' else 2,
                                              'failed':1 if state=='partial' else 0} for m in ('KR','US')}})
        hashes={}
        for file,value in payload.items():
            body=json.dumps(value).encode();(folder/file).write_bytes(body);hashes[file]=hashlib.sha256(body).hexdigest()
        if screen:
            body=json.dumps(collection_receipt(run, hashes['run.json'])).encode()
            (folder/ROLE).write_bytes(body);hashes[ROLE]=hashlib.sha256(body).hexdigest()
        (folder/'manifest.json').write_text(json.dumps({'output_hashes':hashes}))
        (self.root/'runtime/latest.json').write_text(json.dumps({'output':str(folder)}))
        return run
    def runner(self,plan,progress):
        self.calls.append(plan);progress({'stage':'quality'});return self.result('run-'+str(len(self.calls)))
    def test_receipt_retries_temporary_windows_file_lock(self):
        from launch.daily import atomic_write
        with patch('launch.daily.atomic_write',side_effect=[PermissionError(),None]) as writer:
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
    def test_missing_collection_receipt_cannot_claim_success(self):
        self.gate.runner=lambda p,c:self.result('missing',screen=False)
        self.assertEqual(self.gate.once(NOW)['error'],'collection_result_missing')
    def test_hash_tamper_and_outside_run_rejected(self):
        result=self.result('ok');(Path(result['output'])/'run.json').write_text('{}')
        self.assertIsNone(self.gate.usable_run(result['output']))
        with self.assertRaises(ValueError):self.gate.checked_run(self.root.parent)
    def test_moved_latest_and_receipt_reuse_the_saved_run(self):
        run = self.result('saved')
        previous = self.root / 'previous-workspace'
        (self.root / 'config.json').write_text(json.dumps({'records': {
            'previous_roots': [str(previous)]}}), encoding='utf-8')
        old_run = str(previous / 'runtime/saved')
        latest = self.root / 'runtime/latest.json'
        latest.write_text(json.dumps({'output': old_run}), encoding='utf-8')
        self.assertEqual(self.gate.display_run(), run['output'])
        self.gate.save({'day': '2026-09-30', 'state': 'succeeded',
                        'display_run': old_run, 'run': old_run, 'targets': self.targets})
        latest.unlink()
        self.assertEqual(self.gate.display_run(), run['output'])
        self.assertTrue(self.gate.target_complete(self.gate.read('2026-09-30'), self.plan(NOW)))
        progress = self.root / 'runtime/saved/collection/cache/progress.json'
        progress.parent.mkdir(parents=True)
        progress.write_text(json.dumps({'requested': 2, 'completed': 1, 'received': 1, 'failed': 0}), encoding='utf-8')
        public = self.gate.public({'state': 'running', 'run': old_run, 'display_run': run['output']})
        self.assertFalse(public['display_stale'])
        self.assertEqual(public['progress']['completed'], 1)
        self.assertEqual(self.calls, [])
        with self.assertRaisesRegex(ValueError, 'outside_runtime'):
            self.gate.checked_run(previous / 'runtime/../../outside')
    def test_runner_exception_is_durable_and_private(self):
        self.gate.runner=lambda p,c:(_ for _ in ()).throw(ValueError('SECRET'))
        a=self.gate.once(NOW);self.assertEqual(a['state'],'failed');self.assertNotIn('SECRET',json.dumps(a));self.assertTrue(self.gate.once(NOW)['reused'])
    def test_daily_pipeline_prior_seed_not_shadowed_by_latest_file(self):
        import market.daily as daily_market
        prior=self.result('prior')
        with patch.object(daily_market,'ROOT',self.root), patch.object(daily_market,'code_hashes',return_value={}), patch.object(daily_market,'collect',side_effect=ValueError('stop_before_network')) as collector:
            daily_market.run({'engine':str(self.root)},session_cutoff='2026-09-29',evaluated_at=NOW.isoformat(),prior_result=prior['output'])
        self.assertEqual(collector.call_args.kwargs['prior_source'],prior['market_source'])
        self.assertEqual(collector.call_args.kwargs['session_cutoff'],'2026-09-29')

    @contextmanager
    def worker(self, result, *, trailing=None, scenario=None, technical=None, delivery=None):
        self.gate.folder.mkdir(parents=True,exist_ok=True)
        process = MagicMock()
        process.__enter__.return_value = process
        process.poll.return_value = process.returncode = 0
        def spawn(command, **kwargs):
            kwargs['stdout'].write(json.dumps({'stage':'collection','output':result['output']})+'\n')
            kwargs['stdout'].write(json.dumps(result)+'\n')
            kwargs['stdout'].flush()
            return process
        with ExitStack() as stack:
            popen=stack.enter_context(patch('launch.daily.subprocess.Popen',side_effect=spawn))
            account=stack.enter_context(patch('accounts.refresh_account_snapshot',create=True,side_effect=AssertionError('no_account_query')))
            risk=stack.enter_context(patch('alerts.trailing.refresh_saved',return_value=trailing or {'monitors':[],'events':[]}))
            monitor=stack.enter_context(patch('scenarios.monitor.refresh_saved',return_value=scenario or {'monitors':[]}))
            signal=stack.enter_context(patch('signals.store.evaluate_saved',return_value=technical or {'state':'not_configured'}))
            send=stack.enter_context(patch('alerts.delivery.dispatch',return_value=delivery or {'state':'disabled'}))
            yield popen,process,account,risk,monitor,signal,send

    def test_worker_process_passes_cutoff_and_prior_run(self):
        old=self.result('old');new=self.result('new')
        with self.worker(new) as (popen,process,account,risk,monitor,signal,send):
            progress=[]
            got=self.gate.execute({**self.plan(NOW),'prior_run':old['output']},progress.append)
        args=popen.call_args.args[0]
        self.assertEqual(args[args.index('--session-cutoff')+1],self.plan(NOW)['cutoff'])
        self.assertEqual(args[args.index('--evaluated-at')+1],NOW.isoformat())
        self.assertEqual(args[args.index('--prior-run')+1],old['output'])
        self.assertEqual(progress[0]['stage'],'collection')
        process.wait.assert_called_once();process.__exit__.assert_called_once()
        self.assertTrue(popen.call_args.kwargs['stdout'].closed)
        account.assert_not_called()
        risk.assert_called_once_with(self.root,new['output'],self.targets)
        monitor.assert_called_once_with(self.root,new['output'],self.targets)
        self.assertFalse(got['account_attempted'])

    def test_c9_status_wins_over_downstream_worker_status(self):
        self.gate.runner=lambda p,c:{**self.result('collection-complete'),'state':'partial','telegram_state':'unavailable'}
        got=self.gate.once(NOW)
        self.assertEqual(got['state'],'succeeded')
        self.assertEqual(got['collection_receipt']['status'],'complete')
        self.assertEqual(got['telegram_state'],'unavailable')
        self.assertTrue(self.gate.target_complete(got,self.plan(NOW)))
        self.assertFalse((Path(got['run'])/'priority-review.json').exists())

    def test_c9_partial_failed_and_receipt_tamper(self):
        partial=self.result('partial','partial')
        receipt=self.gate.collection_receipt(partial['output'],self.targets)
        self.assertEqual(receipt['status'],'partial')
        self.assertFalse(self.gate.target_complete(dict(state='succeeded',run=partial['output'],targets=self.targets),self.plan(NOW)))
        run=self.result('zero')
        path=Path(run['output'])
        run['quality']['markets']['US'].update(received=0,failed=2)
        raw=json.dumps(run).encode();(path/'run.json').write_bytes(raw)
        hashes={'run.json':hashlib.sha256(raw).hexdigest()}
        raw=json.dumps(collection_receipt(run,hashes['run.json'])).encode();(path/ROLE).write_bytes(raw)
        hashes[ROLE]=hashlib.sha256(raw).hexdigest()
        (path/'manifest.json').write_text(json.dumps({'output_hashes':hashes}))
        self.assertEqual(self.gate.collection_receipt(path)['status'],'failed')
        self.assertIsNone(self.gate.usable_run(path))
        self.gate.runner=lambda p,c:run
        self.assertEqual(self.gate.once(NOW)['state'],'failed')
        path=Path(partial['output'])
        (path/ROLE).write_text('{}')
        self.assertIsNone(self.gate.usable_run(path))
        with self.assertRaises(ValueError):self.gate.collection_receipt(self.root.parent)

    def test_successful_reopen_rechecks_c9_without_mutating_old_attempt(self):
        first=self.gate.once(NOW)
        saved=self.gate.read(first['day'])
        (Path(first['run'])/ROLE).write_text('{}')
        reopened=self.gate.once(NOW)
        self.assertEqual(reopened['state'],'failed')
        self.assertEqual(reopened['error'],'collection_result_missing')
        self.assertTrue(reopened['reused'])
        self.assertEqual(len(self.calls),1)
        self.assertEqual(self.gate.read(first['day']),saved)
        retried=self.gate.once(NOW,retry=True,expected_attempt=first['attempt_id'])
        self.assertFalse(retried['reused']);self.assertEqual(retried['state'],'succeeded')
        self.assertEqual(len(self.calls),2)
        archived=list((self.gate.folder/'attempts').glob('*.json'))
        self.assertEqual(json.loads(archived[0].read_text()),saved)

    def test_worker_auth_error_survives_missing_c9_without_automatic_retry(self):
        run=self.result('worker-failed','failed',screen=False)
        run['error']='MarketReadError: market_authentication_http_403_ip_not_allowed'
        with self.worker(run) as (popen,_,account,risk,monitor,signal,send):
            self.gate.runner=self.gate.execute
            got=self.gate.once(NOW)
            reopened=self.gate.once(NOW)
            popen.assert_called_once()
            account.assert_not_called();risk.assert_not_called();monitor.assert_not_called()
            signal.assert_not_called();send.assert_not_called()
        self.assertEqual(got['error'],'market_authentication_http_403_ip_not_allowed')
        self.assertEqual(got['state'],'failed')
        self.assertTrue(reopened['reused'])

    def test_c9_missing_or_forged_provenance_is_not_reused(self):
        run=self.result('missing-receipt');path=Path(run['output'])
        (path/ROLE).unlink()
        self.assertIsNone(self.gate.usable_run(path))
        run=self.result('forged');path=Path(run['output'])
        receipt=json.loads((path/ROLE).read_text());receipt['provenance']['provider']='other'
        raw=json.dumps(receipt).encode();(path/ROLE).write_bytes(raw)
        manifest=json.loads((path/'manifest.json').read_text())
        manifest['output_hashes'][ROLE]=hashlib.sha256(raw).hexdigest()
        (path/'manifest.json').write_text(json.dumps(manifest))
        self.assertIsNone(self.gate.usable_run(path))

    def test_retry_attempt_guard_and_account_failure_do_not_recollect(self):
        self.gate.runner=lambda p,c:self.result('first','partial')
        first=self.gate.once(NOW)
        self.assertTrue(self.gate.once(NOW,retry=True,expected_attempt='wrong')['reused'])
        self.gate.runner=self.runner
        retried=self.gate.once(NOW,retry=True,expected_attempt=first['attempt_id'])
        self.assertFalse(retried['reused']);self.assertEqual(len(self.calls),1)
        archived=list((self.gate.folder/'attempts').glob('*.json'))
        self.assertEqual(json.loads(archived[0].read_text()),{k:v for k,v in first.items() if k!='reused'})
        self.assertTrue(self.gate.once(NOW,retry=True,expected_attempt=first['attempt_id'])['reused'])
        retried['account_state']='failed';self.gate.save(retried)
        self.assertTrue(self.gate.once(NOW,retry=True,expected_attempt=retried['attempt_id'])['reused'])
        self.assertEqual(len(self.calls),1)

    def test_saved_observation_and_delivery_are_independent_of_accounts_flag(self):
        observation={'as_of':'2026-09-25T01:00:00+00:00','query_state':'partial','source_mode':'saved'}
        risk={'account_observation':observation,'monitors':[{'active':True,'status':'deferred','issue':'account_stale_or_incomplete','observed_account_as_of':observation['as_of']}],'events':[]}
        new=self.result('new')
        for accounts in (False, True):
            self.gate.accounts=accounts
            with self.worker(new,trailing=risk,scenario=risk,technical={'state':'evaluated'}) as (_,_,account,_,_,_,send):
                got=self.gate.execute(self.plan(NOW),lambda p:None)
                send.assert_called_once_with(self.root,self.targets,include_trailing=True,include_scenarios=True,include_technical=True)
                account.assert_not_called()
            self.assertEqual(got['trailing_result'],risk)
            self.assertEqual(got['scenario_result'],risk)
            self.assertEqual(self.gate.public(got)['trailing_result']['account_observation'],observation)
            self.assertIsNone(got['account_state']);self.assertFalse(got['account_attempted'])

    def test_delivery_failure_and_saved_account_failure_keep_collection_success(self):
        new=self.result('new')
        with self.worker(new,technical={'state':'evaluated'}) as (_,_,account,risk,monitor,_,send):
            risk.side_effect=ValueError('PRIVATE_ACCOUNT_FAILURE')
            monitor.side_effect=ValueError('PRIVATE_ACCOUNT_FAILURE')
            send.side_effect=ValueError('PRIVATE_DELIVERY_FAILURE')
            got=self.gate.execute(self.plan(NOW),lambda p:None)
        self.assertEqual(got['state'],'succeeded')
        self.assertEqual(got['trailing_state'],'unavailable')
        self.assertEqual(got['telegram_state'],'unavailable')
        send.assert_called_once_with(self.root,self.targets,include_trailing=False,include_scenarios=False,include_technical=True)
        account.assert_not_called()
        self.assertNotIn('PRIVATE_',json.dumps(got))


class DashboardViews(unittest.TestCase):
    def test_published_view_and_update_status_are_read_only(self):
        from web.server import handler_for, ThreadingHTTPServer
        with tempfile.TemporaryDirectory() as tmp:
            holder={'view':({'generation':1},{'KR:A':{'bars':[1]}})}
            with patch('socket.getfqdn',return_value='127.0.0.1'):
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
