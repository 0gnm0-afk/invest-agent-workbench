import copy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from trailing_warnings import TrailingStore, indicators

class Warnings(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
  self.path=Path(self.temp.name)/'state.sqlite3';self.store=TrailingStore(self.path)
  self.now=datetime(2026,10,6,0,tzinfo=timezone.utc)
  self.bundle={'as_of':self.now.isoformat(),'query_state':'received','accounts':[{'alias':'a','holdings_complete':True,'positions':[{'market':'US','symbol':'S','currency':'USD','quantity':'2','average_cost':'90'}]}]}
  self.bars=[{'date':(datetime(2026,9,1)+timedelta(days=i)).date().isoformat(),'high':102,'low':98,'close':100} for i in range(15)]
 def run_update(self):
  return self.store.update(self.bundle,{'US:S':{'currency':'USD','price_basis':'adjusted','bars':self.bars}}, {'US':self.bars[-1]['date']}, self.now, sessions={'US':[b['date'] for b in self.bars]})
 def add(self,close,high=None,low=None):
  self.bars.append(dict(date=(datetime.fromisoformat(self.bars[-1]['date'])+timedelta(days=1)).date().isoformat(),close=close,high=high or close+1,low=low or close-1))
 def test_formula_intraday_and_persistence(self):
  self.bars[-1].update(high=110,close=108)
  result=self.run_update();p=result['positions'][0]
  self.assertEqual(p['high'],110);self.assertAlmostEqual(p['line'],max(110-2.5*indicators(self.bars)[-1]['atr'],104.5))
  self.assertEqual(TrailingStore(self.path).read(),result)
 def test_atr_price_units_and_wilder(self):
  rows=indicators(self.bars);self.assertIsNone(rows[13]['atr']);self.assertEqual(rows[14]['atr'],4)
  self.add(105,110,100);self.assertAlmostEqual(indicators(self.bars)[-1]['atr'],(4*13+10)/14)
 def test_ratchet_cost_floor_and_loss_keeps_monitoring(self):
  self.run_update();self.add(95);r=self.run_update();p=r['positions'][0];line=p['line']
  self.bundle['accounts'][0]['positions'][0]['average_cost']='99';self.add(89)
  p=self.run_update()['positions'][0];self.assertTrue(p['active']);self.assertGreaterEqual(p['line'],max(99,line))
 def test_trailing_only_duplicate_recovery_recurrence(self):
  self.run_update();self.add(96)
  r=self.run_update();self.assertEqual([e['kind'] for e in r['events']],['trailing'])
  self.assertEqual(self.run_update()['events'],r['events'])
  self.add(101);self.run_update();self.add(95);r=self.run_update()
  self.assertEqual(len([e for e in r['events'] if e['kind']=='trailing']),2)
 def test_ma5_only_and_same_day_mean(self):
  self.run_update();self.add(104);self.run_update();self.add(100.9)
  r=self.run_update();self.assertEqual([e['kind'] for e in r['events']],['ma5'])
  self.assertAlmostEqual(r['events'][0]['threshold'],(100*3+104+100.9)/5)
 def test_simultaneous_independent(self):
  self.run_update();self.add(104);self.run_update();self.add(95)
  self.assertEqual({e['kind'] for e in self.run_update()['events']},{'trailing','ma5'})
 def test_equality_touch(self):
  self.run_update();self.add(104);self.run_update();self.add(101)
  self.assertIn('ma5',[e['kind'] for e in self.run_update()['events']])
 def test_waiting_profit_no_historical_high(self):
  self.bundle['accounts'][0]['positions'][0]['average_cost']='110'
  self.bars[0]['high']=200
  self.assertFalse(self.run_update()['positions'][0]['active'])
  self.add(112,114);p=self.run_update()['positions'][0];self.assertEqual(p['high'],114)
 def test_stale_and_missing_do_not_emit_or_reset(self):
  self.run_update();self.add(80);self.bundle['as_of']=(self.now-timedelta(days=2)).isoformat()
  r=self.run_update();self.assertFalse(r['events']);self.assertTrue(r['positions'][0]['active']);self.assertIsNotNone(r['positions'][0]['issue'])
  self.bundle['as_of']=self.now.isoformat();self.bars[-1]['close']=None
  self.assertFalse(self.run_update()['events'])
 def test_correction_blocked_and_state_preserved(self):
  old=self.run_update()['positions'][0];self.bars[-1]['high']=103;self.add(80)
  p=self.run_update()['positions'][0];self.assertEqual(p['issue'],'processed_price_history_changed');self.assertEqual(p['line'],old['line'])
 def test_full_exit_and_reentry(self):
  self.run_update();self.bundle['accounts'][0]['positions'][0]['quantity']='0'
  self.assertFalse(self.run_update()['positions'][0]['active'])
  self.bundle['accounts'][0]['positions'][0]['quantity']='3';self.add(100)
  self.assertEqual(self.run_update()['positions'][0]['generation'],2)
 def test_incomplete_account_does_not_close(self):
  self.run_update();self.bundle['accounts'][0].update(holdings_complete=False,positions=[])
  self.assertTrue(self.run_update()['positions'][0]['active'])
 def test_removed_holding_closes(self):
  self.run_update();self.bundle['accounts'][0]['positions']=[]
  self.assertEqual(self.run_update()['positions'][0]['status'],'closed')
 def test_no_old_alert_replay(self):
  self.run_update();self.add(80);self.add(100)
  self.assertFalse(self.run_update()['events'])
 def test_missed_breach_still_below_alerts_latest_once(self):
  self.run_update();self.add(80);self.add(81)
  r=self.run_update();self.assertEqual(len(r['events']),1);self.assertEqual(r['events'][0]['date'],self.bars[-1]['date'])
  self.assertEqual(self.run_update()['events'],r['events'])
 def test_account_isolation(self):
  second=copy.deepcopy(self.bundle['accounts'][0]);second['alias']='b';self.bundle['accounts'].append(second)
  self.run_update();self.add(80);r=self.run_update();self.assertEqual(len(r['events']),2)
 def test_transaction_duplicate_concurrency(self):
  from concurrent.futures import ThreadPoolExecutor
  self.run_update();self.add(80)
  with ThreadPoolExecutor(2) as pool:list(pool.map(lambda _:self.run_update(),range(2)))
  self.assertEqual(len(self.store.read()['events']),1)

 def test_same_day_cost_floor_waits_for_new_close(self):
  self.run_update();self.bundle['accounts'][0]['positions'][0]['average_cost']='102'
  r=self.run_update();self.assertEqual(r['positions'][0]['line'],102);self.assertTrue(r['positions'][0]['cost_pending']);self.assertFalse(r['events'])
  self.add(101);self.assertEqual(self.run_update()['events'][0]['kind'],'trailing')
 def test_public_stale_projection_does_not_mutate(self):
  before=self.run_update();r=self.store.public(self.now+timedelta(days=2),{'US':self.bars[-1]['date']})
  self.assertEqual(r['positions'][0]['issue'],'account_stale_or_incomplete');self.assertEqual(self.store.read(),before)
 def test_calendar_gap(self):
  sessions=[b['date'] for b in self.bars];self.bars.pop(5)
  r=self.store.update(self.bundle,{'US:S':{'currency':'USD','bars':self.bars}}, {'US':self.bars[-1]['date']},self.now,sessions={'US':sessions})
  self.assertEqual(r['positions'][0]['issue'],'price_sessions_incomplete');self.assertFalse(r['events'])
 def test_regressed_account_cannot_close(self):
  self.run_update();self.bundle['accounts'][0]['positions']=[];self.bundle['as_of']=(self.now-timedelta(hours=1)).isoformat()
  self.assertTrue(self.run_update()['positions'][0]['active'])
 def test_trailing_equality(self):
  p=self.run_update()['positions'][0];self.add(p['line'],high=100,low=95)
  self.assertIn('trailing',[e['kind'] for e in self.run_update()['events']])

if __name__=='__main__':unittest.main()
