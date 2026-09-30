"""Local user chart workspaces; independent of immutable market/broker data."""
from contextlib import closing
import json
import math
import re
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

COLORS = ['#dd9500', '#2271b2', '#8254a8', '#687780']
KINDS = {'trend': 2, 'channel': 3, 'horizontal': 1, 'box': 2, 'measure': 2}


class Conflict(Exception):
    pass


def defaults():
    return {'schema': 1, 'drawings': [], 'averages': [
        {'period': n, 'color': c, 'width': 2, 'visible': True}
        for n, c in zip([20, 50, 150, 200], COLORS)]}


def validate(state):
    if not isinstance(state, dict) or set(state) != {'schema', 'drawings', 'averages'} or type(state['schema']) is not int or state['schema'] != 1:
        raise ValueError('invalid_schema')
    drawings, averages = state['drawings'], state['averages']
    if not isinstance(drawings, list) or len(drawings) > 100 or not isinstance(averages, list) or len(averages) > 6:
        raise ValueError('item_limit')
    ids, periods = set(), set()
    for average in averages:
        if not isinstance(average, dict) or set(average) != {'period', 'color', 'width', 'visible'}:
            raise ValueError('invalid_average')
        n = average['period']
        if type(n) is not int or not 1 <= n <= 500 or n in periods or type(average['visible']) is not bool:
            raise ValueError('invalid_period')
        periods.add(n)
        style(average)
    for drawing in drawings:
        if not isinstance(drawing, dict) or set(drawing) != {'id', 'kind', 'points', 'color', 'width', 'extend'}:
            raise ValueError('invalid_drawing')
        ident = drawing['id']
        if not isinstance(ident, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', ident) or ident in ids:
            raise ValueError('invalid_id')
        ids.add(ident)
        kind = drawing['kind']
        if kind not in KINDS or type(drawing['extend']) is not bool:
            raise ValueError('invalid_kind')
        points = drawing['points']
        if not isinstance(points, list) or len(points) != KINDS[kind]:
            raise ValueError('invalid_points')
        for point in points:
            if not isinstance(point, dict) or set(point) != {'time', 'price'}:
                raise ValueError('invalid_point')
            if not isinstance(point['time'], str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', point['time']):
                raise ValueError('invalid_date')
            date.fromisoformat(point['time'])
            if type(point['price']) not in (int, float) or not math.isfinite(point['price']) or not 0 < point['price'] <= 1e15:
                raise ValueError('invalid_price')
        if kind in ('trend', 'channel') and points[0]['time'] == points[1]['time']:
            raise ValueError('distinct_dates_required')
        style(drawing)
    return state


def style(item):
    if not isinstance(item['color'], str) or not re.fullmatch(r'#[0-9a-fA-F]{6}', item['color']):
        raise ValueError('invalid_color')
    if type(item['width']) is not int or not 1 <= item['width'] <= 4:
        raise ValueError('invalid_width')


class WorkspaceStore:
    def __init__(self, path):
        self.path = Path(path)

    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=5)
        db.execute('CREATE TABLE IF NOT EXISTS chart_workspaces (identity TEXT PRIMARY KEY, revision INTEGER NOT NULL, state TEXT NOT NULL, updated_at TEXT NOT NULL)')
        return db

    def read(self, identity):
        with closing(self.connect()) as db, db:
            row = db.execute('SELECT revision,state,updated_at FROM chart_workspaces WHERE identity=?', (identity,)).fetchone()
        return {'revision': row[0], 'state': validate(json.loads(row[1])), 'updated_at': row[2]} if row else {
            'revision': 0, 'state': defaults(), 'updated_at': None}

    def save(self, identity, revision, state):
        if type(revision) is not int or revision < 0:
            raise ValueError('invalid_revision')
        validate(state)
        body = json.dumps(state, ensure_ascii=False, allow_nan=False)
        now = datetime.now(timezone.utc).isoformat()
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT revision FROM chart_workspaces WHERE identity=?', (identity,)).fetchone()
            if (row[0] if row else 0) != revision:
                raise Conflict('workspace_changed_elsewhere')
            db.execute('INSERT INTO chart_workspaces VALUES (?,?,?,?) ON CONFLICT(identity) DO UPDATE SET revision=excluded.revision,state=excluded.state,updated_at=excluded.updated_at',
                       (identity, revision + 1, body, now))
        return {'revision': revision + 1, 'updated_at': now}


def moving_averages(stock, periods):
    from invest_agent.trading.momentum_entry import sma
    if len(periods) > 6 or any(type(n) is not int or not 1 <= n <= 500 for n in periods):
        raise ValueError('invalid_period')
    bars = stock.get('bars', [])
    result = {}
    for n in dict.fromkeys(periods):
        data = []
        for i, bar in enumerate(bars):
            window = bars[max(0, i + 1 - n):i + 1]
            valid = len(window) == n and all(type(b.get('close')) in (int, float) and math.isfinite(b['close']) for b in window)
            data.append({'time': bar['date'], **({'value': sma(bars, n, i + 1)} if valid else {})})
        result[str(n)] = data
    return result
