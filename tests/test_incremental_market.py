import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import hybrid_market as market
from toss_candles import daily_series


class IncrementalMarketTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.days = ['2026-09-08', '2026-09-09', '2026-09-10', '2026-09-11']
        rule = dict(ma_period=2, slope_window=1, transition_window=1, rs_period=1,
                    activity_window=1, min_rs=0, min_activity_ratio=1, min_average_value=0)
        self.config = {'profile': {'status': 'review_only', 'KR': rule, 'US': rule},
                       'universe': [{'market': 'KR', 'symbol': '005930.KS',
                                     'provider_symbol': '005930', 'benchmark': '^KS11'}]}
        self.calls = []
        self.changed = False

    def bars(self, days):
        return [dict(date=day, open=99., high=101., low=98., close=100., volume=1000.)
                for day in days]

    def prior(self, provider='Toss OpenAPI candles'):
        return {'market': 'KR', 'symbol': '005930.KS', 'provider_symbol': '005930',
                'benchmark': '^KS11', 'currency': 'KRW', 'provider': provider,
                'price_basis': 'toss_adjusted_ohlc_no_order_authority',
                'collected_at': '2026-09-10T00:00:00+00:00', 'bars': self.bars(self.days[:3])}

    def fetch(self, get, symbol, region, sessions, **options):
        self.calls.append((symbol, options))
        bars = self.bars(self.days)
        if symbol == '005930.KS' and self.changed and options.get('stop_on_or_before'):
            bars[1]['close'] = 100.5
        return {'market': region, 'symbol': symbol,
                'provider_symbol': 'KOSPI' if symbol == '^KS11' else '005930',
                'benchmark': options.get('benchmark'), 'currency': 'KRW',
                'provider': 'Toss OpenAPI candles',
                'instrument_type': 'INDEX' if symbol == '^KS11' else 'EQUITY',
                'price_basis': 'toss_index_points_observation_only' if symbol == '^KS11'
                    else 'toss_adjusted_ohlc_no_order_authority',
                'bars': bars, 'collection': {}}

    def collect(self, previous):
        with patch.object(market, 'completed_sessions', return_value=self.days), \
             patch.object(market, 'daily_series', side_effect=self.fetch):
            return market.collect(self.config, {}, Path(self.temp.name), lambda *a: None,
                now=datetime(2026, 9, 11, tzinfo=timezone.utc),
                prior_series={('KR', '005930.KS'): previous},
                prior_source={'path': 'saved', 'sha256': 'abc'})

    def test_recent_request_merges_verified_history(self):
        result = self.collect(self.prior())
        row = result['series'][0]
        self.assertEqual([b['date'] for b in row['bars']], self.days)
        self.assertEqual(row['collection']['mode'], 'incremental')
        self.assertEqual(row['collection']['new_bars'], 1)
        self.assertEqual(result['collection_stats']['incremental_updates'], 1)
        self.assertEqual([options['stop_on_or_before'] for symbol, options in self.calls
                          if symbol == '005930.KS'], [self.days[2]])
        self.assertEqual(self.calls[-1][1]['page_size'], 33)

    def test_changed_overlap_refreshes_only_that_symbol(self):
        self.changed = True
        result = self.collect(self.prior())
        self.assertEqual(result['series'][0]['collection']['mode'], 'overlap_refresh')
        self.assertEqual(result['collection_stats']['overlap_refreshes'], 1)
        self.assertEqual(sum(symbol == '005930.KS' for symbol, _ in self.calls), 2)

    def test_nh_history_is_not_a_toss_seed(self):
        result = self.collect(self.prior(provider='NH market candles'))
        self.assertEqual(result['series'][0]['collection']['mode'], 'full')
        self.assertEqual(result['collection_stats']['incremental_updates'], 0)
        self.assertNotIn('stop_on_or_before', self.calls[-1][1])

    def test_old_full_history_is_refreshed(self):
        previous = self.prior()
        previous['collected_at'] = '2026-07-01T00:00:00+00:00'
        result = self.collect(previous)
        self.assertEqual(result['series'][0]['collection']['mode'], 'full')
        self.assertNotIn('stop_on_or_before', self.calls[-1][1])

    def test_toss_pagination_stops_at_verified_overlap(self):
        requests = []
        def get(path, params):
            requests.append((path, params))
            candles = [dict(timestamp=day+'T00:00:00+09:00', currency='KRW',
                            openPrice=99, highPrice=101, lowPrice=98,
                            closePrice=100, volume=1000) for day in reversed(self.days)]
            return {'result': {'candles': candles, 'nextBefore': '2026-09-07T00:00:00+09:00'}}
        row = daily_series(get, '005930.KS', 'KR', self.days,
                           stop_on_or_before=self.days[2], page_size=33)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0][1]['count'], 33)
        self.assertEqual(len(row['bars']), 4)


if __name__ == '__main__':
    unittest.main()
