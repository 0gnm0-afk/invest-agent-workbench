"""06:40 scheduling, durable receipts, and manual/scheduled collision tests."""
import json
import sys
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import test_daily_launch as fixtures
from daily_launch import DailyLaunch, plan_for
from scheduled_market import run_scheduled, exit_code

NOW = fixtures.NOW


class Scheduled(unittest.TestCase):
    def setUp(self):
        self.fx = fixtures.Launch()
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

    def test_tampered_screen_not_reused_as_success(self):
        result=self.gate.once(NOW)
        (Path(result['run'])/'priority-review.json').write_text('{}')
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


if __name__ == '__main__':
    unittest.main()
