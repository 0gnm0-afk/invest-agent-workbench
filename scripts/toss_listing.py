"""Shared, explicit Toss listing scope for common stocks and US depositary receipts."""
import re


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
