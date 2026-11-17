"""Read-only L1 reference lines on completed, visible chart bars.

Solver source: 0gnm0-afk/invest-trend-research-archive at
120dd467e7667063f6974a52006a44d218d694fd,
scripts/downtrend_research/l1_solver.py, SHA-256
b8f351a849736d8c16696e374e587949b5c854bcd480a7c8acdff73cd95969e6.
The original primal-dual iterations and its scalar five-diagonal Cholesky
solver are retained. NumPy alone replaces scipy.linalg.solveh_banded.
These ephemeral reference lines are never stored or used as trading signals.
"""
from datetime import date, datetime, timedelta, timezone
import math
import sqlite3
from zoneinfo import ZoneInfo

import numpy as np

from market.history.chart import aggregate
from market.prices import saved_bars


LAMBDA = 1.0
DAILY_SLOPE_THRESHOLD = 0.000001
MIN_BARS = 10
SOLVER_SOURCE_SHA256 = 'b8f351a849736d8c16696e374e587949b5c854bcd480a7c8acdff73cd95969e6'


def d2(x):
    return x[:-2] - 2*x[1:-1] + x[2:]


def dt(u):
    x = np.zeros(len(u)+2)
    x[:-2] += u
    x[1:-1] -= 2*u
    x[2:] += u
    return x


def pentasolve(diagonal, rhs):
    """Original O(n) SPD solve, off-diagonals -4 and +1 (no scipy)."""
    n = len(rhs)
    ld = np.empty(n)
    l1 = np.zeros(n)
    l2 = np.zeros(n)
    for i in range(n):
        if i > 1:
            l2[i] = 1/ld[i-2]
        if i:
            l1[i] = (-4 - l2[i]*l1[i-1])/ld[i-1]
        v = diagonal[i] - l1[i]**2 - l2[i]**2
        if v <= 0:
            raise ArithmeticError(('nonpositive Cholesky pivot', i, v))
        ld[i] = np.sqrt(v)
    z = np.empty(n)
    for i in range(n):
        z[i] = (rhs[i] - (l1[i]*z[i-1] if i else 0)
                - (l2[i]*z[i-2] if i > 1 else 0))/ld[i]
    x = np.empty(n)
    for i in range(n-1, -1, -1):
        x[i] = (z[i] - (l1[i+1]*x[i+1] if i+1 < n else 0)
                - (l2[i+2]*x[i+2] if i+2 < n else 0))/ld[i]
    return x


def l1trend(y, lam=LAMBDA, tol=1e-6):
    """Minimize 0.5*sum((y-x)**2) + lam*sum(abs(diff(x, 2))).

    Returns (fitted log prices, convergence diagnostics). The iteration and
    stopping conditions match the archived scalar=True implementation.
    """
    y = np.asarray(y, float)
    if y.ndim != 1 or len(y) < 3 or not np.all(np.isfinite(y)):
        raise ValueError('invalid_l1_input')
    if not math.isfinite(lam) or lam <= 0 or not math.isfinite(tol) or tol <= 0:
        raise ValueError('invalid_l1_parameters')
    offset = float(np.mean(y))
    y = y-offset
    dy = d2(y)
    u = np.zeros(len(dy))
    mu = 1.0
    iterations = 0
    for outer in range(23):
        for _ in range(100):
            r = dt(u)
            au = d2(r)
            left, right = lam-u, lam+u
            g = au-dy + mu*(1/left - 1/right)
            h = 6 + mu*(1/left**2 + 1/right**2)
            step = pentasolve(h, -g)
            dec = -float(g@step)
            if dec < 1e-13:
                break
            alpha = 1.0
            pos, neg = step > 0, step < 0
            if np.any(pos):
                alpha = min(alpha, .99*float(np.min(left[pos]/step[pos])))
            if np.any(neg):
                alpha = min(alpha, .99*float(np.min(-right[neg]/step[neg])))
            f = .5*(r@r)-dy@u-mu*np.sum(np.log(left)+np.log(right))
            for _ in range(60):
                v = u+alpha*step
                rv = dt(v)
                fv = .5*(rv@rv)-dy@v-mu*np.sum(np.log(lam-v)+np.log(lam+v))
                if fv <= f-.01*alpha*dec:
                    break
                alpha *= .5
            else:
                raise ArithmeticError('Newton line search failed')
            u = v
            iterations += 1
        x = y-dt(u)
        gap = float(np.sum(lam*np.abs(d2(x))-u*d2(x)))
        if gap < tol:
            primal = .5*np.sum((y-x)**2)+lam*np.sum(np.abs(d2(x)))
            return x+offset, {'primal_dual_gap': gap, 'iterations': iterations,
                              'objective': float(primal), 'max_dual_abs': float(np.max(np.abs(u)))}
        mu *= .15
    raise ArithmeticError(('L1 solver did not converge', lam, gap))


def _day(value):
    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
        raise ValueError('invalid_l1_date')
    return date.fromisoformat(value)


def completed_daily_bars(bars, completed_through):
    """Validate and copy saved daily bars, excluding future/incomplete slots."""
    _day(completed_through)
    result = []
    previous = None
    for bar in bars:
        day = bar['date']
        _day(day)
        if previous is not None and day <= previous:
            raise ValueError('invalid_l1_bar_dates')
        previous = day
        if day > completed_through or bar.get('projected') or bar.get('completed') is False:
            continue
        values = [bar[k] for k in ('open', 'high', 'low', 'close')]
        if (any(type(v) not in (float, int) or not math.isfinite(v) or v <= 0 for v in values)
                or not bar['low'] <= min(bar['open'], bar['close']) <= max(bar['open'], bar['close']) <= bar['high']):
            raise ValueError('invalid_l1_ohlc')
        result.append(dict(bar))
    return result


def _period_start(day, frame):
    day = _day(day)
    if frame == '1w':
        day -= timedelta(days=day.weekday())
    elif frame == '1mo':
        day = day.replace(day=1)
    return day.isoformat()


def _six_months_before(day):
    # Match Date.setUTCMonth in chart-workspace.js, including month overflow.
    value = _day(day)
    month = value.year*12 + value.month-1-6
    first = date(month//12, month % 12+1, 1)
    return (first+timedelta(days=value.day-1)).isoformat()


def period_last_session(market, *, timeframe, as_of_date):
    """Last scheduled local session of a week/month; local calendar only.

    This determines whether the current period can already be complete on
    its last trading day (including an early holiday ending).
    """
    if market not in ('KR', 'US') or timeframe not in ('1w', '1mo'):
        raise ValueError('invalid_l1_period')
    import exchange_calendars as xcals
    from invest_agent.trading.market_provider import KR_CLOSURES
    first = _day(_period_start(as_of_date, timeframe))
    after = (first+timedelta(days=7) if timeframe == '1w' else
             date(first.year+(first.month == 12), first.month % 12+1, 1))
    calendar = xcals.get_calendar('XKRX' if market == 'KR' else 'XNYS')
    sessions = [session.date().isoformat() for session in
                calendar.schedule.loc[first.isoformat():(after-timedelta(days=1)).isoformat()].index]
    sessions = [s for s in sessions if market != 'KR' or s not in KR_CLOSURES]
    if not sessions:
        raise ValueError('l1_period_calendar_unavailable')
    return sessions[-1]


def direction_segments(log_prices, sessions, *, min_bars=MIN_BARS):
    """Group consecutive edge directions; a shared turn belongs to both runs.

    The edge arriving at bar i spans that bar's recorded trading sessions.
    A run of nine same-direction edges therefore contains ten chart bars.
    Flat runs remain in this internal list so they cannot join two trends.
    """
    fitted = np.asarray(log_prices, float)
    counts = np.asarray(sessions, float)
    if (len(fitted) != len(counts) or min_bars < 2 or not np.all(np.isfinite(fitted))
            or not np.all(np.isfinite(counts)) or np.any(counts <= 0)):
        raise ValueError('invalid_l1_segments')
    if len(fitted) < 2:
        return []
    slopes = np.diff(fitted)/counts[1:]
    directions = np.where(slopes > DAILY_SLOPE_THRESHOLD, 'up',
                          np.where(slopes < -DAILY_SLOPE_THRESHOLD, 'down', 'flat'))
    runs = []
    start = 0
    for end in range(1, len(directions)+1):
        if end == len(directions) or directions[end] != directions[start]:
            if end-start+1 >= min_bars:
                runs.append({'start': start, 'end': end, 'direction': str(directions[start])})
            start = end
    return runs


def build_trendlines(daily_bars, *, completed_through, as_of_date, start=None,
                     end=None, timeframe='1d', min_bars=MIN_BARS,
                     current_period_complete=False):
    """Pure calculation; dates are inclusive chart-bar labels, not future slots.

    Current week/month groups are excluded unless the caller has confirmed
    the last scheduled session is complete. ``as_of_date`` is market-local.
    """
    packet = {'status': 'ok', 'kind': 'automatic_l1_reference', 'lines': [],
              'window': {'start': start, 'end': end, 'timeframe': timeframe},
              'calculated_window': None, 'latest_completed_bar': None,
              'lambda': LAMBDA, 'minimum_bars': min_bars,
              'daily_slope_threshold': DAILY_SLOPE_THRESHOLD,
              'coordinate_basis': 'chart_logical_bar_index_linear_price',
              'stored': False, 'signal': False}
    try:
        if timeframe not in ('1d', '1w', '1mo'):
            raise ValueError('invalid_timeframe')
        if type(min_bars) is not int or min_bars < 3:
            raise ValueError('invalid_minimum_bars')
        _day(as_of_date)
        if start is not None:
            _day(start)
        if end is not None:
            _day(end)
        if start is not None and end is not None and start > end:
            raise ValueError('invalid_visible_window')
        daily = completed_daily_bars(daily_bars, completed_through)
        bars = aggregate(daily, timeframe)
        if timeframe != '1d' and not current_period_complete:
            boundary = _period_start(as_of_date, timeframe)
            bars = [b for b in bars if b['date'] < boundary]
        if not bars:
            return {**packet, 'status': 'insufficient_data', 'reason': 'no_completed_bars'}
        latest = bars[-1]['date']
        packet['latest_completed_bar'] = latest
        left = start if start is not None else (_six_months_before(latest) if timeframe == '1d' else bars[0]['date'])
        right = min(end, latest) if end is not None else latest
        packet['window'] = {'start': left, 'end': right, 'timeframe': timeframe}
        indexed = [(i, b) for i, b in enumerate(bars) if left <= b['date'] <= right]
        if indexed:
            packet['calculated_window'] = {'start': indexed[0][1]['date'],
                                            'end': indexed[-1][1]['date'], 'timeframe': timeframe,
                                            'bar_count': len(indexed)}
        if len(indexed) < min_bars:
            return {**packet, 'status': 'insufficient_data', 'reason': 'fewer_than_minimum_completed_bars'}
        visible = [b for _, b in indexed]
        fitted, diagnostics = l1trend(np.log([b['close'] for b in visible]), LAMBDA)
        packet['solver'] = {**diagnostics, 'converged': True, 'method': 'numpy_scalar_pentadiagonal',
                            'source_sha256': SOLVER_SOURCE_SHA256, 'tolerance': 1e-6}
        segments = direction_segments(fitted, [b.get('sessions', 1) for b in visible], min_bars=min_bars)
        for segment in segments:
            if segment['direction'] == 'flat':
                continue
            a, z = segment['start'], segment['end']
            rows = visible[a:z+1]
            logical = np.asarray([i for i, _ in indexed[a:z+1]], float)
            prices = np.exp(fitted[a:z+1])
            centered = logical-logical.mean()
            slope = float(centered@(prices-prices.mean())/(centered@centered))
            values = np.asarray([b['low' if segment['direction'] == 'up' else 'high'] for b in rows])
            residual = values-slope*logical
            intercept = float(np.min(residual) if segment['direction'] == 'up' else np.max(residual))
            ongoing = z == len(visible)-1 and visible[-1]['date'] == latest
            packet['lines'].append({
                'direction': segment['direction'], 'start_date': rows[0]['date'], 'end_date': rows[-1]['date'],
                'bar_count': len(rows), 'slope': slope, 'intercept': intercept,
                'points': [{'time': row['date'], 'price': float(intercept+slope*x), 'logical': int(x)}
                           for row, x in ((rows[0], logical[0]), (rows[-1], logical[-1]))],
                'in_progress': ongoing, 'label': '진행 중' if ongoing else '완료 구간',
                'line_style': 'dashed' if ongoing else 'solid'})
        return packet
    except (ValueError, TypeError, KeyError, OverflowError) as exc:
        return {**packet, 'status': 'invalid_data', 'reason': str(exc)}
    except ArithmeticError as exc:
        return {**packet, 'status': 'solver_failed', 'reason': str(exc),
                'solver': {'converged': False}}


def l1_trendlines(stock_key, *, root, start=None, end=None, timeframe='1d',
                  now=None, bars_reader=None, sessions_reader=None, period_reader=None):
    """Saved-price adapter for M09/M10; no default root, refresh, or storage.

    ``now`` is timezone-aware. Injectable readers have the M03 saved_bars and
    completed_sessions signatures; neither a server nor AnalysisService runs.
    """
    now = now or datetime.now(timezone.utc)
    unavailable = {'status': 'unavailable', 'kind': 'automatic_l1_reference', 'stock_key': stock_key,
                   'window': {'start': start, 'end': end, 'timeframe': timeframe}, 'lines': [],
                   'stored': False, 'signal': False}
    try:
        if not isinstance(stock_key, str) or ':' not in stock_key or now.tzinfo is None:
            raise ValueError('invalid_l1_request')
        market, symbol = stock_key.split(':', 1)
        if market not in ('KR', 'US') or not symbol:
            raise ValueError('invalid_l1_stock_key')
        if sessions_reader is None:
            from invest_agent.trading.market_provider import completed_sessions
            sessions_reader = completed_sessions
        completed = sessions_reader(market, now)
        if not completed:
            raise ValueError('no_completed_sessions')
        cutoff = completed[-1]
        source = (bars_reader or saved_bars)(stock_key, root=root, cutoff=cutoff)
        identity = source['identity']
        if identity.get('market') != market or identity.get('symbol') != symbol:
            raise ValueError('l1_price_identity_mismatch')
        local_day = now.astimezone(ZoneInfo('Asia/Seoul' if market == 'KR' else 'America/New_York')).date().isoformat()
        last_session = ((period_reader or period_last_session)(market, timeframe=timeframe, as_of_date=local_day)
                        if timeframe in ('1w', '1mo') else None)
        result = build_trendlines(source['bars'], completed_through=cutoff, as_of_date=local_day,
                                  start=start, end=end, timeframe=timeframe,
                                  current_period_complete=last_session is not None and cutoff >= last_session)
        return {**result, 'stock_key': stock_key, 'price_identity': identity,
                'bars_ref': source.get('bars_ref'), 'completed_through': cutoff,
                'current_period_last_session': last_session}
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError) as exc:
        return {**unavailable, 'reason': str(exc)}
