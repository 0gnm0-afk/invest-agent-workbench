import copy
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from price_events import build, sessions, validate_research
import test_analysis_skill_link as base_tests


def weekdays(start='2024-01-01',count=180):
    days=[];day=date.fromisoformat(start)
    while len(days)<count:
        if day.weekday()<5: days.append(day.isoformat())
        day+=timedelta(days=1)
    return days


class PriceEventsTests(unittest.TestCase):
    def setUp(self):
        self.days=weekdays(count=300)
        self.bars=[dict(date=d,open=100,high=102,low=98,close=100,volume=1000) for d in self.days[:90]]
        self.mock=patch('price_events.sessions',return_value=tuple(self.days));self.mock.start();self.addCleanup(self.mock.stop)
    def run_events(self,bars=None,financial=None):
        return build(dict(market='US',symbol='TEST',provider='fixture',price_basis='adjusted',bars=bars if bars is not None else self.bars),financial)
    def test_actual_good_friday_confirmation_is_thursday(self):
        self.mock.stop()
        days=sessions('US','2024-02-01','2024-04-05')
        bars=[dict(date=d,open=100,high=102,low=98,close=100,volume=1000) for d in days]
        next(b for b in bars if b['date']=='2024-03-12')['high']=120
        p=self.run_events(bars)
        e=next(e for e in p['events'] if e['kind']=='high' and e['timeframe']=='1w')
        self.assertEqual(e['confirmed_at'],'2024-03-28')

    def test_exact_gap_threshold_and_full_gap(self):
        self.bars[30].update(open=103,high=105,low=103,close=104,volume=2000)
        up=next(e for e in self.run_events()['events'] if e['event_date']==self.days[30] and e['kind']=='gap')
        self.assertAlmostEqual(up['gap_pct'],3);self.assertTrue(up['full_gap']);self.assertEqual(up['status'],'confirmed')
        self.assertEqual(up['metrics']['volume_change_pct'],100);self.assertEqual(up['metrics']['volume_ratio_20'],2)
        self.assertEqual(up['news_window']['start'],self.days[27]);self.assertEqual(up['news_window']['end'],self.days[32])
    def test_down_gap_and_partial_overlap(self):
        self.bars[30].update(open=97,high=100,low=96,close=98)
        e=next(e for e in self.run_events()['events'] if e['event_date']==self.days[30] and e['kind']=='gap')
        self.assertAlmostEqual(e['gap_pct'],-3);self.assertFalse(e['full_gap'])
    def test_weekly_real_day_and_confirmation(self):
        self.bars[12]['high']=120
        e=next(e for e in self.run_events()['events'] if e['kind']=='high' and e['timeframe']=='1w')
        self.assertEqual(e['event_date'],self.days[12]);self.assertEqual(e['confirmed_at'],self.days[24])
        e=next(e for e in self.run_events(self.bars[:24])['events'] if e['kind']=='high' and e['timeframe']=='1w')
        self.assertEqual(e['status'],'provisional');self.assertIsNone(e['confirmed_at'])
    def test_invalidated_provisional_removed(self):
        self.bars[12]['high']=120;self.bars[23]['high']=130
        self.assertFalse(any(e['event_date']==self.days[12] and e['timeframe']=='1w' for e in self.run_events()['events']))
    def test_monthly_extreme_high_priority(self):
        bars=[dict(date=d,open=100,high=102,low=98,close=100,volume=1000) for d in self.days[:140]]
        bars[52]['high']=130
        p=self.run_events(bars);e=next(e for e in p['events'] if e['kind']=='high' and e['timeframe']=='1mo')
        self.assertEqual(e['event_date'],self.days[52]);self.assertEqual(e['confirmed_at'],'2024-05-31')
        g=next(g for g in p['groups'] if e['id'] in g['event_ids']);self.assertEqual(g['priority'],'high')
    def test_same_day_original_events_preserved(self):
        self.bars[32].update(open=108,high=130,low=105,close=110)
        p=self.run_events();rows=[e for e in p['events'] if e['event_date']==self.days[32]]
        self.assertGreaterEqual(len(rows),2);self.assertEqual(len({e['group_id'] for e in rows}),1)
    def test_bundles_do_not_chain(self):
        for i in [30,32,34]:self.bars[i].update(open=104,high=106,low=98,close=100)
        p=self.run_events();es=[e for e in p['events'] if e['kind']=='gap']
        self.assertEqual(es[0]['group_id'],es[1]['group_id']);self.assertNotEqual(es[1]['group_id'],es[2]['group_id'])
        for g in p['groups']:self.assertLess(self.days.index(g['end'])-self.days.index(g['start']),3)
    def test_null_volume_is_not_zero(self):
        self.bars[30].update(open=104,high=106,volume=None)
        e=next(e for e in self.run_events()['events'] if e['kind']=='gap')
        self.assertIsNone(e['metrics']['volume_change_pct']);self.assertIsNone(e['metrics']['volume_ratio_20'])
    def test_missing_trading_day_does_not_confirm_gap_or_pivot(self):
        self.bars[12]['high']=120;self.bars[30].update(open=104,high=106)
        bars=[b for i,b in enumerate(self.bars) if i not in (22,29)]
        p=self.run_events(bars)
        for e in p['events']:
            if e['event_date'] in (self.days[12],self.days[30]): self.assertEqual(e['status'],'provisional')
    def test_low_small_return_and_medium_volume(self):
        self.bars[12]['high']=103
        p=self.run_events();g=next(g for g in p['groups'] if g['start']==self.days[12]);self.assertEqual(g['priority'],'low')
        self.bars[32]['high']=103;self.bars[32]['volume']=2000
        p=self.run_events();g=next(g for g in p['groups'] if g['start']==self.days[32]);self.assertEqual(g['priority'],'medium')
    def test_disclosure_filing_not_fetch_time(self):
        self.bars[32]['high']=103
        f={'result':{'annual':[{'cell':{'filed':self.days[32],'sources':['official.json']}}]}}
        g=next(g for g in self.run_events(financial=f)['groups'] if g['start']==self.days[32]);self.assertEqual(g['priority'],'medium')
    def test_followup_is_labeled_ex_post_and_strong(self):
        self.bars[30].update(open=104,high=105,low=99,close=100)
        for i in range(31,51):self.bars[i].update(open=120,high=121,low=119,close=120)
        p=self.run_events();e=next(e for e in p['events'] if e['kind']=='gap' and e['event_date']==self.days[30])
        self.assertAlmostEqual(e['metrics']['followup_return_20_pct'],20);self.assertEqual(e['metrics']['metrics_as_of'],self.days[89])
    def test_input_immutable_and_deterministic(self):
        original=copy.deepcopy(self.bars);self.assertEqual(self.run_events(),self.run_events());self.assertEqual(original,self.bars)
    def test_calendar_failure_is_explicit(self):
        self.bars[30].update(open=104,high=106)
        with patch('price_events.sessions',side_effect=ValueError('calendar')):
            p=self.run_events();self.assertEqual(p['calendar_state'],'unavailable_observed_sessions_only');self.assertTrue(all(e['status']=='provisional' for e in p['events']))
    def test_research_validation_and_provisional_guard(self):
        self.bars[30].update(open=104,high=106);p=self.run_events();gid=p['groups'][0]['id']
        with self.assertRaises(ValueError):validate_research([{'group_id':gid,'status':'cause_unconfirmed'}],p)
        self.assertEqual(validate_research([{'group_id':gid,'status':'failed','failure_reason':'network unavailable'}],p)[0]['status'],'failed')
        p['groups'][0]['confirmed_event_ids']=[]
        with self.assertRaises(ValueError):validate_research([{'group_id':gid,'status':'failed','failure_reason':'network'}],p)
    def test_five_year_cutoff(self):
        days=weekdays('2019-01-01',2200);bars=[dict(date=d,open=100,high=102,low=98,close=100,volume=1000) for d in days[:1800]]
        bars[20].update(open=104,high=106);bars[1750].update(open=104,high=106)
        with patch('price_events.sessions',return_value=tuple(days)):
            p=self.run_events(bars);self.assertTrue(all(e['event_date']>=p['start'] for e in p['events']));self.assertFalse(any(e['event_date']==days[20] for e in p['events']))


class ResearchSnapshotTests(base_tests.AnalysisSkillLinkTests):
    def test_frame_independent_events_and_financial_hash(self):
        before=self.service.snapshot(self.cid,self.sid)
        self.service.dispatch('context',{'analysis_key':self.cid,'context':{'timeframe':'1mo','range':'3'}},ORIGIN:='http://127.0.0.1:1234')
        after=self.service.dispatch('read',{'analysis_key':self.cid},ORIGIN)
        self.assertEqual(before['component_hashes']['price_events'],after['component_hashes']['price_events'])
        self.assertEqual(before['component_hashes']['financials'],after['component_hashes']['financials'])
    def test_legacy_snapshot_not_backfilled(self):
        from analysis_service import digest,packed
        snap=self.service.snapshot(self.cid,self.sid);snap.pop('price_events')
        with closing(self.store.connect()) as db,db:db.execute('UPDATE analysis_snapshots SET payload=?,sha256=? WHERE id=?',(packed(snap),digest(snap),self.sid))
        p=self.read(section='events');self.assertEqual(p['price_events']['state'],'not_in_legacy_snapshot');self.assertEqual(p['events'],[])
    def test_event_research_save_and_replay(self):
        self.stock['bars'][3].update(open=120,high=122,low=117,close=120)
        detail=self.create();sid=detail['snapshots'][0]['id'];cid=detail['case']['id']
        p=self.read(section='research_groups',analysis_key=cid,snapshot_id=sid,limit=1)
        gid=p['groups'][0]['id'];record=dict(analysis_key=cid,snapshot_id=sid,kind='report',title='trace',body='fixture',author='llm',event_research=[{'group_id':gid,'status':'failed','failure_reason':'fixture denied'}])
        r=self.service.dispatch('record',record,'http://127.0.0.1:1234')
        self.assertEqual(self.service.item(cid,r['id'])['event_research'],record['event_research'])
        self.assertEqual(self.read(section='research_groups',analysis_key=cid,snapshot_id=sid)['groups'][0]['research_status'],'unresearched')


if __name__=='__main__': unittest.main()
