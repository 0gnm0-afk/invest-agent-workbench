import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import priority_review as p
import daily_market as daily


def fixture(count=20):
    rows=[]
    for m in ('KR','US'):
        for i in range(count):
            rows.append({'market':m,'symbol':f'S{i:02}','name':'sample','as_of':'2026-09-24','state':'candidate',
                'sperandeo':{'windows':{'126':{'stage':'S2','bars_in_stage':i,'stage_entered_at':'2026-09-24'}}},
                'taver':{'primary_ma_period':240 if m=='KR' else 200,'primary_ma_state':'below_ma',
                         'lines':{'200':{'distance_to_ma_atr':-.5}}},'sector_context':{},'currency':'USD'})
    return {'rows':rows,'candidate_count':count*2,'session_dates':{'KR':'2026-09-24','US':'2026-09-24'}}, {'series':[]}


class ReviewTest(unittest.TestCase):
    def test_cap_invariance_missing_weak_sector_not_exclusion(self):
        screen,snap=fixture(); before=copy.deepcopy(screen)
        result=p.build(screen,snap)
        self.assertEqual(screen,before)
        for group in result['markets'].values():
            self.assertEqual(group['selected_count'],10)
            self.assertEqual(len(group['entries']),20)
            self.assertEqual(group['selected_symbols'],[f'S{i:02}' for i in range(10)])
            self.assertTrue(all(e['selection_reason'] for e in group['entries']))
            self.assertIn('전체 후보 유지',group['entries'][-1]['selection_reason'])

    def test_custom_cap_zero_and_stable_ties(self):
        screen,snap=fixture(3)
        for r in screen['rows']: r['sperandeo']['windows']['126']['bars_in_stage']=0
        for cap in (0,1,15):
            result=p.build(screen,snap,{'limit_per_market':cap})
            self.assertEqual(result['markets']['US']['selected_count'],min(cap,3))
            self.assertEqual([r['symbol'] for r in result['markets']['US']['entries']],['S00','S01','S02'])

    def test_settings_reject_typos_and_invalid_bounds(self):
        for config in ({'limit_per_market':-1},{'limit_per_market':True},{'recent_stage_bars':30},
                       {'sort_fields':['bogus']},{'typo':1},{'near_ma_atr':float('nan')}):
            with self.assertRaises(ValueError): p.settings(config)

    def test_resistance_support_and_recency_separate(self):
        bars=[{'date':f'2026-09-{i:02}','open':10,'high':11,'low':9,'close':10,'volume':10} for i in range(1,22)]
        line={'state':'available','value_latest':10.5,'first_close_break_date':None,
              'first_price_breach_date':None,'latest_close_beyond_line':False,'confirmed_at':'2026-09-05'}
        cfg=p.settings()
        self.assertEqual(p.line_observation(bars,line,'upper',cfg)['state'],'resistance_approach')
        line.update(first_close_break_date='2026-09-19',first_price_breach_date='2026-09-18',latest_close_beyond_line=True)
        self.assertEqual(p.line_observation(bars,line,'upper',cfg)['state'],'resistance_recent_break')
        self.assertEqual(p.line_observation(bars,line,'lower',cfg)['state'],'support_recent_loss')
        line['first_close_break_date']='2026-09-10'
        self.assertEqual(p.line_observation(bars,line,'upper',cfg)['state'],'resistance_old_break')
        line['latest_close_beyond_line']=False
        self.assertEqual(p.line_observation(bars,line,'upper',cfg)['state'],'resistance_retraced')
        self.assertEqual(p.line_observation(bars,line,'lower',cfg)['state'],'support_reclaimed')
        self.assertEqual(p.line_observation(bars,{'state':'no_valid_anchors'},'upper',cfg)['reason'],'no_valid_anchors')

    def test_overlay_starts_after_confirmation_and_stops_at_breach(self):
        bars=[{'date':str(i)} for i in range(10)]
        line={'state':'available','anchor_a_date':'1','anchor_a_price':20,'anchor_b_date':'4','anchor_b_price':17,
              'confirmed_at':'6','slope_per_bar':-1,'first_price_breach_date':'8'}
        result={'primary_window':126,'windows':{'126':{'upper_trendline':line,'lower_trendline':{'state':'no_valid_anchors'}}}}
        ax=Mock(); p.confirmed_overlay(ax,bars,0,result)
        self.assertEqual(ax.plot.call_args.args[0],[6,7,8])
        self.assertTrue(all(call.kwargs['facecolors']=='none' for call in ax.scatter.call_args_list))
        line.update(confirmed_at='9',first_price_breach_date=None)
        ax=Mock();p.confirmed_overlay(ax,bars,0,result)
        self.assertEqual(ax.plot.call_args.args[0],[9])
        self.assertEqual(ax.plot.call_args.kwargs['marker'],'D')

    def test_engine_does_not_confirm_future_or_incomplete_pivot(self):
        from invest_agent.trading.sperandeo import pivots
        bars=[{'date':str(i),'high':v,'low':v-2} for i,v in enumerate([8,9,12,9,8,9,11,9])]
        self.assertNotIn(6,pivots(bars,'high'))
        extended=bars+[{'date':'8','high':8,'low':6,'complete':False}]
        result=p.trendlines.analyze(extended)
        self.assertEqual(result['windows']['63']['available_bars'],8)

    def test_chart_failure_preserves_membership_and_other_chart(self):
        screen,snap=fixture(1); before=copy.deepcopy(screen)
        review=p.build(screen,snap)
        bars=[{'date':'2026-09-24','open':10,'high':11,'low':9,'close':10,'volume':100}]
        lines={'primary_window':126,'windows':{'126':{'upper_trendline':{'state':'no_valid_anchors'},'lower_trendline':{'state':'no_valid_anchors'}}}}
        for group in review['markets'].values(): group['entries'][0]['trendlines']=lines
        count=[0]
        def draw(bars,path,*args,**kwargs):
            count[0]+=1
            if count[0]==1: raise RuntimeError('injected chart error')
            path.write_bytes(b'fake PNG for isolation test')
        with tempfile.TemporaryDirectory() as tmp, patch.object(p,'candidate_bars',return_value=bars):
            result=p.charts(review,screen,snap,Path(tmp),draw=draw)
            self.assertEqual((result['requested'],result['available'],result['failed']),(2,1,1))
            self.assertEqual(screen,before)
            self.assertIn('injected chart error',result['rows'][0]['reason'])
            self.assertTrue((Path(tmp)/result['rows'][1]['path']).is_file())

    def test_saved_nh_summary_keeps_unrecovered_without_live_schema_key(self):
        self.assertEqual(daily.saved_fallback_summary({'attempted':9,'adopted':7,'observed_not_adopted':2}),
                         {'attempted':9,'adopted':7,'unrecovered':2})

    def test_report_survives_missing_chart_result(self):
        from test_daily import ContextTest
        base,observation,groups=ContextTest().fixture()
        base.update(session_dates={'KR':'2026-09-23','US':'2026-09-24'},unavailable_count=0)
        screen=daily.enrich(base,observation,groups)
        run={'state':'failed','processing_state':'failed','mode':'replay','started_at':'now','stages':{},
             'quality':{'received':3,'requested':3,'failed':0,'quality_issue_series':0,'state':'partial',
                        'processing_state':'failed','data_complete':False},
             'data_times':{'fetched_at':'now','collected_until':'now'}}
        with tempfile.TemporaryDirectory() as tmp:
            output=Path(tmp)
            p.write(output/'priority-review.json',p.build(screen,{'series':[]}))
            daily.render(run,output,screen,observation)
            page=(output/'report.html').read_text(encoding='utf-8')
            self.assertIn('차트 실패',page)
            self.assertIn('후보와 섹터 맥락',page)

    def test_daily_chart_initialization_failure_keeps_report_and_candidates(self):
        from test_daily import ContextTest
        from contextlib import ExitStack
        base,observation,groups=ContextTest().fixture()
        base.update(session_dates={'KR':'2026-09-23','US':'2026-09-24'},unavailable_count=0)
        observation['coverage']={'KR':{'unmatched':0},'US':{'unmatched':0}}
        enriched=daily.enrich(base,observation,groups)
        review=p.build(enriched,{'series':[]})
        quality={'state':'complete','processing_state':'completed','data_complete':True,
                 'quality_issue_series':0,'unexpected_failures':0}
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            root=Path(tmp); source=root/'source';source.mkdir()
            p.write(source/'snapshot.json',{'collection_stats':{'requested':3,'received':3,'failed':0},'universe':[],'series':[],'errors':[],'sessions':{}})
            p.write(source/'manifest.json',{})
            replacements=[(daily,'ROOT',root),(daily,'code_hashes',lambda _:{}),
                          (daily,'seed_check',lambda _:({'roots':[str(source),'a','b']},{})),
                          (daily,'source_check',lambda _:None),(daily,'audit',lambda _:quality),
                          (daily,'screen',lambda _:base),(daily.sector,'build',lambda *a,**kw:(observation,groups,{})),
                          (daily.sector,'render',lambda _:('sector','sector')),
                          (daily.sector_signals,'build',lambda *a,**kw:{'config':daily.sector_signals.DEFAULTS,'markets':{},'sectors':[]}),
                          (p,'build',lambda *a,**kw:review)]
            for obj,key,value in replacements:stack.enter_context(patch.object(obj,key,value))
            stack.enter_context(patch.object(p,'charts',side_effect=ImportError('injected plotting unavailable')))
            result=daily.run({'engine':tmp},source=source)
            output=Path(result['output'])
            self.assertEqual(result['state'],'partial')
            self.assertEqual(result['chart_counts'],{'requested':3,'available':0,'failed':3})
            self.assertEqual(daily.sector.load(output/'screen-base.json'),base)
            self.assertTrue((output/'report.html').is_file())
            self.assertIn('injected plotting unavailable',(output/'report.html').read_text(encoding='utf-8'))

    def test_html_links_work_without_untrusted_html(self):
        rendered=daily.inline_links('[A <script>](charts/US-abc.html) [bad](javascript:alert(1))')
        self.assertIn('href="charts/US-abc.html"',rendered)
        self.assertNotIn('<script>',rendered)
        self.assertNotIn('href="javascript:',rendered)

if __name__=='__main__': unittest.main()
