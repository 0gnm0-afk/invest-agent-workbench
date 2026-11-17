"""Saved session reads preserve analysis and price history without writes."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from session_context import saved_context
from market.prices import PriceStore, saved_price_context


class SavedContextTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.path = self.root / 'runtime/chart-workspaces.sqlite3'
        self.prices = self.root / 'runtime/ohlcv.sqlite3'

    def fingerprint(self, path):
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
            schema = db.execute('SELECT type,name,sql FROM sqlite_master ORDER BY type,name').fetchall()
        return hashlib.sha256(path.read_bytes()).hexdigest(), schema

    def analysis_fixture(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.executescript('''
                CREATE TABLE analysis_cases (stock_key TEXT, payload TEXT);
                CREATE TABLE analysis_items (case_id TEXT, kind TEXT, payload TEXT);
                CREATE TABLE shared_chart_workspaces (identity TEXT, revision INTEGER, payload TEXT, updated_at TEXT);
                CREATE TABLE scenario_monitors (payload TEXT);
                CREATE TABLE scenario_monitor_events (payload TEXT);
            ''')

    def price_fixture(self):
        self.prices.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.prices)) as db, db:
            db.executescript('''
                CREATE TABLE price_series (id TEXT PRIMARY KEY, metadata TEXT);
                CREATE TABLE prices (series_id TEXT, day TEXT, close REAL, revision INTEGER);
            ''')

    def test_absent_and_unrelated_database_are_not_created_or_migrated(self):
        empty = saved_context('US:SYNTH', root=self.root)
        self.assertFalse(empty['found'])
        self.assertEqual(empty['prices'], [])
        self.assertEqual(empty['errors'], [])
        self.assertFalse((self.root / 'runtime').exists())
        self.path.parent.mkdir()
        for path in (self.path, self.prices, self.root / 'runtime/records.sqlite3'):
            with closing(sqlite3.connect(path)) as db, db:
                db.execute('CREATE TABLE original (value TEXT)')
                db.execute("INSERT INTO original VALUES ('keep')")
        before = {p: self.fingerprint(p) for p in self.path.parent.iterdir()}
        result = saved_context('US:SYNTH', root=self.root)
        self.assertFalse(result['found'])
        self.assertCountEqual(result['errors'], [
            {'component': 'saved_analysis', 'error_type': 'OperationalError'},
            {'component': 'prices', 'error_type': 'OperationalError'}])
        self.assertEqual(before, {p: self.fingerprint(p) for p in before})

    def test_saved_analysis_pages_preserve_attribution_and_decisions(self):
        self.analysis_fixture()
        with closing(sqlite3.connect(self.path)) as db, db:
            for case in [dict(id='case-one'), dict(id='case-test', test_record=True)]:
                db.execute('INSERT INTO analysis_cases VALUES (?,?)', ('US:SYNTH', json.dumps(case)))
            db.execute('INSERT INTO analysis_cases VALUES (?,?)', ('US:OTHER', json.dumps(dict(id='case-other'))))
            records = [dict(id='discussion-old', author='user', body='Original words'),
                       dict(id='draft', author='ai', body='Unadopted proposal'),
                       dict(id='decision', decision='defer'),
                       dict(id='discussion-new', author='user', body='Still unresolved')]
            for item, kind in zip(records, ['discussion', 'plan', 'plan_decision', 'discussion']):
                db.execute('INSERT INTO analysis_items VALUES (?,?,?)', ('case-one', kind, json.dumps(item)))
            db.execute('INSERT INTO analysis_items VALUES (?,?,?)', ('case-test', 'discussion', json.dumps(dict(id='hidden'))))
            db.execute('INSERT INTO shared_chart_workspaces VALUES (?,?,?,?)', ('US:SYNTH', 7, '{"drawings":[]}', '2026-01-01'))
            db.execute('INSERT INTO scenario_monitors VALUES (?)', (json.dumps(dict(id='monitor', stock_key='US:SYNTH', geometry_bars=['private payload'])),))
        before = self.fingerprint(self.path)
        first = saved_context('US:SYNTH', root=self.root, limit=2)
        second = saved_context('US:SYNTH', root=self.root, limit=2, offset=2)
        self.assertEqual(first['records_page'], {'total': 4, 'offset': 0, 'next_offset': 2})
        self.assertEqual(second['records_page'], {'total': 4, 'offset': 2, 'next_offset': None})
        self.assertEqual([r['id'] for r in first['records'] + second['records']],
                         ['discussion-new', 'decision', 'draft', 'discussion-old'])
        self.assertEqual(first['cases'], [{'id': 'case-one'}])
        self.assertEqual(first['current_discussions'], [records[-1]])
        self.assertEqual(first['plan_decisions'], [records[2]])
        self.assertEqual(first['drawings'][0]['revision'], 7)
        self.assertNotIn('geometry_bars', first['monitors'][0])
        self.assertEqual(first['mode'], 'saved_only')
        self.assertEqual(first['authority'], 'historical_evidence_not_current_confirmation')
        self.assertNotIn('cycles', first)
        self.assertEqual(before, self.fingerprint(self.path))
        self.assertFalse(self.prices.exists())

    def test_saved_price_series_preserve_metadata_latest_bar_and_empty_series(self):
        self.price_fixture()
        series = [dict(market='US', symbol='SYNTH', basis='adjusted'),
                  dict(market='US', symbol='SYNTH', basis='raw'),
                  dict(market='US', symbol='OTHER', basis='raw')]
        with closing(sqlite3.connect(self.prices)) as db, db:
            for number, metadata in enumerate(series):
                db.execute('INSERT INTO price_series VALUES (?,?)', (str(number), json.dumps(metadata)))
            db.executemany('INSERT INTO prices VALUES (?,?,?,?)', [
                ('0', '2026-01-01', 11, 1), ('0', '2026-01-02', 12, 3), ('2', '2026-01-03', 99, 1)])
        before = self.fingerprint(self.prices)
        result = saved_context('US:SYNTH', root=self.root)
        self.assertEqual(result['prices'], [
            {'metadata': series[0], 'last_completed_saved_bar': {'day': '2026-01-02', 'close': 12.0, 'revision': 3}},
            {'metadata': series[1], 'last_completed_saved_bar': None}])
        self.assertEqual(result['errors'], [])
        self.assertTrue(result['found'])
        self.assertEqual(before, self.fingerprint(self.prices))
        self.assertFalse(self.path.exists())

    def test_corrupt_saved_components_are_explicit_errors_without_writes(self):
        self.path.parent.mkdir()
        for path in (self.path, self.prices, self.root / 'runtime/records.sqlite3'):
            path.write_bytes(b'not a sqlite database')
        before = {p: p.read_bytes() for p in self.path.parent.iterdir()}
        result = saved_context('US:SYNTH', root=self.root)
        self.assertCountEqual(result['errors'], [
            {'component': component, 'error_type': 'DatabaseError'}
            for component in ('saved_analysis', 'prices', 'technical_signals')])
        self.assertFalse(result['found'])
        self.assertEqual(before, {p: p.read_bytes() for p in before})

    def test_price_service_matches_complete_schema_results_without_writes(self):
        self.price_fixture()
        series = [dict(market='US', symbol='SYNTH', basis='adjusted'),
                  dict(market='US', symbol='SYNTH', basis='raw'),
                  dict(market='US', symbol='OTHER', basis='raw')]
        with closing(sqlite3.connect(self.prices)) as db, db:
            for number, metadata in enumerate(series):
                db.execute('INSERT INTO price_series VALUES (?,?)', (str(number), json.dumps(metadata)))
            db.executemany('INSERT INTO prices VALUES (?,?,?,?)', [
                ('0', '2026-01-01', 11, 1), ('0', '2026-01-02', 12, 3), ('2', '2026-01-03', 99, 1)])
        before = self.fingerprint(self.prices)
        original_connect = PriceStore.connect

        def readonly(store, *, write=False):
            self.assertFalse(write, 'C2 context attempted a price write')
            return original_connect(store, write=False)

        with patch.object(PriceStore, 'connect', readonly):
            for key in ('US:SYNTH', 'US:OTHER', 'US:MISSING'):
                with self.subTest(key=key):
                    legacy = saved_context(key, root=self.root)
                    self.assertEqual(saved_price_context(key, root=self.root), legacy['prices'])
                    self.assertEqual(legacy['errors'], [])
        self.assertEqual(before, self.fingerprint(self.prices))

    def test_price_service_missing_bars_table_reports_error_without_partial_prices(self):
        # After the 2b switch the context no longer publishes a partial [] when
        # the bars table is missing; the error is recorded and prices is absent.
        self.prices.parent.mkdir(parents=True)
        with closing(sqlite3.connect(self.prices)) as db, db:
            db.execute('CREATE TABLE price_series (id INTEGER PRIMARY KEY, metadata TEXT)')
            db.execute('INSERT INTO price_series VALUES (?,?)',
                       (1, json.dumps(dict(market='US', symbol='SYNTH'))))
        before = self.fingerprint(self.prices)
        legacy = saved_context('US:SYNTH', root=self.root)
        self.assertNotIn('prices', legacy)
        self.assertEqual(legacy['errors'], [{'component': 'prices', 'error_type': 'OperationalError'}])
        self.assertFalse(legacy['found'])
        with self.assertRaisesRegex(sqlite3.OperationalError, 'no such table: prices'):
            saved_price_context('US:SYNTH', root=self.root)
        self.assertEqual(before, self.fingerprint(self.prices))

    def test_price_service_missing_series_table_keeps_prices_key_absent(self):
        # Initializing prices=[] before C2 is not an equivalent workaround:
        # errors while selecting series have historically omitted the key.
        self.prices.parent.mkdir(parents=True)
        with closing(sqlite3.connect(self.prices)) as db, db:
            db.execute('CREATE TABLE original (value TEXT)')
        before = self.fingerprint(self.prices)
        legacy = saved_context('US:SYNTH', root=self.root)
        self.assertNotIn('prices', legacy)
        self.assertEqual(legacy['errors'], [{'component': 'prices', 'error_type': 'OperationalError'}])
        self.assertFalse(legacy['found'])
        with self.assertRaisesRegex(sqlite3.OperationalError, 'no such table: price_series'):
            saved_price_context('US:SYNTH', root=self.root)
        self.assertEqual(before, self.fingerprint(self.prices))

    def test_invalid_input_is_rejected_before_reading(self):
        with patch('session_context._read', side_effect=AssertionError('read attempted')):
            for stock in ('SYNTH', 'US:synth', '', None):
                with self.subTest(stock=stock), self.assertRaisesRegex(ValueError, 'exact_stock_key_required'):
                    saved_context(stock, root=self.root)
            for kwargs in ({'offset': -1}, {'offset': True}, {'limit': 0}, {'limit': 101}, {'limit': True}):
                with self.subTest(kwargs=kwargs), self.assertRaisesRegex(ValueError, 'invalid_page'):
                    saved_context('US:SYNTH', root=self.root, **kwargs)
        self.assertFalse((self.root / 'runtime').exists())

    def test_recent_context_exposes_full_history_tools(self):
        result = saved_context('US:SYNTH', root=self.root)
        navigation = result['history_navigation']
        self.assertEqual(navigation['scope'], 'recent_context_not_complete_history')
        self.assertEqual(navigation['all_cases'], {
            'tool': 'analysis_list', 'arguments': {'stock_key': 'US:SYNTH'}})
        self.assertEqual(navigation['case_history']['tool'], 'analysis_history')
        self.assertEqual(navigation['limits']['signals'], 30)
        self.assertNotIn('cycles', navigation)
        self.assertFalse(self.path.exists())


if __name__ == '__main__':
    unittest.main()
