"""Offline ADR discovery -> daily saved prices -> search/detail regression."""
import http.client
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from toss_universe import discover
from toss_listing import listing_requests
from test_toss_universe import FakeToss
import hybrid_market as hybrid
from market_collect import audit
from dashboard_data import saved_market, write, sha
from dashboard import handler_for, ThreadingHTTPServer


class AdrToss(FakeToss):
    def __call__(self, path, params):
        if path == '/api/v1/stocks/all':
            self.calls.append((path, dict(params)))
            if params['securityType'] == 'DEPOSITARY_RECEIPT':
                symbols = ['NVO', 'TSM'] if params['market'] == 'NYSE' else []
            else:
                symbols = ['AAPL'] if params['market'] == 'NASDAQ' else ['IBM']
            return {'result': [dict(symbol=s, name='노보 노디스크 ADR' if s == 'NVO' else s,
                                    securityType=params['securityType'],
                                    isCommonShare=params['securityType'] == 'STOCK', isinCode=s+'-ISIN')
                               for s in symbols]}
        result = super().__call__(path, params)
        if path == '/api/v1/stocks':
            for row in result['result']:
                if row['symbol'] in ('NVO', 'TSM'):
                    row.update(securityType='DEPOSITARY_RECEIPT', isCommonShare=False,
                               market='NYSE', name='노보 노디스크 ADR' if row['symbol'] == 'NVO' else row['symbol'])
        return result


class AdrCollection(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = datetime(2026, 9, 12, tzinfo=timezone.utc)

    def test_us_union_preserves_receipt_identity_without_common_share_filter(self):
        fake = AdrToss()
        rows, meta = discover(fake, ['US'], now=self.now)
        self.assertEqual({r['symbol'] for r in rows}, {'AAPL', 'IBM', 'NVO', 'TSM'})
        requests = [p for path, p in fake.calls if path == '/api/v1/stocks/all']
        self.assertEqual(len(requests), 4)
        for request in requests:
            if request['securityType'] == 'DEPOSITARY_RECEIPT':
                self.assertNotIn('commonShare', request)
            else:
                self.assertEqual(request['commonShare'], 'true')
        nvo = next(r for r in rows if r['symbol'] == 'NVO')
        self.assertEqual((nvo['instrument_type'], nvo['security_type'], nvo['isin_code']),
                         ('EQUITY', 'DEPOSITARY_RECEIPT', 'NVO-ISIN'))
        self.assertFalse(nvo['is_common_share'])
        self.assertIsNone(nvo['market_cap'])
        self.assertIsNone(nvo['undated_quote_market_cap'])
        self.assertEqual(nvo['market_cap_quality'], 'unavailable_adr_share_basis')
        self.assertEqual(meta['NYSE']['listing_types'], {'STOCK': 1, 'DEPOSITARY_RECEIPT': 2})
        self.assertEqual(meta['NYSE']['state'], 'available')
        self.assertEqual(meta['NYSE']['unverified_adr_caps'], 2)
        self.assertEqual(meta['NASDAQ']['listing_types']['DEPOSITARY_RECEIPT'], 0)
        self.assertEqual(len(listing_requests('KOSPI')), 1)
        self.assertEqual(len(listing_requests('KOSDAQ')), 1)

    def test_old_stock_only_cache_is_rejected_and_new_scope_is_reused(self):
        fake = AdrToss()
        discover(fake, ['US'], cache_root=self.root, now=self.now)
        for file in self.root.glob('*.json'):
            payload = json.loads(file.read_text(encoding='utf-8'))
            payload['schema'] = 2
            file.write_text(json.dumps(payload), encoding='utf-8')
        fake.calls.clear()
        rows, meta = discover(fake, ['US'], cache_root=self.root, now=self.now)
        self.assertIn('NVO', {r['symbol'] for r in rows})
        self.assertEqual(sum(path == '/api/v1/stocks/all' for path, _ in fake.calls), 4)
        fake.calls.clear()
        again, meta = discover(fake, ['US'], cache_root=self.root, now=self.now)
        self.assertEqual(again, rows)
        self.assertFalse(fake.calls)
        self.assertTrue(all(m['cache'] == 'hit' for m in meta.values()))

    def test_adr_failure_invalid_type_and_duplicate_do_not_claim_complete_scope(self):
        for mode in ('failure', 'wrong_type', 'duplicate', 'bad_envelope'):
            fake = AdrToss()
            def get(path, params):
                response = fake(path, params)
                if path == '/api/v1/stocks/all' and params['market'] == 'NYSE' and params['securityType'] == 'DEPOSITARY_RECEIPT':
                    if mode == 'failure':
                        raise ValueError('market_http_403')
                    if mode == 'wrong_type':
                        response['result'][0]['securityType'] = 'ETF'
                    if mode == 'duplicate':
                        response['result'][0]['symbol'] = 'IBM'
                    if mode == 'bad_envelope':
                        response['result'] = None
                return response
            with self.subTest(mode=mode):
                rows, meta = discover(get, ['US'], cache_root=self.root/mode, now=self.now)
                self.assertEqual(meta['NYSE']['state'], 'unavailable')
                self.assertNotIn('NVO', {r['symbol'] for r in rows})
                self.assertFalse((self.root/mode/'NYSE.json').exists())
                if mode == 'failure':
                    self.assertEqual(meta['NYSE']['error_code'], 'market_http_403')

    def test_cross_exchange_conflicting_identity_rejected_on_live_and_cache_paths(self):
        fake = AdrToss()
        def get(path, params):
            result = fake(path, params)
            if path == '/api/v1/stocks/all' and params['market'] == 'NASDAQ':
                if params['securityType'] == 'STOCK':
                    result['result'][0]['symbol'] = 'NVO'
                    result['result'][0]['isinCode'] = 'OTHER-ISIN'
            return result
        for attempt in range(2):
            with self.subTest(attempt=attempt), self.assertRaisesRegex(ValueError, 'cross_exchange_listing_identity_conflict'):
                discover(get, ['US'], cache_root=self.root, now=self.now)
            if attempt == 0:
                fake.calls.clear()
        self.assertFalse(fake.calls, 'second attempt must reject cached conflicting identities too')

    def test_user_exclusion_applies_to_adr_before_detail_requests(self):
        fake = AdrToss()
        policy = {'sha256': 'a'*64, 'records': [{'market': 'US', 'symbol': 'NVO', 'reason': 'user_excluded'}]}
        rows, meta = discover(fake, ['US'], now=self.now, exclusions=policy)
        self.assertNotIn('NVO', {r['symbol'] for r in rows})
        self.assertIn('TSM', {r['symbol'] for r in rows})
        self.assertEqual(meta['NYSE']['approved_exclusions'][0]['symbol'], 'NVO')
        for path, params in fake.calls:
            if path != '/api/v1/stocks/all':
                self.assertNotIn('NVO', params['symbols'].split(','))

    def test_real_daily_adapter_and_http_search_detail_include_collected_nvo(self):
        days = ['2026-09-09', '2026-09-10', '2026-09-11']
        rule = dict(ma_period=2, slope_window=1, transition_window=1, rs_period=1, activity_window=1,
                    min_rs=0, min_activity_ratio=1, min_average_value=0)
        config = {'profile': {'status': 'review_only', 'KR': rule, 'US': rule},
                  'universe_mode': 'exchange_listings', 'markets': ['US']}
        def candles(get, symbol, market, sessions, *, benchmark=None, provider_symbol=None, adjusted=True):
            return dict(symbol=symbol, market=market, currency='USD', benchmark=benchmark,
                        provider='Toss OpenAPI candles', provider_symbol=provider_symbol or symbol,
                        instrument_type='EQUITY' if benchmark else 'BENCHMARK_PROXY',
                        bars=[dict(date=d, open=10, high=12, low=9, close=11, volume=100) for d in days])
        with patch.object(hybrid, 'completed_sessions', return_value=days), patch.object(hybrid, 'daily_series', side_effect=candles):
            snapshot = hybrid.collect(config, {}, self.root/'cache', AdrToss(), now=self.now)
        self.assertEqual({r['symbol'] for r in snapshot['series']}, {'AAPL', 'IBM', 'NVO', 'TSM'})
        self.assertEqual(snapshot['errors'], [])
        quality = audit(snapshot)
        self.assertEqual(quality['processing_state'], 'completed_with_exceptions')
        self.assertEqual(quality['scope_basis'], 'toss_provided_active_common_stock_and_us_depositary_receipts')
        self.assertEqual(quality['unexpected_failures'], 0)
        run = self.root/'runtime/run'
        source = self.root/'runtime/source/snapshot.json'
        write(source, snapshot)
        write(run/'run.json', {'state': quality['state'], 'processing_state': quality['processing_state'], 'market_source': str(source.parent)})
        write(run/'manifest.json', {'output_hashes': {'run.json': sha(run/'run.json')}, 'input_hashes': {str(source): sha(source)}})
        stocks = saved_market(self.root, run)['stocks']
        handler = handler_for({'market': {'entries': []}}, stocks, self.root, 'synthetic', self.root/'workspace.sqlite3')
        server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection('127.0.0.1', server.server_port)
            connection.request('GET', '/api/stock-search?q=nvo')
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.read())['items'][0]['key'], 'US:NVO')
            connection.request('GET', '/api/stock/US:NVO')
            response = connection.getresponse()
            detail = json.loads(response.read())
            self.assertEqual(response.status, 200)
            self.assertEqual(detail['name'], '노보 노디스크 ADR')
            self.assertEqual(len(detail['bars']), 3)
            connection.request('GET', '/api/chart/US:NVO')
            response = connection.getresponse()
            self.assertEqual(len(json.loads(response.read())['bars']), 3)
            connection.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == '__main__':
    unittest.main()
