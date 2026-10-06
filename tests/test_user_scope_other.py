import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import market_scope
import user_classification
from chart_history import HistoryStore
from sector_focus import baskets


class UserScopeOther(unittest.TestCase):
    def setUp(self):
        self.row=dict(market='US',symbol='NEW',isin_code='USNEW',exchange='NASDAQ',
                      group_state='unmatched',reason='security_type_conflict',
                      evidence=[{'source':'original'}])
        self.policy=dict(schema=1,approved_on='2026-10-04',approval='user',records=[
            {**{k:self.row[k] for k in ('market','symbol','isin_code','exchange')},'previous_reason':self.row['reason']}])

    def test_other_is_explicit_and_original_conflict_preserved_without_mutation(self):
        before=copy.deepcopy(self.row)
        value=user_classification.apply([self.row],{'user_classification':self.policy})[0]
        self.assertEqual(value['group_name'],'기타')
        self.assertEqual(value['group_taxonomy'],'US_USER_OTHER')
        self.assertEqual(value['classification_freshness']['state'],'user_defined')
        self.assertEqual(value['user_classification_previous'],before)
        self.assertEqual(self.row,before)
        self.assertEqual(baskets([value],[{'market':'US','group_code':'USER_OTHER_US','group_name':'기타'}])[0]['members'],['NEW'])

    def test_unchosen_or_new_identity_not_automatically_other(self):
        for changes in ({'symbol':'OTHER'},{'isin_code':'REUSED'},{'exchange':'NYSE'},{'market':'KR'}):
            row={**self.row,**changes}
            self.assertEqual(user_classification.apply([row],{'user_classification':self.policy}),[row])
        self.assertEqual(user_classification.apply([self.row],{}),[self.row])

    def test_duplicate_missing_identity_and_missing_approval_fail(self):
        for p in ({**self.policy,'records':self.policy['records']*2},
                  {**self.policy,'records':[{**self.policy['records'][0],'isin_code':None}]},
                  {**self.policy,'approval':''}):
            with self.assertRaises(ValueError):user_classification.apply([self.row],{'user_classification':p})

    def test_explicit_exclusions_not_inherited_by_new_known_isin(self):
        record=dict(market='US',symbol='NEW',isin_code='USNEW',identity_bound=True,reason='user',approved_on='2026-10-04')
        p={'records':[record],'lifecycle_records':[]}
        self.assertIsNotNone(market_scope.match(self.row,p))
        self.assertIsNotNone(market_scope.match({'market':'US','symbol':'NEW'},p))
        self.assertIsNone(market_scope.match({**self.row,'isin_code':'NEWISSUER'},p))

    def test_all_37_block_chart_retry_before_client_creation(self):
        policy=market_scope.load()
        records=[r for r in policy['records'] if r.get('identity_bound')]
        self.assertEqual(len(records),37)
        with tempfile.TemporaryDirectory() as tmp, patch.object(HistoryStore,'view',return_value={'bars':[]}):
            store=HistoryStore(Path(tmp)/'cache.sqlite',lambda:self.fail('network client created'))
            for row in records:
                with self.subTest(symbol=row['symbol']):
                    self.assertEqual(store.ensure(row,retry=True)['history']['state'],'excluded')

    def test_excluded_history_is_not_served_from_old_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=HistoryStore(Path(tmp)/'history.sqlite')
            row=dict(market='US',symbol='BKYI',bars=[{'date':'2026-10-02','close':1}])
            with patch.object(store.saved,'packet',side_effect=AssertionError('old cache read')):
                value=store.view(row)
            self.assertEqual(value['bars'],[])
            self.assertEqual(value['history']['state'],'excluded')

    def test_future_daily_collection_skips_all_37_and_freezes_other_policy(self):
        from test_hybrid_market import TossMarketPipelineTests
        fixture=TossMarketPipelineTests();fixture.setUp()
        try:
            root=Path(__file__).resolve().parents[1]
            user=json.loads((root/'config.json').read_text(encoding='utf-8'))['user_classification']
            excluded=[r for r in market_scope.load()['records'] if r.get('identity_bound')]
            fixture.config['universe'] += excluded
            fixture.config['user_classification']=user
            result=fixture.run_collect()
            requested={c.args[1] for c in fixture.candles.call_args_list}
            self.assertFalse(requested & {r['symbol'] for r in excluded})
            self.assertEqual(len(result['universe']),2)
            self.assertEqual(len(result['scope_exclusions']),37)
            self.assertEqual(result['user_classification'],user)
        finally:fixture.doCleanups()

    def test_52_fixed_other_decisions_and_4_exclusion_overlaps(self):
        root=Path(__file__).resolve().parents[1]
        policy=json.loads((root/'config.json').read_text(encoding='utf-8'))['user_classification']
        rows=[dict(r,group_state='unmatched',reason=r['previous_reason']) for r in policy['records']]
        self.assertEqual(len(rows),52)
        active,excluded=market_scope.partition(rows,market_scope.load())
        self.assertEqual({r['symbol'] for r in excluded},{'ADAMG','ADAMH','ADAMI','RWTS'})
        mapped=user_classification.apply(active,{'user_classification':policy})
        self.assertEqual(len(mapped),48)
        self.assertTrue(all(r['group_code']=='USER_OTHER_US' for r in mapped))

if __name__=='__main__':unittest.main()
