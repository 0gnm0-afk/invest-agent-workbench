import copy
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from market.providers.toss_candles import daily_series
from invest_agent.trading.market import usable_tail


def candle(day, close='100', currency='KRW'):
    return {'timestamp': day + 'T00:00:00+09:00', 'openPrice': '99', 'highPrice': '101',
            'lowPrice': '98', 'closePrice': close, 'volume': '1000', 'currency': currency}


class TossCandleTests(unittest.TestCase):
    def setUp(self):
        self.days = ['2026-09-09', '2026-09-10', '2026-09-11']
        self.pages = [{'candles': [candle(self.days[2]), candle(self.days[1])], 'nextBefore': self.days[1] + 'T00:00:00+09:00'},
                      {'candles': [candle(self.days[1]), candle(self.days[0])], 'nextBefore': None}]
        self.calls = []

    def get(self, path, params):
        self.calls.append((path, params))
        return {'result': self.pages[len(self.calls) - 1]}

    def test_inclusive_overlap_and_completed_session_filter(self):
        self.pages[0]['candles'].insert(0, candle('2026-09-12'))
        result = daily_series(self.get, '005930.KS', 'KR', self.days, benchmark='^KS11')
        self.assertEqual([b['date'] for b in usable_tail(result['bars'], self.days)], self.days)
        self.assertEqual(self.calls[1][1]['before'], self.pages[0]['nextBefore'])
        self.assertEqual(self.calls[0][1]['adjusted'], 'true')
        self.assertEqual(result['price_basis'], 'toss_adjusted_ohlc_no_order_authority')

    def test_conflicting_overlap_is_not_overwritten(self):
        self.pages[1]['candles'][0]['closePrice'] = '99'
        with self.assertRaises(ValueError): daily_series(self.get, '005930.KS', 'KR', self.days)

    def test_page_limit_and_repeated_cursor_fail(self):
        with self.assertRaises(ValueError): daily_series(self.get, '005930.KS', 'KR', self.days, max_pages=1)
        self.calls = []
        self.pages[1] = copy.deepcopy(self.pages[0])
        with self.assertRaises(ValueError): daily_series(self.get, '005930.KS', 'KR', self.days)

    def test_currency_and_null_close_rejected(self):
        for field, value in [('currency', 'USD'), ('closePrice', None), ('closePrice', 'NaN')]:
            self.calls = []
            old = self.pages[0]['candles'][0][field]
            self.pages[0]['candles'][0][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError): daily_series(self.get, '005930.KS', 'KR', self.days)
            self.pages[0]['candles'][0][field] = old

    def test_index_route_and_raw_stock_flag(self):
        result = daily_series(self.get, '^KQ11', 'KR', self.days)
        self.assertEqual(self.calls[0][0], '/api/v1/market-indicators/KOSDAQ/candles')
        self.assertNotIn('adjusted', self.calls[0][1])
        self.assertEqual(result['price_basis'], 'toss_index_points_observation_only')
        self.calls = []
        daily_series(self.get, '005930.KS', 'KR', self.days, adjusted=False)
        self.assertEqual(self.calls[0][1]['adjusted'], 'false')

    def test_duplicate_page_dates_and_naive_timestamps_rejected(self):
        self.pages[0]['candles'].append(copy.deepcopy(self.pages[0]['candles'][0]))
        with self.assertRaises(ValueError): daily_series(self.get, '005930.KS', 'KR', self.days)
        self.calls = []
        self.pages[0]['candles'].pop()
        self.pages[0]['candles'][0]['timestamp'] = '2026-09-11T00:00:00'
        with self.assertRaises(ValueError): daily_series(self.get, '005930.KS', 'KR', self.days)

    def test_us_stock_and_spy_benchmark_proxy_use_usd(self):
        self.pages = [{'candles': [candle(day, currency='USD') for day in reversed(self.days)], 'nextBefore': None}]
        stock = daily_series(self.get, 'BRK-B', 'US', self.days, benchmark='^GSPC')
        self.assertEqual((stock['currency'], stock['provider_symbol'], stock['instrument_type']), ('USD', 'BRK-B', 'EQUITY'))
        self.calls = []
        proxy = daily_series(self.get, '^GSPC', 'US', self.days)
        self.assertEqual(self.calls[0][1]['symbol'], 'SPY')
        self.assertEqual(proxy['benchmark_proxy'], 'SPY')
        self.assertEqual(proxy['instrument_type'], 'BENCHMARK_PROXY')


if __name__ == '__main__': unittest.main()
