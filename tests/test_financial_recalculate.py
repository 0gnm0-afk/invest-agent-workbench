"""Recalculation preserves signed-off raw inputs and does not collect external data."""
import copy
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from financial_store import FinancialStore, packed, save


class RecalculateSaved(unittest.TestCase):
    def setUp(self):
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

    def test_recalculate_retains_raw_date_and_original_snapshot(self):
        with patch('financial_store.Collector') as collector, patch('financial_store.sec_normalize', return_value={'ok': True}), patch('financial_store.summarize', return_value={'summary': [], 'calculation_version': 'new'}):
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

    def test_failed_calculation_keeps_old_success(self):
        with patch('financial_store.sec_normalize', side_effect=ValueError('bad period')):
            result = self.store.recalculate_saved(self.key)
        self.assertEqual(result['state'], 'failed')
        self.assertEqual(result['result']['snapshot_id'], self.sid)
        self.assertEqual(json.loads((self.folder / 'latest.json').read_bytes())['snapshot'], self.sid)

    def test_lock_prevents_concurrent_recalculation(self):
        from financial_store import lock_file, unlock
        lease = lock_file(self.folder / 'request.lock')
        try:
            with self.assertRaisesRegex(ValueError, 'busy'):
                self.store.recalculate_saved(self.key)
        finally:
            unlock(lease)


if __name__ == '__main__':
    unittest.main()
