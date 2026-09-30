import io
import gzip
import json
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

    def test_token_ip_denial_plain_and_gzip_preserves_only_safe_code(self):
        body = json.dumps({'error': 'access_denied', 'error_description': 'IP address not allowed',
                           'access_token': 'private-token', 'client_secret': 'private-secret'}).encode()
        for raw, headers in ((body, {}), (gzip.compress(body), {'Content-Encoding': 'gzip'})):
            with self.subTest(headers=headers):
                stream = io.BytesIO(raw)
                error = urllib.error.HTTPError('https://openapi.tossinvest.com/oauth2/token', 403,
                                               'denied', headers, stream)
                with patch('toss_market_client.get_token', side_effect=error) as auth, \
                        patch('toss_market_client.urllib.request.urlopen') as opening:
                    with self.assertRaisesRegex(MarketReadError, '^market_authentication_http_403_ip_not_allowed$'):
                        TossMarketClient()
                    auth.assert_called_once()
                    opening.assert_not_called()
                self.assertTrue(stream.closed)

    def test_unrecognized_or_oversized_error_stays_generic(self):
        cases = [(b'private-body', {}),
                 (b'{"error":"access_denied","error_description":"private-secret"}', {}),
                 (b'{"error":"invalid_client","error_description":"IP address not allowed"}', {}),
                 (b'["IP address not allowed"]', {}),
                 (b'x' * 4097, {}),
                 (gzip.compress(b'x' * 100000), {'Content-Encoding': 'gzip'}),
                 (b'bad-gzip', {'Content-Encoding': 'gzip'}),
                 (gzip.compress(b'{}')[:-3], {'Content-Encoding': 'gzip'}),
                 (b'private-body', {'Content-Encoding': 'br'})]
        for raw, headers in cases:
            with self.subTest(size=len(raw), headers=headers):
                stream = io.BytesIO(raw)
                error = urllib.error.HTTPError('url', 403, 'denied', headers, stream)
                with patch('toss_market_client.get_token', side_effect=error):
                    with self.assertRaisesRegex(MarketReadError, '^market_authentication_http_403$'):
                        TossMarketClient()
                self.assertTrue(stream.closed)

    def test_market_ip_denial_stops_requests_without_exposing_body(self):
        events = []
        self.client._observer = events.append
        body = b'{"error":"access_denied","error_description":"IP address not allowed","token":"private-token"}'
        error = urllib.error.HTTPError('url', 403, 'denied', {}, io.BytesIO(body))
        with patch('toss_market_client.urllib.request.urlopen', side_effect=error) as opening:
            with self.assertRaisesRegex(MarketReadError, '^market_http_403_ip_not_allowed$'):
                self.client('/api/v1/candles', {})
            with self.assertRaisesRegex(MarketReadError, '^market_authentication_unavailable$'):
                self.client('/api/v1/candles', {})
            opening.assert_called_once()
        self.assertNotIn('private-token', str(events))
        self.assertIsNone(events[0]['payload'])


if __name__ == '__main__': unittest.main()
