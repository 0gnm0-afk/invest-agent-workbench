"""Explicit per-stock watches and deduplicated evidence in the existing records DB."""
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3

from signals.rules import RULES, POLICY


def packed(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def utc():
    return datetime.now(timezone.utc).isoformat()


class SignalStore:
    def __init__(self, root):
        self.root = Path(root)
        self.path = self.root / 'runtime/records.sqlite3'

    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=30)
        db.executescript('''
            CREATE TABLE IF NOT EXISTS technical_watches(stock_key TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS technical_states(stock_key TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS technical_events(id TEXT PRIMARY KEY, stock_key TEXT NOT NULL, payload TEXT NOT NULL);
        ''')
        return db

    def configure(self, stock_key, rules, *, enabled=False, name='', reason=''):
        market, sep, symbol = stock_key.partition(':')
        if market not in ('KR', 'US') or not sep or not symbol or len(stock_key) > 100:
            raise ValueError('invalid_stock_key')
        if not isinstance(rules, list) or not rules or len(set(rules)) != len(rules) or set(rules) - RULES.keys():
            raise ValueError('invalid_signal_rules')
        if type(enabled) is not bool or not isinstance(reason, str) or not reason.strip():
            raise ValueError('explicit_watch_reason_required')
        stamp = utc()
        with closing(self.connect()) as db, db:
            row = db.execute('SELECT payload FROM technical_watches WHERE stock_key=?', (stock_key,)).fetchone()
            old = json.loads(row[0]) if row else None
            if old and all(old.get(k) == v for k, v in dict(rules=rules, enabled=enabled, name=name, reason=reason).items()):
                return {**old, 'verified': True}
            revision = (old or {}).get('revision', 0) + 1
            watch = dict(stock_key=stock_key, rules=rules, enabled=enabled, name=name,
                         reason=reason, revision=revision, updated_at=stamp, policy=POLICY)
            db.execute('INSERT OR REPLACE INTO technical_watches VALUES (?,?)', (stock_key, packed(watch)))
        return {**watch, 'verified': True}

    def public(self, stock_key=None, event_limit=100):
        if not self.path.exists():
            return {'watches': [], 'states': [], 'events': [], 'buy_combination': 'pending_source_review'}
        with closing(sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True, timeout=30)) as db:
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            result = {}
            for key, table in [('watches', 'technical_watches'), ('states', 'technical_states'), ('events', 'technical_events')]:
                if table not in tables:
                    result[key] = []
                else:
                    clause, args = (' WHERE stock_key=?', (stock_key,)) if stock_key else ('', ())
                    result[key] = [json.loads(r[0]) for r in db.execute('SELECT payload FROM ' + table + clause, args)]
            result['events'].sort(key=lambda e: (e['date'], e['created_at']), reverse=True)
            result['event_total'] = len(result['events'])
            if event_limit is not None:
                result['events'] = result['events'][:event_limit]
            result['buy_combination'] = 'pending_source_review'
        return result

    def record(self, watch, evaluation, *, close=None, price_reference=None):
        stock_key = watch['stock_key']
        state = dict(evaluation, evaluated_at=utc(), mode='saved_evaluation', active=watch['enabled'], watch_revision=watch['revision'],
                     enabled=watch['enabled'], price_reference=price_reference)
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            current = db.execute('SELECT payload FROM technical_watches WHERE stock_key=?', (stock_key,)).fetchone()
            if not current or json.loads(current[0])['revision'] != watch['revision']:
                raise ValueError('watch_revision_conflict')
            db.execute('INSERT OR REPLACE INTO technical_states VALUES (?,?)', (stock_key, packed(state)))
            if watch['enabled'] and evaluation['state'] == 'evaluated':
                for event in evaluation['events']:
                    if event['id'] not in watch['rules']:
                        continue
                    market, symbol = stock_key.split(':', 1)
                    payload = dict(event, id=event['event_id'], rule_id=event['id'], kind='technical',
                                   stock_key=stock_key, market=market, symbol=symbol, name=watch.get('name'),
                                   currency='KRW' if market == 'KR' else 'USD', close=close,
                                   created_at=state['evaluated_at'], watch_revision=watch['revision'],
                                   policy=POLICY, price_reference=price_reference)
                    db.execute('INSERT OR IGNORE INTO technical_events VALUES (?,?,?)',
                               (payload['id'], stock_key, packed(payload)))
        return state

    def delivery_events(self, targets):
        saved = self.public(event_limit=None)
        watches = {w['stock_key']: w for w in saved['watches'] if w['enabled']}
        states = {s['stock_key']: s for s in saved['states']}
        result = []
        for event in saved['events']:
            watch, state = watches.get(event['stock_key']), states.get(event['stock_key'])
            if (not watch or not state or state['state'] != 'evaluated'
                    or event['rule_id'] not in watch['rules']
                    or state['watch_revision'] != watch['revision']
                    or event['watch_revision'] != watch['revision']
                    or event['date'] != targets.get(event['market'])
                    or state['as_of'] != event['date']
                    or state.get('price_reference') != event.get('price_reference')):
                continue
            # A corrected candle that removes a signal is held, never sent from
            # an old event retained for audit.
            if event['rule_id'] not in {e['id'] for e in state.get('events', [])}:
                continue
            result.append(event)
        return result


def configure(root, stock_key, rules, *, enabled=False, name='', reason=''):
    return SignalStore(root).configure(stock_key, rules, enabled=enabled, name=name, reason=reason)


def public(root, stock_key=None):
    return SignalStore(root).public(stock_key)


def delivery_events(root, targets):
    return SignalStore(root).delivery_events(targets)


def evaluate_saved(root, targets):
    """Read canonical prices only, evaluate explicit watches, never collect/send."""
    from signals.technical import evaluate
    from market.prices import saved_bars
    import exchange_calendars as xcals
    from invest_agent.trading.market_provider import KR_CLOSURES
    store = SignalStore(root)
    watches = store.public()['watches']
    output = []
    for watch in watches:
        if not watch['enabled']:
            continue
        key = watch['stock_key']
        market, symbol = key.split(':', 1)
        cutoff = targets.get(market)
        try:
            try:
                saved = saved_bars(dict(market=market, symbol=symbol), root=root, cutoff=cutoff)
            except FileNotFoundError as exc:
                # The former read-only SQLite connection exposed this failure type.
                raise sqlite3.OperationalError('saved_price_database_missing') from exc
            bars = saved['bars']
            # The former day <= NULL query yielded no bars; None is not an
            # instruction to evaluate every saved day when a target is missing.
            if cutoff is None or not bars:
                raise ValueError('price_history_missing')
            # Calendar verification covers the complete source range. Long
            # exchange holidays retain the research's separate warmup policy.
            calendar = xcals.get_calendar('XKRX' if market == 'KR' else 'XNYS', start=bars[0]['date'], end=cutoff)
            sessions = [v.date().isoformat() for v in calendar.sessions_in_range(bars[0]['date'], cutoff)]
            if market == 'KR':
                sessions = [d for d in sessions if d not in KR_CLOSURES]
            evaluation = evaluate(bars, completed_asof=cutoff, expected_sessions=sessions, stock_key=key)
            reference = dict(series_id=saved['series_id'], revision=max(saved['revisions'].values()),
                             sha256=hashlib.sha256(packed(bars).encode()).hexdigest(),
                             first=bars[0]['date'], last=bars[-1]['date'])
            output.append(store.record(watch, evaluation, close=bars[-1]['close'], price_reference=reference))
        except (ValueError, KeyError, TypeError, OSError, sqlite3.Error) as exc:
            failed = dict(state='deferred', reason='saved_price_or_calendar_unavailable',
                          error_type=type(exc).__name__, stock_key=key, as_of=cutoff,
                          policy=POLICY, rules=[], events=[], indicators={})
            output.append(store.record(watch, failed))
    return {'state': 'not_configured' if not output else 'evaluated' if all(s['state'] == 'evaluated' for s in output) else 'deferred',
            'states': output}
