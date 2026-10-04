import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from toss_universe import discover


class FakeToss:
    def __init__(self, fail_prices=False):
        self.calls = []
        self.fail_prices = fail_prices

    def __call__(self, path, params):
        self.calls.append((path, params))
        if path == '/api/v1/stocks/all':
            if params['securityType'] == 'DEPOSITARY_RECEIPT':
                return {'result': []}
            market = params['market']
            prefix = '0' if market == 'KOSPI' else ''
            return {'result': [
                {'symbol': prefix + '00001' if market == 'KOSPI' else 'LOW', 'name': 'Low',
                 'securityType': 'STOCK', 'isCommonShare': True, 'isinCode': 'ONE'},
                {'symbol': prefix + '00002' if market == 'KOSPI' else 'HIGH', 'name': 'High',
                 'securityType': 'STOCK', 'isCommonShare': True, 'isinCode': 'TWO'},
            ]}
        symbols = params['symbols'].split(',')
        if path == '/api/v1/stocks':
            market = 'KOSPI' if symbols[0].isdigit() else 'NASDAQ'
            currency = 'KRW' if market == 'KOSPI' else 'USD'
            return {'result': [
                {'symbol': symbol, 'name': symbol, 'market': market, 'securityType': 'STOCK',
                 'isCommonShare': True, 'status': 'ACTIVE', 'currency': currency,
                 'sharesOutstanding': '100', 'isinCode': symbol + '-ISIN',
                 'koreanMarketDetail': {'krxTradingSuspended': False} if market == 'KOSPI' else None}
                for symbol in symbols
            ]}
        if path == '/api/v1/prices':
            if self.fail_prices:
                raise ValueError('synthetic price failure')
            currency = 'KRW' if symbols[0].isdigit() else 'USD'
            return {'result': [
                {'symbol': symbol, 'lastPrice': '20' if symbol.endswith('2') or symbol == 'HIGH' else '10',
                 'currency': currency, 'timestamp': '2026-09-11T16:00:00+09:00'}
                for symbol in symbols
            ]}
        raise AssertionError(path)


class TossUniverseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = datetime(2026, 9, 12, tzinfo=timezone.utc)

    def test_market_cap_order_limit_and_provenance(self):
        get = FakeToss()
        rows, meta = discover(get, ['KR', 'US'], 1, cache_root=self.root, now=self.now)
        self.assertEqual({row['symbol'] for row in rows}, {'000002.KS', 'HIGH'})
        self.assertTrue(all(row['market_cap'] == 2000 for row in rows))
        self.assertTrue(all(row['market_cap_basis'] == 'toss_last_price_times_shares_outstanding_derived'
                            for row in rows))
        self.assertEqual(meta['KOSPI']['ordering'], 'derived_market_cap_descending')
        self.assertEqual(meta['NASDAQ']['requested'], 1)
        self.assertEqual(meta['NYSE']['requested'], 1)

    def test_complete_exchange_is_cached_but_partial_exchange_is_not_hidden(self):
        get = FakeToss()
        first, first_meta = discover(get, ['KR'], None, cache_root=self.root, now=self.now)
        call_count = len(get.calls)
        second, second_meta = discover(get, ['KR'], None, cache_root=self.root, now=self.now)
        self.assertEqual(second, first)
        self.assertEqual(len(get.calls), call_count)
        self.assertEqual((first_meta['KOSPI']['cache'], second_meta['KOSPI']['cache']), ('miss', 'hit'))

        failed = FakeToss(fail_prices=True)
        rows, meta = discover(failed, ['US'], None, cache_root=self.root / 'partial', now=self.now)
        self.assertTrue(rows)
        self.assertTrue(all(row['market_cap'] is None for row in rows))
        self.assertEqual(meta['NASDAQ']['state'], 'partial')
        self.assertTrue(meta['NASDAQ']['batch_failures'])

    def test_invalid_limits_and_markets_fail_closed(self):
        for markets, limit in [([], None), (['CA'], None), (['KR'], 0), (['US'], True)]:
            with self.subTest(markets=markets, limit=limit), self.assertRaises(ValueError):
                discover(FakeToss(), markets, limit, now=self.now)

    def test_nullable_quote_is_received_but_not_a_dated_market_cap(self):
        original = FakeToss()
        def get(path, params):
            result = original(path, params)
            if path == '/api/v1/prices':
                for row in result['result']:
                    row['timestamp'] = None
            return result
        rows, meta = discover(get, ['KR'], now=self.now)
        self.assertEqual(meta['KOSPI']['state'], 'available')
        self.assertEqual(meta['KOSPI']['missing_price_time'], 2)
        self.assertTrue(all(r['market_cap'] is None and r['market_cap_as_of'] is None for r in rows))
        self.assertTrue(all(r['undated_quote_market_cap'] > 0 for r in rows))


if __name__ == '__main__':
    unittest.main()
