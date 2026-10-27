import json,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import atomic_io as artifact_io
import market.scope as a
from sectors.classification_refresh import refresh_classification_source
import market.scope as m

class AutomaticScopeTests(unittest.TestCase):
    def setUp(self):
        self.registry_patch=patch('market.exclusions.read_records',return_value=([],b''))
        self.registry_patch.start();self.addCleanup(self.registry_patch.stop)

    def test_retired_rule_ignored_even_when_legacy_config_is_enabled(self):
        legacy={'market':'US','symbol':'OLD','isin_code':'ISIN','automatic_rule':'old','reason':'automatic_low_importance_problem','approved_on':'2026-10-02','approval':'old','evidence_run':'old'}
        dual={'market':'US','symbol':'MISSING','isin_code':'MISS','dual_provider_rule':'dual','reason':'dual_provider_data_unavailable','approved_on':'2026-10-02','approval':'user','evidence_run':'proof','effective_session':'2026-10-01'}
        with tempfile.TemporaryDirectory() as tmp,patch.object(a,'REGISTRY',Path(tmp)/'records.json'):
            a.REGISTRY.write_text(json.dumps({'schema':1,'records':[legacy,dual]}))
            p=Path(tmp)/'policy.json'
            policy={'schema':1,'scope':'market_symbol_until_user_restores','records':[], 'automatic':{'enabled':True,'rule_id':'old'},'dual_provider':{'enabled':True,'rule_id':'dual'}}
            p.write_text(json.dumps(policy)); loaded=m.load(p)
            self.assertEqual([r['symbol'] for r in loaded['records']],['MISSING'])
            self.assertIsNone(m.match({'market':'US','symbol':'OLD','isin_code':'ISIN'},loaded))
            self.assertEqual(a.remember([legacy],policy),0)
            policy['dual_provider']['enabled']=False;p.write_text(json.dumps(policy))
            self.assertEqual(m.load(p)['records'],[])
    def test_reused_ticker_keeps_distinct_identity_records_without_load_failure(self):
        policy={'schema':1,'scope':'market_symbol_until_user_restores','records':[],'dual_provider':{'enabled':True,'rule_id':'dual'}}
        with tempfile.TemporaryDirectory() as tmp,patch.object(a,'REGISTRY',Path(tmp)/'records.json'):
            for isin in ('OLD','NEW'):
                row={'market':'US','symbol':'SAME','isin_code':isin,'dual_provider_rule':'dual','reason':'dual_provider_data_unavailable','approved_on':'2026-10-02','approval':'user','evidence_run':'proof','effective_session':'2026-10-01'}
                self.assertEqual(a.remember([row],policy),1)
            path=Path(tmp)/'policy.json';path.write_text(json.dumps(policy))
            loaded=m.load(path)
            for isin in ('OLD','NEW'):
                self.assertEqual(m.match({'market':'US','symbol':'SAME','isin_code':isin},loaded)['isin_code'],isin)
            self.assertIsNone(m.match({'market':'US','symbol':'SAME','isin_code':'FUTURE'},loaded))

    def test_type_size_classification_cannot_exclude_normal_prices(self):
        policy={'records':[],'lifecycle_records':[]}
        for typ in ('BOND','PREFERRED','CLOSED_END_FUND','ETF','EQUITY'):
            row={'market':'US','symbol':'NORMAL','isin_code':'NORMAL','security_type':typ,'market_cap':1,'classification_error':'finviz_public_evidence_absent'}
            self.assertIsNone(m.match(row,policy))
        self.assertFalse(hasattr(a,'problem_exclusion'))
        self.assertFalse(hasattr(a,'derive'))
    def test_live_policy_has_only_current_explicit_manual_records(self):
        policy=json.loads(m.POLICY.read_text(encoding='utf-8'))
        self.assertNotIn('automatic',policy)
        self.assertEqual(len(policy['records']),37)
        self.assertTrue(all(x['approved_on'] in ('2026-10-04','2026-10-06') and x.get('identity_bound') for x in policy['records']))

    def test_review_classification_refresh_changes_no_observations(self):
        from atomic_io import write_json
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'source';source.mkdir()
            snapshot={'universe':[{'market':'US','symbol':'TEST'}],'series':[{'symbol':'TEST','bars':[{'close':123}]}],
                      'classification_refresh':{'path':'old','sha256':'old'}}
            write_json(source/'snapshot.json',snapshot);write_json(source/'summary.json',{'state':'partial'})
            write_json(source/'manifest.json',{'snapshot.json':artifact_io.sha(source/'snapshot.json'),'summary.json':artifact_io.sha(source/'summary.json')})
            ref={'path':'new','sha256':'new'}
            out=refresh_classification_source(source,ref,root/'derived')
            self.assertEqual(artifact_io.load(out/'snapshot.json'),{**snapshot,'classification_refresh':ref})
            self.assertEqual(artifact_io.load(source/'snapshot.json'),snapshot)
            self.assertEqual(refresh_classification_source(out,ref,root/'unused'),out)
            self.assertFalse((root/'unused').exists())


if __name__=='__main__':unittest.main()
