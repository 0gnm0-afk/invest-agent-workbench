"""Exact source preservation and offline reuse after retiring loose responses."""
from contextlib import closing
import gzip
import hashlib
import json
import os
import stat as stat_types
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from atomic_io import read_bytes
from invest_agent.trading.store import instance_lock, BusyError
from market.history.collect import Collector, packed
from records.history import migrate, compact, save_response, _source
from records.store import RecordStore
import records.history as history


class HistoryRecordsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.collection = self.root / 'runtime/us-history/sample'
        (self.collection / 'responses').mkdir(parents=True)
        self.store = RecordStore(self.root / 'runtime/records.sqlite3')
        with closing(self.store.connect(write=True)):
            pass  # The importer must use an already existing records database.
        self.request = ['/api/v1/candles', {'symbol': 'AAA', 'interval': '1d'}]
        self.payload = {'result': {'candles': [], 'nextBefore': None}}
        self.name = hashlib.sha256(packed(self.request)).hexdigest() + '.json.gz'
        self.raw = gzip.compress(packed(dict(request=self.request, payload=self.payload,
            sha256=hashlib.sha256(packed(self.payload)).hexdigest(), fetched_at='2026-10-01T10:00:00+00:00')))
        self.path = self.collection / 'responses' / self.name
        self.path.write_bytes(self.raw)
        self.manifest = packed({'responses/' + self.name: hashlib.sha256(self.raw).hexdigest()})
        (self.collection / 'manifest.json').write_bytes(self.manifest)

    def add_sources(self, count):
        manifest = json.loads(self.manifest)
        for index in range(count - 1):
            request = [self.request[0], {**self.request[1], 'symbol': 'parallel-' + str(index)}]
            name = hashlib.sha256(packed(request)).hexdigest() + '.json.gz'
            raw = gzip.compress(packed(dict(request=request, payload=self.payload,
                sha256=hashlib.sha256(packed(self.payload)).hexdigest(), fetched_at='2026-10-01T10:00:00+00:00')))
            (self.collection / 'responses' / name).write_bytes(raw)
            manifest['responses/' + name] = hashlib.sha256(raw).hexdigest()
        (self.collection / 'manifest.json').write_bytes(packed(manifest))
        migrate(self.collection, root=self.root)

    def test_roundtrip_resume_and_collector_does_not_download_again(self):
        first = migrate(self.collection, root=self.root)
        self.assertEqual(first['imported'], 1)
        self.assertTrue(self.path.exists())
        self.assertEqual(compact(self.collection, root=self.root)['original_files_removed'], 0)
        result = compact(self.collection, root=self.root, apply=True)
        self.assertEqual(result['original_files_removed'], 1)
        self.assertFalse(self.path.exists())
        self.assertEqual(read_bytes(self.path), self.raw)
        with patch('market.history.collect.TossMarketClient', side_effect=AssertionError('network forbidden')):
            payload, key = Collector(self.collection).get(*self.request)
        self.assertEqual(payload, self.payload)
        self.assertEqual(key + '.json.gz', self.name)
        self.assertFalse(self.path.exists())
        self.assertEqual(migrate(self.collection, root=self.root)['reused'], 1)
        self.assertEqual(compact(self.collection, root=self.root, apply=True)['original_files_removed'], 0)
        self.assertEqual((self.collection / 'manifest.json').read_bytes(), self.manifest)

    def test_unlisted_original_remains_and_existing_corruption_is_not_hidden(self):
        extra = self.collection / 'responses' / ('0' * 64 + '.json.gz')
        extra.write_bytes(b'keep unlisted source')
        migrate(self.collection, root=self.root)
        self.path.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'original_changed'):
            compact(self.collection, root=self.root, apply=True)
        self.assertEqual(self.path.read_bytes(), b'changed')
        self.assertTrue(extra.exists())
        with self.assertRaises(Exception):
            Collector(self.collection).get(*self.request)

    def test_missing_database_entries_prevent_any_removal(self):
        migrate(self.collection, root=self.root)
        names = json.loads(self.manifest)
        names['responses/' + '1' * 64 + '.json.gz'] = '2' * 64
        (self.collection / 'manifest.json').write_bytes(packed(names))
        with self.assertRaisesRegex(ValueError, 'migration_incomplete'):
            compact(self.collection, root=self.root, apply=True)
        self.assertTrue(self.path.exists())

    def test_source_and_database_corruption_are_rejected(self):
        self.path.write_bytes(b'bad gzip')
        with self.assertRaisesRegex(ValueError, 'source_hash_mismatch'):
            migrate(self.collection, root=self.root)
        self.path.write_bytes(self.raw)
        migrate(self.collection, root=self.root)
        with closing(sqlite3.connect(self.store.path)) as db, db:
            db.execute("UPDATE record_blobs SET payload=?", (b'broken',))
        with self.assertRaises(Exception):
            compact(self.collection, root=self.root, apply=True)
        self.assertEqual(self.path.read_bytes(), self.raw)

    def test_packet_inner_hash_is_checked_even_with_matching_outer_hash(self):
        value = json.loads(gzip.decompress(self.raw)); value['sha256'] = '0' * 64
        raw = gzip.compress(packed(value)); self.path.write_bytes(raw)
        (self.collection / 'manifest.json').write_bytes(packed({'responses/' + self.name: hashlib.sha256(raw).hexdigest()}))
        with self.assertRaisesRegex(ValueError, 'packet_hash_mismatch'):
            migrate(self.collection, root=self.root)

    def test_scope_and_collector_lock_are_enforced(self):
        with self.assertRaisesRegex(ValueError, 'outside_runtime'):
            migrate(self.root, root=self.root)
        with instance_lock(self.collection / 'lock'):
            with self.assertRaises(BusyError):
                migrate(self.collection, root=self.root)

    def test_same_source_has_no_new_version_on_retry(self):
        migrate(self.collection, root=self.root)
        migrate(self.collection, root=self.root)
        with closing(self.store.connect()) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM record_versions').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT count(*) FROM record_blobs').fetchone()[0], 1)

    def test_missing_database_is_not_created(self):
        self.store.path.unlink()
        with self.assertRaisesRegex(ValueError, 'existing_database_required'):
            migrate(self.collection, root=self.root)
        self.assertFalse(self.store.path.exists())
        self.assertEqual(self.path.read_bytes(), self.raw)

    def test_preserved_original_cannot_gain_a_different_latest_version(self):
        migrate(self.collection, root=self.root)
        for kind, raw in (('artifact', b'changed'), ('artifact', self.raw), ('history_response', b'changed')):
            with self.subTest(kind=kind, raw=raw[:8]):
                with self.assertRaisesRegex(ValueError, 'immutable_conflict'):
                    self.store.put(self.path, raw, kind=kind, root=self.root)
        self.assertEqual(self.store.read(self.path), self.raw)
        with closing(self.store.connect()) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM record_versions').fetchone()[0], 1)

    def test_legacy_conflicting_kind_is_found_before_any_removal(self):
        migrate(self.collection, root=self.root)
        reference = self.store.put(self.root / 'runtime/other.json', b'{}', root=self.root)
        with closing(self.store.connect(write=True)) as db, db:
            db.execute('UPDATE record_versions SET source_path=?,source_key=? WHERE id=?',
                       (str(self.path), os.path.normcase(str(self.path)), reference['version_id']))
        with self.assertRaisesRegex(ValueError, 'database_conflict'):
            compact(self.collection, root=self.root, apply=True)
        self.assertEqual(self.path.read_bytes(), self.raw)

    def test_relocated_original_reuses_old_key_and_stays_immutable(self):
        migrate(self.collection, root=self.root)
        current = self.root / 'current'
        current.mkdir()
        (self.root / 'runtime').replace(current / 'runtime')
        (current / 'config.json').write_text(json.dumps({'records': {'previous_root': str(self.root)}}))
        collection = current / self.collection.relative_to(self.root)
        path = collection / 'responses' / self.name
        store = RecordStore(current / 'runtime/records.sqlite3')
        result = migrate(collection, root=current)
        self.assertEqual((result['imported'], result['reused']), (0, 1))
        with closing(store.connect()) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM record_versions').fetchone()[0], 1)
        with self.assertRaisesRegex(ValueError, 'immutable_conflict'):
            store.put(path, b'changed', root=current)
        reference = store.put(path, self.raw, kind='history_response', root=current)
        self.assertEqual(reference['source_path'], str(self.path))
        self.assertEqual(store.read(reference), self.raw)
        self.assertEqual(compact(collection, root=current, apply=True)['original_files_removed'], 1)
        with patch('runtime_paths.ROOT', current), patch('records.files.ROOT', current):
            self.assertEqual(read_bytes(path), self.raw)
            self.assertEqual(read_bytes(self.path), self.raw)
        self.assertEqual(migrate(collection, root=current)['reused'], 1)

    def test_same_size_same_timestamp_edit_is_not_deleted(self):
        migrate(self.collection, root=self.root)
        original_read = Path.read_bytes
        reads = []
        changed = bytes([self.raw[0] ^ 1]) + self.raw[1:]

        def competing_edit(path):
            raw = original_read(path)
            if path == self.path:
                reads.append(path)
                if len(reads) == 1:
                    stat = path.stat()
                    path.write_bytes(changed)
                    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
            return raw

        with patch.object(Path, 'read_bytes', competing_edit), \
             patch('records.windows_retire._WINDOWS', False):
            with self.assertRaisesRegex(ValueError, 'original_changed'):
                compact(self.collection, root=self.root, apply=True)
        self.assertEqual(self.path.read_bytes(), changed)

    def test_batch_interruption_preserves_originals_and_resumes_without_versions(self):
        manifest = {}
        for index in range(257):
            request = [self.request[0], {**self.request[1], 'symbol': str(index)}]
            name = hashlib.sha256(packed(request)).hexdigest() + '.json.gz'
            raw = gzip.compress(packed(dict(request=request, payload=self.payload,
                sha256=hashlib.sha256(packed(self.payload)).hexdigest(), fetched_at='2026-10-01T10:00:00+00:00')))
            (self.collection / 'responses' / name).write_bytes(raw)
            manifest['responses/' + name] = hashlib.sha256(raw).hexdigest()
        (self.collection / 'manifest.json').write_bytes(packed(manifest))
        with self.assertRaisesRegex(RuntimeError, 'fixture interrupted'):
            migrate(self.collection, root=self.root,
                    progress=lambda value: (_ for _ in ()).throw(RuntimeError('fixture interrupted')))
        with closing(self.store.connect()) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM record_versions').fetchone()[0], 256)
        self.assertTrue(all((self.collection / name).is_file() for name in manifest))
        result = migrate(self.collection, root=self.root)
        self.assertEqual((result['imported'], result['reused']), (1, 256))
        self.assertEqual(compact(self.collection, root=self.root, apply=True)['original_files_removed'], 257)
        self.assertEqual(self.path.read_bytes(), self.raw)  # Unmanifested original remains.

    def test_new_collector_response_uses_existing_db_without_loose_file_or_manifest(self):
        (self.collection / 'manifest.json').unlink()
        request = [self.request[0], {**self.request[1], 'symbol': 'NEW'}]
        client = Mock(return_value=self.payload)
        collector = Collector(self.collection, client=client)
        payload, key = collector.get(*request)
        self.assertEqual(payload, self.payload)
        path = self.collection / 'responses' / (key + '.json.gz')
        self.assertFalse(path.exists())
        raw = read_bytes(path)
        packet = json.loads(gzip.decompress(raw))
        self.assertEqual(packet['request'], request)
        self.assertEqual(packet['payload'], self.payload)
        self.assertEqual(collector.get(*request), (self.payload, key))
        client.assert_called_once_with(*request)
        self.assertTrue(save_response(self.collection, path, raw, root=self.root))
        with closing(self.store.connect()) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM record_versions').fetchone()[0], 1)
        packet['fetched_at'] = '2026-10-02T10:00:00+00:00'
        with self.assertRaisesRegex(ValueError, 'immutable_conflict'):
            save_response(self.collection, path, gzip.compress(packed(packet)), root=self.root)
        self.assertEqual(read_bytes(path), raw)

    def test_standalone_collector_keeps_file_format_without_creating_db(self):
        self.store.path.unlink()
        request = [self.request[0], {**self.request[1], 'symbol': 'NEW'}]
        client = Mock(return_value=self.payload)
        collector = Collector(self.collection, client=client)
        _, key = collector.get(*request)
        path = self.collection / 'responses' / (key + '.json.gz')
        self.assertTrue(path.is_file())
        self.assertEqual(json.loads(gzip.decompress(path.read_bytes()))['request'], request)
        self.assertFalse(self.store.path.exists())
        self.assertEqual(collector.get(*request), (self.payload, key))
        client.assert_called_once_with(*request)

    def test_direct_save_rejects_outside_response_path(self):
        with self.assertRaisesRegex(ValueError, 'collection_invalid'):
            save_response(self.collection, self.root / self.name, self.raw, root=self.root)
        self.assertEqual(self.path.read_bytes(), self.raw)

    def test_directory_stat_is_reused_but_final_delete_rechecks_source(self):
        original_lstat = Path.lstat
        calls = []

        def tracked(path, *args, **kwargs):
            if path == self.path:
                calls.append(path)
            return original_lstat(path, *args, **kwargs)

        with patch.object(Path, 'lstat', tracked), patch('records.windows_retire._WINDOWS', False):
            migrate(self.collection, root=self.root)
            self.assertEqual(calls, [])
            compact(self.collection, root=self.root)
            self.assertEqual(calls, [])
            compact(self.collection, root=self.root, apply=True)
            self.assertEqual(calls, [self.path])
        self.assertEqual(read_bytes(self.path), self.raw)

    def test_cached_directory_entry_reparse_flag_is_rejected(self):
        from types import SimpleNamespace
        entry = Mock(path=str(self.path), name=self.name)
        entry.stat.return_value = SimpleNamespace(st_file_attributes=0x400, st_mode=stat_types.S_IFREG)
        with self.assertRaisesRegex(ValueError, 'link_forbidden'):
            _source(self.collection, self.name, entry)
        entry.stat.assert_called_once_with(follow_symlinks=False)
        self.assertEqual(self.path.read_bytes(), self.raw)

    def test_missing_response_directory_is_recovered_from_db(self):
        migrate(self.collection, root=self.root)
        self.path.unlink()
        self.path.parent.rmdir()
        self.assertEqual(migrate(self.collection, root=self.root)['reused'], 1)
        result = compact(self.collection, root=self.root, apply=True)
        self.assertEqual((result['verified'], result['original_files_removed']), (1, 0))
        self.assertFalse(self.path.parent.exists())
        self.assertEqual(read_bytes(self.path), self.raw)

    def test_compaction_closes_enumeration_before_retirement_and_dry_run_is_sequential(self):
        self.add_sources(12)
        original_responses, original_retire = history._responses, history._retire_response
        enumerating = []

        def listing(*args):
            enumerating.append(True)
            try:
                yield from original_responses(*args)
            finally:
                enumerating.pop()

        def retire(*args):
            self.assertFalse(enumerating)
            return original_retire(*args)

        with patch.object(history, 'ThreadPoolExecutor', side_effect=AssertionError('dry_run_pool_forbidden')):
            self.assertEqual(compact(self.collection, root=self.root)['original_files_removed'], 0)
        with patch.object(history, '_responses', listing), patch.object(history, '_retire_response', retire):
            self.assertEqual(compact(self.collection, root=self.root, apply=True)['original_files_removed'], 12)

    def test_compaction_bounds_workers_pending_jobs_and_keeps_db_validation_on_parent(self):
        from concurrent.futures import ThreadPoolExecutor
        self.add_sources(20)
        release, four_started, eight_submitted = threading.Event(), threading.Event(), threading.Event()
        guard = threading.Lock()
        active = peak = submitted = 0
        results, errors, packet_threads, parent_threads, worker_limits = [], [], [], [], []
        original_retire, original_packet = history._retire_response, history._packet

        class TrackedPool(ThreadPoolExecutor):
            def __init__(pool, **kwargs):
                worker_limits.append(kwargs['max_workers'])
                super().__init__(**kwargs)

            def submit(pool, *args, **kwargs):
                nonlocal submitted
                submitted += 1
                if not release.is_set() and submitted > 8:
                    raise AssertionError('too_many_pending_jobs')
                future = super().submit(*args, **kwargs)
                if submitted == 8:
                    eight_submitted.set()
                return future

        def retire(*args):
            nonlocal active, peak
            with guard:
                active += 1
                peak = max(peak, active)
                if active == 4:
                    four_started.set()
            try:
                if not release.wait(10):
                    raise AssertionError('fixture_worker_timeout')
                return original_retire(*args)
            finally:
                with guard:
                    active -= 1

        def packet(*args):
            packet_threads.append(threading.get_ident())
            return original_packet(*args)

        def runner():
            parent_threads.append(threading.get_ident())
            try:
                results.append(compact(self.collection, root=self.root, apply=True))
            except BaseException as exc:
                errors.append(exc)

        with patch.object(history, 'ThreadPoolExecutor', TrackedPool), \
             patch.object(history, '_retire_response', retire), patch.object(history, '_packet', packet):
            thread = threading.Thread(target=runner)
            thread.start()
            try:
                self.assertTrue(four_started.wait(5))
                self.assertTrue(eight_submitted.wait(5))
                self.assertEqual(submitted, 8)
                with self.assertRaises(BusyError), instance_lock(self.collection / 'lock'):
                    pass
            finally:
                release.set()
                thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(worker_limits, [4])
        self.assertEqual(peak, 4)
        self.assertEqual(set(packet_threads), set(parent_threads))
        self.assertEqual(results[0]['original_files_removed'], 20)

    def test_worker_failure_holds_lock_until_running_jobs_finish_then_resumes(self):
        self.add_sources(12)
        four_started, failed, release = threading.Event(), threading.Event(), threading.Event()
        guard = threading.Lock()
        started = 0
        errors = []
        original_retire = history._retire_response

        def retire(*args):
            nonlocal started
            with guard:
                index = started
                started += 1
                if started == 4:
                    four_started.set()
            if not four_started.wait(5):
                raise AssertionError('fixture_start_timeout')
            if index == 0:
                failed.set()
                raise ValueError('fixture retirement failed')
            if not release.wait(10):
                raise AssertionError('fixture_worker_timeout')
            return original_retire(*args)

        def runner():
            try:
                compact(self.collection, root=self.root, apply=True)
            except BaseException as exc:
                errors.append(exc)

        with patch.object(history, '_retire_response', retire):
            thread = threading.Thread(target=runner)
            thread.start()
            try:
                self.assertTrue(failed.wait(5))
                self.assertTrue(thread.is_alive())
                with self.assertRaises(BusyError), instance_lock(self.collection / 'lock'):
                    pass
            finally:
                release.set()
                thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertRegex(str(errors[0]), 'fixture retirement failed')
        remaining = list((self.collection / 'responses').glob('*.json.gz'))
        self.assertGreater(len(remaining), 0)
        self.assertLess(len(remaining), 12)
        result = compact(self.collection, root=self.root, apply=True)
        self.assertEqual(result['original_files_removed'], len(remaining))
        self.assertEqual(result['verified'], 12)


if __name__ == '__main__':
    unittest.main()
