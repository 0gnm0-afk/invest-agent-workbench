import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from market.price_receipts import address, packed_exists, packed_record, save_one


class ReceiptAddresses(unittest.TestCase):
    def test_pre_normalization_group_preserves_the_existing_archive_address(self):
        runtime=Path.cwd().anchor
        runtime=Path(runtime)/'synthetic-prices-runtime'
        for market in ('kr-history','us-history'):
            for collection in ('2026-10-01-toss','future-collection'):
                with self.subTest(market=market,collection=collection):
                    existing,_=address(runtime,runtime/market/collection/'series/a.json')
                    historical,_=address(runtime,runtime/market/collection/'pre-local-date-normalization/series/a.json')
                    self.assertEqual(existing.name,hashlib.sha256((market+'/'+collection).encode()).hexdigest()+'.zip')
                    self.assertEqual(historical.name,hashlib.sha256((market+'/'+collection+'/pre-local-date-normalization').encode()).hexdigest()+'.zip')
                    self.assertNotEqual(existing,historical)

    def test_moved_pack_uses_current_folder_and_original_entry_key(self):
        with tempfile.TemporaryDirectory() as folder:
            old = Path(folder) / 'old'
            intermediate = Path(folder) / 'usb'
            current = Path(folder) / 'consolidated'
            relative = Path('financials/KR-A/run/saved-price.json')
            source = old / 'runtime' / relative
            local = current / 'runtime' / relative
            save_one(old / 'runtime', source, 'source-sha', 10, 'recipe-sha', b'packed-body')
            original_pack, _ = address(old / 'runtime', source)
            current_pack, _ = address(current / 'runtime', local)
            current_pack.parent.mkdir(parents=True)
            original_pack.replace(current_pack)
            (current / 'config.json').write_text(
                json.dumps({'records': {'previous_root': str(old),
                                       'previous_roots': [str(intermediate)]}}), encoding='utf-8')
            with patch('runtime_paths.ROOT', current):
                for path in (source, intermediate / 'runtime' / relative, local):
                    self.assertTrue(packed_exists(current / 'runtime', path))
                    self.assertEqual(packed_record(current / 'runtime', path),
                                     ('source-sha', 10, 'recipe-sha', b'packed-body'))
                self.assertIsNone(packed_record(current / 'runtime', local.with_name('unknown.json')))

    def test_other_run_subfolders_keep_the_same_two_component_group(self):
        runtime=Path(Path.cwd().anchor)/'synthetic-prices-runtime'
        first,_=address(runtime,runtime/'2026-10-16/run/collection/attempts/attempt/snapshot.json')
        second,_=address(runtime,runtime/'2026-10-16/run/adr-market/snapshot.json')
        self.assertEqual(first,second)


if __name__=='__main__':unittest.main()
