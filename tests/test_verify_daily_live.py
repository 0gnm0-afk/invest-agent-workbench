"""The live verification command exercised without authentication or network IO."""
from contextlib import closing, ExitStack
from datetime import date, timedelta
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from market import verify_daily_live as live
from market import toss_collection
from market import price_files
from market.prices import PriceStore, hydrate_snapshot


class LiveVerificationOfflineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1] / 'runtime')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.database = self.root / 'runtime/ohlcv.sqlite3'
        self.output = self.root / 'evidence'
        rule = dict(ma_period=2, slope_window=1, transition_window=1, rs_period=1,
                    activity_window=1, min_rs=0, min_activity_ratio=1, min_average_value=0)
        (self.root / 'config.json').write_text(json.dumps({
            'profile': {'status': 'review_only', 'KR': rule, 'US': rule}}), encoding='utf-8')
        start, stop = date(2026, 9, 1), date(2026, 10, 20)
        self.days = [(start + timedelta(days=n)).isoformat()
                     for n in range((stop-start).days + 1) if (start + timedelta(days=n)).weekday() < 5]
        self.requests = []
        self.changed = False
        self.fail = False
        stack = ExitStack()
        self.addCleanup(stack.close)
        # Fail closed even if a future implementation accidentally bypasses the fake client.
        for target in ('socket.socket.connect', 'socket.create_connection', 'urllib.request.urlopen'):
            stack.enter_context(patch(target, side_effect=AssertionError('offline_fixture_network_forbidden')))
        original_connect = PriceStore.connect

        def connect(store, **kwargs):
            if not store.path.resolve().is_relative_to(self.root):
                raise AssertionError('offline_fixture_production_database_forbidden')
            return original_connect(store, **kwargs)

        stack.enter_context(patch.object(PriceStore, 'connect', connect))
        stack.enter_context(patch.object(live, 'ROOT', self.root))
        stack.enter_context(patch.object(live, 'default_path', return_value=self.database))
        stack.enter_context(patch.object(price_files, 'default_path', return_value=self.database))
        stack.enter_context(patch.object(live, 'completed_sessions', return_value=self.days))
        stack.enter_context(patch.object(toss_collection, 'completed_sessions', return_value=self.days))
        stack.enter_context(patch.object(toss_collection.market_scope, 'load', return_value={
            'records': [], 'lifecycle_records': [], 'scope': 'fixture', 'path': 'fixture', 'sha256': 'fixture'}))
        self.client = stack.enter_context(patch.object(live, 'TossMarketClient', side_effect=self.client_for))
        stack.enter_context(patch('sys.stdout', new_callable=io.StringIO))
        self.store = PriceStore(self.database)
        self.seed()

    def seed(self):
        for market, symbol, provider, isin, exchange, benchmark in (
            ('KR', '005930.KS', '005930', 'KR7005930003', 'KOSPI', '^KS11'),
            ('KR', '000660.KS', '000660', 'KR7000660001', 'KOSPI', '^KS11'),
            ('US', 'AAPL', 'AAPL', 'US0378331005', 'NASDAQ', '^GSPC'),
            ('US', 'NVDA', 'NVDA', 'US67066G1040', 'NASDAQ', '^GSPC'),
            ('KR', '^KS11', 'KOSPI', '', None, None),
            ('US', '^GSPC', 'SPY', '', None, None),
        ):
            index = symbol == '^KS11'
            row = dict(market=market, symbol=symbol, provider_symbol=provider, isin_code=isin,
                       benchmark=benchmark, currency='KRW' if market == 'KR' else 'USD',
                       provider='Toss OpenAPI candles',
                       instrument_type='INDEX' if index else 'BENCHMARK_PROXY' if symbol == '^GSPC' else 'EQUITY',
                       price_basis='toss_index_points_observation_only' if index else 'toss_adjusted_ohlc_no_order_authority',
                       collected_at='2026-10-15T22:00:00+00:00',
                       collection={'full_history_requested_from': self.days[0]},
                       bars=[dict(date=day, open=99., high=101., low=98., close=100., volume=1000.)
                             for day in self.days if day <= '2026-10-15'])
            if exchange:
                row['listing_exchange_family'] = exchange
            self.store.put(row, source='fixture-seed/' + symbol, kind='benchmark' if symbol.startswith('^') else 'series')

    def client_for(self, *, observer):
        def get(endpoint, params):
            code = params.get('symbol', 'KOSPI')
            cutoff = params['before'][:10]
            self.requests.append((code, dict(params)))
            days = [day for day in self.days if day <= cutoff][-params['count']:]
            if self.fail and code == 'AAPL' and cutoff == '2026-10-19':
                days = [day for day in days if day != cutoff]
            currency = 'KRW' if code in ('005930', '000660', 'KOSPI') else 'USD'
            candles = []
            for day in reversed(days):
                close = '100.5' if self.changed and code == 'AAPL' and cutoff >= '2026-10-19' and day == '2026-10-16' else '100'
                candles.append(dict(timestamp=day + ('T00:00:00+09:00' if currency == 'KRW' else 'T00:00:00-04:00'),
                                    openPrice='99', highPrice='101', lowPrice='98', closePrice=close,
                                    volume='1000', currency=currency))
            result = {'result': {'candles': candles, 'nextBefore': None}}
            observer({'endpoint': endpoint, 'params': dict(params), 'response': result, 'fixture': True})
            return result
        return get

    def manifest(self):
        return json.loads((self.output / 'manifest.json').read_text(encoding='utf-8'))

    def test_success_persists_thin_snapshots_and_resumes_without_requests(self):
        result = live.run(self.output)
        manifest = self.manifest()
        self.assertEqual(manifest['state'], 'completed')
        self.assertTrue(result['collection_completed'])
        self.assertEqual(result['verified_references'], 22)
        self.assertEqual(len(self.requests), 18)  # Six instruments, three stages, no resume GETs.
        self.assertTrue(all(params['count'] == 2 for _, params in self.requests))
        self.assertEqual([stage['cutoff'] for stage in manifest['stages']], ['2026-10-16', '2026-10-19', '2026-10-20'])
        for stage in manifest['stages']:
            self.assertTrue(stage['resume_no_network'])
            self.assertEqual(stage['stats']['incremental_updates'], 4)
            snapshot = json.loads((self.output / ('stage-' + stage['cutoff']) / 'snapshot.json').read_text(encoding='utf-8'))
            self.assertTrue(all('bars' not in row for row in snapshot['series']))
            hydrated = hydrate_snapshot(snapshot)
            self.assertTrue(all(row['bars'][-1]['date'] == stage['cutoff'] for row in hydrated['series']))
        wire = json.loads((self.output / 'wire.json').read_text(encoding='utf-8'))
        self.assertEqual(len(wire), 18)
        clients_before = self.client.call_count
        self.assertTrue(live.verify(self.output / 'manifest.json')['collection_completed'])
        self.assertEqual(self.client.call_count, clients_before)
        with self.assertRaisesRegex(ValueError, 'fresh output folder'):
            live.run(self.output)
        self.assertEqual(self.client.call_count, clients_before)

    def test_one_overlap_correction_preserves_all_prior_references(self):
        self.changed = True
        live.run(self.output)
        manifest = self.manifest()
        self.assertEqual(manifest['stages'][1]['stats']['overlap_refreshes'], 1)
        self.assertEqual(manifest['stages'][1]['stats']['incremental_updates'], 3)
        with closing(self.store.connect()) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM price_corrections').fetchone()[0], 1)
        self.assertEqual(live.verify(self.output / 'manifest.json')['verified_references'], 22)
        old = next(item['reference'] for item in manifest['references'] if item['label'] == '2026-10-16/AAPL')
        new = next(item['reference'] for item in manifest['references'] if item['label'] == '2026-10-19/AAPL')
        self.assertEqual(self.store.read(old)[-1]['close'], 100.)
        self.assertEqual(next(b['close'] for b in self.store.read(new) if b['date'] == '2026-10-16'), 100.5)

    def test_market_holiday_uses_each_markets_completed_session(self):
        def sessions(market, now):
            return self.days[:-1] if market == 'KR' else self.days
        with patch.object(live, 'completed_sessions', side_effect=sessions), \
             patch.object(toss_collection, 'completed_sessions', side_effect=sessions):
            self.assertTrue(live.run(self.output)['collection_completed'])
        latest = self.manifest()['stages'][-1]
        self.assertEqual(latest['cutoff'], '2026-10-20')
        for row in latest['rows']:
            self.assertEqual(row['last']['date'],
                             '2026-10-19' if row['symbol'] in ('005930.KS', '000660.KS', '^KS11') else '2026-10-20')

    def test_missing_latest_bar_leaves_failed_manifest_and_verifiable_prior_stage(self):
        self.fail = True
        with self.assertRaisesRegex(ValueError, 'Live collection incomplete'):
            live.run(self.output)
        manifest = self.manifest()
        self.assertEqual(manifest['state'], 'failed')
        self.assertEqual(len(manifest['stages']), 1)
        self.assertIn('failure', manifest)
        result = live.verify(self.output / 'manifest.json')
        self.assertFalse(result['collection_completed'])
        self.assertEqual(result['verified_references'], 10)

    def test_client_initialization_failure_records_no_completed_collection(self):
        self.client.side_effect = RuntimeError('fixture authentication failure')
        with self.assertRaises(RuntimeError):
            live.run(self.output)
        manifest = self.manifest()
        self.assertEqual(manifest['state'], 'failed')
        self.assertEqual(manifest['failure']['stage'], 'client_initialization')
        self.assertFalse(live.verify(self.output / 'manifest.json')['collection_completed'])
        self.assertEqual(self.requests, [])

    def test_manifest_numeric_tampering_is_rejected_without_client(self):
        live.run(self.output)
        manifest = self.manifest()
        manifest['references'][0]['numeric_sha256'] = '0' * 64
        (self.output / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
        before = self.client.call_count
        with self.assertRaises(AssertionError):
            live.verify(self.output / 'manifest.json')
        self.assertEqual(self.client.call_count, before)


if __name__ == '__main__':
    unittest.main()
