"""Exact research signal previews on verified completed daily bars.

Definitions: workbench Git f786c59, scripts/research/downtrend_longhistory/
legacy.py + old_signals.py; upward definitions are the preserved
uptrend_longhistory/old_signals.py. No retrospective labels or future inputs.
These are separate observations, not adopted watches or trading instructions.
"""
from datetime import date
import hashlib
import math

import numpy as np
import pandas as pd

from signals.rules import POLICY, RULES, FINAL_RESEARCH, PROPOSED_RULES


def _lag(x, n=1):
    return np.r_[np.full(n, np.nan), x[:-n]]


def _roll(x, n):
    return pd.Series(x).rolling(n).mean().to_numpy()


def _ema(x, n):
    return pd.Series(x).ewm(span=n, adjust=False, min_periods=n).mean().to_numpy()


def _wilder(x, n):
    out = np.full(len(x), np.nan)
    for i in range(n - 1, len(x)):
        if i and np.isfinite(out[i - 1]):
            out[i] = (out[i - 1] * (n - 1) + x[i]) / n
        elif np.isfinite(x[i - n + 1:i + 1]).all():
            out[i] = np.mean(x[i - n + 1:i + 1])
    return out


def _ratio(a, b):
    return np.divide(a, b, out=np.full(np.broadcast(a, b).shape, np.nan),
                     where=np.isfinite(b) & (b != 0))


def _cross_down(a, b):
    a = np.asarray(a)
    b = np.broadcast_to(b, a.shape)
    tol = 1e-12 * np.maximum(abs(a), abs(b))
    diff = a - b
    ok = np.isfinite(a) & np.isfinite(b) & np.isfinite(_lag(a)) & np.isfinite(_lag(b))
    return np.where(ok, (diff < -tol) & (_lag(diff) >= -_lag(tol)), np.nan)


def _cross_up(a, b):
    return _cross_down(-a, -np.broadcast_to(b, a.shape))


def evaluate(bars, *, completed_asof, expected_sessions=None, stock_key=None):
    """Preview six separately labeled rules. Calendar dates must cover the input range.

    No network, DB, activation, adoption, or delivery. expected_sessions must be
    the exchange calendar, not dates copied from bars. A mismatch is held rather
    than interpreted as no signal. At least 68 consecutive bars cover the frozen
    65-bar warmup plus all three days of the composite rule. Future bars are
    excluded. Event IDs are stable for stock/date/rule and can be deduplicated by
    an explicitly activated caller. Stale results never produce eligible events.
    """
    result = {'policy': POLICY, 'stock_key': stock_key, 'as_of': completed_asof,
              'mode': 'preview', 'active': False, 'state': 'deferred',
              'reason': None, 'indicators': {}, 'rules': [], 'events': [], 'research': FINAL_RESEARCH}

    def deferred(reason):
        result['reason'] = reason
        result['rules'] = [dict(id=key, purpose=side, label=label,
                                state='deferred', reason=reason)
                           for key, (side, label) in RULES.items()]
        return result

    try:
        date.fromisoformat(completed_asof)
        rows = [b for b in bars if date.fromisoformat(b['date']).isoformat() <= completed_asof]
        dates = [b['date'] for b in rows]
    except (ValueError, KeyError, TypeError):
        return deferred('invalid_dates')
    if not rows or dates[-1] != completed_asof:
        return deferred('latest_completed_bar_missing')
    if dates != sorted(set(dates)):
        return deferred('duplicate_or_unsorted_bars')
    if expected_sessions is None:
        return deferred('exchange_calendar_required')
    try:
        sessions = [date.fromisoformat(d).isoformat() for d in expected_sessions
                    if dates[0] <= d <= completed_asof]
    except (ValueError, TypeError):
        return deferred('invalid_calendar')
    if sessions != dates:
        return deferred('incomplete_history')
    if len(rows) < 68:
        return deferred('insufficient_history')
    # Preserve the research's segmentation at gaps greater than ten calendar
    # days; a post-gap segment needs its own warmup, never bridge the indicators.
    last_gap = 0
    for i in range(1, len(rows)):
        if (date.fromisoformat(dates[i]) - date.fromisoformat(dates[i-1])).days > 10:
            last_gap = i
    rows = rows[last_gap:]
    if len(rows) < 68:
        return deferred('insufficient_contiguous_history')
    for b in rows:
        values = [b.get(k) for k in ('open', 'high', 'low', 'close')]
        if any(type(v) not in (int, float) or not math.isfinite(v) or v <= 0 for v in values):
            return deferred('invalid_price')
        o, h, l, c = values
        if not l <= min(o, c) <= max(o, c) <= h:
            return deferred('invalid_ohlc')
    c, h, l = [np.array([b[k] for b in rows], dtype=float) for k in ('close', 'high', 'low')]
    delta = c - _lag(c)
    gain, loss = _wilder(np.maximum(delta, 0), 14), _wilder(np.maximum(-delta, 0), 14)
    rsi = 100 * _ratio(gain, gain + loss)
    rsi[(gain + loss) == 0] = 50
    tp = (h + l + c) / 3
    windows = np.lib.stride_tricks.sliding_window_view(tp, 20)
    mad = np.r_[np.full(19, np.nan), np.mean(abs(windows - windows.mean(1)[:, None]), 1)]
    cci = _ratio(tp - _roll(tp, 20), .015 * mad)
    ma20 = _roll(c, 20)
    macd = _ema(c, 12) - _ema(c, 26)
    signal = _ema(macd, 9)
    down60 = _cross_down(rsi, 60)
    down60[:65] = np.nan
    recent60 = pd.Series(down60).rolling(3).max().to_numpy()
    cc = _cross_up(cci, -100)
    flags = {'rsi30': _cross_up(rsi, 30), 'cci-100': cc,
             'cci_recovery': cc * (ma20 < _lag(ma20, 5)),
             'macd_signal_down': _cross_down(macd, signal), 'rsi60_down': down60,
             'macd_rsi60': _cross_down(macd, signal) * (recent60 == 1)}
    result.update(state='evaluated', bar_count=len(rows))
    result['indicators'] = {key: float(values[-1]) if np.isfinite(values[-1]) else None
                            for key, values in {'rsi14': rsi, 'cci20': cci, 'sma20': ma20,
                                                'macd12_26': macd, 'macd_signal9': signal}.items()}
    for key, (side, label) in RULES.items():
        value = flags[key][-1]
        state = 'deferred' if not np.isfinite(value) else 'triggered' if value == 1 else 'no_signal'
        item = dict(id=key, purpose=side, label=label, state=state,
                    reason='indicator_undefined' if state == 'deferred' else None)
        result['rules'].append(item)
        if state == 'triggered':
            event = dict(item, date=completed_asof, stock_key=stock_key)
            if stock_key:
                event['event_id'] = hashlib.sha256(f'{POLICY}|{stock_key}|{completed_asof}|{key}'.encode()).hexdigest()
            result['events'].append(event)
    return result