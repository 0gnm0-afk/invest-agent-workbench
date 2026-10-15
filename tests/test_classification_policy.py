"""Validity, freshness and cache integrity are separate contracts."""
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import sector_observation as so
import sector_projection as projection
from wics_classification import select_wics
from finviz_public_classification import select_classification


class ClassificationPolicy(unittest.TestCase):
    applied='2026-09-30T08:00:00+09:00'

    def proof(self):
        return dict(code='G30',name='필수소비재',symbol='030720',path='saved',
                    observed_at='2026-09-25T08:00:00+09:00',classification_as_of='2026-09-23')

    def test_last_verified_classification_survives_age_without_new_confirmation(self):
        proof=self.proof();before=copy.deepcopy(proof)
        selected,issue=select_wics([proof],self.applied,so.classification_age_issue)
        self.assertIsNone(issue)
        self.assertEqual(selected,before)
        freshness=so.classification_freshness([proof],self.applied)
        self.assertEqual(freshness['state'],'refresh_due')
        self.assertEqual(freshness['source_as_of'],'2026-09-23')
        self.assertEqual(freshness['latest_observed_at'],proof['observed_at'])
        proof['classification_as_of']='2020-01-01'
        self.assertIsNone(select_wics([proof],self.applied,so.classification_age_issue)[1])

    def test_conflicts_absent_future_and_invalid_dates_still_block(self):
        proof=self.proof()
        self.assertIsNotNone(select_wics([],self.applied,so.classification_age_issue)[1])
        self.assertIsNotNone(select_wics([proof,dict(proof,path='other',code='G10')],self.applied,so.classification_age_issue)[1])
        for value in ['2026-10-01T00:00:00+09:00','bad','2026-09-25T00:00:00']:
            p=dict(proof,observed_at=value)
            self.assertIsNotNone(select_wics([p],self.applied,so.classification_age_issue)[1])
            self.assertEqual(so.classification_freshness([p],self.applied)['state'],'unverified')
        self.assertIsNotNone(select_wics([dict(proof,context_observed_at=['2026-10-01T00:00:00+09:00'])],self.applied,so.classification_age_issue)[1])

    def test_recent_and_due_boundary_are_explicit(self):
        proof=dict(self.proof(),classification_as_of=None,observed_at='2026-09-23T08:00:00+09:00')
        self.assertEqual(so.classification_freshness([proof],self.applied)['state'],'recent')
        proof['observed_at']='2026-09-23T07:59:59+09:00'
        self.assertEqual(so.classification_freshness([proof],self.applied)['state'],'refresh_due')
        self.assertEqual(so.classification_freshness([],self.applied)['state'],'unverified')

    def test_us_direct_classification_uses_same_policy_and_keeps_type_checks(self):
        proof=dict(sector='Technology',industry='Software',path='direct',observed_at='2020-01-01T00:00:00+00:00')
        def select(p):
            return select_classification(p,None,'NASDAQ',True,self.applied,so.classification_age_issue)
        self.assertEqual(select([proof])[0],'Technology')
        self.assertEqual(select([dict(proof,industry='Exchange Traded Fund')])[1],'finviz_security_type_conflict')
        self.assertEqual(select([proof,dict(proof,path='other',sector='Energy')])[1],'finviz_direct_sources_conflict')

    def test_unrelated_price_contract_age_rule_unchanged(self):
        self.assertEqual(so.age_issue('2020-01-01T00:00:00+00:00',self.applied),'stale_classification')


class ProjectionCache(unittest.TestCase):
    def test_reuse_and_pinned_source_and_output_tampering(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); source=root/'market';source.mkdir()
            so.write(source/'snapshot.json',{'series':[]})
            so.write(source/'manifest.json',{'snapshot.json':so.sha(source/'snapshot.json')})
            evidence=root/'evidence.json';so.write(evidence,{'proof':True})
            seed=root/'seed.json'
            so.write(seed,{'roots':[str(root/'old'),str(root/'classification'),str(root/'evidence'),str(root/'nh')],
                           'input_hashes':{str(evidence):so.sha(evidence)}})
            config={'sector_manifest':str(seed),'sector_manifest_sha256':so.sha(seed)}
            (root/'run').mkdir()
            so.write(root/'run/run.json',{})
            so.write(root/'run/manifest.json',{'input_hashes':{},'output_hashes':{'run.json':so.sha(root/'run/run.json')}})
            args=(root,root/'run',config,source,{'applied_at':'2026-09-30T00:00:00+00:00'},{'markets':{}})
            with patch.object(so,'build',return_value=({'observations':[]},[],{str(evidence):so.sha(evidence)})) as build, \
                 patch.object(projection.sector_signals,'build',return_value={'sectors':[]}), \
                 patch.object(projection.sector_signals,'attach_candidates'):
                snapshot=so.load(source/'snapshot.json')
                original_load=so.load
                def no_snapshot_reload(path):
                    if Path(path) == source/'snapshot.json':
                        raise MemoryError('duplicate_market_snapshot_parse')
                    return original_load(path)
                with patch.object(so,'load',side_effect=no_snapshot_reload):
                    first=projection.cached(*args,snapshot=snapshot)
                self.assertIs(build.call_args.kwargs['snapshot'],snapshot)
                self.assertEqual(projection.cached(*args),first)
                self.assertEqual(build.call_count,1)
                file=next((root/'runtime/sector-projection').glob('*/view.json'))
                file.write_text('{}',encoding='utf-8')
                with self.assertRaisesRegex(ValueError,'input_hash_mismatch'):
                    projection.cached(*args)
                evidence.write_text('{}',encoding='utf-8')
                with self.assertRaisesRegex(ValueError,'input_hash_mismatch'):
                    projection.cached(*args)


if __name__=='__main__':unittest.main()
