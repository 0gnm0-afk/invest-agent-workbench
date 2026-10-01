"""Read a pinned, fully audited US history collection without network calls."""
import hashlib
import json
import math
from pathlib import Path


def packed(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode('utf-8')


class SavedUSHistory:
    def __init__(self, pointer):
        self.pointer = Path(pointer)
        self.enabled = self.pointer.exists()
        self.stocks = {}
        self.error = False
        if not self.enabled:
            return
        try:
            active = json.loads(self.pointer.read_bytes())
            name = active['collection']
            if not isinstance(name, str) or Path(name).name != name or name in ('.', '..'):
                raise ValueError('invalid_collection')
            self.root = self.pointer.parent / name
            raw = (self.root / 'manifest.json').read_bytes()
            if hashlib.sha256(raw).hexdigest() != active['manifest_sha256']:
                raise ValueError('manifest_mismatch')
            self.manifest = {k.replace('\\', '/'): v for k, v in json.loads(raw).items()}
            self.config = self.read('collection.json', wrapped=True)
            verification = self.read('verification.json')
            universe = self.read('universe.json', wrapped=True)['rows']
            rows = self.read('coverage.json')
            self.coverage = {x['symbol']: x for x in rows}
            self.listings = {x['symbol']: x for x in universe}
            if (verification['integrity'] != 'PASS' or not verification['all_attempted']
                    or len(self.listings) != len(universe) or len(self.coverage) != len(rows)
                    or set(self.listings) != set(self.coverage)
                    or verification['verified'] != len(universe)):
                raise ValueError('incomplete_audit')
            self.stocks = {'US:' + s: dict(market='US', symbol=s, name=x.get('name') or s,
                           currency='USD', bars=[], provider='Toss OpenAPI candles')
                           for s, x in self.listings.items()}
        except (OSError, ValueError, KeyError, TypeError):
            self.error = True

    def read(self, name, wrapped=False):
        raw = (self.root / name).read_bytes()
        if hashlib.sha256(raw).hexdigest() != self.manifest.get(name):
            raise ValueError('artifact_hash_mismatch')
        value = json.loads(raw)
        if wrapped:
            if hashlib.sha256(packed(value['data'])).hexdigest() != value['sha256']:
                raise ValueError('content_hash_mismatch')
            value = value['data']
        return value

    def packet(self, stock):
        if stock.get('market') != 'US' or not self.enabled:
            return None
        info = dict(kind='local_verified_us', can_retry=False)
        try:
            if self.error:
                raise ValueError('collection_unavailable')
            symbol = stock['symbol']
            info.update(requested_start='provider_earliest_available', requested_end=self.config['end'],
                        source_id=self.root.name)
            if symbol not in self.coverage:
                return {'history': dict(info, state='unavailable', reason='saved_us_symbol_unavailable')}
            record = self.coverage[symbol]
            d = self.read('series/' + hashlib.sha256(symbol.encode()).hexdigest() + '.json', wrapped=True)
            if (d['symbol'] != symbol or d['market'] != 'US' or d['currency'] != 'USD'
                    or d['listing'] != self.listings[symbol] or d['state'] != 'received'
                    or not d['provider_end_reached'] or d['requested_end'] != self.config['end']
                    or record['bars'] != len(d['bars']) or record['state'] != d['state']
                    or not record['provider_end_reached']):
                raise ValueError('series_identity_mismatch')
            bars = d['bars']
            dates = [b['date'] for b in bars]
            if (not dates or dates != sorted(set(dates)) or dates[0] != record['first']
                    or dates[-1] != record['last'] or dates[-1] > self.config['end']):
                raise ValueError('invalid_series_dates')
            if any(type(b[k]) not in (int, float) or not math.isfinite(b[k]) or b[k] < 0
                   for b in bars for k in ('open', 'high', 'low', 'close', 'volume')):
                raise ValueError('invalid_series_value')
            invalid = [b['date'] for b in bars if not
                       0 < b['low'] <= min(b['open'], b['close']) <= max(b['open'], b['close']) <= b['high']]
            if invalid != record['invalid_ohlc']:
                raise ValueError('quality_mismatch')
            excluded = set(invalid) | set(record['non_sessions'])
            displayed = [b for b in bars if b['date'] not in excluded]
            prior = {b['date']: b for b in stock.get('bars', [])}
            different = any(b['date'] in prior and any(not math.isclose(b[k], prior[b['date']][k], rel_tol=1e-8, abs_tol=1e-8)
                            for k in ('open', 'high', 'low', 'close')) for b in displayed)
            screening_end = max(prior, default=None)
            newer_screen = bool(screening_end and screening_end > self.config['end'])
            quality = {k: record[k] for k in ('invalid_ohlc', 'non_sessions', 'missing_sessions', 'latest_session_missing')}
            quality['excluded_dates'] = sorted(excluded)
            flagged = bool(excluded or record['missing_sessions'] or record['latest_session_missing'] or newer_screen)
            info.update(state='partial' if flagged else 'complete', reason='saved_history_quality_flags' if flagged else 'saved_history_verified',
                        stored_count=len(bars), source_first=dates[0], source_last=dates[-1], quality=quality,
                        screening_as_of=screening_end, screening_price_difference=different,
                        screening_newer_than_history=newer_screen)
            source = {k: d.get(k) for k in ('market','symbol','currency','provider','price_basis','collected_at')}
            return {'series': dict(source, bars=displayed), 'history': info}
        except (OSError, ValueError, KeyError, TypeError):
            return {'history': dict(info, state='failed', reason='saved_us_history_verification_failed')}
