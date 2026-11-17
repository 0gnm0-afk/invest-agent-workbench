"""M07 group-2 contracts over synthetic stores; C2/C3 provider cutovers follow later."""
from contextlib import closing, contextmanager
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import math
import re
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import pandas as pd

from alerts.delivery import dispatch
from alerts.trailing import TrailingStore
from drawings.store import WorkspaceStore, defaults
from market.prices import PriceStore
from scenarios import dispatch as scenario_dispatch
from scenarios.monitor import ScenarioStore
from signals.store import SignalStore, evaluate_saved


OWNED_TABLES = {
    'technical_watches', 'technical_states', 'technical_events',
    'scenario_monitors', 'scenario_monitor_history', 'scenario_monitor_events',
    'scenario_monitor_evaluations', 'monitors', 'warnings', 'deliveries',
}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def series_fixture():
    # Frozen synthetic waveform reaches the existing RSI30 cross on bar 94.
    days, current = [], date(2026, 1, 1)
    while len(days) < 94:
        if current.weekday() < 5:
            days.append(current.isoformat())
        current += timedelta(days=1)
    bars = []
    for i, day in enumerate(days):
        close = 100 + 18 * math.sin(i / 8) + 5 * math.sin(i / 3)
        bars.append(dict(date=day, open=close, high=close + 1,
                         low=close - 1, close=close, volume=1000))
    return bars, days


class MonitorContracts(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.runtime = self.root / 'runtime'
        self.runtime.mkdir()
        self.path = self.runtime / 'chart-workspaces.sqlite3'
        self.bars, self.days = series_fixture()
        self.targets = {'US': self.days[-1]}
        self.now = datetime.fromisoformat(self.days[-1]).replace(tzinfo=timezone.utc)
        self.stocks = {
            'US:' + symbol: dict(market='US', symbol=symbol, currency='USD',
                                isin_code='SYNTHETIC_' + symbol, price_basis='adjusted',
                                exchange='NASDAQ', bars=deepcopy(self.bars))
            for symbol in ('A', 'B', 'C')
        }
        self.bundle = dict(as_of=self.now.isoformat(), query_state='received', accounts=[
            dict(alias='synthetic_account', holdings_complete=True, positions=[
                dict(market='US', symbol='B', quantity=2, average_cost=10,
                     currency='USD', isin_code='SYNTHETIC_B')])])
        self.ws = WorkspaceStore(self.path)
        for symbol in ('A', 'C'):
            state = defaults()
            state['drawings'] = [dict(id='line', kind='horizontal',
                points=[dict(time=self.days[-1], price=self.bars[-1]['close'])],
                color='#2271b2', width=2, extend=False)]
            self.ws.shared('US:' + symbol, revision=0, state=state)
        prices = PriceStore(self.runtime / 'ohlcv.sqlite3')
        for stock in self.stocks.values():
            prices.put(stock, source='M07 synthetic contract')
        self.signals = SignalStore(self.root)

    def register(self, store, symbol, **extra):
        stock = self.stocks['US:' + symbol]
        return store.register(stock_key='US:' + symbol,
            identity=dict(market='US', symbol=symbol, name='synthetic ' + symbol,
                          currency='USD', stable_id=stock['isin_code'],
                          price_basis='adjusted', exchange='NASDAQ'),
            case_id='an_synthetic_' + symbol, plan_record_id='it_synthetic_' + symbol,
            plan_id='pl_synthetic_' + symbol, plan_version=1,
            drawing_id='line', workspace_revision=1, purpose='buy',
            boundary='line', chart=dict(timeframe='1d', bars=self.bars),
            test_record=True, **extra)

    def evaluate_signals(self):
        with patch('exchange_calendars.get_calendar') as calendar:
            calendar.return_value.sessions_in_range.return_value = pd.to_datetime(self.days)
            return evaluate_saved(self.root, self.targets)

    def test_explicit_signal_scenario_and_automatic_holding_scope(self):
        self.signals.configure('US:A', ['rsi30'], enabled=True, reason='explicit synthetic A')
        self.signals.configure('US:C', ['rsi30'], enabled=False, reason='disabled synthetic C')
        evaluated = self.evaluate_signals()
        self.assertEqual(evaluated['state'], 'evaluated')
        self.assertEqual([s['stock_key'] for s in evaluated['states']], ['US:A'])
        signal_events = self.signals.public()['events']
        self.assertEqual({e['stock_key'] for e in signal_events}, {'US:A'})
        self.assertNotIn('US:B', {w['stock_key'] for w in self.signals.public()['watches']})

        scenarios = ScenarioStore(self.path)
        self.register(scenarios, 'A')
        inactive = self.register(scenarios, 'C')
        scenarios.configure(inactive['id'], inactive['revision'], active=False)
        result = scenarios.update(self.bundle, self.stocks, self.targets, self.now, {'US': self.days})
        self.assertEqual({e['stock_key'] for e in result['events']}, {'US:A'})
        self.assertEqual({m['stock_key'] for m in result['monitors'] if m['latest']}, {'US:A'})
        self.assertNotIn('US:B', {m['stock_key'] for m in result['monitors']})

        trailing = TrailingStore(self.runtime / 'trailing-warnings.sqlite3')
        held = trailing.update(self.bundle, self.stocks, self.targets, self.now, {'US': self.days})
        self.assertEqual([p['symbol'] for p in held['positions']], ['B'])
        self.assertTrue(held['positions'][0]['active'])
        self.assertEqual(held['positions'][0]['account_as_of'], self.bundle['as_of'])
        self.assertIsNone(held['positions'][0]['issue'])

    @contextmanager
    def owned_writes_only(self):
        # M00 already installs a nonreplaceable SQLite authorizer. Keep it intact
        # and reject foreign writes at M07's connection boundary before execution.
        original = sqlite3.connect
        denied, written = [], set()
        patterns = (
            r'CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_][A-Za-z0-9_]*)',
            r'CREATE\s+(?:UNIQUE\s+)?INDEX\s+\S+\s+ON\s+([A-Za-z_][A-Za-z0-9_]*)',
            r'INSERT\s+(?:OR\s+\w+\s+)?INTO\s+([A-Za-z_][A-Za-z0-9_]*)',
            r'REPLACE\s+INTO\s+([A-Za-z_][A-Za-z0-9_]*)',
            r'UPDATE\s+([A-Za-z_][A-Za-z0-9_]*)',
            r'DELETE\s+FROM\s+([A-Za-z_][A-Za-z0-9_]*)',
            r'(?:DROP|ALTER)\s+TABLE\s+(?:IF\s+EXISTS\s+)?([A-Za-z_][A-Za-z0-9_]*)',
        )

        def check(sql):
            statement = sql.strip()
            if not statement:
                return
            for pattern in patterns:
                match = re.match(pattern, statement, flags=re.IGNORECASE)
                if match:
                    table = match.group(1)
                    if table not in OWNED_TABLES:
                        denied.append(table)
                        raise sqlite3.DatabaseError('M07 foreign table write denied')
                    written.add(table)
                    return
            if not re.match(r'(SELECT|BEGIN|COMMIT|ROLLBACK|PRAGMA\s+query_only)\b', statement,
                            flags=re.IGNORECASE):
                denied.append('unrecognized statement')
                raise sqlite3.DatabaseError('M07 unrecognized write denied')

        class OwnedConnection:
            def __init__(self, db):
                self.db = db

            def execute(self, sql, *args):
                check(sql)
                return self.db.execute(sql, *args)

            def executemany(self, sql, *args):
                check(sql)
                return self.db.executemany(sql, *args)

            def executescript(self, script):
                # The schema scripts here contain only fixed CREATE statements.
                for statement in script.split(';'):
                    check(statement)
                return self.db.executescript(script)

            def __getattr__(self, name):
                return getattr(self.db, name)

            def __enter__(self):
                self.db.__enter__()
                return self

            def __exit__(self, *args):
                return self.db.__exit__(*args)

        def connect(*args, **kwargs):
            return OwnedConnection(original(*args, **kwargs))

        with patch.object(sqlite3, 'connect', side_effect=connect):
            yield denied, written
        self.assertEqual(denied, [], 'M07 attempted a write outside its owned tables')

    def foreign_rows(self):
        result = {}
        for path in (self.path, self.runtime / 'records.sqlite3', self.runtime / 'ohlcv.sqlite3'):
            with closing(sqlite3.connect(path)) as db:
                tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
                          if r[0] not in OWNED_TABLES]
                result[path.name] = {
                    table: list(db.execute('SELECT * FROM "' + table.replace('"', '""') + '" ORDER BY 1'))
                    for table in tables}
        return digest(result)

    def test_only_owned_tables_written_and_source_rows_unchanged(self):
        # Immutable source/analysis payloads share databases with M07's own tables.
        snapshot = self.runtime / 'synthetic-snapshot.json'
        snapshot.write_text(json.dumps({'source': 'synthetic', 'bars': self.bars}), encoding='utf-8')
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('CREATE TABLE analysis_snapshots(id TEXT PRIMARY KEY,payload TEXT NOT NULL)')
            db.execute('INSERT INTO analysis_snapshots VALUES (?,?)', ('snapshot', snapshot.read_text()))
            db.execute('CREATE TABLE trade_cycle_preserved(id TEXT PRIMARY KEY,payload TEXT NOT NULL)')
            db.execute('INSERT INTO trade_cycle_preserved VALUES (?,?)', ('retired', 'synthetic preserved row'))
        with closing(sqlite3.connect(self.runtime / 'records.sqlite3')) as db, db:
            db.execute('CREATE TABLE knowledge_sources(id TEXT PRIMARY KEY,payload TEXT NOT NULL)')
            db.execute('INSERT INTO knowledge_sources VALUES (?,?)', ('source', 'synthetic original'))
        before, source_sha = self.foreign_rows(), hashlib.sha256(snapshot.read_bytes()).hexdigest()
        sent = []
        with self.owned_writes_only() as (denied, written):
            self.signals.configure('US:A', ['rsi30'], enabled=True, reason='explicit contract')
            self.assertEqual(self.evaluate_signals()['state'], 'evaluated')
            scenarios = ScenarioStore(self.path)
            self.register(scenarios, 'A')
            scenarios.update(self.bundle, self.stocks, self.targets, self.now, {'US': self.days})
            trailing = TrailingStore(self.runtime / 'trailing-warnings.sqlite3')
            trailing.update(self.bundle, self.stocks, self.targets, self.now, {'US': self.days})
            delivered = dispatch(self.root, self.targets, config={'enabled_at': '2000-01-01', 'chat': 'synthetic'},
                sender=lambda text: sent.append(text) or {'message_id': 1}, include_trailing=False)
            self.assertEqual(delivered['state'], 'ready')
        self.assertEqual(len(sent), 1)
        self.assertTrue({'technical_events', 'scenario_monitor_evaluations', 'monitors', 'deliveries'} <= written)
        self.assertEqual(self.foreign_rows(), before)
        self.assertEqual(hashlib.sha256(snapshot.read_bytes()).hexdigest(), source_sha)

    def test_empty_scenario_store_does_not_initialize_other_modules(self):
        path = self.runtime / 'empty-monitor.sqlite3'
        with self.owned_writes_only():
            value = ScenarioStore(path).read()
            self.assertEqual(value['monitors'], [])
            self.assertEqual(value['events'], [])
        with closing(sqlite3.connect(path)) as db:
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(tables, {t for t in OWNED_TABLES if t.startswith('scenario_')})

    def test_hypothesis_text_is_passive_plan_context(self):
        hypothesis = dict(verification_date='2020-01-01',
            invalidation_statement='다음 실적에서 성장 가설이 무너지면 사용자가 재검토한다.',
            pre_verification_allocation='총 투자 가능 자산의 7.5% 이하 — 원문 유지')
        workspace = self.ws.shared('US:A')
        stock = self.stocks['US:A']
        identity = dict(market='US', symbol='A', currency='USD', stable_id=stock['isin_code'],
                        price_basis='adjusted', exchange='NASDAQ')
        plan = dict(id='it_passive', plan_id='pl_passive', plan_version=1, identity=identity,
            plan=dict(account_alias=None, hypothesis=hypothesis, conditions=[dict(
                drawing_ref={'drawing_id': 'line', 'line': 'line'},
                evaluation='daily_close', statement='사용자가 저장한 대응만 전달')]))
        snapshot = dict(workspace=workspace, chart=dict(timeframe='1d', bars=self.bars), context={})
        original = digest([plan, snapshot])
        case = dict(id='an_passive', stock_key='US:A', test_record=True)
        service = SimpleNamespace(store=self.ws, collaboration=SimpleNamespace(
            plan_basis=Mock(return_value=(plan, snapshot, {'adopted': True}))),
            case=lambda key: case, links=lambda value, origin: {'dashboard_url': 'http://127.0.0.1:1/#synthetic'})
        monitors = ScenarioStore(self.path)
        self.assertEqual(monitors.update(self.bundle, self.stocks, self.targets, self.now,
                                         {'US': self.days})['monitors'], [])
        args = dict(analysis_key=case['id'], plan_record_id=plan['id'], drawing_id='line',
                    workspace_revision=workspace['revision'], purpose='buy', boundary='line', tolerance_atr=.5)
        registered = scenario_dispatch(service, 'monitor_register', args, 'http://127.0.0.1:1', 'browser', 'synthetic')
        self.assertEqual(registered['hypothesis'], hypothesis)
        service.collaboration.plan_basis.assert_called_once_with(case['id'], plan['id'], require_adopted=True)
        evaluated = monitors.update(self.bundle, self.stocks, self.targets, self.now, {'US': self.days})
        self.assertEqual(evaluated['monitors'][0]['hypothesis'], hypothesis)
        self.assertEqual({e['kind'] for e in evaluated['events']}, {'scenario'})
        self.assertEqual({e['state'] for e in evaluated['events']}, {'reached'})
        self.assertTrue(all('hypothesis' not in e for e in evaluated['events']))
        # An expired verification date has no scheduler, inference, or order effect.
        self.assertEqual(monitors.update(self.bundle, self.stocks, self.targets, self.now,
                                         {'US': self.days})['events'], evaluated['events'])
        baseline = self.register(monitors, 'A')
        both = monitors.update(self.bundle, self.stocks, self.targets, self.now, {'US': self.days})
        by_monitor = {event['monitor_id']: event for event in both['events']}
        self.assertEqual(len(both['events']), 2)
        price_fields = ('kind', 'state', 'boundary', 'close', 'threshold', 'atr', 'tolerance', 'reason')
        self.assertEqual({key: by_monitor[registered['id']][key] for key in price_fields},
                         {key: by_monitor[baseline['id']][key] for key in price_fields})
        self.assertNotIn('hypothesis', baseline)
        self.assertEqual(digest([plan, snapshot]), original)
