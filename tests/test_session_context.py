"""Saved session reads preserve current cycle state without schema writes."""
from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from cycles.ledger import TradeCycleStore
from session_context import saved_context
from test_trade_cycle import COMBO, fill, plan


class SavedCycleContextTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.path = self.root / 'runtime/chart-workspaces.sqlite3'

    def test_absent_and_unrelated_database_are_not_created_or_migrated(self):
        self.assertEqual(saved_context('US:SYNTH', root=self.root)['cycles'], [])
        self.assertFalse(self.path.exists())
        self.path.parent.mkdir(parents=True)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('CREATE TABLE original (value TEXT)')
        before = self.path.read_bytes()
        self.assertEqual(saved_context('US:SYNTH', root=self.root)['cycles'], [])
        self.assertEqual(before, self.path.read_bytes())

    def test_exact_adopted_version_and_actual_progress_survive_new_draft(self):
        store = TradeCycleStore(self.path)
        cycle = store.save_draft('US:SYNTH', plan())
        cycle = store.adopt(cycle['cycle_id'], 1, COMBO, cycle['revision'], principal='browser')
        cycle = store.record_fill(cycle['cycle_id'], fill('buy', 20, 10, 1), cycle['revision'], principal='browser')
        revised = plan()
        revised['title'] = 'Unadopted revision'
        cycle = store.save_draft('US:SYNTH', revised, cycle['cycle_id'], cycle['revision'])
        other = store.save_draft('US:SYNTH', plan())
        before = self.path.read_bytes()
        page = saved_context('US:SYNTH', root=self.root, limit=1)
        self.assertEqual(page['cycles'][0]['cycle_id'], other['cycle_id'])
        self.assertEqual(page['cycles_page'], {'total': 2, 'offset': 0, 'next_offset': 1})
        current = saved_context('US:SYNTH', root=self.root, limit=1, offset=1)['cycles'][0]
        self.assertEqual((current['draft_version'], current['adopted']['version']), (2, 1))
        self.assertEqual(current['adopted']['title'], plan()['title'])
        self.assertEqual(current['draft_title'], 'Unadopted revision')
        self.assertEqual(current['fill_count'], 1)
        self.assertEqual(current['state'], cycle['state'])
        self.assertEqual(current['next_steps'], cycle['next_steps'])
        self.assertEqual(current['full_record']['arguments'], {'cycle_id': cycle['cycle_id']})
        self.assertTrue(current['verified'])
        self.assertEqual(before, self.path.read_bytes())

    def test_recent_context_exposes_full_history_tools(self):
        result = saved_context('US:SYNTH', root=self.root)
        navigation = result['history_navigation']
        self.assertEqual(navigation['scope'], 'recent_context_not_complete_history')
        self.assertEqual(navigation['all_cases'], {
            'tool': 'analysis_list', 'arguments': {'stock_key': 'US:SYNTH'}})
        self.assertEqual(navigation['case_history']['tool'], 'analysis_history')
        self.assertEqual(navigation['limits']['signals'], 30)
        self.assertFalse(self.path.exists())

    def test_corrupt_immutable_draft_is_explicit_failure_not_head_only_success(self):
        store = TradeCycleStore(self.path)
        store.save_draft('US:SYNTH', plan())
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE trade_cycle_drafts SET sha256='broken'")
        result = saved_context('US:SYNTH', root=self.root)
        self.assertEqual(result['cycles'], [])
        self.assertIn({'component': 'cycles', 'error_type': 'ValueError'}, result['errors'])


if __name__ == '__main__':
    unittest.main()
