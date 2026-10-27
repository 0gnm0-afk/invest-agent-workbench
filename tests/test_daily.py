import copy
import sys
import unittest
import tempfile
import json
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from screening.context import enrich
from sectors.observation import align_groups
import market.daily as daily


class ContextTest(unittest.TestCase):
    def fixture(self):
        base = {'rows':[{'market':'KR','symbol':s,'state':'candidate','candidate_reasons':['S1'],
                         'sperandeo':{'candidate':True},'taver':{'state':'below'}} for s in ('A','B','C')],
                'candidate_count':3}
        groups = [{'market':'KR','symbol':s,'group_code':s,'group_name':s,'group_taxonomy':'KR_WICS_SECTOR',
                   'group_state':'matched','reason':None,'evidence':[]} for s in ('A','B')]
        report = {'applied_at':'2026-09-25T00:00:00+00:00','markets':{'KR':{'completed_session':'2026-09-23'}},
                  'observations':[{'market':'KR','group_code':s,'horizons':{'63':{'relative_strength':v,'used':10},
                       '126':{'relative_strength':None,'used':0}},'trend':{},'breadth_sma60':{},'activity':{}}
                       for s,v in [('A',-.1),('B',.2)]]}
        return base,report,groups

    def test_membership_structure_location_and_order_unchanged(self):
        base, report, groups = self.fixture()
        original = copy.deepcopy(base)
        out = enrich(base, report, groups)
        self.assertEqual(base, original)
        self.assertEqual([{k:v for k,v in r.items() if k!='sector_context'} for r in out['rows']],base['rows'])
        self.assertEqual(out['candidate_count'],3)
        self.assertEqual([r['symbol'] for r in out['research_order']['KR']],['B','A','C'])
        self.assertEqual(out['rows'][2]['sector_context']['state'],'unavailable')

    def test_null_rs_and_tie_preserve_original_order(self):
        base, report, groups = self.fixture()
        for r in report['observations']:
            r['horizons']['63']['relative_strength'] = None
        out = enrich(base,report,groups)
        self.assertEqual([r['symbol'] for r in out['research_order']['KR']],['A','B','C'])
        for r in report['observations']:
            r['horizons']['63']['relative_strength'] = -.2
        self.assertEqual([r['symbol'] for r in enrich(base,report,groups)['research_order']['KR']],['A','B','C'])

    def test_new_and_changed_identity_are_not_guessed(self):
        old = [{'market':'US','symbol':s,'provider_symbol':s,'exchange':'NYSE','isin_code':'old',
                'group_state':'matched','group_code':'Technology'} for s in ('A','B','REMOVED')]
        universe = {('NYSE',s):{'market':'US','symbol':s,'provider_symbol':s,'listing_exchange_family':'NYSE',
                     'isin_code':'changed' if s=='B' else 'old'} for s in ('A','B','NEW')}
        rows, changes = align_groups(old, universe, '2026-09-25T00:00:00+00:00')
        self.assertEqual(rows[0],old[0])
        self.assertEqual([r['group_state'] for r in rows],['matched','unmatched','unmatched'])
        self.assertIsNone(rows[1]['group_code'])
        self.assertEqual(changes['absent_from_current_listing'],[['NYSE','REMOVED']])
        self.assertEqual(changes['added'],[['NYSE','NEW']])


class FailureTest(unittest.TestCase):
    def test_invalid_seed_is_failed_with_saved_report_and_no_collection(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(daily,'ROOT',Path(tmp)), \
                patch.object(daily,'code_hashes',return_value={}), \
                patch.object(daily,'seed_check',side_effect=ValueError('input_hash_mismatch')), \
                patch.object(daily,'collect') as collector:
            result = daily.run({'engine':tmp})
            self.assertEqual(result['state'],'failed')
            self.assertEqual(result['stages']['inputs'],'failed')
            self.assertEqual(result['stages']['collection'],'not_started')
            collector.assert_not_called()
            output = Path(result['output'])
            self.assertTrue((output/'report.html').exists())
            manifest = json.loads((output/'manifest.json').read_text())
            self.assertIn('run.json',manifest['output_hashes'])

    def test_review_rejects_changed_screen_code_at_both_old_and_new_paths(self):
        for relative in ('scripts/stock_screen.py', 'scripts/screening/engine.py'):
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); source = root/relative; source.parent.mkdir(parents=True)
                source.write_text('changed code')
                saved = root/'runtime/saved'; saved.mkdir(parents=True)
                daily.write_json(saved/'manifest.json', {'input_hashes':{}, 'output_hashes':{},
                    'code_hashes':{str(source):'original-code-hash'}})
                with patch.object(daily,'ROOT',root), patch.object(daily,'code_hashes',return_value={}), \
                        patch.object(daily,'seed_check',return_value=({},{})), patch.object(daily,'collect') as collector:
                    result = daily.run({'engine':str(root/'engine')}, saved_run=saved)
                self.assertEqual(result['state'],'failed')
                self.assertIn('input_hash_mismatch:'+str(source.resolve()), result['error'])
                collector.assert_not_called()

    def test_input_tamper_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp)/'input.json'
            file.write_text('{}')
            with self.assertRaisesRegex(ValueError,'input_hash_mismatch'):
                daily.check_hashes({str(file):'wrong'})

    def test_report_renders_null_and_escapes_names(self):
        base, report, groups = ContextTest().fixture()
        for row in base['rows']:
            row['name'] = '<script>alert(1)</script>'
        base.update(session_dates={'KR':'2026-09-23','US':'2026-09-24'},unavailable_count=0)
        enriched = enrich(base,report,groups)
        run = {'state':'partial','processing_state':'completed_with_exceptions','mode':'replay','started_at':'now',
               'stages':{},'quality':{'received':3,'requested':3,'failed':0,'quality_issue_series':0,
                 'state':'partial','processing_state':'completed_with_exceptions','data_complete':False},
               'data_times':{'fetched_at':'now','collected_until':'now'}}
        with tempfile.TemporaryDirectory() as tmp:
            daily.render(run,Path(tmp),enriched,report)
            page = (Path(tmp)/'report.html').read_text(encoding='utf-8')
            self.assertIn('<table>',page)
            self.assertNotIn('<script>',page)
            self.assertIn('결측',page)


if __name__ == '__main__':
    unittest.main()
