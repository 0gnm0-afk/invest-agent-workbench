"""Market data usability, distinct from receipt and full-history completeness."""
from datetime import datetime
from math import isfinite
import json
from pathlib import Path

from invest_agent.trading.market import usable_tail
from toss_candles import daily_series

LIFECYCLE = json.loads(Path(__file__).with_name('market_lifecycle.json').read_text(encoding='utf-8'))['records']


def quote_time_state(stamp):
    if stamp is None:
        return 'not_provided'
    try:
        return 'provided' if datetime.fromisoformat(stamp).tzinfo else 'invalid'
    except (ValueError, TypeError):
        return 'invalid'


def complete_cap(get, row, sessions):
    """Only a dated, unadjusted completed close can replace an undated quote."""
    if row.get('quote_time_state') != 'not_provided':
        return {}
    if row.get('tradable') is False:
        return {'market_cap': None, 'market_cap_as_of': None,
                'market_cap_quality': 'unavailable_not_tradable'}
    shares = row.get('shares_outstanding')
    if isinstance(shares, bool) or not isinstance(shares, int) or shares <= 0:
        return {'market_cap': None, 'market_cap_as_of': None,
                'market_cap_quality': 'unavailable_shares'}
    raw = daily_series(get, row['symbol'], row['market'], sessions[-1:],
                       provider_symbol=row.get('provider_symbol'), adjusted=False, max_pages=1)
    bar = usable_tail(raw['bars'], sessions[-1:])[-1]
    cap = bar['close'] * shares
    if not isfinite(cap) or cap <= 0:
        raise ValueError('invalid_completed_close_market_cap')
    return {'market_cap': cap, 'market_cap_as_of': bar['date'],
            'market_cap_currency': raw['currency'],
            'market_cap_basis': 'toss_unadjusted_completed_close_times_observed_shares_estimate',
            'market_cap_quality': 'completed_close_estimate',
            'market_cap_price': bar['close'],
            'market_cap_price_date': bar['date'],
            'market_cap_price_source': raw['source_url'],
            'market_cap_price_basis': raw['price_basis']}


def error_disposition(error, row, completed_session):
    # A security identifier and bounded observation date prevent ticker reuse
    # or a historical run from inheriting this verified corporate action.
    for record in LIFECYCLE:
        if (all(row.get(k) == record[k] for k in ('market', 'symbol', 'isin_code'))
                and record['effective_session'] <= completed_session <= record['verified_on']
                and error.get('error_code') in ('stale_or_invalid_last_session', 'no_completed_session_bars')):
            return {**record, 'analysis_allowed': False}
    code = error.get('error_code')
    if code in ('stale_or_invalid_last_session', 'no_completed_session_bars'):
        return {'state': 'quarantined_' + code, 'analysis_allowed': False,
                'cause': 'unconfirmed', 'retry_policy': 'next_requested_collection_no_same_run_loop'}
    return {'state': 'collection_failure', 'analysis_allowed': False,
            'cause': code or 'unknown'}


def history_usability(bars, sessions, rule):
    tail = usable_tail(bars, sessions)
    needed = max(rule['ma_period'] + rule['slope_window'] + rule['transition_window'],
                 rule['rs_period'] + 1, rule['activity_window'] + 1)
    return {'continuous_bars': len(tail), 'continuous_start': tail[0]['date'],
            'as_of': tail[-1]['date'], 'liquidity_20_bars_available': len(tail) >= 20,
            'profile_required_bars': needed, 'profile_history_available': len(tail) >= needed,
            'sperandeo_history_available': {str(n): len(tail) >= n for n in (63, 126, 252)},
            'scope': 'history_length_only_not_candidate_or_all_inputs_ready'}
