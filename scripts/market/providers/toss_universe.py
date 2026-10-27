"""Private Toss stock-universe discovery with explicit derived market caps."""
import json
import math
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from invest_agent.trading.contracts import digest
from invest_agent.trading.runner import atomic_write
from market.quality import quote_time_state, adr_cap_unavailable
from market.scope import match as excluded_record

EXCHANGES = {'KR': ('KOSPI',), 'US': ('NASDAQ', 'NYSE')}
BATCH_SIZE = 200
CACHE_MAX_AGE_SECONDS = 36 * 60 * 60
CACHE_SCHEMA = 3


def listing_requests(exchange, *, include_adr=True):
    if exchange not in ('KOSPI', 'KOSDAQ', 'NASDAQ', 'NYSE'):
        raise ValueError('unsupported_listing_exchange')
    requests = [dict(market=exchange, status='ACTIVE', securityType='STOCK', commonShare='true')]
    if include_adr and exchange in ('NASDAQ', 'NYSE'):
        # ADRs are a separate security type; their common-share flag need not be true.
        requests.append(dict(market=exchange, status='ACTIVE', securityType='DEPOSITARY_RECEIPT'))
    return requests


def validate_listing_rows(rows, params):
    kind = params['securityType']
    if not isinstance(rows, list) or (kind == 'STOCK' and not rows):
        raise ValueError('listing_unavailable')
    seen = set()
    for row in rows:
        symbol = row.get('symbol') if isinstance(row, dict) else None
        if not isinstance(symbol, str) or not re.fullmatch(r'[A-Za-z0-9.\-]+', symbol) or symbol in seen:
            raise ValueError('invalid_listing_symbol')
        seen.add(symbol)
        if kind == 'STOCK':
            valid = row.get('securityType') in (None, 'STOCK') and row.get('isCommonShare') is not False
        else:
            valid = kind == 'DEPOSITARY_RECEIPT' and row.get('securityType') == kind
        if not valid:
            raise ValueError('listing_filter_mismatch')
    return rows


def _batches(values):
    for start in range(0, len(values), BATCH_SIZE):
        yield values[start:start + BATCH_SIZE]


def _result(get, path, params):
    envelope = get(path, params)
    value = envelope.get('result') if isinstance(envelope, dict) else None
    if not isinstance(value, list):
        raise ValueError('Invalid Toss list envelope')
    return value


def _number(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not number.is_finite() or number < 0:
        return None
    return number


def _cache_read(path, now):
    try:
        stored = json.loads(path.read_text(encoding='utf-8'))
        observed = datetime.fromisoformat(stored['observed_at'])
        if observed.tzinfo is None or not 0 <= (now - observed).total_seconds() <= CACHE_MAX_AGE_SECONDS:
            return None
        if (stored.get('schema') != CACHE_SCHEMA or digest(stored['rows']) != stored['hash']
                or stored['meta'].get('state') != 'available'):
            return None
        return stored['rows'], {**stored['meta'], 'cache': 'hit'}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _cache_write(path, rows, meta, observed_at):
    payload = {'schema': CACHE_SCHEMA, 'observed_at': observed_at, 'rows': rows, 'meta': meta, 'hash': digest(rows)}
    atomic_write(path, json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _live_exchange(get, exchange, observed_at, exclusions=None):
    base = []
    listing_counts = {}
    for params in listing_requests(exchange):
        rows = validate_listing_rows(_result(get, '/api/v1/stocks/all', params), params)
        listing_counts[params['securityType']] = len(rows)
        base.extend(rows)
    # A conflicting/repeated identity must never be silently overwritten or counted twice.
    if len({row['symbol'] for row in base}) != len(base):
        raise ValueError('Duplicate Toss listing symbol')
    symbols = []
    excluded = []
    by_symbol = {}
    for item in base:
        symbol = item['symbol']
        market = 'KR' if exchange == 'KOSPI' else 'US'
        record = excluded_record({'market':market,'symbol':symbol+'.KS' if market=='KR' else symbol,
                                  'isin_code':item.get('isinCode'),'exchange':exchange,
                                  'name':item.get('name'),'english_name':item.get('englishName'),
                                  'securityType':item.get('securityType')}, exclusions) if exclusions else None
        if record:
            excluded.append({**record,'exchange':exchange,'observed_isin_code':item.get('isinCode')})
            continue
        symbols.append(symbol)
        by_symbol[symbol] = item

    details = {}
    prices = {}
    failures = []
    for batch_number, batch in enumerate(_batches(symbols), start=1):
        joined = ','.join(batch)
        for stage, path, target in (
            ('stock_detail', '/api/v1/stocks', details),
            ('current_price', '/api/v1/prices', prices),
        ):
            try:
                rows = _result(get, path, {'symbols': joined})
                for row in rows:
                    symbol = row.get('symbol') if isinstance(row, dict) else None
                    if symbol in by_symbol:
                        target[symbol] = row
            except (Exception, SystemExit) as exc:
                failures.append({'stage': stage, 'batch': batch_number, 'error_type': type(exc).__name__})

    rows = []
    missing_enrichment = 0
    missing_price_time = 0
    expected_currency = 'KRW' if exchange == 'KOSPI' else 'USD'
    for provider_symbol in symbols:
        listed = by_symbol[provider_symbol]
        detail = details.get(provider_symbol, {})
        price = prices.get(provider_symbol, {})
        shares = _number(detail.get('sharesOutstanding'))
        last_price = _number(price.get('lastPrice'))
        currency = detail.get('currency') or price.get('currency') or expected_currency
        cap_guard = adr_cap_unavailable(listed)
        cap = None
        if not cap_guard and shares is not None and last_price is not None and currency == expected_currency:
            raw_cap = shares * last_price
            cap = float(raw_cap)
            if not math.isfinite(cap):
                cap = None
        time_state = quote_time_state(price.get('timestamp'))
        missing_price_time += time_state == 'not_provided'
        if not detail or not price or (cap is None and not cap_guard) or time_state == 'invalid':
            missing_enrichment += 1
        market = 'KR' if exchange == 'KOSPI' else 'US'
        engine_symbol = provider_symbol + '.KS' if market == 'KR' else provider_symbol
        korean = detail.get('koreanMarketDetail') or {}
        rows.append({
            'market': market,
            'symbol': engine_symbol,
            'provider_symbol': provider_symbol,
            'benchmark': '^KS11' if market == 'KR' else '^GSPC',
            'name': detail.get('name') or listed.get('name') or provider_symbol,
            'english_name': detail.get('englishName'),
            'isin_code': detail.get('isinCode') or listed.get('isinCode'),
            'raw_exchange_code': detail.get('market') or exchange,
            'listing_exchange_family': exchange,
            'instrument_type': 'EQUITY',
            'security_type': listed.get('securityType') or 'STOCK',
            'is_common_share': listed.get('isCommonShare'),
            'currency': currency,
            'tradable': detail.get('status', 'ACTIVE') == 'ACTIVE' and korean.get('krxTradingSuspended') is not True,
            'shares_outstanding': int(shares) if shares is not None and shares == shares.to_integral_value() else None,
            'shares_outstanding_observed_at': observed_at,
            'market_cap': cap if time_state == 'provided' else None,
            'undated_quote_market_cap': cap if time_state == 'not_provided' else None,
            'last_price': float(last_price) if last_price is not None else None,
            'quote_timestamp': price.get('timestamp'),
            'quote_time_state': time_state,
            'quote_observed_at': observed_at,
            'market_cap_quality': ('unavailable_shares' if shares is None or shares <= 0 else
                                   'unavailable_price_or_currency' if cap is None or cap <= 0 else
                                   'quoted_price' if time_state == 'provided' else 'unavailable_quote_time'),
            'market_cap_currency': expected_currency if cap is not None else None,
            'market_cap_as_of': price.get('timestamp'),
            'market_cap_basis': 'toss_last_price_times_shares_outstanding_derived',
            'listing_observed_at': observed_at,
            **cap_guard,
        })
    rows.sort(key=lambda row: (row['market_cap'] is None, -(row['market_cap'] or 0), row['symbol']))
    state = 'available' if not failures and not missing_enrichment else 'partial' if rows else 'unavailable'
    meta = {
        'state': state,
        'reason': None if state == 'available' else 'listing_enrichment_partial',
        'listed': len(base),
        'listing_types': listing_counts,
        'eligible_by_listing_heuristic': len(symbols),
        'approved_exclusions': excluded,
        'enriched': len(symbols) - missing_enrichment,
        'missing_enrichment': missing_enrichment,
        'missing_price_time': missing_price_time,
        'unverified_adr_caps': sum(r['market_cap_quality'] == 'unavailable_adr_share_basis' for r in rows),
        'batch_failures': failures,
        'ordering': 'derived_market_cap_descending',
        'market_cap_basis': 'Toss /prices lastPrice × /stocks sharesOutstanding',
        'cache': 'miss',
    }
    return rows, meta


def discover(get, markets, limit_per_exchange=None, *, cache_root=None, now=None, exclusions=None):
    if limit_per_exchange is not None and (
        isinstance(limit_per_exchange, bool) or not isinstance(limit_per_exchange, int) or limit_per_exchange < 1
    ):
        raise ValueError('limit_per_exchange must be positive or null')
    if not markets or any(market not in EXCHANGES for market in markets):
        raise ValueError('Invalid universe markets')
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError('Universe observation time requires timezone')
    observed_at = now.isoformat()
    cache_root = Path(cache_root).resolve() if cache_root is not None else None
    universe = []
    metadata = {}
    for market in ('KR', 'US'):
        if market not in markets:
            continue
        for exchange in EXCHANGES[market]:
            cache_path = cache_root / (exchange + ('-'+exclusions['sha256'][:12] if exclusions else '') + '.json') if cache_root else None
            cached = _cache_read(cache_path, now) if cache_path and cache_path.is_file() else None
            try:
                rows, meta = cached or _live_exchange(get, exchange, observed_at, exclusions)
                if cache_path and cached is None and meta['state'] == 'available':
                    _cache_write(cache_path, rows, meta, observed_at)
            except (Exception, SystemExit) as exc:
                metadata[exchange] = {
                    'state': 'unavailable', 'reason': 'listing_unavailable',
                    'error_type': type(exc).__name__, 'listed': None,
                    'error_code': str(exc) if str(exc).startswith('market_') else type(exc).__name__,
                    'eligible_by_listing_heuristic': None, 'requested': 0,
                    'limit': limit_per_exchange, 'cache': 'miss',
                }
                continue
            selected = rows if limit_per_exchange is None else rows[:limit_per_exchange]
            metadata[exchange] = {**meta, 'requested': len(selected), 'limit': limit_per_exchange}
            universe.extend(selected)
    unique = {}
    for row in universe:
        key = (row['market'], row['symbol'])
        previous = unique.get(key)
        if previous and any(previous.get(field) != row.get(field)
                            for field in ('isin_code', 'security_type', 'currency')):
            raise ValueError('cross_exchange_listing_identity_conflict')
        # Preserve legacy deduplication only for the same security identity.
        unique.setdefault(key, row)
    return list(unique.values()), metadata
