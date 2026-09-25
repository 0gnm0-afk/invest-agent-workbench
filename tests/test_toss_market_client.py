import io
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from toss_market_client import TossMarketClient, MarketReadError


def response():
    value = io.BytesIO(b'{"result":{"candles":[]}}')
    value.headers = {'X-RateLimit-Limit': '2'}
    return value


class MarketClientTests(unittest.TestCase):
    def setUp(self):
        with patch('toss_market_client.get_token', return_value='synthetic-token'):
            self.client = TossMarketClient()

    def test_account_and_order_endpoints_never_requested(self):
        with patch('toss_market_client.urllib.request.urlopen') as opening:
            for path in ('/api/v1/accounts', '/api/v1/orders', 'https://other.example/api/v1/candles'):
                with self.assertRaises(MarketReadError): self.client(path, {})
            opening.assert_not_called()

    def test_provider_rate_limit_reduces_request_frequency(self):
        with patch('toss_market_client.urllib.request.urlopen', return_value=response()), patch.object(self.client, '_wait'):
            self.assertIn('result', self.client('/api/v1/candles', {}))
        self.assertEqual(self.client._interval, .5)

    def test_only_market_reference_endpoints_are_added_to_allowlist(self):
        with patch('toss_market_client.urllib.request.urlopen', side_effect=lambda *args, **kwargs: response()) as opening, patch.object(self.client, '_wait'):
            for path in ('/api/v1/stocks/all', '/api/v1/stocks', '/api/v1/prices'):
                self.client(path, {})
        self.assertEqual(opening.call_count, 3)

    def test_429_retries_bounded_without_error_body(self):
        errors = [urllib.error.HTTPError('url', 429, 'busy', {'Retry-After': '0'}, io.BytesIO(b'private-body')) for _ in range(3)]
        with patch('toss_market_client.urllib.request.urlopen', side_effect=errors) as opening, patch.object(self.client, '_wait'), patch('toss_market_client.time.sleep'):
            with self.assertRaisesRegex(MarketReadError, '^market_http_429$'):
                self.client('/api/v1/candles', {})
            self.assertEqual(opening.call_count, 3)

    def test_auth_failure_stops_subsequent_requests(self):
        error = urllib.error.HTTPError('url', 401, 'denied', {}, io.BytesIO(b'private-body'))
        with patch('toss_market_client.urllib.request.urlopen', side_effect=error) as opening:
            with self.assertRaisesRegex(MarketReadError, '^market_http_401$'):
                self.client('/api/v1/candles', {})
            with self.assertRaisesRegex(MarketReadError, '^market_authentication_unavailable$'):
                self.client('/api/v1/candles', {})
            self.assertEqual(opening.call_count, 1)

    def test_token_403_is_specific_and_never_exposes_response_body(self):
        error = urllib.error.HTTPError('url', 403, 'denied', {}, io.BytesIO(b'private-body'))
        with patch('toss_market_client.get_token', side_effect=error), \
                patch('toss_market_client.urllib.request.urlopen') as opening:
            with self.assertRaisesRegex(MarketReadError, '^market_authentication_http_403$'):
                TossMarketClient()
            opening.assert_not_called()

    def test_observer_records_only_market_response_and_rate_headers(self):
        events = []
        self.client._observer = events.append
        with patch('toss_market_client.urllib.request.urlopen', return_value=response()), patch.object(self.client, '_wait'):
            self.client('/api/v1/candles', {'symbol': 'EXAMPLE'})
        self.assertEqual(events[0]['status'], 200)
        self.assertEqual(events[0]['headers'], {'X-RateLimit-Limit': '2'})
        self.assertNotIn('synthetic-token', str(events))


if __name__ == '__main__': unittest.main()
