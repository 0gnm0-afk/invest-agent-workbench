from contextlib import closing
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

from atomic_io import read_bytes
from market.history.collect import Collector, packed, read_checked, save_checked, unpack_checked
from market.history.saved import SavedHistory
from market.history.verify import verify
from records.files import available
from records.history_series import save_series_checkpoint
from records.store import RecordStore
import records.history_series as history_series


def candle(day='2026-09-30', market='US'):
    return dict(timestamp=day + ('T00:00:00+09:00' if market == 'KR' else 'T14:00:00+09:00'),
                currency='KRW' if market == 'KR' else 'USD',
                openPrice='10', highPrice='12', lowPrice='9', closePrice='11', volume='100')


class HistorySeriesRecordTests(unittest.TestCase):
    def setUp(self):
        # These tests exercise records DB recovery. Keep the unrelated legacy
        # price-file recipe fallback from opening the operating OHLCV database.
        for target in ('market.history.collect.available', 'market.history.verify.available'):
            guard = patch(target, lambda path: Path(path).is_file())
            guard.start()
            self.addCleanup(guard.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.collection = self.root / 'runtime/us-history/fixture'
        self.collection.mkdir(parents=True)
        self.store = RecordStore(self.root / 'runtime/records.sqlite3')
        self.item = {'symbol': 'AAA', 'market': 'US'}
        self.path = self.collection / 'series' / (hashlib.sha256(b'AAA').hexdigest() + '.json')
        self.end = '2026-09-30'

    def initialize(self):
        with closing(self.store.connect(write=True)):
            pass

    def good(self, *_):
        return {'result': {'candles': [candle()], 'nextBefore': None}}

    def test_kr_us_no_loose_series_normal_resume_verify_and_saved_reader(self):
        self.initialize()
        for market in ('US', 'KR'):
            with self.subTest(market=market):
                collection = self.root / ('runtime/' + market.lower() + '-history/fixture')
                collection.mkdir(parents=True, exist_ok=True)
                item = {'symbol': 'AAA' if market == 'US' else '005930.KS', 'market': market}
                calls = []
                def fake(*args):
                    calls.append(args)
                    return {'result': {'candles': [candle(market=market)], 'nextBefore': None}}
                collector = Collector(collection, fake, market=market)
                result = collector.collect_one(item, self.end)
                path = collection / 'series' / (hashlib.sha256(item['symbol'].encode()).hexdigest() + '.json')
                self.assertFalse(path.exists())
                self.assertFalse(path.parent.exists())
                self.assertTrue(available(path))
                self.assertEqual(read_checked(path), result)
                self.assertEqual(collector.collect_one(item, self.end), result)
                self.assertEqual(len(calls), 1)
                self.assertNotIn('bars', json.loads(read_bytes(path))['data'])
                save_checked(collection / 'collection.json', {'markets': [market], 'end': self.end, 'scope': 'fixture'})
                save_checked(collection / 'universe.json', {'rows': [item]})
                audit = verify(collection, require_finished=True)
                self.assertEqual(audit['counts'], {'received': 1})
                manifest = {key.replace('\\', '/'): value for key, value in
                            json.loads((collection / 'manifest.json').read_bytes()).items()}
                self.assertEqual(manifest['series/' + path.name], hashlib.sha256(read_bytes(path)).hexdigest())
                pointer = collection.parent / 'active.json'
                pointer.write_text(json.dumps({'collection': collection.name, 'manifest_sha256':
                    hashlib.sha256((collection / 'manifest.json').read_bytes()).hexdigest()}))
                saved = SavedHistory(pointer, market)
                self.assertFalse(saved.error)
                self.assertEqual(saved.read('series/' + path.name, wrapped=True), result)

    def test_empty_failure_retry_preserves_old_bytes_and_completed_value(self):
        self.initialize()
        def failure(*_):
            raise ValueError('fixture-failure')
        collector = Collector(self.collection, failure)
        failed = collector.collect_one(self.item, self.end)
        self.assertEqual((failed['state'], failed['bars']), ('failed', []))
        old = read_bytes(self.path)
        self.assertEqual(collector.collect_one(self.item, self.end), failed)
        collector.client = self.good
        result = collector.collect_one(self.item, self.end, retry=True)
        self.assertEqual(result['state'], 'received')
        reference = self.store.lookup(self.path, expected_sha=hashlib.sha256(old).hexdigest())
        self.assertEqual(self.store.read(reference), old)
        self.assertEqual(unpack_checked(json.loads(old)), failed)
        self.assertEqual(read_checked(self.path), result)
        self.assertFalse(self.path.exists())
        with closing(self.store.connect()) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM record_versions WHERE kind='collection_checkpoint'").fetchone()[0], 2)
        empty = Collector(self.collection, lambda *_: {'result': {'candles': [], 'nextBefore': None}})
        empty_value = empty.collect_one({'symbol': 'EMPTY', 'market': 'US'}, self.end)
        empty_path = self.path.with_name(hashlib.sha256(b'EMPTY').hexdigest() + '.json')
        self.assertEqual(empty_value['state'], 'empty')
        self.assertFalse(empty_path.exists())
        self.assertEqual(read_checked(empty_path), empty_value)

    def test_partial_failure_physical_thin_retry_preserves_previous_packet(self):
        calls = []
        def partial(_, params):
            calls.append(params)
            if len(calls) == 1:
                return {'result': {'candles': [candle()], 'nextBefore': '2026-09-29T14:00:00+09:00'}}
            raise ValueError('fixture-page-failure')
        collector = Collector(self.collection, partial)
        failed = collector.collect_one(self.item, self.end)
        self.assertEqual((failed['state'], len(failed['bars'])), ('failed', 1))
        original = self.path.read_bytes()
        self.assertNotIn('bars', json.loads(original)['data'])
        self.initialize()
        collector.client = lambda *_: {'result': {'candles': [candle('2026-09-29')], 'nextBefore': None}}
        result = collector.collect_one(self.item, self.end, retry=True)
        self.assertEqual((result['state'], len(result['bars'])), ('received', 2))
        self.assertFalse(self.path.exists())
        previous = self.store.lookup(self.path, expected_sha=hashlib.sha256(original).hexdigest())
        self.assertEqual(self.store.read(previous), original)
        self.assertEqual(unpack_checked(json.loads(original)), failed)
        self.assertEqual(read_checked(self.path), result)

    def test_no_database_and_standalone_keep_file_format(self):
        for location in (self.collection, self.root / 'standalone'):
            with self.subTest(location=str(location)):
                result = Collector(location, self.good).collect_one(self.item, self.end)
                path = location / 'series' / self.path.name
                self.assertTrue(path.is_file())
                self.assertEqual(read_checked(path), result)
                self.assertFalse(self.store.path.exists())

    def test_inline_previous_packet_is_never_removed_or_overwritten(self):
        self.initialize()
        result = Collector(self.collection, self.good).collect_one(self.item, self.end)
        current = read_bytes(self.path)
        original = packed({'sha256': hashlib.sha256(packed(result)).hexdigest(), 'data': result})
        self.path.parent.mkdir()
        self.path.write_bytes(original)
        with self.assertRaisesRegex(ValueError, 'inline_source_preserved'):
            save_series_checkpoint(self.path, current)
        self.assertEqual(self.path.read_bytes(), original)

    def test_verification_failure_and_changed_physical_thin_are_preserved(self):
        self.initialize()
        Collector(self.collection, self.good).collect_one(self.item, self.end)
        raw = read_bytes(self.path)
        self.path.parent.mkdir()
        self.path.write_bytes(raw)
        with patch.object(history_series, '_verify', side_effect=ValueError('fixture-verification-failure')):
            with self.assertRaisesRegex(ValueError, 'fixture-verification-failure'):
                save_series_checkpoint(self.path, raw)
        self.assertEqual(self.path.read_bytes(), raw)
        original_verify = history_series._verify
        def changed(*args):
            original_verify(*args)
            self.path.write_bytes(b'changed-by-another-writer')
        with patch.object(history_series, '_verify', side_effect=changed):
            with self.assertRaisesRegex(ValueError, 'source_changed'):
                save_series_checkpoint(self.path, raw)
        self.assertEqual(self.path.read_bytes(), b'changed-by-another-writer')
        self.assertEqual(self.store.read(self.path), raw)

    def test_identity_hash_and_link_fail_before_publication(self):
        self.initialize()
        Collector(self.collection, self.good).collect_one(self.item, self.end)
        raw = read_bytes(self.path)
        bad = deepcopy(json.loads(raw))
        bad['sha256'] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'packet_invalid'):
            save_series_checkpoint(self.path, packed(bad))
        other = self.path.with_name('0' * 64 + '.json')
        with self.assertRaisesRegex(ValueError, 'packet_invalid'):
            save_series_checkpoint(other, raw)
        original_junction = Path.is_junction
        with patch.object(Path, 'is_junction', lambda p: p == self.path.parent or original_junction(p)):
            with self.assertRaisesRegex(ValueError, 'link_forbidden'):
                save_series_checkpoint(self.path, raw)
        self.assertEqual(self.store.read(self.path), raw)


if __name__ == '__main__':
    unittest.main()
