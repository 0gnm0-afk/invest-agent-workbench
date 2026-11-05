from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from records.store import RecordStore, default_path


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def raw_serializer(*args):
    return None


class RecordStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1] / 'runtime')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.runtime = self.root / 'runtime'
        self.runtime.mkdir()
        self.store = RecordStore(self.runtime / 'records.sqlite3')
        self.run = self.runtime / '2026-10-16' / 'saved-run'
        self.run.mkdir(parents=True)
        self.outputs = {}
        self.write('run.json', {'state': 'partial', 'processing_state': 'completed_with_exceptions',
                               'mode': 'replay', 'finished_at': '2026-10-16T00:00:00+00:00',
                               'data_times': {'fetched_at': '2026-10-15T22:00:00+00:00'}})
        self.write('screen.json', {'rows': [{'market': 'US', 'symbol': 'AAPL', 'state': 'candidate'}]})
        self.write('quality.json', {'data_complete': False})
        self.manifest()

    def write(self, name, value):
        path = self.run / name
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = json.dumps(value, ensure_ascii=False, indent=2).encode('utf-8')
        path.write_bytes(raw)
        self.outputs[name] = sha(raw)
        return raw

    def manifest(self):
        (self.run / 'manifest.json').write_text(json.dumps({
            'schema': 1, 'input_hashes': {'outside-source': 'original-hash'},
            'code_hashes': {'policy-version': 'kept-as-observed'}, 'output_hashes': self.outputs}), encoding='utf-8')

    def publish(self):
        return self.store.publish_run(self.run, root=self.root, serializer=raw_serializer)

    def count(self, table):
        with closing(self.store.connect()) as db:
            return db.execute('SELECT count(*) FROM ' + table).fetchone()[0]

    def test_read_only_missing_does_not_create_database_and_configured_path(self):
        self.assertIsNone(self.store.lookup(self.run / 'run.json'))
        self.assertIsNone(self.store.read(self.run / 'run.json'))
        self.assertEqual(self.store.subjects('US', 'AAPL'), [])
        self.assertFalse(self.store.path.exists())
        self.assertEqual(default_path(self.root), self.runtime / 'records.sqlite3')
        (self.root / 'config.json').write_text(json.dumps({'records': {'path': 'runtime/other.sqlite3'}}))
        self.assertEqual(default_path(self.root), self.runtime / 'other.sqlite3')

    def test_publish_and_restore_without_source_files_or_live_connections(self):
        raw = (self.run / 'screen.json').read_bytes()
        receipt = self.publish()
        self.assertEqual(receipt['state'], 'partial')
        self.assertEqual(receipt['artifact_count'], 4)
        refs = self.store.artifacts(self.run)
        self.assertEqual({r['role'] for r in refs}, {'manifest.json', 'run.json', 'screen.json', 'quality.json'})
        reference = self.store.reference(self.run / 'screen.json', sha(raw))
        self.assertEqual(reference['path'], str(self.store.path))
        (self.run / 'screen.json').unlink()
        self.assertEqual(self.store.read(reference), raw)
        self.assertEqual(self.store.read(self.run / 'screen.json'), raw)
        self.assertEqual(self.store.runs()[0]['data_times']['fetched_at'], '2026-10-15T22:00:00+00:00')
        self.assertEqual(self.count('record_subjects'), 1)

    def test_idempotent_publication_reads_no_outputs_after_thin_publication(self):
        self.publish()
        count = self.count('record_versions')
        for role in ('run.json', 'screen.json', 'quality.json'):
            (self.run / role).write_text('{"record_artifact_ref":"fixture"}')
        again = self.publish()
        self.assertTrue(again['idempotent'])
        self.assertEqual(self.count('record_versions'), count)
        self.outputs['run.json'] = '0' * 64
        self.manifest()
        with self.assertRaisesRegex(ValueError, 'published_run_conflict'):
            self.publish()

    def test_simultaneous_publication_commits_one_run_and_one_version_per_role(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.publish(), range(4)))
        self.assertEqual(sum(not item['idempotent'] for item in results), 1)
        self.assertEqual(self.count('record_runs'), 1)
        self.assertEqual(self.count('record_versions'), 4)
        self.assertEqual(self.count('record_run_artifacts'), 4)

    def test_initialization_lock_retries_but_other_failure_closes_connection(self):
        connection = sqlite3.connect(self.store.path)
        proxy = Mock(wraps=connection)
        attempts = []

        def execute(sql, *args):
            if sql == 'PRAGMA journal_mode=WAL':
                attempts.append(sql)
                if len(attempts) == 1:
                    raise sqlite3.OperationalError('database is locked')
            return connection.execute(sql, *args)

        proxy.execute.side_effect = execute
        with patch('records.store.sqlite3.connect', return_value=proxy):
            with closing(self.store.connect(write=True)):
                pass
        self.assertEqual(len(attempts), 2)
        proxy.close.assert_called_once()

        connection = sqlite3.connect(self.store.path)
        proxy = Mock(wraps=connection)

        def broken(sql, *args):
            if sql == 'PRAGMA journal_mode=WAL':
                raise sqlite3.OperationalError('fixture storage failure')
            return connection.execute(sql, *args)

        proxy.execute.side_effect = broken
        with patch('records.store.sqlite3.connect', return_value=proxy):
            with self.assertRaisesRegex(sqlite3.OperationalError, 'storage failure'):
                self.store.connect(write=True)
        proxy.close.assert_called_once()

    def test_same_content_deduplicates_and_old_path_versions_stay_pinned(self):
        first = self.store.put(self.runtime / 'policy.json', b'{"v":1}', root=self.root)
        second = self.store.put(self.runtime / 'policy.json', b'{"v":2}', root=self.root)
        self.store.put(self.runtime / 'other-policy.json', b'{"v":1}', root=self.root)
        self.assertEqual(self.count('record_blobs'), 2)
        self.assertEqual(self.store.read(first), b'{"v":1}')
        self.assertEqual(self.store.read(second), b'{"v":2}')
        self.assertEqual(self.store.read(self.runtime / 'policy.json'), b'{"v":2}')
        with self.assertRaisesRegex(ValueError, 'hash_mismatch'):
            self.store.read(self.runtime / 'policy.json', '0' * 64)

    def test_subject_lookup_keeps_versions_row_numbers_and_exact_identity(self):
        path = self.runtime / 'screen.json'
        old_raw = json.dumps({'rows': [{'market': 'US', 'symbol': 'AAA'},
                                      {'market': 'KR', 'symbol': 'AAA'},
                                      {'market': 'US', 'symbol': 'AAA'}]}).encode()
        old = self.store.put(path, old_raw, kind='screen.json', root=self.root)
        new_raw = json.dumps({'rows': [{'market': 'US', 'symbol': 'OTHER'},
                                      {'market': 'US', 'symbol': 'AAA'}]}).encode()
        new = self.store.put(path, new_raw, kind='screen.json', root=self.root)
        hits = self.store.subjects('US', 'AAA', kind='screen.json')
        self.assertEqual([(r['version_id'], r['row_number']) for r in hits],
                         [(old['version_id'], 0), (old['version_id'], 2), (new['version_id'], 1)])
        self.assertTrue(all(not r['run_paths'] for r in hits))
        self.assertEqual(self.store.read(hits[0]), old_raw)
        self.assertEqual(self.store.read(hits[-1]), new_raw)
        self.assertEqual(len(self.store.subjects('KR', 'AAA')), 1)
        self.assertEqual(self.store.subjects('US', 'aaa'), [])
        self.assertEqual(self.store.subjects('US', 'AAA', kind='quality.json'), [])

    def test_malformed_output_hash_and_missing_file_roll_back_whole_run(self):
        self.outputs['screen.json'] = '0' * 64
        self.manifest()
        with self.assertRaisesRegex(ValueError, 'output_hash_mismatch:screen.json'):
            self.publish()
        self.assertEqual(self.count('record_runs'), 0)
        self.assertEqual(self.count('record_versions'), 0)
        self.assertEqual(self.count('record_blobs'), 0)
        self.outputs['screen.json'] = sha((self.run / 'screen.json').read_bytes())
        self.manifest()
        (self.run / 'quality.json').unlink()
        with self.assertRaises(FileNotFoundError):
            self.publish()
        self.assertEqual(self.count('record_runs'), 0)

    def test_path_traversal_and_external_source_and_nonterminal_are_rejected(self):
        self.outputs['../escape.json'] = '0' * 64
        self.manifest()
        with self.assertRaisesRegex(ValueError, 'outside_run'):
            self.publish()
        del self.outputs['../escape.json']
        self.write('run.json', {'state': 'running'})
        self.manifest()
        with self.assertRaisesRegex(ValueError, 'not_terminal'):
            self.publish()
        with self.assertRaisesRegex(ValueError, 'outside_runtime'):
            self.store.put(self.root / 'outside.json', b'{}', root=self.root)

    def test_price_arrays_require_verified_reference_recipe(self):
        bar = {'date': '2026-10-15', 'open': 1, 'high': 2, 'low': 1, 'close': 2, 'volume': 1}
        self.write('screen.json', {'rows': [{'bars': [bar]}]})
        self.manifest()
        with self.assertRaisesRegex(ValueError, 'price_arrays_require_reference:screen.json'):
            self.publish()
        self.assertEqual(self.count('record_versions'), 0)
        self.assertEqual(self.count('record_blobs'), 0)

    def test_snapshot_and_account_outputs_are_not_migrated_and_failures_remain_failures(self):
        self.write('snapshot.json', {'series': [{'bars': [{'date': 'x', 'open': 1, 'high': 1, 'low': 1, 'close': 1, 'volume': 1}]}]})
        self.write('account-snapshot.json', {'accounts': [{'positions': []}]})
        self.write('run.json', {'state': 'failed', 'processing_state': 'failed', 'finished_at': '2026-10-16',
                               'error': 'fixture failure'})
        self.manifest()
        self.assertEqual(self.publish()['state'], 'failed')
        self.assertEqual(self.store.runs()[0]['state'], 'failed')
        self.assertIsNone(self.store.runs()[0]['data_times'])
        self.assertIsNone(self.store.lookup(self.run / 'snapshot.json'))
        self.assertIsNone(self.store.lookup(self.run / 'account-snapshot.json'))

    def test_blob_tampering_reference_database_and_hash_are_rejected(self):
        reference = self.store.put(self.runtime / 'policy.json', b'{"v":1}', root=self.root)
        with self.assertRaisesRegex(ValueError, 'database_mismatch'):
            self.store.read({**reference, 'path': str(self.runtime / 'other.sqlite3')})
        with closing(self.store.connect(write=True)) as db, db:
            db.execute('UPDATE record_blobs SET payload=?', (b'not-zlib',))
        with self.assertRaisesRegex(ValueError, 'blob_invalid'):
            self.store.read(reference)

    def test_another_stores_database_is_not_modified(self):
        with closing(sqlite3.connect(self.store.path)) as db, db:
            db.execute('CREATE TABLE prices(value TEXT)')
        before = self.store.path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'another_store'):
            self.store.put(self.runtime / 'policy.json', b'{}', root=self.root)
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_encoded_artifact_verifies_both_payload_and_original_hash(self):
        raw = b'{"answer":42}'
        reference = self.store.put(self.runtime / 'recipe.json', raw, root=self.root,
            encoded={'encoding': 'fixture-recipe', 'payload': b'{"answer_ref":1}'})
        self.assertEqual(self.store.read(reference, decoder=lambda *args: raw), raw)
        with self.assertRaisesRegex(ValueError, 'source_integrity_failed'):
            self.store.read(reference, decoder=lambda *args: b'{}')

    def test_publish_default_price_recipe_and_restore_with_pinned_price_database(self):
        from market.prices import PriceStore
        from records.prices import completed_weeks_v1, ENCODING
        prices = PriceStore(self.runtime / 'ohlcv.sqlite3')
        bars = [dict(date='2026-10-16', open=10., high=12., low=9., close=11., volume=100.)]
        series = dict(market='US', symbol='AAA', currency='USD', provider='Toss OpenAPI candles',
                      price_basis='toss_adjusted_ohlc_no_order_authority', bars=bars)
        price_ref = prices.put(series, source='fixture', kind='series')
        source = self.runtime / 'source/snapshot.json'
        source.parent.mkdir()
        source.write_text(json.dumps({'ohlcv_store': {'path': str(prices.path)},
            'series': [{**{k: v for k, v in series.items() if k != 'bars'}, 'bars_ref': price_ref}]}), encoding='utf-8')
        self.write('run.json', {'state': 'partial', 'finished_at': '2026-10-16T22:00:00+00:00',
                               'market_source': str(source.parent)})
        raw = self.write('screen.json', {'rows': [{'market': 'US', 'symbol': 'AAA',
            'technical_context': {'daily_bars': bars, 'weekly_bars': completed_weeks_v1(bars, bars[-1]['date'])}}]})
        (self.run / 'manifest.json').write_text(json.dumps({'input_hashes': {
            str(source.resolve()): sha(source.read_bytes())}, 'output_hashes': self.outputs}), encoding='utf-8')
        self.store.publish_run(self.run, root=self.root)
        reference = self.store.lookup(self.run / 'screen.json')
        self.assertEqual(reference['encoding'], ENCODING)
        hits = self.store.subjects('US', 'AAA', kind='screen.json')
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]['sha256'], reference['sha256'])
        self.assertEqual(hits[0]['row_number'], 0)
        self.assertEqual(hits[0]['run_paths'], [str(self.run)])
        self.assertEqual(self.count('record_subjects'), 1)
        # Subject lookup uses metadata alone; a broken blob is detected only
        # when the caller explicitly asks to reconstruct that original artifact.
        with closing(self.store.connect(write=True)) as db, db:
            db.execute('UPDATE record_blobs SET payload=? WHERE sha256=?',
                       (b'not-zlib', reference['blob_sha256']))
        self.assertEqual(self.store.subjects('US', 'AAA')[0]['sha256'], reference['sha256'])
        with self.assertRaisesRegex(ValueError, 'blob_invalid'):
            self.store.read(hits[0])
        # Restore the fixture blob for the existing source-deletion roundtrip.
        from records.prices import encode
        import zlib
        payload = encode('screen.json', raw, self.run, self.root)['payload']
        with closing(self.store.connect(write=True)) as db, db:
            db.execute('UPDATE record_blobs SET payload=? WHERE sha256=?',
                       (zlib.compress(payload, 6), reference['blob_sha256']))
        (self.run / 'screen.json').unlink()
        self.assertEqual(self.store.read(reference), raw)


if __name__ == '__main__':
    unittest.main()
