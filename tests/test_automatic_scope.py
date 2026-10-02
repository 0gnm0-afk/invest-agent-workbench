import copy,json,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import automatic_scope as a
import market_scope as m

class AutomaticScopeTests(unittest.TestCase):
    def setUp(self):
        self.policy={'schema':1,'scope':'market_symbol_until_user_restores','records':[],
            'automatic':{'schema':1,'enabled':True,'rule_id':'test','approved_on':'2026-10-02','approval':'test',
                'exclude_types':['bond','preferred_or_bond','non_etf_fund'],'preserve_types':['etf'],
                'problem_market_cap_below_usd':2_000_000_000,'max_cap_age_days':7}}
        self.row={'market':'US','symbol':'TEST','isin_code':'US0001','listing_exchange_family':'NYSE',
            'name':'Test Inc','market_cap':100_000_000,'market_cap_currency':'USD',
            'market_cap_quality':'completed_close_estimate','market_cap_as_of':'2026-10-01'}
    def decide(self,row=None,reasons=('finviz_public_evidence_absent',)):
        return a.problem_exclusion(row or self.row,reasons,self.policy,as_of='2026-10-02',run='test')
    def test_normal_data_types_and_small_caps_never_excluded(self):
        for typ in ('BOND','PREFERRED','CLOSED_END_FUND','EQUITY'):
            row={**self.row,'security_type':typ}
            self.assertIsNone(self.decide(row,[]))
            self.assertIsNone(m.match(row,self.policy))
            self.assertFalse(a.decisions({'universe':[row],'errors':[]},[{'market':'US','symbol':'TEST','group_state':'matched'}],self.policy,as_of='2026-10-02',run='test'))
    def test_normal_google_classes_both_preserved(self):
        for symbol in ('GOOG','GOOGL'):
            row={**self.row,'symbol':symbol,'name':'Alphabet Class A' if symbol=='GOOGL' else 'Alphabet Class C'}
            self.assertIsNone(self.decide(row,[]));self.assertIsNone(m.match(row,self.policy))
    def test_problem_types_require_explicit_problem(self):
        for typ in ('BOND','PREFERRED','CLOSED_END_FUND'):
            row={**self.row,'security_type':typ,'market_cap':None}
            self.assertTrue(self.decide(row)['reason'].startswith('automatic_problem_instrument_type:'))
    def test_etf_exception_precedes_type_name_and_small_cap(self):
        for row in ({**self.row,'security_type':'ETF','name':'채권 우선주 ETF'},
                    {**self.row,'security_type':'BOND','english_name':'Bond ETF'}):
            self.assertIsNone(self.decide(row))
    def test_no_fund_manager_name_inference(self):
        self.assertEqual(a.instrument({**self.row,'name':'Fund Management Inc'},self.policy['automatic'])[0],'unconfirmed')
    def test_small_problem_boundary_and_missing_cap(self):
        self.assertIsNotNone(self.decide())
        for cap in (None,0,-1,True,float('nan'),float('inf'),2_000_000_000,3_000_000_000):
            self.assertIsNone(self.decide({**self.row,'market_cap':cap}))
    def test_cap_requires_current_usd_quality(self):
        for changes in ({'market':'KR'},{'market_cap_currency':'KRW'},{'market_cap_quality':'unavailable_quote_time'},
                        {'market_cap_as_of':'2026-09-01'},{'market_cap_as_of':'2026-10-03'},{'market_cap_as_of':None}):
            self.assertIsNone(self.decide({**self.row,**changes}))
    def test_small_class_value_not_assumed_company_cap(self):
        self.assertIsNone(self.decide({**self.row,'symbol':'TEST.A'}))
        self.assertIsNone(self.decide({**self.row,'name':'Test Class A'}))
    def test_auth_transport_and_short_normal_history_never_excluded(self):
        for reason in ('market_transport_or_payload_unavailable','401','unavailable_insufficient_liquidity_bars'):
            self.assertIsNone(self.decide(reasons=[reason]))
        snapshot={'universe':[self.row],'errors':[{'market':'US','symbol':'TEST','error_code':'market_transport_or_payload_unavailable'}]}
        groups=[{'market':'US','symbol':'TEST','group_state':'unmatched','reason':'finviz_public_evidence_absent'}]
        self.assertFalse(a.decisions(snapshot,groups,self.policy,as_of='2026-10-02',run='test'))
    def test_registry_persistence_and_identity_etf_protection(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(a,'REGISTRY',Path(tmp)/'records.json'):
            decision=self.decide();self.assertEqual(a.remember([decision],self.policy),1)
            self.assertEqual(a.remember([decision],self.policy),0)
            path=Path(tmp)/'policy.json';path.write_text(json.dumps(self.policy),encoding='utf-8')
            policy=m.load(path);self.assertEqual(m.match(self.row,policy)['symbol'],'TEST')
            self.assertIsNone(m.match({**self.row,'isin_code':'DIFFERENT'},policy))
            self.assertIsNone(m.match({**self.row,'security_type':'ETF'},policy))
    def test_immutable_derivation_and_independent_screen_replay(self):
        from invest_agent.trading.market import screen
        from market_collect import write_json
        import sector_observation as so
        good={**self.row,'symbol':'GOOD','market_cap':3e9}
        snap={'universe':[self.row,good],'series':[],
              'errors':[{'market':'US','symbol':r['symbol'],'error_code':'stale_or_invalid_last_session'} for r in (self.row,good)],
              'coverage':{'scope_errors':[],'listings':{'NYSE':{'state':'available','limit':None}},'market_wide':False},
              'collection_stats':{},'sessions':{'US':['2026-10-01']},'source':'fixture','fetched_at':'2026-10-02',
              'benchmarks':{},'profile':{}}
        before=copy.deepcopy(snap)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'source';source.mkdir()
            write_json(source/'snapshot.json',snap);write_json(source/'manifest.json',{'snapshot.json':so.sha(source/'snapshot.json')})
            policy={**self.policy,'path':'fixture','sha256':'fixture'}
            derived,records=a.derive(source,[],policy,root/'derived',as_of='2026-10-02')
            self.assertEqual([r['symbol'] for r in records],['TEST'])
            a.verify_derivation(derived)
            narrowed=so.load(derived/'snapshot.json')
            self.assertEqual(a.filter_screen(screen(snap),narrowed),screen(narrowed))
            self.assertEqual(so.load(source/'snapshot.json'),before)
            meta=so.load(derived/'automatic-derivation.json');meta['decisions'][0]['symbol']='GOOD'
            write_json(derived/'automatic-derivation.json',meta)
            with self.assertRaisesRegex(ValueError,'decision_replay_mismatch'):a.verify_derivation(derived)

    def test_review_classification_refresh_changes_no_observations(self):
        from market_collect import write_json
        import sector_observation as so
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'source';source.mkdir()
            snapshot={'universe':[self.row],'series':[{'symbol':'TEST','bars':[{'close':123}]}],
                      'classification_refresh':{'path':'old','sha256':'old'}}
            write_json(source/'snapshot.json',snapshot);write_json(source/'summary.json',{'state':'partial'})
            write_json(source/'manifest.json',{'snapshot.json':so.sha(source/'snapshot.json'),'summary.json':so.sha(source/'summary.json')})
            ref={'path':'new','sha256':'new'}
            out=a.refresh_classification_source(source,ref,root/'derived')
            self.assertEqual(so.load(out/'snapshot.json'),{**snapshot,'classification_refresh':ref})
            self.assertEqual(so.load(source/'snapshot.json'),snapshot)
            self.assertEqual(a.refresh_classification_source(out,ref,root/'unused'),out)
            self.assertFalse((root/'unused').exists())

    def test_fact_type_only_for_exact_isin_exchange(self):
        rule={**self.policy['automatic'],'facts':{'path':'test','sha256':'test'}}
        key=('US','TEST','US0001','NYSE')
        with patch.object(a,'facts',return_value={key:{'type':'non_etf_fund'}}):
            self.assertEqual(a.instrument(self.row,rule)[0],'non_etf_fund')
            self.assertEqual(a.instrument({**self.row,'isin_code':'OTHER'},rule)[0],'unconfirmed')
            self.assertEqual(a.instrument({**self.row,'listing_exchange_family':'NASDAQ'},rule)[0],'unconfirmed')

if __name__=='__main__':unittest.main()
