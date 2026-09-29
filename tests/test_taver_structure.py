import copy
from datetime import date,timedelta
import math
from pathlib import Path
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from invest_agent.trading.taver_structure import analyze,settings,line_metrics
import priority_review
import taver_review
from unittest.mock import patch


def bars(n=800):
    result=[]
    for i in range(n):
        c=100+.15*i+12*math.sin(i*2*math.pi/80)
        result.append({'date':(date(2020,1,1)+timedelta(days=i)).isoformat(),'open':c+.2,'high':c+1.5,'low':c-1.5,'close':c,'volume':1000000})
    return result


class StructureTests(unittest.TestCase):
    def test_asof_filters_future_and_unfinished(self):
        b=bars(); cutoff=b[650]['date']
        self.assertEqual(analyze(b,as_of=cutoff),analyze(b[:651]))
        future=copy.deepcopy(b); future[651]['high']=100000
        self.assertEqual(analyze(future,as_of=cutoff),analyze(b[:651]))
        b[-1]['complete']=False
        self.assertEqual(analyze(b),analyze(b[:-1]))

    def test_eme_without_recovery_requirement(self):
        b=bars(781);r=analyze(b)
        self.assertTrue(r['primary']['channel']['shape_valid'])
        # Latest trough is near the fitted lower boundary, no S stage supplied.
        self.assertTrue(r['candidate']); self.assertIn('EME',r['patterns']); self.assertIn('CEG',r['patterns'])
        self.assertTrue(all(a['confirmed_at']<=r['as_of'] for a in r['primary']['support'].get('anchors',[])))

    def test_short_history_and_flat_not_candidates(self):
        self.assertEqual(analyze(bars(100))['state'],'insufficient_history')
        b=bars(800)
        for row in b: row.update(open=100,close=100,low=99,high=101)
        self.assertFalse(analyze(b)['candidate'])

    def test_primary_not_chosen_by_nearness(self):
        r=analyze(bars(800)); self.assertEqual(r['primary_window'],504)
        self.assertEqual(r['primary']['channel']['excluded_bars'],0)
        self.assertIn('1260',r['windows']);self.assertEqual(r['windows']['1260']['state'],'insufficient_history')

    def test_below_reclaimed_deep_and_distance(self):
        b=bars(30)
        for row in b:row.update(close=101,low=100,high=102,open=101)
        b[-2].update(close=99,low=98,open=99)
        m=line_metrics(b,100,0,2,settings());self.assertEqual(m['position'],'reclaimed')
        b[-1].update(close=95,low=94,open=95)
        m=line_metrics(b,100,0,2,settings());self.assertEqual(m['position'],'below');self.assertFalse(m['near'])
        b[-1].update(close=90,low=89,open=90)
        self.assertGreater(line_metrics(b,100,0,2,settings())['recent_max_breach_atr'],3)

    def test_nonpositive_projected_lower_is_explicit(self):
        b=bars(781);r=analyze(b)
        r['primary']['channel']['distance_pct']=None
        reasons=taver_review.describe(r)
        self.assertTrue(any('하단 가격 비양수' in reason for reason in reasons))

    def test_disabled_skips_scan_and_preserves_sperandeo(self):
        from test_priority_review import fixture
        screen,snap=fixture(2); before=copy.deepcopy(screen)
        with patch.object(taver_review.taver_structure,'analyze',side_effect=AssertionError('disabled scan called')) as scan:
            review=priority_review.build(screen,snap,{'taver_enabled':False})
        scan.assert_not_called()
        self.assertEqual(screen,before)
        self.assertEqual(review['taver_state'],'disabled')
        self.assertEqual(review['taver_observations'],[])
        for group in review['markets'].values():
            self.assertEqual(group['taver']['entries'],[])
            self.assertEqual(group['candidate_count'],2)
            self.assertTrue(all(e['origins']==['sperandeo'] for e in group['entries']))
        rendered='\n'.join(priority_review.report_lines(review,{'requested':4,'available':0,'failed':4,'rows':[]}))
        self.assertNotIn('taver-scan.html',rendered)
        self.assertNotIn('테이버 최대',rendered)
        self.assertIn('PDF 기반 새 진입 조건은 아직 적용 전',rendered)
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            taver_review.write_scan(Path(tmp),review)
            self.assertFalse((Path(tmp)/'taver-scan.html').exists())

    def test_disabled_charts_skip_structure_rendering(self):
        from test_priority_review import fixture
        import tempfile
        screen,snap=fixture(1)
        review=priority_review.build(screen,snap,{'taver_enabled':False})
        b=bars(40)
        lines={'primary_window':126,'windows':{}}
        for group in review['markets'].values(): group['entries'][0]['trendlines']=lines
        def draw(b,path,*args,**kwargs): path.write_bytes(b'fixture')
        with tempfile.TemporaryDirectory() as tmp, patch.object(priority_review,'candidate_bars',return_value=b), patch.object(taver_review,'draw_structure',side_effect=AssertionError('disabled chart called')) as structure:
            result=priority_review.charts(review,screen,snap,Path(tmp),draw=draw)
            self.assertEqual(result['available'],2)
            structure.assert_not_called()
            for row in result['rows']:
                self.assertNotIn('structure_image',row)
                self.assertNotIn('테이버',(Path(tmp)/row['path']).read_text(encoding='utf-8'))

    def test_disabled_flag_is_strict_and_old_config_stays_unchanged(self):
        self.assertNotIn('taver_enabled',priority_review.settings())
        for value in (0,1,'false',None):
            with self.assertRaises(ValueError): priority_review.settings({'taver_enabled':value})

    def test_settings(self):
        for cfg in ({'windows':[252,252]},{'near_atr':True},{'near_atr':float('nan')},{'min_r2':2},{'short_window':1}):
            with self.assertRaises(ValueError): settings(cfg)

    def test_independent_union_and_caps_no_screen_mutation(self):
        b=bars(781);last=b[-1]['date']
        rows=[{'market':'US','symbol':s,'name':s,'currency':'USD','as_of':last,
               'state':'candidate' if s=='BOTH' else 'not_selected','universe':{'universe_pass':s!='REJECT'},
               'sperandeo':{'windows':{}},'taver':{}} for s in ['BOTH','ONLY','REJECT']]
        screen={'rows':rows,'session_dates':{'US':last,'KR':last}};before=copy.deepcopy(screen)
        snap={'series':[{'market':'US','symbol':r['symbol'],'bars':b} for r in rows],
              'sessions':{'US':[x['date'] for x in b],'KR':[x['date'] for x in b]}}
        review=priority_review.build(screen,snap,{'limit_per_market':1,'taver_limit_per_market':2})
        g=review['markets']['US'];self.assertEqual(g['taver']['candidate_count'],2)
        self.assertEqual(g['selected_count'],2); self.assertEqual(g['overlap_symbols'],['BOTH'])
        self.assertEqual(screen,before)
        self.assertEqual(priority_review.build(screen,snap)['markets']['US']['selected_count'],2)
        self.assertEqual(next(e for e in g['entries'] if e['symbol']=='ONLY')['origins'],['taver'])
        self.assertEqual(next(e for e in g['entries'] if e['symbol']=='BOTH')['selected_by'],['sperandeo','taver'])
        zero=priority_review.build(screen,snap,{'limit_per_market':0,'taver_limit_per_market':0})
        self.assertEqual(zero['markets']['US']['selected_count'],0)

if __name__=='__main__':unittest.main()
