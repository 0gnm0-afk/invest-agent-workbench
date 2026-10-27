"""Recalculation preserves signed-off raw inputs and does not collect external data."""
import copy
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from financials.store import FinancialStore, packed, save


class RecalculateSaved(unittest.TestCase):
    def setUp(self):
        for name in ('financials.store.settings','financials.collect.settings'):
            settings = patch(name,return_value={})
            settings.start(); self.addCleanup(settings.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.store = FinancialStore(self.temp.name)
        self.key = 'US:ABC'
        self.sid = 'a' * 32
        self.folder = self.store.folder(self.key)
        self.base = self.folder / 'snapshots' / self.sid
        self.prior = dict(key=self.key, snapshot_id=self.sid, collected_at='2026-09-30T01:00:00Z', sources=[dict(id='facts.json', url='https://www.sec.gov/example')])
        save(self.base / 'source.json', dict(provider='SEC', symbol='ABC'))
        save(self.base / 'saved-price.json', dict(market='US', symbol='ABC', bars=[]))
        save(self.base / 'result.json', self.prior)
        save(self.base / 'raw/facts.json', dict(official='retained'))
        files = {str(p.relative_to(self.base)).replace('\\', '/'): hashlib.sha256(p.read_bytes()).hexdigest() for p in self.base.rglob('*') if p.is_file()}
        save(self.base / 'manifest.json', dict(files=files))
        save(self.folder / 'latest.json', dict(snapshot=self.sid, manifest_sha256=hashlib.sha256((self.base / 'manifest.json').read_bytes()).hexdigest()))
        self.original = {str(p.relative_to(self.base)): p.read_bytes() for p in self.base.rglob('*') if p.is_file()}

    def tearDown(self):
        self.temp.cleanup()

    def test_cached_version_is_explicit_without_changing_snapshot(self):
        from financials.store import VERSION
        with patch('financials.store.settings',return_value={}):
            result=self.store.read(self.key)
        self.assertTrue(result['result_stale'])
        self.assertEqual(result['calculation_version'],VERSION)
        for name,raw in self.original.items():
            self.assertEqual((self.base/name).read_bytes(),raw)

    def test_start_reuses_current_but_rebuilds_stale_cached_result(self):
        stock={'market':'US','symbol':'ABC'}
        for stale in (False,True):
            packet={'result':{'snapshot_id':self.sid},'result_stale':stale}
            with patch.object(self.store,'read',return_value=packet), patch('financials.store.lock_file',return_value=None) as lock:
                result=self.store.start(self.key,stock)
            self.assertEqual(result.get('state'), 'running' if stale else None)
            self.assertEqual(lock.call_count,int(stale))

    def test_recalculate_retains_raw_date_and_original_snapshot(self):
        with patch('financials.store.Collector') as collector, patch('financials.store.sec_normalize', return_value={'ok': True}), patch('financials.store.summarize', return_value={'summary': [], 'calculation_version': 'new'}):
            result = self.store.recalculate_saved(self.key)
            collector.assert_not_called()
        self.assertEqual(result['state'], 'complete')
        current = result['result']
        self.assertNotEqual(current['snapshot_id'], self.sid)
        self.assertEqual(current['source_snapshot_id'], self.sid)
        self.assertEqual(current['collected_at'], self.prior['collected_at'])
        self.assertIn('recalculated_at', current)
        for name, raw in self.original.items():
            self.assertEqual((self.base / name).read_bytes(), raw)
        new_base = self.folder / 'snapshots' / current['snapshot_id']
        self.assertEqual((new_base / 'raw/facts.json').read_bytes(), (self.base / 'raw/facts.json').read_bytes())
        manifest = json.loads((new_base / 'manifest.json').read_bytes())
        self.assertIn('financial_revenue.py', manifest['code'])

    def test_tampered_source_rejected_before_work(self):
        (self.base / 'source.json').write_bytes(packed({'provider': 'tampered'}))
        with patch.object(self.store, '_work') as work:
            with self.assertRaisesRegex(ValueError, 'hash_mismatch'):
                self.store.recalculate_saved(self.key)
            work.assert_not_called()
        self.assertEqual(json.loads((self.folder / 'latest.json').read_bytes())['snapshot'], self.sid)

    def signed_price(self, stock):
        """Replace and sign only this test's synthetic saved price input."""
        (self.base/'saved-price.json').write_bytes(packed(stock))
        manifest=json.loads((self.base/'manifest.json').read_bytes())
        manifest['files']['saved-price.json']=hashlib.sha256((self.base/'saved-price.json').read_bytes()).hexdigest()
        (self.base/'manifest.json').write_bytes(packed(manifest))
        (self.folder/'latest.json').write_bytes(packed(dict(snapshot=self.sid,
            manifest_sha256=hashlib.sha256((self.base/'manifest.json').read_bytes()).hexdigest())))

    def current_price(self, **overrides):
        stock=dict(market='US',symbol='ABC',currency='USD',isin_code='SYNTHETIC-ABC',
                   price_basis='adjusted',history={'state':'complete'},bars=[
                       dict(date='2026-10-01',open=10,high=12,low=9,close=11,volume=100)])
        return dict(stock,**overrides)

    def test_price_recalculation_verifies_every_saved_input_before_work(self):
        pointer=(self.folder/'latest.json').read_bytes()
        for name in ('source.json','raw/facts.json','saved-price.json','result.json'):
            with self.subTest(name=name):
                path=self.base/name; original=path.read_bytes()
                path.write_bytes(packed({'synthetic_tamper':True}))
                try:
                    with patch.object(self.store,'_work') as work:
                        with self.assertRaisesRegex(ValueError,'saved_input_hash_mismatch'):
                            self.store.recalculate_saved(self.key,stock=self.current_price())
                        work.assert_not_called()
                    self.assertEqual((self.folder/'latest.json').read_bytes(),pointer)
                    self.assertEqual([p.name for p in (self.folder/'snapshots').iterdir()],[self.sid])
                finally:
                    path.write_bytes(original)

    def test_price_recalculation_requires_existing_financial_snapshot(self):
        with patch.object(self.store,'_work') as work:
            with self.assertRaisesRegex(ValueError,'saved_financial_required'):
                self.store.recalculate_saved('US:NEW',stock=self.current_price(symbol='NEW'))
            work.assert_not_called()

    def test_price_recalculation_preserves_auxiliary_metadata_and_partial_history(self):
        old=self.current_price(preferred_price={'symbol':'PREFERRED','bar':{'close':5}})
        self.signed_price(old)
        before={str(p.relative_to(self.base)):p.read_bytes() for p in self.base.rglob('*') if p.is_file()}
        updated=self.current_price(history={'state':'partial','reason':'saved_history_quality_flags'})
        updated['bars'][0]['close']=12
        with patch('financials.store.Collector') as collector, patch('financials.store.sec_normalize',return_value={}), \
                patch('financials.store.summarize',return_value={'summary':[]}) as calculate:
            result=self.store.recalculate_saved(self.key,stock=updated)
            collector.assert_not_called()
        used=calculate.call_args.args[1]
        self.assertEqual(used['preferred_price'],old['preferred_price'])
        self.assertEqual(used['bars'],updated['bars'])
        self.assertEqual(used['history'],updated['history'])
        self.assertNotIn('preferred_price',updated)
        self.assertEqual(result['state'],'complete')
        self.assertNotEqual(result['result']['snapshot_id'],self.sid)
        self.assertEqual(result['result']['source_snapshot_id'],self.sid)
        self.assertEqual(result['result']['collected_at'],self.prior['collected_at'])
        for name,raw in before.items():self.assertEqual((self.base/name).read_bytes(),raw)

    def test_price_recalculation_rejects_other_identity_and_basis(self):
        self.signed_price(self.current_price(listing_exchange_family='NASDAQ'))
        for fields in ({'market':'KR'},{'symbol':'OTHER'},{'currency':'KRW'},
                       {'isin_code':'OTHER-ISSUE'},{'price_basis':'unadjusted'},
                       {'listing_exchange_family':'NYSE'}):
            with self.subTest(fields=fields), patch.object(self.store,'_work') as work:
                with self.assertRaisesRegex(ValueError,'saved_price_identity'):
                    self.store.recalculate_saved(self.key,stock=self.current_price(**fields))
                work.assert_not_called()

    def test_price_recalculation_rejects_failed_or_incompatible_history(self):
        for fields in ({'bars':[]},*({'history':{'state':s}} for s in ('failed','excluded','unavailable','pending')),
                       {'history':{'state':'partial','daily_update_reason':'listing_identity_changed'}},
                       {'history':{'state':'partial','daily_update_reason':'price_basis_changed'}},
                       {'history':{'state':'complete','screening_price_difference':True}}):
            with self.subTest(fields=fields), patch.object(self.store,'_work') as work:
                with self.assertRaisesRegex(ValueError,'saved_price_unavailable'):
                    self.store.recalculate_saved(self.key,stock=self.current_price(**fields))
                work.assert_not_called()

    def test_analysis_price_refresh_uses_store_contract_without_snapshot_paths(self):
        from types import SimpleNamespace
        from analysis.service import AnalysisService
        service=AnalysisService.__new__(AnalysisService)
        stock=self.current_price()
        service.case=lambda key:dict(id=key,stock_key=self.key)
        service.view=lambda:({}, {self.key:stock})
        service.histories=SimpleNamespace(saved=SimpleNamespace(stocks={}),view=lambda s,f:dict(s,timeframe=f))
        service.financials=Mock(spec=['recalculate_saved'])
        service.financials.recalculate_saved.return_value=dict(state='complete',attempt={},result={'snapshot_id':'new'})
        result=service.dispatch('refresh',{'analysis_key':'synthetic','mode':'financial_price_recalculate'},'')
        service.financials.recalculate_saved.assert_called_once_with(self.key,stock=dict(stock,timeframe='1d'))
        self.assertEqual(result['snapshot_id'],'new')
        self.assertEqual(result['scope'],'saved_disclosures_and_saved_prices_no_network')

    def test_failed_calculation_keeps_old_success(self):
        with patch('financials.store.sec_normalize', side_effect=ValueError('bad period')):
            result = self.store.recalculate_saved(self.key)
        self.assertEqual(result['state'], 'failed')
        self.assertEqual(result['result']['snapshot_id'], self.sid)
        self.assertEqual(json.loads((self.folder / 'latest.json').read_bytes())['snapshot'], self.sid)

    def test_lock_prevents_concurrent_recalculation(self):
        from financials.store import lock_file, unlock
        lease = lock_file(self.folder / 'request.lock')
        try:
            with self.assertRaisesRegex(ValueError, 'busy'):
                self.store.recalculate_saved(self.key)
        finally:
            unlock(lease)

    def _make_dart_fixture(self):
        self.key = 'KR:123456.KS'
        self.folder = self.store.folder(self.key)
        self.base = self.folder / 'snapshots' / self.sid
        self.prior = dict(self.prior, key=self.key)
        save(self.base / 'source.json', dict(provider='DART', identity={'corp_code':'00000001'}, reports=[]))
        save(self.base / 'saved-price.json', dict(market='KR', symbol='123456.KS', bars=[]))
        save(self.base / 'result.json', self.prior)
        save(self.base / 'raw/facts.json', dict(official='retained'))
        files = {str(p.relative_to(self.base)).replace('\\', '/'): hashlib.sha256(p.read_bytes()).hexdigest() for p in self.base.rglob('*') if p.is_file()}
        save(self.base / 'manifest.json', dict(files=files))
        save(self.folder / 'latest.json', dict(snapshot=self.sid, manifest_sha256=hashlib.sha256((self.base / 'manifest.json').read_bytes()).hexdigest()))
        self.original = {str(p.relative_to(self.base)): p.read_bytes() for p in self.base.rglob('*') if p.is_file()}

    def test_explicit_supplement_preserves_old_source_and_records_new_provenance(self):
        self._make_dart_fixture()
        def supplement(collector, source):
            save(collector.folder / 'annual-extra.json', {'status':'000','list':[]})
            collector.sources.append(dict(id='annual-extra.json', url='https://opendart.fss.or.kr/api/alotMatter.json'))
            return dict(source, annual_supplements=[{'year':2025,'raw':{'status':'000','list':[]}}])
        with patch('financials.providers.dart_supplement.collect_annual_supplements', side_effect=supplement) as supplement_call, patch('financials.store.dart_normalize', return_value={'ok':True}), patch('financials.store.summarize', return_value={'summary':[], 'calculation_version':'new'}):
            response = self.store.recalculate_saved(self.key, supplement=True)
        supplement_call.assert_called_once()
        self.assertEqual(response['state'], 'complete')
        result = response['result']
        self.assertEqual(result['collected_at'], self.prior['collected_at'])
        self.assertIn('supplemented_at', result)
        self.assertEqual({s['id'] for s in result['sources']}, {'facts.json','annual-extra.json'})
        for name, raw in self.original.items():
            self.assertEqual((self.base / name).read_bytes(), raw)
        new_base = self.folder / 'snapshots' / result['snapshot_id']
        self.assertTrue((new_base / 'raw/annual-extra.json').is_file())
        self.assertIn('annual_supplements', json.loads((new_base / 'source.json').read_bytes()))

    def test_failed_supplement_keeps_previous_success(self):
        self._make_dart_fixture()
        with patch('financials.providers.dart_supplement.collect_annual_supplements', side_effect=ValueError('bad identity')):
            response = self.store.recalculate_saved(self.key, supplement=True)
        self.assertEqual(response['state'], 'failed')
        self.assertEqual(response['result']['snapshot_id'], self.sid)
        self.assertEqual(json.loads((self.folder / 'latest.json').read_bytes())['snapshot'], self.sid)

    def test_sec_supplement_stays_offline(self):
        with patch('financials.store.Collector') as collector, patch('financials.store.sec_normalize', return_value={'ok':True}), patch('financials.store.summarize', return_value={'summary':[]}):
            response = self.store.recalculate_saved(self.key, supplement=True)
        collector.assert_not_called()
        self.assertEqual(response['state'], 'complete')


if __name__ == '__main__':
    unittest.main()
