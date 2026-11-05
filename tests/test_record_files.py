"""Record DB publication exercised through the actual saved-file consumers."""
from contextlib import closing, ExitStack
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import atomic_io
import market.daily as daily
from launch.daily import DailyLaunch
from market.prices import PriceStore
from records.files import MARKER, publish_thin
from records.prices import completed_weeks_v1
from records.store import RecordStore


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


class RecordFileIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1] / 'runtime')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.run = self.root / 'runtime/2026-10-16/run'
        self.run.mkdir(parents=True)
        self.store = RecordStore(self.root / 'runtime/records.sqlite3')
        self.prices = PriceStore(self.root / 'runtime/ohlcv.sqlite3')
        stack = ExitStack()
        self.addCleanup(stack.close)
        for target in ('socket.socket.connect', 'socket.create_connection', 'urllib.request.urlopen'):
            stack.enter_context(patch(target, side_effect=AssertionError('record_fixture_network_forbidden')))
        price_connect = PriceStore.connect
        record_connect = RecordStore.connect

        def guarded(original):
            def connect(store, **kwargs):
                if not store.path.resolve().is_relative_to(self.root):
                    raise AssertionError('record_fixture_production_database_forbidden')
                return original(store, **kwargs)
            return connect

        stack.enter_context(patch.object(PriceStore, 'connect', guarded(price_connect)))
        stack.enter_context(patch.object(RecordStore, 'connect', guarded(record_connect)))
        self.bars = [dict(date=day, open=10., high=12., low=9., close=11., volume=100.)
                     for day in ('2026-10-12', '2026-10-13', '2026-10-14', '2026-10-15', '2026-10-16')]
        series = dict(market='US', symbol='AAA', currency='USD', provider='Toss OpenAPI candles',
                      price_basis='toss_adjusted_ohlc_no_order_authority', bars=self.bars)
        reference = self.prices.put(series, source='fixture', kind='series')
        self.source = self.root / 'runtime/2026-10-16/source/snapshot.json'
        self.source.parent.mkdir()
        self.source.write_text(json.dumps({'ohlcv_store': {'path': str(self.prices.path)},
            'series': [{**{k: v for k, v in series.items() if k != 'bars'}, 'bars_ref': reference}]}), encoding='utf-8')
        context = {k: v for k, v in series.items() if k != 'bars'}
        context.update(daily_bars=self.bars, weekly_bars=completed_weeks_v1(self.bars, self.bars[-1]['date']))
        self.values = {
            'run.json': {'state': 'partial', 'processing_state': 'completed_with_exceptions',
                         'finished_at': '2026-10-16T22:00:00+00:00', 'market_source': str(self.source.parent)},
            'screen.json': {'rows': [{'market': 'US', 'symbol': 'AAA', 'technical_context': context}]},
            'priority-review.json': {'schema': 4, 'candidate_count': 1},
            'quality.json': {'data_complete': False, 'memo': '원문 보존'},
        }
        self.originals = {}
        for role, value in self.values.items():
            raw = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode('utf-8')
            (self.run / role).write_bytes(raw)
            self.originals[role] = raw
        self.manifest = {'schema': 1, 'input_hashes': {str(self.source): sha(self.source.read_bytes())},
                         'output_hashes': {role: sha(raw) for role, raw in self.originals.items()}}
        (self.run / 'manifest.json').write_text(json.dumps(self.manifest), encoding='utf-8')

    def publish(self):
        return self.store.publish_run(self.run, root=self.root)

    def count_versions(self):
        with closing(self.store.connect()) as db:
            return db.execute('SELECT count(*) FROM record_versions').fetchone()[0]

    def test_new_thin_run_preserves_hashes_prices_and_launcher_reads(self):
        before = sha(self.prices.path.read_bytes())
        self.publish()
        self.assertEqual(publish_thin(self.run, root=self.root, store=self.store), len(self.originals))
        for role, raw in self.originals.items():
            path = self.run / role
            self.assertEqual(set(json.loads(path.read_bytes())), {MARKER})
            self.assertEqual(atomic_io.read_bytes(path), raw)
            self.assertEqual(atomic_io.sha(path), sha(raw))
            self.assertEqual(atomic_io.checked_saved(path, sha(raw)), self.values[role])
        self.assertEqual(atomic_io.load(self.run / 'screen.json'), self.values['screen.json'])
        launcher = DailyLaunch(self.root, accounts=False)
        self.assertEqual(launcher.checked_run(self.run), self.values['run.json'])
        self.assertEqual(launcher.usable_run(self.run), str(self.run))
        self.assertEqual(sha(self.prices.path.read_bytes()), before)

    def test_migrated_missing_original_recovers_but_existing_corruption_is_not_bypassed(self):
        self.publish()
        path = self.run / 'quality.json'
        self.assertEqual(path.read_bytes(), self.originals['quality.json'])  # Migration leaves originals alone.
        path.unlink()
        self.assertTrue(atomic_io.available(path))
        self.assertEqual(atomic_io.read_bytes(path), self.originals['quality.json'])
        self.assertEqual(atomic_io.sha(path), self.manifest['output_hashes']['quality.json'])
        path.write_bytes(b'{"data_complete":true}')
        self.assertEqual(atomic_io.load(path), {'data_complete': True})
        with self.assertRaisesRegex(ValueError, 'saved_artifact_hash_mismatch'):
            atomic_io.checked_saved(path, self.manifest['output_hashes']['quality.json'])

    def test_thin_reference_path_database_and_hash_tampering_are_rejected(self):
        self.publish()
        publish_thin(self.run, root=self.root, store=self.store)
        path = self.run / 'quality.json'
        original = json.loads(path.read_bytes())
        for field, changed, message in (
            ('source_path', str(self.run / 'run.json'), 'record_reference_path_mismatch'),
            ('path', str(self.root / 'runtime/missing.sqlite3'), 'record_reference_missing'),
            ('sha256', '0' * 64, 'record_reference_hash_mismatch'),
        ):
            with self.subTest(field=field):
                value = json.loads(json.dumps(original))
                value[MARKER][field] = changed
                path.write_text(json.dumps(value), encoding='utf-8')
                with self.assertRaisesRegex(ValueError, message):
                    atomic_io.read_bytes(path)

    def test_partial_thin_publication_can_resume_without_new_versions(self):
        self.publish()
        count = self.count_versions()
        atomic_write = atomic_io.atomic_write
        writes = []

        def interrupted(path, text):
            if writes:
                raise OSError('fixture publication interruption')
            atomic_write(path, text)
            writes.append(path)

        with patch.object(atomic_io, 'atomic_write', side_effect=interrupted):
            with self.assertRaisesRegex(OSError, 'fixture publication interruption'):
                publish_thin(self.run, root=self.root, store=self.store)
        self.assertEqual(len(writes), 1)
        for role, raw in self.originals.items():
            self.assertEqual(atomic_io.read_bytes(self.run / role), raw)
        self.assertTrue(self.publish()['idempotent'])
        self.assertEqual(publish_thin(self.run, root=self.root, store=self.store), len(self.originals))
        self.assertEqual(self.count_versions(), count)

    def test_daily_record_failure_does_not_publish_latest_or_collect(self):
        latest = self.root / 'runtime/latest.json'
        prior = b'{"output":"previous-saved-run","state":"partial"}'
        latest.write_bytes(prior)
        with patch.object(daily, 'ROOT', self.root), patch.object(daily, 'code_hashes', return_value={}), \
             patch.object(daily, 'seed_check', side_effect=ValueError('fixture invalid seed')), \
             patch.object(daily, 'collect') as collector, \
             patch('records.store.publish_run', side_effect=OSError('fixture record write failed')):
            with self.assertRaisesRegex(OSError, 'fixture record write failed'):
                daily.run({'engine': str(self.root / 'engine')})
        collector.assert_not_called()
        self.assertEqual(latest.read_bytes(), prior)
        generated = [p for p in (self.root / 'runtime').glob('????-??-??/*/run.json') if p.parent != self.run]
        self.assertEqual(len(generated), 1)
        self.assertEqual(json.loads(generated[0].read_bytes())['state'], 'failed')


if __name__ == '__main__':
    unittest.main()
