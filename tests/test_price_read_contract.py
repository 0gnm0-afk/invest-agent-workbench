"""C2 reads and C1 financial callers use synthetic stores, never collect or migrate."""
import copy
from contextlib import closing, contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from market.prices import PriceStore, saved_price_context, saved_bars, saved_snapshot_series, freeze_snapshot
from market.history.chart import HistoryStore
from market.financial_price_receipts import pack_price_input, read_price_input
from market.price_files import PriceFiles


def stock(**changes):
    value = dict(market='US', symbol='SYNTH', currency='USD', provider='Toss OpenAPI candles',
                 price_basis='adjusted', isin_code='US0000000001', name='Synthetic', bars=[
                     dict(date='2026-10-01', open=10, high=12, low=9, close=11, volume=100),
                     dict(date='2026-10-02', open=11, high=13, low=10, close=12, volume=None)])
    return {**value, **changes}


class PriceReadContract(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = PriceStore(self.root / 'runtime/ohlcv.sqlite3', root=self.root)

    def files(self):
        return {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file() and not p.name.endswith(('-wal', '-shm'))}

    @contextmanager
    def readonly(self):
        original = PriceStore.connect
        def connect(store, *, write=False):
            self.assertFalse(write, 'saved read attempted price/schema writes')
            return original(store, write=False)
        # SQLite WAL readers may create bookkeeping sidecars; persisted DB and
        # artifact bytes remain fixed and every price connection must be mode=ro.
        before = self.files()
        with patch.object(PriceStore, 'connect', connect), \
                patch('market.history.chart.TossMarketClient', side_effect=AssertionError('provider created')), \
                patch.object(HistoryStore, 'ensure', side_effect=AssertionError('collection invoked')):
            yield
        self.assertEqual(self.files(), before)

    def test_context_missing_empty_single_multiple_and_configured_path(self):
        with self.readonly():
            self.assertEqual(saved_price_context('US:SYNTH', root=self.root), [])
            with self.assertRaisesRegex(FileNotFoundError, 'database_missing'):
                saved_bars('US:SYNTH', root=self.root)
        self.assertFalse(self.store.path.exists())
        empty = self.store.put(stock(bars=[]), source='empty')
        with self.readonly():
            rows = saved_price_context('US:SYNTH', root=self.root)
            self.assertIsNone(rows[0]['last_completed_saved_bar'])
            self.assertEqual(saved_bars('US:SYNTH', root=self.root)['bars'], [])
        ref = self.store.put(stock(), source='daily', kind='series')
        with self.readonly():
            value = saved_price_context('US:SYNTH', root=self.root)
            self.assertEqual(value[0]['last_completed_saved_bar'],
                             dict(day='2026-10-02', close=12., revision=ref['revision']))
            self.assertEqual(value[0]['metadata'], ref['identity'])
        self.store.put(stock(provider='NH saved candles', price_basis='unadjusted'), source='legacy')
        with self.readonly():
            self.assertEqual(len(saved_price_context('US:SYNTH', root=self.root)), 2)
            with self.assertRaisesRegex(ValueError, 'missing_or_ambiguous'):
                saved_bars('US:SYNTH', root=self.root)
        configured = self.root / 'runtime/configured.sqlite3'
        PriceStore(configured, root=self.root).put(stock(symbol='ALT'), source='configured')
        (self.root / 'config.json').write_text(json.dumps({'ohlcv': {'path': str(configured)}}))
        with self.readonly():
            self.assertEqual(saved_price_context('US:SYNTH', root=self.root), [])
            self.assertEqual(len(saved_price_context('US:ALT', root=self.root)), 1)

    def test_cutoff_revision_and_correction_preserve_pinned_input(self):
        original = stock()
        ref = self.store.put(original, source='initial', kind='series')
        revised = copy.deepcopy(original)
        revised['bars'][0]['close'] = 11.5
        self.store.put(revised, source='correction', kind='series')
        with self.readonly():
            current = saved_bars(original, root=self.root, cutoff='2026-10-01')
            self.assertEqual(current['bars'], revised['bars'][:1])
            self.assertEqual(self.store.read(current['bars_ref']), current['bars'])
            historical = saved_bars(original, root=self.root, reference=ref)
            self.assertEqual(historical['bars'], original['bars'])
            sliced = saved_bars(original, root=self.root, reference=ref, cutoff='2026-10-01')
            self.assertEqual(self.store.read(sliced['bars_ref']), original['bars'][:1])
            self.assertEqual(saved_bars(original, root=self.root, cutoff='2026-09-30')['bars'], [])
            self.assertIsNone(current['bars_ref'].get('variants'))
            self.assertEqual(current['observation_metadata']['name'], 'Synthetic')

    def test_identity_conflict_and_reference_corruption_fail_closed(self):
        value = stock()
        ref = self.store.put(value, source='fixture')
        with self.readonly():
            with self.assertRaisesRegex(ValueError, 'missing_or_ambiguous'):
                saved_bars(stock(isin_code='US0000000099'), root=self.root)
            with self.assertRaisesRegex(ValueError, 'identity_mismatch'):
                saved_bars(stock(isin_code='US0000000099'), root=self.root, reference=ref)
            for field, changed, error in [('sha256', '0' * 64, 'hash_mismatch'),
                                          ('count', 3, 'count_mismatch')]:
                bad = {**ref, field: changed}
                if field == 'sha256':
                    bad['numeric_sha256'] = changed
                with self.assertRaisesRegex(ValueError, error):
                    saved_bars(value, root=self.root, reference=bad)
        self.store.put(stock(isin_code='US0000000099'), source='other-listing')
        with self.readonly():
            self.assertEqual(saved_bars(value, root=self.root)['identity']['isin'], value['isin_code'])
            with self.assertRaisesRegex(ValueError, 'missing_or_ambiguous'):
                saved_bars('US:SYNTH', root=self.root)

    def test_old_embedded_snapshot_and_new_refs_keep_original_hash(self):
        path = self.root / 'legacy.json'
        value = dict(series=[stock(), stock(symbol='OTHER')])
        raw = b'\xef\xbb\xbf' + json.dumps(value, indent=2).replace('\n', '\r\n').encode()
        path.write_bytes(raw)
        expected = hashlib.sha256(raw).hexdigest()
        with self.readonly(), patch.object(Path, 'read_bytes', side_effect=AssertionError('whole legacy snapshot read')):
            self.assertEqual(saved_snapshot_series(path, expected, {'US:SYNTH'}, root=self.root),
                             {'US:SYNTH': stock()})
        frozen = freeze_snapshot(value, path, self.store)
        raw = json.dumps(frozen).encode()
        path.write_bytes(raw)
        expected = hashlib.sha256(raw).hexdigest()
        changed = stock()
        changed['bars'][0]['close'] = 11.5
        self.store.put(changed, source='new-price')
        with self.readonly():
            selected = saved_snapshot_series(path, expected, {'US:SYNTH'}, root=self.root)
            self.assertEqual(selected['US:SYNTH']['bars'], stock()['bars'])
            with self.assertRaisesRegex(ValueError, 'hash_mismatch'):
                saved_snapshot_series(path, '0' * 64, {'US:SYNTH'}, root=self.root)
        path.write_bytes(raw + b' ')
        with self.readonly():
            with self.assertRaisesRegex(ValueError, 'hash_mismatch'):
                saved_snapshot_series(path, expected, {'US:SYNTH'}, root=self.root)

    def test_record_pointer_and_moved_root_keep_original_snapshot(self):
        from records.store import RecordStore
        previous = self.root / 'previous'
        old_store = PriceStore(previous / 'runtime/ohlcv.sqlite3', root=previous)
        source = previous / 'runtime/daily/fixture/snapshot.json'
        frozen = freeze_snapshot({'series': [stock()]}, source, old_store)
        raw = json.dumps(frozen).encode()
        expected = hashlib.sha256(raw).hexdigest()
        records = RecordStore(previous / 'runtime/records.sqlite3')
        reference = records.put(source, raw, root=previous)
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(json.dumps({'record_artifact_ref': reference}), encoding='utf-8')
        # Move only this fixture's closed stores; no native CopyFile2 bypass of C8.
        (previous / 'runtime').replace(self.root / 'runtime')
        (self.root / 'config.json').write_text(json.dumps({'records': {'previous_roots': [str(previous)]}}))
        with self.readonly():
            selected = saved_snapshot_series(source, expected, {'US:SYNTH'}, root=self.root)
            self.assertEqual(selected['US:SYNTH']['bars'], stock()['bars'])
        local_source = self.root / 'runtime/daily/fixture/snapshot.json'
        bad = {**reference, 'source_path': str(source.with_name('other.json'))}
        local_source.write_text(json.dumps({'record_artifact_ref': bad}), encoding='utf-8')
        with self.readonly():
            with self.assertRaisesRegex(ValueError, 'path_mismatch'):
                saved_snapshot_series(source, expected, {'US:SYNTH'}, root=self.root)
        local_source.unlink()
        with self.readonly():
            self.assertEqual(saved_snapshot_series(source, expected, {'US:SYNTH'}, root=self.root)
                             ['US:SYNTH']['bars'], stock()['bars'])

    def test_retired_snapshot_recovery_and_duplicate_rejection(self):
        value = stock()
        self.store.put(value, source='initial')
        path = self.root / 'runtime/daily/fixture/snapshot.json'
        raw = json.dumps({'series': [value]}, ensure_ascii=False, sort_keys=True).encode()
        PriceFiles(self.store).prepare_bytes(path, raw)
        with self.readonly():
            selected = saved_snapshot_series(path, hashlib.sha256(raw).hexdigest(), {'US:SYNTH'}, root=self.root)
            self.assertEqual(selected['US:SYNTH'], value)
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = json.dumps({'series': [value, value]}).encode()
        path.write_bytes(raw)
        with self.readonly():
            with self.assertRaisesRegex(ValueError, 'duplicate_market_series'):
                saved_snapshot_series(path, hashlib.sha256(raw).hexdigest(), {'US:SYNTH'}, root=self.root)

    def test_snapshot_pinned_ref_rejects_same_symbol_different_identity(self):
        ref = self.store.put(stock(), source='fixture')
        path = self.root / 'snapshot.json'
        packet = {'ohlcv_store': {'path': str(self.store.path)},
                  'series': [{**stock(isin_code='US0000000099'), 'bars_ref': ref}]}
        raw = json.dumps(packet).encode()
        path.write_bytes(raw)
        with self.readonly():
            with self.assertRaisesRegex(ValueError, 'identity_mismatch'):
                saved_snapshot_series(path, hashlib.sha256(raw).hexdigest(), {'US:SYNTH'}, root=self.root)

    def test_archive_entry_limits_metadata_to_requested_symbol(self):
        self.store.set_meta('archive_US', {'source': str(self.root / 'archive'), 'config': {'end': '2026-10-02'},
            'listings': {'SYNTH': {'isinCode': 'US0000000001'}, 'OTHER': {'name': 'Unrequested'}},
            'coverage': {'SYNTH': {'state': 'received'}, 'OTHER': {'state': 'failed'}}, 'references': {}})
        with self.readonly():
            selected = self.store.archive_entry('US', 'SYNTH')
            self.assertEqual(set(selected['listings']), {'SYNTH'})
            self.assertEqual(set(selected['coverage']), {'SYNTH'})
            self.assertEqual(self.store.archive_entry('US', 'ABSENT')['listings'], {})
            self.assertIsNone(self.store.archive_entry('KR', 'SYNTH'))

    def test_chart_legacy_cache_is_readonly_and_does_not_create_schema(self):
        path = self.root / 'runtime/chart-history.sqlite3'
        chart = HistoryStore(path, Mock(side_effect=AssertionError('network')))
        value = stock()
        with self.readonly():
            self.assertEqual(chart.read(value)['history']['state'], 'not_requested')
        self.assertFalse(path.exists())
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({'series': value, 'history': {'state': 'partial', 'reason': 'saved_partial'}})
        with closing(sqlite3.connect(path)) as db, db:
            db.execute('CREATE TABLE history(identity TEXT PRIMARY KEY,payload TEXT,sha256 TEXT)')
            db.execute('INSERT INTO history VALUES (?,?,?)', (chart.legacy_identity(value), payload,
                       hashlib.sha256(payload.encode()).hexdigest()))
        with self.readonly():
            result = chart.read(value)
            self.assertEqual(result['history']['state'], 'partial')
            self.assertEqual(result['bars'], value['bars'])
        with closing(sqlite3.connect(path)) as db, db:
            db.execute('UPDATE history SET sha256=?', ('0' * 64,))
        with self.readonly():
            self.assertEqual(chart.read(value)['history']['state'], 'failed')

    def test_chart_saved_failure_and_empty_are_not_replaced(self):
        chart = HistoryStore(self.root / 'runtime/chart-history.sqlite3')
        for packet in ({'history': {'state': 'failed', 'reason': 'saved_failed'}},
                       {'series': stock(bars=[]), 'history': {'state': 'unavailable',
                        'reason': 'saved_history_empty_before_cutoff'}}):
            with self.subTest(packet=packet), self.readonly(), \
                    patch.object(chart.saved, 'packet', return_value=packet), \
                    patch.object(chart, 'cached', side_effect=AssertionError('fallback on invalid history')):
                result = chart.read(stock())
                self.assertEqual(result['history']['state'], packet['history']['state'])
                self.assertEqual(result['bars'], packet.get('series', stock())['bars'])

    def test_financial_api_preserves_variant_and_original_bytes(self):
        from financials.store import FinancialStore, pack_price_input as consumer_pack
        financials = FinancialStore(self.root / 'runtime/financials', price_store=self.store)
        original = stock()
        active = self.store.put(original, source='daily')
        historical = copy.deepcopy(original)
        historical['bars'][0]['close'] = 10.5
        path = self.root / 'runtime/financials/US-SYNTH/snapshots/fixture/saved-price.json'
        marker = consumer_pack(historical, path, financials.price_store)
        raw = json.dumps(historical, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()
        self.assertEqual(marker['input_sha256'], hashlib.sha256(raw).hexdigest())
        with self.readonly():
            self.assertEqual(read_price_input(json.dumps(marker).encode(), path, self.store), historical)
            self.assertEqual(PriceFiles(self.store).recover(path), raw)
            self.assertEqual(self.store.read(active), original['bars'])
        with closing(self.store.connect()) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM price_variants').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT count(*) FROM price_corrections').fetchone()[0], 0)

    def test_context_rejects_invalid_key_and_corrupt_database(self):
        for key in ('SYNTH', 'US:*', 'US:SYNTH OR 1=1', None):
            with self.assertRaisesRegex(ValueError, 'exact_stock_key_required'):
                saved_price_context(key, root=self.root)
        self.store.path.parent.mkdir(parents=True)
        self.store.path.write_bytes(b'not a sqlite database')
        with self.assertRaises(sqlite3.DatabaseError):
            saved_price_context('US:SYNTH', root=self.root)
