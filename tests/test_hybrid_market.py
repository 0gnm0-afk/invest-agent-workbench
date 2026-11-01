import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import market.toss_collection as h


class TossMarketPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.now = datetime(2026, 9, 12, tzinfo=timezone.utc)
        self.days = ['2026-09-09', '2026-09-10', '2026-09-11']
        rule = dict(ma_period=2, slope_window=1, transition_window=1, rs_period=1, activity_window=1,
                    min_rs=0, min_activity_ratio=1, min_average_value=0)
        self.config = {'profile': {'status': 'review_only', 'KR': rule, 'US': rule}, 'universe': [
            {'market': 'KR', 'symbol': '005930.KS', 'provider_symbol': '005930', 'benchmark': '^KS11'},
            {'market': 'US', 'symbol': 'EXAMPLE', 'provider_symbol': 'EXAMPLE', 'benchmark': '^GSPC'}]}
        self.calendar = patch.object(h, 'completed_sessions', return_value=self.days).start()
        self.candles = patch.object(h, 'daily_series', side_effect=self.candle_series).start()
        self.addCleanup(patch.stopall)
        self.addCleanup(self.temp.cleanup)

    def series(self, symbol, market, benchmark=None, adjusted=True, provider_symbol=None):
        result = {
            'symbol': symbol, 'provider_symbol': provider_symbol or symbol, 'market': market,
            'currency': 'KRW' if market == 'KR' else 'USD', 'benchmark': benchmark,
            'provider': 'Toss OpenAPI candles', 'instrument_type': 'EQUITY',
            'price_basis': 'toss_adjusted_ohlc_no_order_authority' if adjusted else 'toss_raw_ohlc_no_order_authority',
            'bars': [{'date': day, 'open': 99, 'high': 101, 'low': 98, 'close': 100, 'volume': 1000}
                     for day in self.days],
        }
        if symbol in ('^KS11', '^GSPC'):
            result['instrument_type'] = 'INDEX' if market == 'KR' else 'BENCHMARK_PROXY'
        return result

    def candle_series(self, get, symbol, market, days, *, benchmark=None, provider_symbol=None, adjusted=True):
        return self.series(symbol, market, benchmark, adjusted, provider_symbol)

    def run_collect(self, **kwargs):
        return h.collect(self.config, kwargs.pop('account', {}), self.root, lambda *args: None,
                         now=kwargs.pop('now', self.now), **kwargs)

    def test_dashboard_cutoff_reaches_candle_collector(self):
        self.config['session_cutoff']='2026-09-10'
        original=self.candle_series
        def bounded(get,symbol,market,days,**kwargs):
            self.assertEqual(days[-1],'2026-09-10')
            result=original(get,symbol,market,days,**kwargs)
            result['bars']=[b for b in result['bars'] if b['date']<=days[-1]]
            return result
        self.candles.side_effect=bounded
        result=self.run_collect()
        self.assertEqual(result['sessions']['KR'][-1],'2026-09-10')
        self.assertEqual(result['sessions']['US'][-1],'2026-09-10')
        self.assertTrue(all(r['bars'][-1]['date']=='2026-09-10' for r in result['series']))

    def test_kr_and_us_use_toss_and_completed_series_are_cached(self):
        first = self.run_collect()
        self.assertEqual(first['collection_stats']['received'], 2)
        self.assertEqual(first['collection_mode'], h.COLLECTOR_ID)
        self.assertTrue(all('Toss' in provider for provider in first['providers'].values()))
        self.assertEqual(self.candles.call_count, 4)  # two benchmarks + two stocks
        again = self.run_collect()
        self.assertEqual(again['collection_stats']['cache_hits'], 2)
        self.assertEqual(self.candles.call_count, 4)
        progress = json.loads((self.root / 'progress.json').read_text())
        self.assertEqual((progress['state'], progress['completed']), ('finished', 2))

    def test_raw_holdings_in_both_markets_use_separate_cache(self):
        self.config['risk_universe'] = [
            {'market': 'KR', 'symbol': '005930.KS', 'provider_symbol': '005930'},
            {'market': 'US', 'symbol': 'EXAMPLE', 'provider_symbol': 'EXAMPLE'},
        ]
        first = self.run_collect()
        self.assertEqual(len(first['risk_series']), 2)
        self.assertTrue(all(row['price_basis'] == 'toss_raw_ohlc_no_order_authority'
                            for row in first['risk_series']))
        count = self.candles.call_count
        second = self.run_collect()
        self.assertEqual(self.candles.call_count, count)
        self.assertEqual(second['risk_errors'], [])

    def test_corrupt_stale_and_refresh_cache_are_refetched(self):
        self.run_collect()
        path = next((self.root / 'series').glob('*.json'))
        payload = json.loads(path.read_text())
        payload['payload']['bars_ref']['sha256'] = 'tampered'
        path.write_text(json.dumps(payload))
        self.run_collect()
        self.assertEqual(self.candles.call_count, 5)
        self.run_collect(now=self.now + timedelta(hours=25))
        self.assertEqual(self.candles.call_count, 9)
        self.run_collect(now=self.now + timedelta(hours=25), refresh=True)
        self.assertEqual(self.candles.call_count, 13)

    def test_failure_isolated_not_cached_and_retried(self):
        def fail_one(get, symbol, market, days, *, benchmark=None, provider_symbol=None, adjusted=True):
            if benchmark and market == 'KR':
                raise ValueError('unavailable')
            return self.series(symbol, market, benchmark, adjusted, provider_symbol)
        self.candles.side_effect = fail_one
        first = self.run_collect()
        self.assertEqual((len(first['series']), len(first['errors'])), (1, 1))
        self.candles.side_effect = self.candle_series
        second = self.run_collect()
        self.assertEqual((len(second['series']), len(second['errors'])), (2, 0))

    def test_exchange_discovery_uses_private_toss_discoverer_and_surfaces_partial_scope(self):
        self.config = {**self.config, 'universe_mode': 'exchange_listings', 'markets': ['KR'], 'limit_per_exchange': 1}
        self.config.pop('universe')
        rows = [{'market': 'KR', 'symbol': '005930.KS', 'provider_symbol': '005930', 'benchmark': '^KS11'}]
        meta = {'KOSPI': {'state': 'partial', 'reason': 'listing_enrichment_partial', 'batch_failures': [{'batch': 1}]}}
        with patch.object(h, 'discover', return_value=(rows, meta)) as discover:
            result = self.run_collect()
        self.assertTrue(callable(discover.call_args.args[0]))
        self.assertEqual(result['coverage']['scope_errors'][0]['state'], 'partial')
        self.assertFalse(result['coverage']['market_wide'])

    def test_holdings_are_added_outside_configured_universe(self):
        account = {'accounts': [{'positions': [
            {'market': 'US', 'quote_symbol': 'HELD', 'provider_symbol': 'HELD', 'benchmark': '^GSPC'},
        ]}]}
        result = self.run_collect(account=account)
        self.assertEqual(result['coverage']['requested'], 3)
        self.assertIn('HELD', {row['symbol'] for row in result['series']})

    def test_callback_rejects_session_change_and_old_yahoo_seed_before_auth(self):
        from invest_agent.trading.morning import CollectorUnavailableError
        self.calendar.side_effect = [self.days + ['2026-09-14'], self.days]
        with patch('market.providers.toss_client.TossMarketClient') as client:
            callback = h.make_collector(self.root)
            with self.assertRaises(CollectorUnavailableError):
                callback({'markets': ['KR']}, self.now)
            client.assert_not_called()
        self.calendar.side_effect = None
        self.calendar.return_value = self.days
        with patch('market.providers.toss_client.TossMarketClient') as client:
            callback = h.make_collector(self.root)
            with self.assertRaises(CollectorUnavailableError):
                callback({'markets': ['US'], 'reuse_us_from_run': 'old-run'}, self.now)
            client.assert_not_called()

    def test_callback_preserves_frozen_window_and_collector_identity(self):
        self.calendar.side_effect = [self.days[1:], self.days]
        with patch('market.providers.toss_client.TossMarketClient'), patch.object(h, 'collect', return_value={'ok': True}) as collect:
            callback = h.make_collector(self.root)
            self.assertEqual(callback({'markets': ['KR']}, self.now), {'ok': True})
            self.assertEqual(callback.collector_id, h.COLLECTOR_ID)
            self.assertEqual(collect.call_args.kwargs['now'], self.now)

    def test_progress_permission_error_does_not_discard_collected_data(self):
        original = h.atomic_write

        def write(path, text):
            if Path(path).name == 'progress.json':
                raise PermissionError('synthetic reader holds progress')
            return original(path, text)

        with patch.object(h, 'atomic_write', side_effect=write):
            result = self.run_collect()
        self.assertEqual(len(result['series']), 2)
        self.assertEqual(result['collection_stats']['progress_write_failures'], 4)


if __name__ == '__main__':
    unittest.main()
