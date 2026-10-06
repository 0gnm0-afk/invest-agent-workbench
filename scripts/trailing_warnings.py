"""Persistent completed-session price warnings. No providers, orders or delivery."""
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3


def number(value):
    if isinstance(value, bool) or value is None:
        raise ValueError('missing_number')
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise ValueError('invalid_number')
    return value


def stamp(value):
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('timezone_required')
    return result


def equity(p):
    return p.get('instrument_class_hint') != 'CMA_note_from_broker_label' and str(
        p.get('asset_type') or p.get('product_type') or p.get('security_type') or 'stock').lower() in (
        'stock', 'equity', 'etf', 'adr', 'common_stock', 'commonshare', 'depositary_receipt')


def indicators(rows):
    """Wilder ATR14 seeded by 14 TRs requiring a preceding close."""
    bars, trs, atr, prior = [], [], None, None
    for row in rows:
        day = row['date']
        if datetime.strptime(day, '%Y-%m-%d').date().isoformat() != day or (bars and day <= bars[-1]['date']):
            raise ValueError('invalid_bar_dates')
        high, low, close = (number(row[k]) for k in ('high', 'low', 'close'))
        if not low <= close <= high:
            raise ValueError('invalid_ohlc')
        if prior is not None:
            tr = max(high-low, abs(high-prior), abs(low-prior))
            trs.append(tr)
            if len(trs) == 14:
                atr = sum(trs)/14
            elif len(trs) > 14:
                atr = (atr*13+tr)/14
        values = [r['close'] for r in bars[-4:]]+[close]
        bars.append(dict(date=day, high=high, low=low, close=close, atr=atr,
                         ma5=sum(values)/5 if len(values) == 5 else None))
        prior = close
    return bars


def packed(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True)


class TrailingStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS monitors(key TEXT PRIMARY KEY, payload TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS warnings(id TEXT PRIMARY KEY, payload TEXT NOT NULL)')

    def connect(self):
        return sqlite3.connect(self.path, timeout=30)

    def read(self):
        with closing(self.connect()) as db:
            return dict(schema=1, positions=[json.loads(r[0]) for r in db.execute('SELECT payload FROM monitors ORDER BY key')],
                        events=[json.loads(r[0]) for r in db.execute('SELECT payload FROM warnings ORDER BY rowid DESC')])

    def public(self, now, targets):
        value = self.read()
        for p in value['positions']:
            if p.get('status') == 'closed':
                continue
            try:
                age = (now-stamp(p.get('observed_account_as_of') or p['account_as_of'])).total_seconds()
                if not 0 <= age <= 86400:
                    p['issue'] = 'account_stale_or_incomplete'
                elif p.get('last_date') and p['last_date'] != targets.get(p['market']):
                    p['issue'] = 'completed_price_stale_or_missing'
            except (KeyError, TypeError, ValueError):
                p['issue'] = 'account_stale_or_incomplete'
        return value

    def update(self, bundle, stocks, targets, now=None, sessions=None):
        now = now or datetime.now(timezone.utc)
        if sessions is None:
            from invest_agent.trading.market_provider import completed_sessions
            sessions = {m:completed_sessions(m, now) for m in targets}
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            saved = {k:json.loads(v) for k,v in db.execute('SELECT key,payload FROM monitors')}
            seen = set()
            try:
                account_time = stamp(bundle['as_of'])
                fresh = 0 <= (now-account_time).total_seconds() <= 86400 and bundle.get('query_state') != 'failed'
            except (KeyError, TypeError, ValueError):
                fresh = False
            accounts = {a['alias']:a for a in bundle.get('accounts', [])}
            for alias, account in accounts.items():
                for position in account.get('positions', []):
                    if not equity(position):
                        continue
                    p = position
                    key = alias+'|'+p['market']+'|'+p['symbol']
                    seen.add(key)
                    state = deepcopy(saved.get(key) or dict(key=key, account=alias, market=p['market'], symbol=p['symbol'],
                        currency=p.get('currency'), name=p.get('name') or p['symbol'], active=False, generation=0, history=[]))
                    issue = None
                    state['observed_account_as_of'] = bundle.get('as_of')
                    try:
                        if not fresh or not account.get('holdings_complete'):
                            raise ValueError('account_stale_or_incomplete')
                        if p.get('data_unavailable_reason'):
                            raise ValueError('holding_unconfirmed')
                        if state.get('account_as_of') and account_time < stamp(state['account_as_of']):
                            raise ValueError('account_date_regressed')
                        qty = float(p['quantity'])
                        if not math.isfinite(qty) or qty < 0:
                            raise ValueError('holding_unconfirmed')
                        if qty == 0:
                            state.update(active=False, status='closed', quantity=0, account_as_of=bundle['as_of'])
                        else:
                            cost = number(p.get('average_cost'))
                            stock = stocks.get(p['market']+':'+p.get('quote_symbol',p['symbol']))
                            if not stock or stock.get('currency') != p.get('currency'):
                                raise ValueError('market_identity_unconfirmed')
                            identity = [stock.get('isin_code') or stock.get('isin'), stock.get('price_basis'), stock.get('currency')]
                            if state.get('identity') and state['identity'] != identity:
                                raise ValueError('market_identity_or_basis_changed')
                            if (p.get('isin_code') or p.get('isin')) and identity[0] and (p.get('isin_code') or p.get('isin')) != identity[0]:
                                raise ValueError('market_identity_unconfirmed')
                            bars = indicators(stock.get('bars', []))
                            if not bars or bars[-1]['date'] != targets.get(p['market']):
                                raise ValueError('completed_price_stale_or_missing')
                            expected = [d for d in sessions.get(p['market'], []) if bars[0]['date'] <= d <= bars[-1]['date']]
                            actual = [b['date'] for b in bars if expected and b['date'] >= expected[0]]
                            if not expected or actual != expected:
                                raise ValueError('price_sessions_incomplete')
                            if bars[-1]['atr'] is None:
                                raise ValueError('atr_history_missing')
                            by_day = {r['date']:r for r in bars}
                            if state.get('last_date') and state.get('active'):
                                previous = by_day.get(state['last_date'])
                                if not previous or any(previous[k] != state['last_bar'][k] for k in ('high','low','close')):
                                    raise ValueError('processed_price_history_changed')
                            if state.get('last_date') and bars[-1]['date'] < state['last_date']:
                                raise ValueError('price_date_regressed')
                            previous_cost = state.get('average_cost', cost)
                            state.update(quantity=qty, average_cost=cost, account_as_of=bundle['as_of'], identity=identity)
                            latest = bars[-1]
                            if not state['active']:
                                state['status'] = 'waiting_profit'
                                if latest['close'] > cost:
                                    state.update(active=True, generation=state['generation']+1, started_on=latest['date'],
                                                 high=latest['high'], line=None, history=[], last_date=None, below=False)
                            if state['active']:
                                pending = [b for b in bars if b['date'] >= state['started_on'] and b['date'] > (state.get('last_date') or '')]
                                breach_pending = False
                                for bar in pending:
                                    # Additional purchases preserve both H and the previous line.
                                    state['high'] = max(state['high'], bar['high'])
                                    candidate = max(state['high']-2.5*bar['atr'], .95*state['high'])
                                    floor = cost if bar['date'] == latest['date'] else previous_cost
                                    state['line'] = max(state.get('line') or floor, floor, candidate)
                                    below = bar['close'] <= state['line']
                                    if below and not state.get('below', False):
                                        breach_pending = True
                                    elif not below:
                                        breach_pending = False
                                    idx = bars.index(bar)
                                    prior = bars[idx-1] if idx else None
                                    ma_cross = prior is not None and prior['ma5'] is not None and prior['close'] > prior['ma5'] and bar['close'] <= bar['ma5']
                                    # Backlogged bars rebuild the state; only the latest completed session issues new warnings.
                                    if bar['date'] == latest['date']:
                                        for kind, triggered, threshold in [('trailing', below and breach_pending, state['line']), ('ma5',ma_cross,bar['ma5'])]:
                                            if triggered:
                                                event_id = hashlib.sha256(f"{key}|{state['generation']}|{bar['date']}|{kind}".encode()).hexdigest()
                                                event = dict(id=event_id, key=key, account=alias, market=p['market'], symbol=p['symbol'], currency=p['currency'],
                                                    date=bar['date'], kind=kind, close=bar['close'], threshold=threshold, created_at=now.isoformat(),
                                                    reason='종가가 최종 트레일링 경고선에 접촉·이탈' if kind=='trailing' else '전일 종가는 MA5 위, 당일 종가는 당일 MA5에 접촉·이탈')
                                                db.execute('INSERT OR IGNORE INTO warnings VALUES(?,?)',(event_id,packed(event)))
                                    state['history'].append(dict(date=bar['date'],close=bar['close'],line=state['line']))
                                    state.update(last_date=bar['date'], last_bar=bar, below=below)
                                if not pending and cost > state['line']:
                                    state['line'] = cost
                                    state['history'][-1]['line'] = cost
                                    state['cost_pending'] = True
                                elif pending:
                                    state['cost_pending'] = False
                                state['status'] = 'warning' if state['below'] else 'monitoring'
                            state['prices'] = [dict(date=b['date'],close=b['close']) for b in bars[-126:]]
                    except (ValueError, KeyError, TypeError, OverflowError) as exc:
                        issue = str(exc) if isinstance(exc,ValueError) and str(exc).replace('_','').isalnum() else 'invalid_input'
                    state.update(issue=issue, checked_at=now.isoformat())
                    saved[key] = state
            for key, state in saved.items():
                if key not in seen:
                    account = accounts.get(state['account'])
                    if fresh and account and account.get('holdings_complete') and (not state.get('account_as_of') or account_time >= stamp(state['account_as_of'])):
                        state.update(active=False,status='closed',quantity=0,issue=None,account_as_of=bundle['as_of'])
                    else:
                        state['issue'] = 'account_stale_or_incomplete'
                db.execute('INSERT OR REPLACE INTO monitors VALUES(?,?)',(key,packed(state)))
        return self.read()


def holding_series(path, expected_hash, keys):
    """Read selected series from our indent=2 immutable snapshot without loading the universe."""
    with path.open('rb') as stream:
        if not expected_hash or hashlib.file_digest(stream, 'sha256').hexdigest() != expected_hash:
            raise ValueError('saved_artifact_hash_mismatch')
    found, ended, item, result = False, False, [], {}
    with path.open(encoding='utf-8-sig') as stream:
        for line in stream:
            if not found:
                if line.rstrip() == '  "series": [':
                    found = True
                elif line.rstrip() in ('  "series": [],', '  "series": []'):
                    found = ended = True
                    break
                continue
            if line.rstrip() in ('  ],', '  ]'):
                ended = True
                break
            item.append(line)
            if line.rstrip() in ('    },', '    }'):
                row = json.loads(''.join(item).rstrip().rstrip(','))
                key = row['market']+':'+row['symbol']
                if key in keys:
                    if key in result:
                        raise ValueError('duplicate_market_series')
                    result[key] = row
                item = []
    if not found or not ended or item:
        raise ValueError('snapshot_series_format_unconfirmed')
    return result


def refresh_saved(root, market_run, targets, now=None):
    """Morning path: verified saved market + saved accounts only; no broker calls."""
    from dashboard_data import account_view, load, checked
    import tempfile
    root = Path(root)
    run_dir = Path(market_run).resolve()
    if not run_dir.is_relative_to((root/'runtime').resolve()):
        raise ValueError('market_run_outside_runtime')
    manifest = load(run_dir/'manifest.json')
    run = checked(run_dir/'run.json', manifest['output_hashes'].get('run.json'))
    source = Path(run['market_source'])/'snapshot.json'
    with tempfile.TemporaryDirectory(prefix='risk-account-', dir=root/'runtime') as folder:
        accounts = account_view(load(root/'config.json'), Path(folder), refresh=False,
                                saved_dashboard_root=root/'runtime/dashboard')
    keys = {p['market']+':'+p.get('quote_symbol',p['symbol'])
            for a in accounts['accounts'] for p in a['positions'] if equity(p)}
    stocks = holding_series(source, manifest['input_hashes'].get(str(source)), keys)
    return TrailingStore(root/'runtime/trailing-warnings.sqlite3').update(accounts, stocks, targets, now)
