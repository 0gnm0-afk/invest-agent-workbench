"""Bounded, rate-aware market GET client. Never accepts account/order endpoints."""
import http.client
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib

from market.providers.toss_api import BASE, get_token


class MarketReadError(ValueError):
    pass


def _http_error_code(exc, prefix):
    """Recognize one provider denial; never expose arbitrary HTTP content."""
    code = f'{prefix}_{exc.code}'
    if exc.code != 403:
        return code
    try:
        # Both wire and decompressed bodies are bounded (including gzip bombs).
        raw = exc.read(4097)
        if len(raw) > 4096:
            return code
        encoding = (exc.headers or {}).get('Content-Encoding', '').lower()
        if encoding == 'gzip':
            decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
            raw = decoder.decompress(raw, 4097)
            if len(raw) > 4096 or not decoder.eof or decoder.unused_data:
                return code
        elif encoding not in ('', 'identity'):
            return code
        body = json.loads(raw)
        if (isinstance(body, dict) and body.get('error') == 'access_denied'
                and body.get('error_description') == 'IP address not allowed'):
            return code + '_ip_not_allowed'
    except (OSError, ValueError, zlib.error, http.client.HTTPException):
        pass
    return code


class TossMarketClient:
    def __init__(self, observer=None, *, min_interval=.25):
        if isinstance(min_interval, bool) or not isinstance(min_interval, (int, float)) or not .05 <= min_interval <= 1:
            raise ValueError('invalid_market_request_interval')
        self._min_interval = min_interval
        self._observer = observer
        self._lock = threading.Lock()
        self._auth_lock = threading.Lock()
        self._next = 0.0
        self._interval = 1.0
        self._auth_failed = False
        self._token = self._current_token()

    def _current_token(self, rejected_token=None):
        with self._auth_lock:
            with self._lock:
                if self._auth_failed:
                    raise MarketReadError('market_authentication_unavailable')
            try:
                value = get_token() if rejected_token is None else get_token(rejected_token=rejected_token)
            except urllib.error.HTTPError as exc:
                try:
                    code = _http_error_code(exc, 'market_authentication_http')
                finally:
                    exc.close()
            except (Exception, SystemExit):
                code = 'market_authentication_unavailable'
            else:
                self._token = value
                return value
            with self._lock:
                self._auth_failed = True
            raise MarketReadError(code) from None

    def _observe(self, path, params, status, headers, started, payload=None):
        if self._observer:
            self._observer({'at': time.time(), 'path': path, 'params': params,
                            'status': status, 'elapsed_seconds': time.monotonic() - started,
                            'headers': {key: headers.get(key) for key in (
                                'X-RateLimit-Limit', 'X-RateLimit-Remaining',
                                'X-RateLimit-Reset', 'Retry-After') if headers.get(key) is not None},
                            'payload': payload})

    def _wait(self):
        while True:
            with self._lock:
                if self._auth_failed:
                    raise MarketReadError('market_authentication_unavailable')
                delay = max(0, self._next - time.monotonic())
                if not delay:
                    self._next = time.monotonic() + self._interval
                    return
            time.sleep(min(delay, 60))

    def __call__(self, path, params):
        allowed = {
            '/api/v1/candles',
            '/api/v1/market-indicators/KOSPI/candles',
            '/api/v1/market-indicators/KOSDAQ/candles',
            '/api/v1/stocks/all',
            '/api/v1/stocks',
            '/api/v1/prices',
        }
        if path not in allowed:
            raise MarketReadError('market_endpoint_not_allowed')
        url = BASE + path + '?' + urllib.parse.urlencode(params)
        attempt = 0
        refreshed = False
        while attempt < 3:
            self._wait()
            request_token = self._current_token()
            started = time.monotonic()
            request = urllib.request.Request(url, headers={'Authorization': 'Bearer ' + request_token})
            try:
                with urllib.request.urlopen(request, timeout=15) as response:
                    limit = response.headers.get('X-RateLimit-Limit')
                    if limit and limit.isdigit() and int(limit) > 0:
                        with self._lock:
                            self._interval = max(self._min_interval, 1 / int(limit))
                    payload = json.load(response)
                    self._observe(path, params, 200, response.headers, started, payload)
                    return payload
            except urllib.error.HTTPError as exc:
                status = exc.code
                self._observe(path, params, status, exc.headers, started)
                retry = exc.headers.get('Retry-After') or exc.headers.get('X-RateLimit-Reset') or '1'
                try:
                    code = _http_error_code(exc, 'market_http')
                finally:
                    exc.close()
                if status == 401 and not refreshed:
                    refreshed = True
                    self._current_token(rejected_token=request_token)
                    continue
                if status in (401, 403):
                    with self._lock:
                        self._auth_failed = True
                if status not in (429, 500, 502, 503, 504) or attempt == 2:
                    raise MarketReadError(code) from None
                try:
                    delay = float(retry)
                    if not 0 <= delay <= 60:
                        raise ValueError()
                except ValueError:
                    raise MarketReadError('market_retry_delay_unavailable') from None
                with self._lock:
                    self._next = max(self._next, time.monotonic() + max(delay, 1))
                time.sleep(max(delay, 1))
            except (OSError, ValueError) as exc:
                if isinstance(exc, MarketReadError):
                    raise
                self._observe(path, params, 'transport_or_payload_error', {}, started)
                if attempt == 2:
                    raise MarketReadError('market_transport_or_payload_unavailable') from None
                time.sleep(attempt + 1)
            attempt += 1
