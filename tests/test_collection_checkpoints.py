import hashlib
import json
from contextlib import closing
from pathlib import Path
import sys
import tempfile
import time
import unittest
from datetime import timedelta
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

from atomic_io import read_bytes
from invest_agent.trading.contracts import digest
from invest_agent.trading.store import instance_lock
from records.checkpoints import compact, save_checkpoint
import records.checkpoints as checkpoints
from records.files import available
from records.store import RecordStore, default_path
import market.toss_collection as collection
import test_hybrid_market as provider_fixture


class CollectionCheckpointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cache = self.root / 'runtime' / '2026-09-12' / '080000-fixture' / 'collection' / 'cache'
        self.store = RecordStore(default_path(self.root))

    def initialize(self):
        self.store.put(self.root / 'runtime' / 'fixture.json', b'{}', root=self.root)

    def packet(self, number=1, family='series', observed='2026-09-12T00:00:00+00:00'):
        key = digest({'fixture': number, 'family': family})
        path = self.cache / family / (key + '.json')
        payload = ({'symbol': 'EXAMPLE', 'bars_ref': {'sha256': 'a' * 64, 'series_id': 1}}
                   if family == 'series' else
                   {'market_cap_quality': 'completed_close_estimate', 'market_cap': 100,
                    'market_cap_price_source': 'fixture://completed-close'})
        value = {'key': key, 'observed_at': observed, 'payload': payload, 'hash': digest(payload)}
        if family == 'series':
            value['ohlcv_path'] = str(self.root / 'runtime' / 'ohlcv.sqlite3')
        return path, json.dumps(value, ensure_ascii=False, indent=2).encode()

    def write(self, path, raw):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)

    def test_existing_database_only_and_strict_scope(self):
        path, raw = self.packet()
        self.assertFalse(save_checkpoint(path, raw))
        self.assertFalse(self.store.path.exists())
        self.initialize()
        standalone = self.root / 'market-cache' / 'series' / path.name
        self.assertFalse(save_checkpoint(standalone, raw))
        with self.assertRaisesRegex(ValueError, 'scope_invalid'):
            compact(self.root / 'runtime' / 'dashboard' / 'collection' / 'cache')
        with self.assertRaisesRegex(ValueError, 'source_invalid'):
            save_checkpoint(path.with_name('unrelated.json'), raw)

    def test_direct_database_roundtrip_preserves_original_bytes(self):
        self.initialize()
        for family in ('series', 'cap-prices'):
            path, raw = self.packet(family=family)
            self.assertTrue(save_checkpoint(path, raw))
            self.assertFalse(path.exists())
            self.assertFalse(path.parent.exists())
            self.assertTrue(available(path))
            self.assertEqual(read_bytes(path), raw)
            reference = self.store.lookup(path)
            self.assertEqual(reference['kind'], 'collection_checkpoint')
            self.assertEqual(reference['sha256'], hashlib.sha256(raw).hexdigest())

    def test_refresh_keeps_physical_and_database_versions(self):
        self.initialize()
        path, old = self.packet()
        _, current = self.packet(observed='2026-09-12T01:00:00+00:00')
        self.write(path, old)
        self.assertTrue(save_checkpoint(path, current))
        self.assertFalse(path.exists())
        self.assertEqual(read_bytes(path), current)
        old_ref = self.store.lookup(path, expected_sha=hashlib.sha256(old).hexdigest())
        self.assertEqual(self.store.read(old_ref), old)
        _, newest = self.packet(observed='2026-09-12T02:00:00+00:00')
        self.assertTrue(save_checkpoint(path, newest))
        self.assertEqual(read_bytes(path), newest)
        self.assertEqual(self.store.read(old_ref), old)

    def test_compaction_is_opt_in_exact_and_skips_inline_bars(self):
        self.initialize()
        expected = [self.packet(), self.packet(family='cap-prices')]
        for path, raw in expected:
            self.write(path, raw)
        legacy_path, legacy_raw = self.packet(number=2)
        legacy = json.loads(legacy_raw)
        legacy['payload']['bars'] = [{'date': '2026-09-11', 'close': 100}]
        legacy['hash'] = digest(legacy['payload'])
        legacy_raw = json.dumps(legacy).encode()
        self.write(legacy_path, legacy_raw)
        universe = self.cache / 'universe' / 'original.json'
        self.write(universe, b'original-universe')
        before = self.store.path.read_bytes()
        preview = compact(self.cache)
        self.assertEqual((preview['files'], preview['skipped_inline'], preview['removed']), (2, 1, 0))
        self.assertEqual(self.store.path.read_bytes(), before)
        result = compact(self.cache, apply=True)
        self.assertEqual(result['manifest_sha256'], preview['manifest_sha256'])
        self.assertEqual(result['removed'], 2)
        for path, raw in expected:
            self.assertFalse(path.exists())
            self.assertTrue(available(path))
            self.assertEqual(read_bytes(path), raw)
        self.assertEqual(legacy_path.read_bytes(), legacy_raw)
        self.assertEqual(universe.read_bytes(), b'original-universe')
        self.assertEqual(compact(self.cache, apply=True)['removed'], 0)

    def test_changed_invalid_and_shadowed_sources_are_not_removed(self):
        self.initialize()
        path, raw = self.packet()
        bad = json.loads(raw)
        bad['hash'] = '0' * 64
        self.write(path, json.dumps(bad).encode())
        with self.assertRaisesRegex(ValueError, 'packet_invalid'):
            compact(self.cache, apply=True)
        self.assertTrue(path.is_file())
        self.write(path, raw)
        _, other = self.packet(observed='2026-09-12T01:00:00+00:00')
        self.store.put(path, other, root=self.root, kind='collection_checkpoint')
        with self.assertRaisesRegex(ValueError, 'existing_version_conflict'):
            compact(self.cache, apply=True)
        self.assertEqual(path.read_bytes(), raw)

    def test_database_failure_or_busy_collector_preserves_files(self):
        self.initialize()
        path, raw = self.packet()
        self.write(path, raw)
        with patch.object(RecordStore, '_insert_version', side_effect=OSError('fixture-disk-failure')):
            with self.assertRaises(OSError):
                compact(self.cache, apply=True)
        self.assertEqual(path.read_bytes(), raw)
        with instance_lock(self.cache):
            with self.assertRaises((ValueError, RuntimeError, OSError)):
                compact(self.cache, apply=True)
        self.assertEqual(path.read_bytes(), raw)

    def test_failed_verification_keeps_files_and_committed_copy_can_resume(self):
        self.initialize()
        path, raw = self.packet()
        self.write(path, raw)
        with patch('records.checkpoints._verify', side_effect=ValueError('fixture-verification-failure')):
            with self.assertRaisesRegex(ValueError, 'fixture-verification-failure'):
                compact(self.cache, apply=True)
        self.assertEqual(path.read_bytes(), raw)
        self.assertEqual(self.store.read(path), raw)
        self.assertEqual(compact(self.cache, apply=True)['removed'], 1)
        self.assertEqual(read_bytes(path), raw)

    def test_bulk_uses_one_writer_and_bounded_routing_checks(self):
        self.initialize()
        expected = [self.packet(i, family='series' if i % 2 else 'cap-prices') for i in range(20)]
        for path, raw in expected:
            self.write(path, raw)
        original = RecordStore.connect
        calls = []
        def counted(store, *, write=False):
            calls.append(write)
            return original(store, write=write)
        started = time.perf_counter()
        with patch.object(RecordStore, 'connect', counted), patch.object(
                checkpoints, 'runtime_lookup_paths', wraps=checkpoints.runtime_lookup_paths) as aliases:
            preview = compact(self.cache)
            self.assertEqual(calls, [False])
            self.assertEqual(aliases.call_count, 1)
            calls.clear()
            result = compact(self.cache, apply=True)
            self.assertEqual(calls.count(True), 1)
            self.assertLessEqual(len(calls), 5)
            self.assertEqual(aliases.call_count, 2)
        self.assertEqual(result['removed'], 20)
        self.assertEqual(result['manifest_sha256'], preview['manifest_sha256'])
        for path, raw in expected:
            self.assertEqual(read_bytes(path), raw)
        print(f'checkpoint bulk fixture: 20 files, {len(calls)} DB connections, '
              f'{time.perf_counter() - started:.3f}s including dry-run and restored-byte checks')

    def test_alias_latest_conflict_and_blob_corruption_preserve_sources(self):
        self.initialize()
        old_root = self.root / 'previous-workbench'
        (self.root / 'config.json').write_text(json.dumps({'records': {'previous_root': str(old_root)}}))
        path, raw = self.packet()
        _, other = self.packet(observed='2026-09-12T01:00:00+00:00')
        old_path = old_root / path.relative_to(self.root)
        self.store.put(old_path, other, root=old_root, kind='collection_checkpoint')
        self.write(path, raw)
        with self.assertRaisesRegex(ValueError, 'existing_version_conflict'):
            compact(self.cache, root=self.root, apply=True)
        self.assertEqual(path.read_bytes(), raw)
        self.write(path, other)
        with closing(self.store.connect(write=True)) as db, db:
            db.execute('UPDATE record_blobs SET payload=? WHERE sha256=?',
                       (b'broken-compression', hashlib.sha256(other).hexdigest()))
        with self.assertRaisesRegex(ValueError, 'roundtrip_failed'):
            compact(self.cache, root=self.root, apply=True)
        self.assertEqual(path.read_bytes(), other)


class DatabaseCollectorResumeTests(unittest.TestCase):
    def setUp(self):
        # Reuse the provider fixture, not its inherited test methods.
        self.fixture = provider_fixture.TossMarketPipelineTests(
            'test_kr_and_us_use_toss_and_completed_series_are_cached')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.fixture.root = (self.root / 'runtime' / '2026-09-12' / '080000-fixture' /
                             'collection' / 'cache')
        self.fixture.config['ohlcv_path'] = str(self.root / 'runtime' / 'ohlcv.sqlite3')
        self.store = RecordStore(default_path(self.root))
        self.store.put(self.root / 'runtime' / 'fixture.json', b'{}', root=self.root)

    def test_resume_reuses_db_series_and_cap_without_provider_queries(self):
        for row in self.fixture.config['universe']:
            row['quote_time_state'] = 'not_provided'
        cap = {'market_cap_quality': 'completed_close_estimate', 'market_cap': 100,
               'market_cap_price_source': 'fixture://completed-close'}
        with patch.object(collection, 'complete_cap', return_value=cap) as complete:
            first = self.fixture.run_collect()
            self.assertEqual(first['collection_stats']['received'], 2)
            self.assertEqual(self.fixture.candles.call_count, 4)
            self.assertEqual(complete.call_count, 2)
            second = self.fixture.run_collect()
            self.assertEqual(second['collection_stats']['cache_hits'], 2)
            self.assertEqual(self.fixture.candles.call_count, 4)
            self.assertEqual(complete.call_count, 2)
            self.assertFalse(list(self.fixture.root.glob('series/*.json')))
            self.assertFalse(list(self.fixture.root.glob('cap-prices/*.json')))

    def test_db_checkpoint_refresh_is_versioned_and_then_reused(self):
        self.fixture.run_collect()
        with closing(self.store.connect()) as db:
            before = db.execute("SELECT COUNT(*) FROM record_versions WHERE kind='collection_checkpoint'").fetchone()[0]
        later = self.fixture.now + timedelta(minutes=1)
        self.fixture.run_collect(now=later, refresh=True)
        self.assertEqual(self.fixture.candles.call_count, 8)
        with closing(self.store.connect()) as db:
            after = db.execute("SELECT COUNT(*) FROM record_versions WHERE kind='collection_checkpoint'").fetchone()[0]
        self.assertEqual(after, before * 2)
        resumed = self.fixture.run_collect(now=later)
        self.assertEqual(resumed['collection_stats']['cache_hits'], 2)
        self.assertEqual(self.fixture.candles.call_count, 8)


if __name__ == '__main__':
    unittest.main()
