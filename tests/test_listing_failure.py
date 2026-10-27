"""Provider listing denial must survive the active-scope filter unchanged."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import market.toss_collection as h
import test_hybrid_market as base
from market.providers.toss_client import MarketReadError


class ListingFailureTests(unittest.TestCase):
    def setUp(self):
        self.fixture = base.TossMarketPipelineTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.config.update(universe_mode='exchange_listings', markets=['KR', 'US'])
        self.fixture.config.pop('universe')

    def test_listing_denials_keep_original_cause_without_candle_calls(self):
        for code in ('market_http_403', 'market_http_403_ip_not_allowed',
                     'market_http_401', 'market_transport_or_payload_unavailable'):
            with self.subTest(code=code):
                calls = []
                def denied(path, params):
                    calls.append(path)
                    raise MarketReadError(code if len(calls) == 1 else 'market_authentication_unavailable')
                with self.assertRaisesRegex(ValueError, '^' + code + '$'):
                    h.collect(self.fixture.config, {}, self.fixture.root, denied, now=self.fixture.now)
                self.fixture.candles.assert_not_called()
                self.assertTrue(all(path == '/api/v1/stocks/all' for path in calls))

    def test_unknown_listing_failure_is_safe_and_not_an_exclusion_error(self):
        meta = {'KOSPI': {'state': 'unavailable', 'error_code': 'market_403: private response body'}}
        with patch.object(h, 'discover', return_value=([], meta)):
            with self.assertRaisesRegex(ValueError, '^market_listing_unavailable$'):
                self.fixture.run_collect()
        self.fixture.candles.assert_not_called()

    def test_partial_listing_keeps_error_code_and_available_market(self):
        rows = [{'market': 'KR', 'symbol': '005930.KS', 'provider_symbol': '005930',
                 'benchmark': '^KS11', 'listing_exchange_family': 'KOSPI'}]
        meta = {'KOSPI': {'state': 'available'},
                'NASDAQ': {'state': 'unavailable', 'reason': 'listing_unavailable',
                           'error_code': 'market_http_403'}}
        with patch.object(h, 'discover', return_value=(rows, meta)):
            result = self.fixture.run_collect()
        self.assertEqual(result['coverage']['scope_errors'][0]['error_code'], 'market_http_403')
        self.assertFalse(result['coverage']['active_scope_complete'])
        self.assertEqual([r['symbol'] for r in result['series']], ['005930.KS'])

    def test_listing_failure_does_not_drop_explicit_additional_targets(self):
        self.fixture.config['additional_universe'] = [
            {'market': 'KR', 'symbol': '005930.KS', 'provider_symbol': '005930', 'benchmark': '^KS11'}]
        meta = {'KOSPI': {'state': 'unavailable', 'error_code': 'market_http_403'}}
        with patch.object(h, 'discover', return_value=([], meta)):
            result = self.fixture.run_collect()
        self.assertEqual([r['symbol'] for r in result['series']], ['005930.KS'])
        self.assertEqual(result['coverage']['scope_errors'][0]['error_code'], 'market_http_403')
        self.assertFalse(result['coverage']['active_scope_complete'])

    def test_actual_exclusion_of_every_listed_stock_still_has_distinct_error(self):
        rows = [{'market': 'US', 'symbol': 'EXAMPLE', 'provider_symbol': 'EXAMPLE',
                 'benchmark': '^GSPC', 'listing_exchange_family': 'NASDAQ'}]
        meta = {'NASDAQ': {'state': 'available'}}
        policy = {'records': [{'market': 'US', 'symbol': 'EXAMPLE', 'reason': 'user_excluded'}],
                  'lifecycle_records': [], 'scope': 'market_symbol_until_user_restores',
                  'path': 'test', 'sha256': 'a' * 64}
        with patch.object(h, 'discover', return_value=(rows, meta)), \
                patch.object(h.market_scope, 'load', return_value=policy):
            with self.assertRaisesRegex(ValueError, '^market_scope_empty_after_exclusions$'):
                self.fixture.run_collect()
        self.fixture.candles.assert_not_called()


if __name__ == '__main__':
    unittest.main()
