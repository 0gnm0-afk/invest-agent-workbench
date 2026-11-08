"""Financial record bundles keep original bytes and the active snapshot contract."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from financials.store import FinancialStore, lock_file, save
from records.financial import ARTIFACTS, LEGACY_BUNDLE, publish_snapshot, read_artifact, verify
from records.store import RecordStore, default_path


class FinancialRecords(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.store = RecordStore(default_path(self.root))
        self.financial = FinancialStore(self.root / 'runtime' / 'financials')
        self.key = 'US:NVDA'
        self.symbol = 'NVDA'
        self.sid = 'a' * 32
        self.folder = self.financial.folder(self.key)
        self.base = self.folder / 'snapshots' / self.sid
        self.result = dict(key=self.key, snapshot_id=self.sid, collected_at='2026-11-01T00:00:00Z', sources=[])
        save(self.base / 'result.json', self.result)
        save(self.base / 'normalized.json', dict(annual=[], quarterly=[]))
        save(self.base / 'source.json', dict(provider='SEC', symbol=self.symbol))
        save(self.base / 'saved-price.json', dict(market='US', symbol=self.symbol, bars=[]))
        save(self.base / 'raw' / 'official.json', {'fact': 3})
        files = {
            p.relative_to(self.base).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in self.base.rglob('*') if p.is_file()
        }
        save(self.base / 'manifest.json', dict(schema=1, key=self.key, version='financial-focus-8', files=files))
        self.manifest_sha = hashlib.sha256((self.base / 'manifest.json').read_bytes()).hexdigest()
        save(self.folder / 'latest.json', dict(snapshot=self.sid, manifest_sha256=self.manifest_sha))
        self.pointer = (self.folder / 'latest.json').read_bytes()
        settings = patch('financials.store.settings', return_value={})
        settings.start()
        self.addCleanup(settings.stop)

    def publish(self):
        return publish_snapshot(self.base, root=self.root, store=self.store, manifest_sha=self.manifest_sha)

    def test_bundle_reuse_read_without_original_and_tamper_rejection(self):
        originals = {n: (self.base / n).read_bytes() for n in ARTIFACTS}
        self.assertFalse(self.publish()['reused'])
        self.assertTrue(self.publish()['reused'])
        for name, raw in originals.items():
            self.assertEqual((self.base / name).read_bytes(), raw)
            self.assertEqual(self.store.read(self.base / name), raw)
        self.assertIsNone(self.store.read(self.base / 'raw' / 'official.json'))
        self.assertIsNone(self.store.read(self.base / 'saved-price.json'))
        for name in ARTIFACTS:
            (self.base / name).rename(self.base / (name + '.fixture-hidden'))
        self.assertEqual(self.financial.read(self.key)['result'], self.result)
        self.assertEqual(read_artifact(self.base / 'normalized.json'), originals['normalized.json'])
        self.assertEqual(verify(self.store, root=self.root)['artifacts'], 4)
        self.assertTrue(self.publish()['reused'])
        (self.base / 'result.json').write_bytes(b'{}')
        result = self.financial.read(self.key)
        self.assertIsNone(result['result'])
        self.assertEqual(result['attempt']['error'], 'cached_snapshot_integrity_failed')

    def test_legacy_bundle_backfills_only_verified_source(self):
        metadata = dict(key=self.key, snapshot_id=self.sid, manifest_sha256=self.manifest_sha,
                        version='financial-focus-8')
        old = {
            name: self.store.put(self.base / name, (self.base / name).read_bytes(),
                                 kind=ARTIFACTS[name], root=self.root, metadata=metadata)
            for name in LEGACY_BUNDLE
        }
        self.assertEqual(verify(self.store, root=self.root)['legacy_snapshots_missing_source'], 1)
        source = self.base / 'source.json'
        raw = source.read_bytes()
        source.write_bytes(b'{}')
        with self.assertRaisesRegex(ValueError, 'artifact_hash_mismatch:source.json'):
            self.publish()
        self.assertEqual(verify(self.store, root=self.root)['artifacts'], 3)
        source.write_bytes(raw)
        receipt = self.publish()
        self.assertEqual(receipt['inserted_artifacts'], 1)
        self.assertEqual(receipt['inserted_bytes'], len(raw))
        self.assertEqual(self.store.read(source), raw)
        for name, ref in old.items():
            self.assertEqual(self.store.lookup(self.base / name), ref)
        self.assertEqual(verify(self.store, root=self.root)['legacy_snapshots_missing_source'], 0)
        self.assertEqual(self.publish()['inserted_artifacts'], 0)

    def test_hash_failure_and_transaction_failure_leave_no_partial_bundle(self):
        p = self.base / 'normalized.json'
        original = p.read_bytes()
        p.write_bytes(b'{}')
        with self.assertRaisesRegex(ValueError, 'artifact_hash_mismatch'):
            self.publish()
        self.assertFalse(self.store.path.exists())
        p.write_bytes(original)
        put = self.store._put

        def fail_second(db, source, raw, **kwargs):
            if source.name == 'result.json':
                raise OSError('simulated write failure')
            return put(db, source, raw, **kwargs)

        with patch.object(self.store, '_put', side_effect=fail_second):
            with self.assertRaisesRegex(OSError, 'simulated'):
                self.publish()
        with closing(self.store.connect()) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM record_versions').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT count(*) FROM record_blobs').fetchone()[0], 0)
        self.assertEqual((self.folder / 'latest.json').read_bytes(), self.pointer)
        self.assertFalse(self.publish()['reused'])

    def test_new_snapshot_preserves_old_pointer_on_publication_failure(self):
        source = dict(provider='SEC', identity={'name': 'Fixture'},
                      eligibility={'status': 'unsupported', 'reason': 'Fixture'})
        stock = dict(market='US', symbol=self.symbol)
        for fail in (True, False):
            def publish(base, **kwargs):
                self.assertEqual((self.folder / 'latest.json').read_bytes(), self.pointer)
                if fail:
                    raise OSError('record write failed')
                return publish_snapshot(base, **kwargs)

            lease = lock_file(self.folder / 'request.lock')
            with patch('financials.store.Collector') as collector, \
                    patch('financials.store.publish_snapshot', side_effect=publish) as publish_call:
                collector.return_value.sources = []
                collector.return_value.usa.return_value = source
                self.financial._work(self.key, stock, lease)
                publish_call.assert_called_once()
            active = self.financial.read(self.key)
            if fail:
                self.assertEqual(active['state'], 'failed')
                self.assertEqual(active['result']['snapshot_id'], self.sid)
                self.assertEqual((self.folder / 'latest.json').read_bytes(), self.pointer)
                self.assertFalse(self.store.path.exists())
            else:
                self.assertEqual(active['state'], 'unsupported')
                self.assertNotEqual(active['result']['snapshot_id'], self.sid)
                self.assertEqual(verify(self.store, root=self.root)['snapshots'], 1)

    def test_recalculate_with_record_outputs_and_saved_inputs(self):
        self.publish()
        for name in ARTIFACTS:
            (self.base / name).rename(self.base / (name + '.fixture-hidden'))
        with patch('financials.store.Collector') as collector, \
                patch('financials.store.sec_normalize', return_value={'annual': []}), \
                patch('financials.store.summarize', return_value={'summary': []}):
            result = self.financial.recalculate_saved(self.key)
            collector.assert_not_called()
        self.assertEqual(result['state'], 'complete')
        self.assertEqual(result['result']['source_snapshot_id'], self.sid)
        self.assertFalse((self.base / 'source.json').exists())
        self.assertEqual(verify(self.store, root=self.root)['snapshots'], 2)


if __name__ == '__main__':
    unittest.main()
