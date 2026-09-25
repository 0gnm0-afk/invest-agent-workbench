"""Bounded, rate-aware market GET client. Never accepts account/order endpoints."""
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from toss_api import BASE, get_token


class MarketReadError(ValueError):
    pass


class TossMarketClient:
    def __init__(self, observer=None):
        self._observer = observer
        try:
            self._token = get_token()
        except urllib.error.HTTPError as exc:
            status = exc.code
            exc.close()
            raise MarketReadError(f'market_authentication_http_{status}') from None
        except (Exception, SystemExit):
            raise MarketReadError('market_authentication_unavailable') from None
        self._lock = threading.Lock()
        self._next = 0.0
        self._interval = 1.0  # Safe first request for the lowest market group limit.
        self._auth_failed = False

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
        for attempt in range(3):
            self._wait()
            started = time.monotonic()
            request = urllib.request.Request(url, headers={'Authorization': 'Bearer ' + self._token})
            try:
                with urllib.request.urlopen(request, timeout=15) as response:
                    limit = response.headers.get('X-RateLimit-Limit')
                    if limit and limit.isdigit() and int(limit) > 0:
                        with self._lock:
                            self._interval = max(.25, 1 / int(limit))
                    payload = json.load(response)
                    self._observe(path, params, 200, response.headers, started, payload)
                    return payload
            except urllib.error.HTTPError as exc:
                status = exc.code
                self._observe(path, params, status, exc.headers, started)
                retry = exc.headers.get('Retry-After') or exc.headers.get('X-RateLimit-Reset') or '1'
                exc.close()
                if status in (401, 403):
                    with self._lock:
                        self._auth_failed = True
                if status not in (429, 500, 502, 503, 504) or attempt == 2:
                    raise MarketReadError(f'market_http_{status}') from None
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
