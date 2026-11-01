import copy
from contextlib import closing
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from market.prices import PriceStore, freeze_snapshot, hydrate_snapshot, normalized
from atomic_io import write_json, load, sha
from alerts import holding_series


def series():
    return dict(market='US',symbol='AAA',currency='USD',provider='Toss OpenAPI candles',
                price_basis='adjusted',isin_code='US0000000001',bars=[
                    dict(date='2026-10-01',open=10,high=12,low=9,close=11,volume=100),
                    dict(date='2026-10-02',open=11,high=13,low=10,close=12,volume=120)])


class PricesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = PriceStore(self.root/'ohlcv.sqlite3')

    def count(self, table):
        with closing(self.store.connect()) as db:
            return db.execute('SELECT count(*) FROM '+table).fetchone()[0]

    def test_idempotence_append_correction_and_past_reference(self):
        old = series()
        ref = self.store.put(old, source='archive')
        self.assertEqual(self.store.put(old, source='archive'),ref)
        self.store.put(old, source='daily')
        self.assertEqual(self.count('prices'),2)
        self.assertEqual(self.count('price_corrections'),0)
        new = copy.deepcopy(old)
        new['bars'][0]['close'] = 11.5
        new['bars'].append(dict(new['bars'][-1],date='2026-10-05'))
        current = self.store.put(new, source='new-day')
        self.assertEqual(self.store.read(ref), old['bars'])
        self.assertEqual(self.store.read(current), new['bars'])
        self.assertEqual(self.count('prices'),3)
        self.assertEqual(self.count('price_corrections'),1)
        # One more revision must preserve both historic versions.
        newer = copy.deepcopy(new);newer['bars'][0]['close'] = 11.8
        self.store.put(newer,source='corrected-again')
        self.assertEqual(self.store.read(ref),old['bars'])
        self.assertEqual(self.store.read(current),new['bars'])

    def test_reject_overlap_without_correction_permission_and_rollback(self):
        old=series(); self.store.put(old,source='archive')
        new=copy.deepcopy(old);new['bars'][0]['close']=11.5
        with self.assertRaisesRegex(ValueError,'overlap_changed'):
            self.store.put(new,source='uncertain',allow_corrections=False)
        self.assertEqual(self.count('price_revisions'),1)

    def test_pinning_a_past_view_never_rolls_back_a_new_correction(self):
        old=series();ref=self.store.put(old,source='archive')
        new=copy.deepcopy(old);new['bars'][0]['close']=11.5
        current=self.store.put(new,source='new')
        self.assertEqual(self.store.pin(old),ref)
        self.assertEqual(self.store.read(current),new['bars'])
        self.assertEqual(self.count('price_corrections'),1)
        self.assertEqual(self.store.put(old,source='archive',allow_corrections=False),ref)
        unknown=copy.deepcopy(old);unknown['bars'][0]['close']=10.5
        with self.assertRaisesRegex(ValueError,'reference_unavailable'):self.store.pin(unknown)
        self.assertEqual(self.count('price_corrections'),1)

    def test_toss_failures_are_visible_without_nh_fallback_reports(self):
        from market.exclusions import inventory
        value=series();value.update(listing_exchange_family='NASDAQ')
        snapshot={'price_collection_provider':'toss','universe':[value],'series':[],
                  'errors':[{'market':'US','symbol':'AAA','error_code':'market_http_429'}],
                  'sessions':{'US':['2026-10-02']}}
        issue=inventory(snapshot,{'quality':[]})['issues'][0]
        self.assertEqual(issue['field'],'가격 이력');self.assertFalse(issue['nh_attempted'])
        self.assertIn('429',issue['toss_reason'])

    def test_reference_checks_identity_count_and_price_corruption(self):
        ref=self.store.put(series(),source='one')
        bad=copy.deepcopy(ref);bad['identity']['isin']='other'
        with self.assertRaisesRegex(ValueError,'identity_mismatch'): self.store.read(bad)
        with closing(self.store.connect(write=True)) as db,db:
            db.execute('UPDATE prices SET close=999')
        with self.assertRaisesRegex(ValueError,'hash_mismatch'): self.store.read(ref)

    def test_thin_derived_snapshot_and_selected_alerts(self):
        original=dict(series=[series()],benchmarks={},risk_series=[])
        path=self.root/'snapshot.json'
        frozen=freeze_snapshot(original,path,self.store)
        write_json(path,frozen)
        self.assertNotIn('bars',json.loads(path.read_text(encoding='utf-8'))['series'][0])
        restored=load(path)
        self.assertEqual(restored['series'][0]['bars'],original['series'][0]['bars'])
        derived=self.root/'derived.json';write_json(derived,restored)
        self.assertNotIn('bars',json.loads(derived.read_text(encoding='utf-8'))['series'][0])
        self.assertEqual(self.count('price_observations'),1)
        self.assertEqual(holding_series(path,sha(path),{'US:AAA'})['US:AAA']['bars'],series()['bars'])

    def test_empty_reference_still_checks_identity(self):
        value=series();value['bars']=[]
        ref=self.store.put(value,source='empty');self.assertEqual(self.store.read(ref),[])
        ref['identity']['symbol']='bad'
        with self.assertRaisesRegex(ValueError,'identity_mismatch'):self.store.read(ref)


if __name__ == '__main__': unittest.main()
