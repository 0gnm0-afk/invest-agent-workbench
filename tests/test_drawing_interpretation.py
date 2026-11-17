"""C3 synthetic saved DB tests. All IO stays inside the M00 temporary root."""
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from analysis import digest, packed
from analysis.collaboration import Collaboration
from drawings.geometry import VERSION
from drawings.interpretation import investment_drawings, interpret_drawing, distance, saved_adopted_references
from drawings.store import WorkspaceStore, defaults
from market.prices import PriceStore


def drawing(kind='trend'):
    points = [{'time': '2026-09-01', 'price': 100.0},
              {'time': '2026-09-10', 'price': 110.0},
              {'time': '2026-09-11', 'price': 105.0}]
    if kind == 'riskreward':
        points[1]['price'], points[2]['price'] = 90.0, 120.0
    return dict(id=kind, kind=kind, points=points[:{'horizontal': 1, 'channel': 3, 'riskreward': 3}.get(kind, 2)],
                color='#218775', width=2, extend=True)


class DrawingInterpretation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = WorkspaceStore(self.root / 'chart.sqlite3')
        self.now = datetime(2026, 9, 23, 22, tzinfo=timezone.utc)
        days = ['2026-09-%02d' % n for n in range(1, 24)
                if datetime(2026, 9, n).weekday() < 5]
        self.bars = [dict(date=d, open=105.0, high=120.0, low=90.0, close=108.0, volume=10) for d in days]
        self.identity = dict(market='US', symbol='FIX', currency='USD', price_basis='adjusted', isin='US-FIX')
        self.packet = dict(identity=self.identity, bars=self.bars,
                           bars_ref=dict(identity=self.identity, revision=1, sha256='fixture'))
        self.contexts = [dict(metadata=self.identity, last_completed_saved_bar=dict(day=days[-1], close=108.0, revision=1))]
        self.state = defaults()
        self.state['drawings'] = [drawing(k) for k in ('trend', 'channel', 'horizontal', 'box', 'measure', 'riskreward')]
        self.store.shared('US:FIX', revision=0, state=self.state)

    def tearDown(self):
        self.tmp.cleanup()

    def read(self, **kw):
        options = dict(root=self.root, workspace_store=self.store, now=self.now,
                       price_reader=lambda *a, **k: deepcopy(self.packet),
                       price_context_reader=lambda *a, **k: deepcopy(self.contexts),
                       sessions_reader=lambda *a: ['2026-09-23'], include_l1=False)
        options.update(kw)
        return investment_drawings('US:FIX', **options)

    def adopt(self, *, plan_id='pl_fixture', timeframe='1w', action='adopt', revision=1):
        workspace = self.store.saved_workspace('US:FIX', revision)
        snapshot_workspace = dict(revision=revision, updated_at=workspace['workspace_saved_at'],
                                  state=dict(schema=1, drawings=workspace['payload']['drawings'],
                                             averages=workspace['payload']['averages'][timeframe]))
        sid, cid = 'sn_' + plan_id, 'an_fixture'
        snap = dict(id=sid, case_id=cid, stock_key='US:FIX', workspace=snapshot_workspace,
                    chart=dict(timeframe=timeframe, price_basis='adjusted'),
                    component_hashes=dict(workspace=digest(snapshot_workspace)))
        ref = dict(drawing_id='trend', workspace_revision=revision, timeframe=timeframe,
                   price_basis='adjusted', geometry_version=VERSION, line='line')
        plan = dict(title='fixture', conditions=[dict(drawing_ref=ref)])
        record = dict(id='it_' + plan_id, case_id=cid, stock_key='US:FIX', kind='plan_version',
                      plan_id=plan_id, plan_version=1, plan=plan, plan_sha256=digest(plan),
                      snapshot_id=sid, snapshot_sha256=digest(snap), identity=self.identity,
                      revision=1, status='draft')
        decision = dict(id='it_decision_' + plan_id, case_id=cid, stock_key='US:FIX', kind='plan_decision',
                        plan_id=plan_id, plan_record_id=record['id'], plan_version=1,
                        plan_sha256=record['plan_sha256'], action=action, revision=2, status='recorded')
        for row in (record, decision):
            body = Collaboration.markdown(None, row).encode()
            path = self.root / (row['id'] + '.md')
            path.write_bytes(body)
            row.update(markdown_path=path.name, markdown_sha256=hashlib.sha256(body).hexdigest())
        with closing(sqlite3.connect(self.store.path)) as db, db:
            db.executescript('''CREATE TABLE IF NOT EXISTS analysis_cases(id TEXT PRIMARY KEY,stock_key TEXT,payload TEXT);
                CREATE TABLE IF NOT EXISTS analysis_items(id TEXT PRIMARY KEY,case_id TEXT,kind TEXT,payload TEXT);
                CREATE TABLE IF NOT EXISTS analysis_snapshots(id TEXT PRIMARY KEY,case_id TEXT,payload TEXT,sha256 TEXT);''')
            db.execute('INSERT OR IGNORE INTO analysis_cases VALUES (?,?,?)', (cid, 'US:FIX', packed(dict(id=cid, stock_key='US:FIX'))))
            db.execute('INSERT INTO analysis_snapshots VALUES (?,?,?,?)', (sid, cid, packed(snap), digest(snap)))
            for row in (record, decision):
                db.execute('INSERT INTO analysis_items VALUES (?,?,?,?)', (row['id'], cid, row['kind'], packed(row)))
            db.execute('INSERT OR IGNORE INTO analysis_items VALUES (?,?,?,?)',
                       ('it_ai', cid, 'proposal', packed(dict(id='it_ai', drawings=[drawing('horizontal')]))))
        return record

    def test_saved_drawings_packet_and_summary(self):
        result = self.read()
        self.assertEqual(result['price']['date'], '2026-09-23')
        self.assertEqual(len(result['latest']['drawings']), 6)
        for original, item in zip(self.state['drawings'], result['latest']['drawings']):
            self.assertEqual(item['coordinates'], original['points'])
            self.assertEqual(item['workspace_revision'], 1)
            self.assertEqual(item['workspace_saved_at'], result['latest']['workspace_saved_at'])
            self.assertEqual(item['timeframe'], '1d')
            self.assertIn('revision 1', item['summary'])
            self.assertIn('2026-09-23', item['summary'])
            for value in item['levels']:
                self.assertAlmostEqual(value['distance'], 108-value['line_value'])
                self.assertAlmostEqual(value['distance_pct'], (108-value['line_value'])/value['line_value']*100)
                self.assertIn(f"{value['distance']:+.10g}", item['summary'])

    def test_adopted_and_latest_revisions_stay_separate(self):
        self.adopt(timeframe='1w')
        self.adopt(plan_id='pl_other', timeframe='1mo')
        self.store.shared('US:FIX', revision=1, state=defaults())
        result = self.read()
        self.assertEqual(result['latest']['workspace_revision'], 2)
        self.assertEqual(result['latest']['drawings'], [])
        self.assertEqual(len(result['adopted']['drawings']), 2)
        self.assertEqual({d['timeframe'] for d in result['adopted']['drawings']}, {'1w', '1mo'})
        self.assertTrue(all(d['workspace_revision'] == 1 for d in result['adopted']['drawings']))
        self.assertTrue(all(d['drawing_id'] == 'trend' for d in result['adopted']['drawings']))
        self.assertNotIn('it_ai', packed(result))

    def test_unadopted_revoked_and_missing_references(self):
        self.adopt(action='revoke')
        self.assertEqual(self.read()['adopted']['drawings'], [])
        self.adopt(plan_id='pl_active')
        self.store.shared('US:FIX', revision=1, state=defaults())
        with closing(sqlite3.connect(self.store.path)) as db, db:
            db.execute('DELETE FROM chart_workspace_versions WHERE revision=1')
        adopted = self.read()['adopted']['drawings'][0]
        self.assertEqual(adopted['status'], 'missing_reference')
        self.assertEqual(adopted['reason'], 'missing_revision')

    def test_interpretation_never_writes_or_refreshes(self):
        self.adopt()
        before = {p.name: p.read_bytes() for p in self.root.iterdir() if p.is_file()}
        with patch.object(self.store, 'connect', side_effect=AssertionError('writer called')), \
                patch.object(self.store, 'shared', side_effect=AssertionError('migration called')):
            result = self.read(include_l1=True)
        self.assertTrue(result['read_only'])
        self.assertEqual(result['automatic_l1']['kind'], 'automatic_l1_reference')
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.iterdir() if p.is_file()})

    def test_latest_completed_bar_and_price_identity(self):
        self.packet['bars'] += [dict(self.bars[-1], date='2026-09-24', close=111, completed=False),
                                dict(self.bars[-1], date='2026-09-25', projected=True)]
        self.assertEqual(self.read()['price']['close'], 108)
        self.packet['identity']['currency'] = 'KRW'
        # Metadata references are independent after read; reject a conflicting context.
        self.contexts[0]['metadata'] = dict(self.identity, currency='USD')
        self.assertEqual(self.read()['price']['status'], 'data_error')
        def bad_hash(*a, **k):
            raise ValueError('ohlcv_reference_hash_mismatch')
        self.assertEqual(self.read(price_reader=bad_hash)['price']['reason'], 'ohlcv_reference_hash_mismatch')

    def test_distance_basis_and_missing_values(self):
        for close in (90, 100, 110):
            self.assertEqual(distance(close, 100)['value'], close-100)
            self.assertEqual(distance(close, 100)['percent'], close-100)
        for close, line in ((None, 100), (100, 0), (float('nan'), 100)):
            value = distance(close, line)
            self.assertIsNone(value['percent'])
            self.assertEqual(value['status'], 'unavailable')
        self.packet['bars'] = []
        result = self.read()
        self.assertEqual(result['price']['status'], 'price_missing')
        self.assertTrue(all(v['distance_pct'] is None for d in result['latest']['drawings'] for v in d['levels']))

    def test_all_geometry_kinds_and_limits(self):
        price = self.read()['price']
        for frame in ('1d', '1w', '1mo'):
            for original in self.state['drawings']:
                item = interpret_drawing(original, workspace_revision=1, workspace_saved_at='fixture',
                                         price=price, bars=self.bars, timeframe=frame)
                self.assertEqual(item['status'], 'ok')
                blocked = interpret_drawing(original, workspace_revision=1, workspace_saved_at='fixture',
                                             price=price, bars=self.bars, timeframe=frame,
                                             context={'price_scale': 'log'})
                self.assertEqual(blocked['reason'], 'unsupported_price_transform')
        outside = drawing()
        outside['points'][0]['time'] = '1990-01-01'
        item = interpret_drawing(outside, workspace_revision=1, workspace_saved_at=None, price=price, bars=self.bars)
        self.assertEqual(item['reason'], 'anchor_outside_supported_timeline')

    def test_snapshot_and_plan_corruption_is_explicit(self):
        plan = self.adopt()
        with closing(sqlite3.connect(self.store.path)) as db, db:
            db.execute("UPDATE analysis_snapshots SET sha256='bad'")
        result = self.read()
        self.assertEqual(result['adopted']['status'], 'data_error')
        self.assertEqual(result['adopted']['reason'], 'snapshot_integrity_failed')
        (self.root / plan['markdown_path']).write_text('changed', encoding='utf-8')
        self.assertEqual(self.read()['adopted']['reason'], 'record_integrity_failed')

    def test_real_m03_saved_readers(self):
        prices = PriceStore(self.root / 'runtime/ohlcv.sqlite3', root=self.root)
        prices.put(dict(market='US', symbol='FIX', currency='USD', price_basis='adjusted',
                        provider='fixture', bars=self.bars), source='synthetic', kind='series')
        result = self.read(price_reader=None, price_context_reader=None)
        self.assertEqual(result['price']['status'], 'ok')
        self.assertEqual(result['price']['date'], '2026-09-23')
        self.assertIn('sha256', result['price']['bars_ref'])

    def test_missing_workspaces_do_not_create_files(self):
        missing = WorkspaceStore(self.root / 'does-not-exist.sqlite3')
        result = self.read(workspace_store=missing)
        self.assertEqual(result['latest']['status'], 'missing_database')
        self.assertFalse(missing.path.exists())

    def test_corrupt_workspace_is_explicit_and_unchanged(self):
        self.store.path.write_bytes(b'not a sqlite database')
        result = self.read()
        self.assertEqual(result['latest']['status'], 'corrupt_data')
        self.assertEqual(result['adopted']['status'], 'data_error')
        self.assertEqual(self.store.path.read_bytes(), b'not a sqlite database')

    def test_packet_uses_one_workspace_transaction(self):
        self.adopt()
        with closing(sqlite3.connect(self.store.path)) as db:
            db.execute('PRAGMA journal_mode=WAL')
        def concurrent_adopted(stock_key, *, root, connection):
            self.store.shared(stock_key, revision=1, state=defaults())
            return saved_adopted_references(stock_key, root=root, connection=connection)
        result = self.read(adopted_reader=concurrent_adopted)
        self.assertEqual(result['latest']['workspace_revision'], 1)
        self.assertEqual(result['adopted']['drawings'][0]['workspace_revision'], 1)
        self.assertEqual(self.store.saved_workspace('US:FIX')['workspace_revision'], 2)
