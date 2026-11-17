"""06:40 scheduling, durable receipts, and manual/scheduled collision tests."""
import io
import json
import tempfile
import sys
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from contextlib import redirect_stdout
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import hashlib
from market.collection_receipts import collection_receipt, ROLE
from launch.daily import DailyLaunch, plan_for
from launch.scheduled import run_scheduled, exit_code
from launch.journal import RunJournal
import launch.scheduled as scheduled

NOW = datetime(2026,9,30,8,tzinfo=timezone.utc)


class LaunchFixture:
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




class Scheduled(unittest.TestCase):
    def setUp(self):
        self.fx = LaunchFixture()
        self.fx.setUp()
        self.gate = self.fx.gate

    def tearDown(self):
        self.fx.tearDown()

    def test_before_0640_does_not_consume_today(self):
        before = datetime(2026,9,29,21,39,tzinfo=timezone.utc)
        self.assertEqual(self.gate.once(before, scheduled=True)['state'], 'not_due')
        self.assertIsNone(self.gate.read('2026-09-30'))
        at = datetime(2026,9,29,21,40,tzinfo=timezone.utc)
        self.assertEqual(self.gate.once(at, scheduled=True)['state'], 'succeeded')
        self.assertEqual(len(self.fx.calls),1)

    def test_schedule_then_dashboard_and_schedule_reuse(self):
        result = self.gate.once(NOW, scheduled=True)
        self.assertEqual(result['scheduled_day'],'2026-09-30')
        self.assertFalse(result['manual_retry'])
        self.assertTrue(self.gate.once(NOW)['reused'])
        self.assertTrue(self.gate.once(NOW,scheduled=True)['reused'])
        self.assertEqual(len(self.fx.calls),1)

    def test_successful_matching_manual_run_is_not_recollected(self):
        self.gate.once(NOW)
        self.assertTrue(self.gate.once(NOW,scheduled=True)['reused'])
        self.assertEqual(len(self.fx.calls),1)
        self.assertEqual(self.gate.read('2026-09-30')['scheduled_day'],'2026-09-30')

    def test_early_us_session_is_replaced_by_completed_session(self):
        self.fx.targets = {'KR':'2026-09-29','US':'2026-09-28'}
        self.gate.once(NOW)
        self.fx.targets = {'KR':'2026-09-29','US':'2026-09-29'}
        result=self.gate.once(NOW,scheduled=True)
        self.assertFalse(result['reused'])
        self.assertEqual(result['targets'],self.fx.targets)
        self.assertEqual(len(self.fx.calls),2)

    def test_earlier_failure_gets_only_one_scheduled_attempt(self):
        def failed(plan, progress):
            self.fx.calls.append(plan)
            return self.fx.result('failed','failed',screen=False)
        self.gate.runner=failed
        self.gate.once(NOW)
        self.assertFalse(self.gate.once(NOW,scheduled=True)['reused'])
        self.assertTrue(self.gate.once(NOW,scheduled=True)['reused'])
        self.assertEqual(len(self.fx.calls),2)

    def test_manual_retry_preserves_scheduled_receipt(self):
        self.gate.once(NOW,scheduled=True)
        self.gate.once(NOW,retry=True)
        self.assertTrue(self.gate.once(NOW,scheduled=True)['reused'])
        self.assertEqual(len(self.fx.calls),2)

    def test_tampered_collection_run_not_reused_as_success(self):
        result=self.gate.once(NOW)
        (Path(result['run'])/'run.json').write_text('{}')
        self.assertFalse(self.gate.once(NOW,scheduled=True)['reused'])
        self.assertEqual(len(self.fx.calls),2)

    def test_next_day_gets_new_scheduled_attempt(self):
        self.gate.once(NOW,scheduled=True)
        self.gate.once(datetime(2026,10,1,0,tzinfo=timezone.utc),scheduled=True)
        self.assertEqual(len(self.fx.calls),2)

    def test_orphaned_scheduled_owner_does_not_loop(self):
        self.gate.save({'day':'2026-09-30','state':'running','scheduled_day':'2026-09-30','display_run':None})
        self.assertEqual(self.gate.once(NOW,scheduled=True)['state'],'interrupted')
        self.assertEqual(self.fx.calls,[])

    def test_concurrent_dashboard_and_scheduler_share_lock(self):
        entered=threading.Event();release=threading.Event();out=[]
        def slow(plan,progress):
            entered.set();release.wait(5)
            return self.fx.runner(plan,progress)
        self.gate.runner=slow
        owner=threading.Thread(target=lambda:out.append(self.gate.once(NOW)))
        owner.start();self.assertTrue(entered.wait(5))
        other=DailyLaunch(self.fx.root,self.fx.runner,self.fx.plan)
        try:
            self.assertTrue(other.once(NOW,scheduled=True)['following'])
        finally:
            release.set();owner.join(5)
        self.assertTrue(other.once(NOW,scheduled=True)['reused'])
        self.assertEqual(len(self.fx.calls),1)

    def test_headless_follows_live_owner_and_returns_final_status(self):
        daily=Mock()
        daily.once.side_effect=[{'following':True,'state':'running'}, {'state':'partial'}]
        sleep=Mock()
        self.assertEqual(run_scheduled(daily,NOW,sleep)['state'],'partial')
        sleep.assert_called_once_with(5)
        self.assertEqual(daily.once.call_count,2)
        self.assertEqual([exit_code({'state':s}) for s in ('succeeded','partial','failed','interrupted')],[0,2,1,1])

    def test_real_calendar_dst_standard_and_holidays(self):
        summer=plan_for(datetime.fromisoformat('2026-07-08T06:40:00+09:00'))
        winter=plan_for(datetime.fromisoformat('2026-12-02T06:40:00+09:00'))
        weekend=plan_for(datetime.fromisoformat('2026-10-04T06:40:00+09:00'))
        self.assertEqual(summer['targets']['US'],'2026-07-07')
        self.assertEqual(winter['targets']['US'],'2026-12-01')
        self.assertEqual(weekend['targets']['US'],'2026-10-02')


class Diagnostics(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root/'config.json').write_text(json.dumps({
            'engine': str(self.root), 'mother': str(self.root),
        }), encoding='utf-8')
        self.stdout = io.StringIO()
        for target, value in (('launch.scheduled.ROOT', self.root),
                              ('launch.scheduled.getpass.getuser', Mock(return_value='fixture-user'))):
            scope = patch(target, value)
            scope.start()
            self.addCleanup(scope.stop)

    def test_check_uses_synthetic_config_without_collection(self):
        plan = {'day': '2026-09-30', 'cutoff': '2026-09-29',
                'targets': {'KR': '2026-09-29', 'US': '2026-09-29'}}
        with patch.object(sys, 'argv', ['scheduled_market.py', '--check']), \
                patch.object(scheduled, 'plan_for', return_value=plan), \
                patch.object(scheduled, 'DailyLaunch') as daily, \
                patch('market.daily.run') as collect, redirect_stdout(self.stdout):
            self.assertEqual(scheduled.main(), 0)
        daily.assert_not_called()
        collect.assert_not_called()
        result = json.loads(self.stdout.getvalue())
        self.assertEqual(result['state'], 'check_passed')
        self.assertEqual(result['user'], 'fixture-user')
        self.assertEqual(result['network_calls'], 0)
        self.assertEqual(result['plan'], plan)
        self.assertEqual(json.loads((self.root/'runtime/morning-schedule/latest-check.json').read_text()), result)
        self.assertFalse((self.root/'runtime/dashboard-daily').exists())

    def test_scheduled_entry_uses_no_account_query_and_preserves_partial_exit(self):
        record = {'state': 'partial', 'run': None, 'scheduled_day': '2026-09-30'}
        with patch.object(sys, 'argv', ['scheduled_market.py']), \
                patch.object(scheduled, 'DailyLaunch') as daily, redirect_stdout(self.stdout):
            daily.return_value.once.return_value = record
            daily.return_value.public.return_value = {'state': 'partial', 'account_attempted': False}
            self.assertEqual(scheduled.main(), 2)
        daily.assert_called_once_with(self.root, accounts=False)
        daily.return_value.once.assert_called_once()
        self.assertTrue(daily.return_value.once.call_args.kwargs['scheduled'])
        result = json.loads(self.stdout.getvalue())
        self.assertEqual(result['state'], 'partial')
        self.assertFalse(result['account_attempted'])

    def test_check_failure_records_type_without_secret_text(self):
        secret = 'synthetic-token-and-account-never-log'
        with patch.object(sys, 'argv', ['scheduled_market.py', '--check']), \
                patch.object(scheduled, 'plan_for', side_effect=ValueError(secret)), \
                patch.object(scheduled, 'DailyLaunch') as daily, redirect_stdout(self.stdout):
            self.assertEqual(scheduled.main(), 1)
        daily.assert_not_called()
        result = json.loads(self.stdout.getvalue())
        self.assertEqual(result['error'], 'scheduled_job_failed')
        self.assertEqual(result['error_type'], 'ValueError')
        self.assertTrue(result['error_frames'])
        for path in (self.root/'runtime/morning-schedule').glob('*.json'):
            self.assertNotIn(secret, path.read_text())
        self.assertNotIn(secret, self.stdout.getvalue())

    def test_journal_exit_interrupt_and_secret_exception_remain_distinct(self):
        instant = '2026-09-30T00:00:00+00:00'
        secret = 'synthetic-credential-never-log'
        with patch('launch.journal.timestamp', return_value=instant):
            for code, state in ((0, 'succeeded'), (2, 'partial'), (1, 'failed')):
                with RunJournal(self.root, 'fixture', interval=60) as journal:
                    journal.complete(code)
                result = json.loads(journal.path.read_text())
                self.assertEqual(result['state'], state)
                self.assertEqual(result['exit_code'], code)
                self.assertEqual(result['finished_at'], instant)
                self.assertFalse(journal.thread.is_alive())
            for exception, state in ((KeyboardInterrupt(secret), 'interrupted'),
                                     (RuntimeError(secret), 'failed'), (SystemExit(2), 'partial')):
                with self.assertRaises(type(exception)):
                    with RunJournal(self.root, 'fixture', interval=60) as journal:
                        raise exception
                result = json.loads(journal.path.read_text())
                self.assertEqual(result['state'], state)
                self.assertEqual(result['finished_at'], instant)
                self.assertNotIn(secret, journal.path.read_text())
                self.assertFalse(journal.thread.is_alive())

if __name__ == '__main__':
    unittest.main()
