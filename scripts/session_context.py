"""Read the saved stock context without starting a dashboard or refreshing providers."""
import json
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from workbench_paths import ROOT


def _read(path, query, args=()):
    if not path.is_file():
        return []
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute(query, args)]


def _payloads(path, table, where='', args=(), limit=50):
    allowed = {'analysis_cases', 'analysis_items', 'scenario_monitors', 'scenario_monitor_events'}
    if table not in allowed:
        raise ValueError('unsupported_context_table')
    rows = _read(path, 'SELECT payload FROM ' + table + ' ' + where + ' ORDER BY rowid DESC LIMIT ?', (*args, limit))
    return [json.loads(row['payload']) for row in rows]


def saved_context(stock_key, *, root=ROOT, offset=0, limit=30):
    if not isinstance(stock_key, str) or not re.fullmatch(r'(KR|US):[A-Z0-9.\-]{1,24}', stock_key):
        raise ValueError('exact_stock_key_required')
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('invalid_page')
    root = Path(root)
    db = root / 'runtime/chart-workspaces.sqlite3'
    result = {'stock_key': stock_key, 'mode': 'saved_only', 'observed_at': datetime.now(timezone.utc).isoformat(),
              'authority': 'historical_evidence_not_current_confirmation', 'errors': [],
              'history_navigation': {
                  'scope': 'recent_context_not_complete_history',
                  'limits': {'cases': 100, 'plan_decisions': 100, 'monitors': 100, 'signals': 30},
                  'all_cases': {'tool': 'analysis_list', 'arguments': {'stock_key': stock_key}},
                  'all_monitors': {'tool': 'analysis_monitors', 'arguments': {'stock_key': stock_key}},
                  'case_history': {'tool': 'analysis_history', 'required_argument': 'analysis_key',
                                   'source': 'all_cases.cases[].id'},
              }}
    try:
        cases = _payloads(db, 'analysis_cases', "WHERE stock_key=? AND coalesce(json_extract(payload,'$.test_record'),0)=0", (stock_key,), 100)
        result['cases'] = cases
        case_ids = [c['id'] for c in cases]
        result['records'] = []
        result['current_discussions'] = []
        if case_ids:
            placeholders = ','.join('?' for _ in case_ids)
            where = ' WHERE case_id IN (' + placeholders + ')'
            total = _read(db, 'SELECT count(*) AS n FROM analysis_items' + where, case_ids)[0]['n']
            records = _read(db, 'SELECT payload FROM analysis_items' + where + ' ORDER BY rowid DESC LIMIT ? OFFSET ?', (*case_ids, limit, offset))
            result['records'] = [json.loads(r['payload']) for r in records]
            result['records_page'] = {'total': total, 'offset': offset, 'next_offset': offset + limit if offset + limit < total else None}
            for cid in case_ids:
                result['current_discussions'] += _payloads(db, 'analysis_items', "WHERE case_id=? AND kind='discussion'", (cid,), 1)
            # Decisions remain their own events. Never infer adoption from an author or a draft.
            result['plan_decisions'] = _payloads(db, 'analysis_items', where + " AND kind='plan_decision'", tuple(case_ids), 100)
        rows = _read(db, 'SELECT revision,payload,updated_at FROM shared_chart_workspaces WHERE identity=?', (stock_key,))
        result['drawings'] = [{**r, 'payload': json.loads(r['payload'])} for r in rows]
        result['monitors'] = _payloads(db, 'scenario_monitors', "WHERE json_extract(payload,'$.stock_key')=?", (stock_key,), 100)
        for monitor in result['monitors']:
            monitor.pop('geometry_bars', None)
        result['signals'] = _payloads(db, 'scenario_monitor_events', "WHERE json_extract(payload,'$.stock_key')=?", (stock_key,), 30)
    except (sqlite3.Error, ValueError, KeyError) as exc:
        result['errors'].append({'component': 'saved_analysis', 'error_type': type(exc).__name__})
    try:
        market, symbol = stock_key.split(':', 1)
        prices = root / 'runtime/ohlcv.sqlite3'
        series = _read(prices, "SELECT id,metadata FROM price_series WHERE json_extract(metadata,'$.market')=? AND json_extract(metadata,'$.symbol')=?", (market, symbol))
        result['prices'] = []
        for s in series:
            last = _read(prices, 'SELECT day,close,revision FROM prices WHERE series_id=? ORDER BY day DESC LIMIT 1', (s['id'],))
            result['prices'].append({'metadata': json.loads(s['metadata']), 'last_completed_saved_bar': last[0] if last else None})
    except (sqlite3.Error, ValueError) as exc:
        result['errors'].append({'component': 'prices', 'error_type': type(exc).__name__})
    try:
        from signals.store import public
        result['technical'] = public(root, stock_key)
    except (ImportError, sqlite3.Error, ValueError) as exc:
        result['errors'].append({'component': 'technical_signals', 'error_type': type(exc).__name__})
    result['account_note'] = 'Historical accounts are not current balances. Use official broker read tools when current balances are needed.'
    result['found'] = bool(result.get('cases') or result.get('prices'))
    return result
