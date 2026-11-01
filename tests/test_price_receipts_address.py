import hashlib
from pathlib import Path
import sys
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from market.price_receipts import address


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

    def test_other_run_subfolders_keep_the_same_two_component_group(self):
        runtime=Path(Path.cwd().anchor)/'synthetic-prices-runtime'
        first,_=address(runtime,runtime/'2026-10-16/run/collection/attempts/attempt/snapshot.json')
        second,_=address(runtime,runtime/'2026-10-16/run/adr-market/snapshot.json')
        self.assertEqual(first,second)


if __name__=='__main__':unittest.main()
