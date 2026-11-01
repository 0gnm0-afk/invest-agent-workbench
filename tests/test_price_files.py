from contextlib import closing
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path[:0] = [str(Path(__file__).resolve().parents[1]/'scripts'), str(Path(__file__).parent)]
from atomic_io import checked_saved, load, sha
from alerts import holding_series
from market.price_files import PriceFiles, available
from market.prices import PriceStore
from test_prices import series


class PriceFilesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = PriceStore(self.root/'ohlcv.sqlite3')
        self.files = PriceFiles(self.store)
        self.old = series()
        self.old['name'] = '\uc6d0\ubcf8'
        self.old['bars'][0]['open'] = 10.0
        self.store.put(self.old, source='archive', kind='archive')

    def save(self, name, value, **encoder):
        path = self.root/name
        raw = json.dumps(value, allow_nan=False, **encoder).encode('utf-8')
        path.write_bytes(raw)
        return path, raw

    def test_exact_snapshot_recovery_and_common_readers_after_deletion(self):
        value = dict(series=[self.old], benchmarks={}, risk_series=[], provenance={'attempt': 'original'})
        path, raw = self.save('snapshot.json', value, ensure_ascii=False, sort_keys=True, indent=2)
        before = sha(path)
        result = self.files.prepare(path)
        self.assertEqual(result['bars'], 2)
        self.assertEqual(path.read_bytes(), raw)
        newer = deepcopy(self.old);newer['bars'][0]['close'] = 11.5
        self.store.put(newer, source='correction')
        path.unlink()
        self.assertEqual(self.files.recover(path), raw)
        with patch('market.price_files.default_path', return_value=self.store.path):
            self.assertEqual(sha(path), before)
            with patch('market.price_files.recover_bytes', wraps=self.files.recover) as recover:
                self.assertEqual(checked_saved(path, before), value)
                recover.assert_called_once_with(path)
            self.assertEqual(load(path), value)
            self.assertEqual(holding_series(path, before, {'US:AAA'})['US:AAA'], value['series'][0])
            with self.assertRaisesRegex(ValueError, 'saved_artifact_hash_mismatch'):
                checked_saved(path, 'bad')

    def test_archive_and_checkpoint_keep_wrappers_types_order_and_whitespace(self):
        mixed = deepcopy(self.old)
        mixed['bars'][1] = dict(reversed(list(mixed['bars'][1].items())))
        for number, value in enumerate(({'sha256': 'wrapped', 'data': mixed},
                                        {'key': 'original-key', 'payload': mixed, 'hash': 'original-hash'})):
            path, raw = self.save(str(number)+'.json', value, ensure_ascii=True)
            raw = b'\xef\xbb\xbf'+raw+b'\n'
            path.write_bytes(raw)
            self.files.prepare(path)
            path.unlink()
            self.assertEqual(self.files.recover(path), raw)

    def test_prepare_never_imports_unknown_prices(self):
        missing = deepcopy(self.old);missing['bars'][0]['close'] = 10.25
        path, _ = self.save('unknown.json', {'data': missing})
        with self.assertRaisesRegex(ValueError, 'roundtrip_mismatch'):
            self.files.prepare(path)
        with closing(self.store.connect()) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM price_revisions').fetchone()[0], 1)

    def test_corruption_is_rejected_and_unknown_missing_file_stays_missing(self):
        path, _ = self.save('known.json', {'data': self.old})
        self.files.prepare(path)
        from market.price_receipts import address
        import zipfile
        pack,name = address(self.root,path)
        with zipfile.ZipFile(pack) as z:
            header,body=z.read(name).split(b'\n',1)
        data=json.loads(header);data['recipe_sha256']='bad'
        with zipfile.ZipFile(pack,'w') as z:
            z.writestr(name,json.dumps(data).encode()+b'\n'+body)
        with self.assertRaisesRegex(ValueError, 'recipe_hash_mismatch'):
            self.files.recover(path)
        with self.assertRaises(FileNotFoundError):
            self.files.recover(self.root/'unknown.json')

    def test_tampered_existing_file_cannot_be_hidden_by_recovery(self):
        path, _ = self.save('known.json', {'data': self.old})
        before = sha(path);self.files.prepare(path)
        path.write_text(json.dumps({'data': {**self.old, 'name': 'changed'}}), encoding='utf-8')
        with patch('market.price_files.default_path', return_value=self.store.path):
            with self.assertRaisesRegex(ValueError, 'saved_artifact_hash_mismatch'):
                checked_saved(path, before)
            with self.assertRaisesRegex(ValueError, 'source_changed'):
                self.files.prepare(path)

    def test_retired_history_resume_reuses_exact_saved_series_without_transport(self):
        from market.history.collect import Collector, packed
        item = {'symbol': 'AAA'}
        saved = {**self.old, 'listing': item, 'requested_end':'2026-10-02', 'state':'received'}
        payload = {'sha256':hashlib.sha256(packed(saved)).hexdigest(), 'data':saved}
        folder = self.root/'series';folder.mkdir()
        path, raw = self.save('series/'+hashlib.sha256(b'AAA').hexdigest()+'.json', payload, ensure_ascii=False, sort_keys=True)
        self.files.prepare(path);path.unlink()
        def no_transport(*args):
            self.fail('saved history must not trigger a provider request')
        with patch('market.price_files.default_path',return_value=self.store.path):
            self.assertTrue(available(path))
            self.assertEqual(Collector(self.root,no_transport).collect_one(item,'2026-10-02'),saved)

    def test_changed_price_rows_fail_original_file_hash_validation(self):
        path, _ = self.save('known.json', {'data':self.old})
        self.files.prepare(path);path.unlink()
        with closing(self.store.connect(write=True)) as db, db:
            db.execute('UPDATE prices SET close=10')
        with self.assertRaisesRegex(ValueError, 'recovery_hash_mismatch'):
            self.files.recover(path)


if __name__ == '__main__':
    unittest.main()
