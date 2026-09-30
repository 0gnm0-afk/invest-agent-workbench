import copy,sys,unittest,tempfile,json,csv
from pathlib import Path
from datetime import date,timedelta
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from invest_agent.trading import base_structure as base
from test_momentum_entry import fixture
import priority_review,momentum_review,watch_review

def bars(kind='bottom'):
    if kind=='bottom':prices=[180.]*220+[180.-80*i/59 for i in range(60)]
    else:prices=[60.+110*i/264 for i in range(265)]+[170.-20*i/14 for i in range(15)]
    floor=100 if kind=='bottom' else 148
    prices += [floor+(0,2,5,8,6,3,1,4)[i%8] for i in range(39)]+[floor+4]
    return [dict(date=(date(2025,1,1)+timedelta(days=i)).isoformat(),open=c,close=c,high=c+1,low=c-1,volume=100000.) for i,c in enumerate(prices)]

class BaseTests(unittest.TestCase):
    def test_falling_long_trend_bottom_is_observable(self):
        r=base.analyze(bars());self.assertTrue(r['signals'])
        self.assertEqual(r['signals'][0]['kind'],'bottom_base')
        for s in r['signals']:
            self.assertEqual(s['phase'],'watch');self.assertFalse(s['entry_pass'])
            self.assertIsNone(s['breakout_date']);self.assertIsNone(s['entry_zone'])
            self.assertGreater(s['metrics']['separated_test_pairs'],0)
            self.assertLess(s['known_at'],bars()[-1]['date'])
            self.assertAlmostEqual(s['range_reward_risk'],(s['pivot']-104)/(104-s['structure_stop']))

    def test_rising_then_pullback_is_separate_pattern(self):
        r=base.analyze(bars('pullback'));self.assertTrue(r['signals'])
        self.assertTrue(any(s['kind']=='pullback_base' for s in r['signals']))

    def test_recent_pullback_not_hidden_by_older_higher_peak(self):
        b=bars('pullback');b[160].update(open=400,close=400,high=401,low=399)
        self.assertTrue(any(s['kind']=='pullback_base' for s in base.analyze(b)['signals']))

    def test_broken_floor_and_chase_rejected(self):
        for price in (90,130):
            b=bars();b[-1].update(open=price,close=price,high=price+1,low=price-1)
            self.assertFalse(base.analyze(b)['signals'])

    def test_no_preceding_move_or_continued_decline(self):
        b=bars()
        for x in b[:-40]:x.update(open=104,close=104,high=105,low=103)
        self.assertFalse(base.analyze(b)['signals'])
        b=bars()
        for i,x in enumerate(b[-100:]):
            c=180-i;x.update(open=c,close=c,high=c+1,low=c-1)
        self.assertFalse(base.analyze(b)['signals'])

    def test_future_incomplete_and_current_extremes_do_not_set_floor(self):
        b=bars();r=base.analyze(b)
        f=copy.deepcopy(b[-1]);f.update(date='2099-01-01',low=1,high=1000)
        self.assertEqual(r,base.analyze(b+[f],as_of=b[-1]['date']))
        f['complete']=False;self.assertEqual(r,base.analyze(b+[f]))
        b[-1]['high']=300
        self.assertEqual([s['pivot'] for s in r['signals']],[s['pivot'] for s in base.analyze(b)['signals']])

    def test_slow_directional_slide_is_not_a_flat_base(self):
        b=bars()
        for i,x in enumerate(b[-40:]):
            c=110-.35*i+.7*(i%5);x.update(open=c,close=c,high=c+.8,low=c-.8)
        r=base.analyze(b)
        self.assertFalse(r['signals'])
        self.assertTrue(any('not_directional_range' in x['rejections'] for x in r['windows']))

    def test_return_to_floor_during_selloff_is_not_stabilized(self):
        from math import sin,pi
        b=bars()
        for i,x in enumerate(b[-40:]):
            c=100+16*sin(pi*i/39);x.update(open=c,close=c,high=c+1,low=c-1)
        r=base.analyze(b);self.assertFalse(r['signals'])
        self.assertTrue(all('recent_stabilization' in x['rejections'] for x in r['windows']))

    def test_config_and_short_input(self):
        for cfg in ({'max_risk':True},{'max_depth':float('nan')},{'other':1},{'min_pullback':.3}):
            with self.assertRaises(ValueError):base.settings(cfg)
        self.assertFalse(base.analyze(bars()[:100])['signals'])

    def test_routes_counts_preservation_and_report(self):
        screen,snapshot=fixture();b=bars()
        for x in snapshot['series']:x['bars']=copy.deepcopy(b)
        for x in snapshot['benchmarks'].values():x['bars']=copy.deepcopy(b)
        for m in snapshot['sessions']:
            snapshot['sessions'][m]=[x['date'] for x in b];screen['session_dates'][m]=b[-1]['date']
        for row in screen['rows']:row['as_of']=b[-1]['date']
        cfg={'strategy':'pdf_momentum','watchlist':{}}
        old=priority_review.build(screen,snapshot,cfg)
        cfg['watchlist']['base_structure']={'limit_per_market':1}
        r=priority_review.build(screen,snapshot,cfg)
        self.assertEqual(r['candidate_count'],4);self.assertEqual(r['entry_candidate_count'],old['entry_candidate_count'])
        for m,g in r['markets'].items():
            self.assertEqual(g['base_watch']['candidate_count'],2)
            for route in ('pdf_momentum','sperandeo'):self.assertEqual(g[route],old['markets'][m][route])
        with tempfile.TemporaryDirectory() as temp:
            out=Path(temp);momentum_review.write(out/'priority-review.json',r);watch_review.write_outputs(out,r)
            def draw(b,p,e):p.write_bytes(b'fake')
            charts=priority_review.charts(r,screen,snapshot,out,draw)
            self.assertEqual(charts['available'],2)
            momentum_review.write(out/'priority-charts.json',charts);watch_review.render({'state':'failed'},out)
            self.assertIn('하락 후 바닥 횡보',(out/'report.html').read_text(encoding='utf-8'))
            self.assertIn('거리비',(out/'report.html').read_text(encoding='utf-8'))
            with (out/'candidates.csv').open(encoding='utf-8-sig') as f:
                rows=list(csv.DictReader(f));self.assertTrue(all(x['base_pattern']=='bottom_base' for x in rows))

if __name__=='__main__':unittest.main()
