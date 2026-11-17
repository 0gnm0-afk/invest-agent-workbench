"""M07 C3 consumer contracts over synthetic saved drawing revisions."""
from contextlib import closing, contextmanager, ExitStack
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from drawings.store import WorkspaceStore, defaults
from scenarios.monitor import ScenarioStore


class MonitorDrawingReads(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / 'chart-workspaces.sqlite3'
        self.store = ScenarioStore(self.path)
        self.ws = WorkspaceStore(self.path)
        self.now = datetime(2026, 10, 8, tzinfo=timezone.utc)
        self.bars = [dict(date=(datetime(2026, 9, 1) + timedelta(days=i)).date().isoformat(),
                          open=100, high=102, low=98, close=100, volume=10) for i in range(15)]
        self.identity = dict(market='US', symbol='S', currency='USD',
                             stable_id='SYNTHETIC_ISIN', price_basis='adjusted', exchange='NASDAQ')
        self.stock = dict(currency='USD', isin_code='SYNTHETIC_ISIN',
                          price_basis='adjusted', exchange='NASDAQ', bars=self.bars)
        self.bundle = dict(as_of=self.now.isoformat(), query_state='received', accounts=[
            dict(alias='synthetic', holdings_complete=True, positions=[
                dict(market='US', symbol='S', quantity=2, currency='USD')])])
        self.state = defaults()
        self.state['drawings'] = [dict(id='box', kind='box', points=[
            dict(time='2026-09-01', price=90), dict(time='2026-09-15', price=110)],
            color='#2271b2', width=2, extend=False)]
        self.ws.shared('US:S', revision=0, state=self.state)

    def register(self, revision=1, purpose='holding', drawing_id='box'):
        return self.store.register(stock_key='US:S', identity=self.identity,
            case_id='an_synthetic', plan_record_id='it_synthetic', plan_id='pl_synthetic',
            plan_version=1, drawing_id=drawing_id, workspace_revision=revision,
            purpose=purpose, chart=dict(bars=self.bars), test_record=True)

    def evaluate(self):
        return self.store.update(self.bundle, {'US:S': self.stock},
            {'US': self.bars[-1]['date']}, self.now, {'US': [b['date'] for b in self.bars]})

    def save(self, revision, price=None, deleted=False):
        state = deepcopy(self.state)
        if deleted:
            state['drawings'] = []
        elif price is not None:
            state['drawings'][0]['points'][0]['price'] = price
        return self.ws.shared('US:S', revision=revision, state=state)

    @contextmanager
    def public_readers_only(self):
        # Run the real M06 implementation, while forbidding the previous entry points.
        with ExitStack() as stack:
            for name in ('connect', 'shared', 'read_shared', 'shared_versions'):
                stack.enter_context(patch.object(WorkspaceStore, name,
                    side_effect=AssertionError('legacy drawing entry point: ' + name)))
            yield

    def test_public_readers_share_the_monitor_write_transaction(self):
        calls, writes = [], []
        original_saved = self.store.workspace.saved_workspace
        original_revisions = self.store.workspace.workspace_revisions
        original_save = self.store._save

        def saved(key, *args, connection=None, **kwargs):
            self.assertIsNotNone(connection)
            self.assertTrue(connection.in_transaction)
            calls.append(('saved', connection))
            value = original_saved(key, *args, connection=connection, **kwargs)
            self.assertTrue(connection.in_transaction)
            return value

        def revisions(key, after_revision, *, connection=None):
            self.assertIsNotNone(connection)
            self.assertTrue(connection.in_transaction)
            calls.append(('revisions', connection))
            value = original_revisions(key, after_revision, connection=connection)
            self.assertTrue(connection.in_transaction)
            return value

        def save(db, monitor, reason=None):
            self.assertIs(db, calls[-1][1])
            self.assertTrue(db.in_transaction)
            writes.append(calls[-1][0])
            return original_save(db, monitor, reason)

        with self.public_readers_only(), \
                patch.object(self.store.workspace, 'saved_workspace', side_effect=saved) as latest, \
                patch.object(self.store.workspace, 'workspace_revisions', side_effect=revisions) as history, \
                patch.object(self.store, '_save', side_effect=save):
            self.register()
            latest.assert_called_once()
            history.assert_not_called()
        self.save(1, price=92)
        with self.public_readers_only(), \
                patch.object(self.store.workspace, 'workspace_revisions', side_effect=revisions) as history, \
                patch.object(self.store, '_save', side_effect=save):
            self.store.sync_workspace('US:S')
            self.assertEqual(history.call_args.args, ('US:S', 1))
        self.assertEqual(writes, ['saved', 'revisions'])
        self.assertEqual([h['monitor']['workspace_revision']
                          for h in self.store.history(self.store.read()['monitors'][0]['id'])], [1, 2])

    def test_mixed_baselines_keep_missed_changes_deletion_and_restoration(self):
        early = self.register()
        self.evaluate()
        original_evaluations = self.store.read()['monitors'][0]['evaluations']
        self.save(1, price=92)
        later = self.register(revision=2, purpose='buy')
        self.save(2, deleted=True)
        self.save(3, price=95)
        reader = self.store.workspace.workspace_revisions
        with self.public_readers_only(), \
                patch.object(self.store.workspace, 'workspace_revisions', wraps=reader) as history:
            self.store.sync_workspace('US:S')
            self.assertEqual(history.call_args.args, ('US:S', 1))
        expected = {early['id']: [1, 2, 3, 4], later['id']: [2, 3, 4]}
        for ident, revisions in expected.items():
            rows = self.store.history(ident)
            self.assertEqual([h['monitor']['workspace_revision'] for h in rows], revisions)
            self.assertEqual([h['reason'] for h in rows][-2:],
                             ['saved_drawing_deleted', 'saved_drawing_changed'])
            self.assertEqual(rows[-2]['monitor']['issue'], 'saved_drawing_deleted_or_type_changed')
            self.assertIsNone(rows[-1]['monitor']['issue'])
        current = self.store.read()
        self.assertEqual(current['monitors'][0]['evaluations'], original_evaluations)
        self.assertTrue(current['monitors'][0]['latest']['superseded'])
        self.store.sync_workspace('US:S')
        self.assertEqual(self.store.read(), current)

    def test_missing_intermediate_revision_does_not_partially_sync(self):
        monitor = self.register()
        self.evaluate()
        self.save(1, price=92)
        self.save(2, deleted=True)
        self.save(3, price=95)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('DELETE FROM chart_workspace_versions WHERE identity=? AND revision=3', ('US:S',))
        before, history = self.store.read(), self.store.history(monitor['id'])
        with self.public_readers_only(), self.assertRaisesRegex(ValueError, 'saved_drawing_history_incomplete'):
            self.store.sync_workspace('US:S')
        self.assertEqual(self.store.read(), before)
        self.assertEqual(self.store.history(monitor['id']), history)

    def test_corrupt_intermediate_revision_does_not_partially_sync(self):
        monitor = self.register()
        self.evaluate()
        self.save(1, price=92)
        self.save(2, price=95)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('UPDATE chart_workspace_versions SET payload=? WHERE identity=? AND revision=2',
                       ('{', 'US:S'))
        before, history = self.store.read(), self.store.history(monitor['id'])
        with self.public_readers_only(), self.assertRaisesRegex(ValueError, 'saved_drawing_corrupt_data'):
            self.store.sync_workspace('US:S')
        self.assertEqual(self.store.read(), before)
        self.assertEqual(self.store.history(monitor['id']), history)

    def test_history_before_active_baseline_does_not_block_sync(self):
        paused = self.register()
        self.store.configure(paused['id'], paused['revision'], active=False)
        self.save(1, price=92)
        active = self.register(revision=2)
        self.save(2, price=95)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('DELETE FROM chart_workspace_versions WHERE identity=? AND revision<2', ('US:S',))
        paused_before = self.store.history(paused['id'])
        reader = self.store.workspace.workspace_revisions
        with self.public_readers_only(), \
                patch.object(self.store.workspace, 'workspace_revisions', wraps=reader) as history:
            self.store.sync_workspace('US:S')
            self.assertEqual(history.call_args.args, ('US:S', 2))
        self.assertEqual([h['monitor']['workspace_revision'] for h in self.store.history(active['id'])], [2, 3])
        self.assertEqual(self.store.history(paused['id']), paused_before)

    def test_l1_and_unsaved_proposal_ids_cannot_enter_monitoring(self):
        with self.public_readers_only(), \
                patch('drawings.l1_trendlines.l1_trendlines', side_effect=AssertionError('L1 read forbidden')), \
                patch('drawings.l1_trendlines.l1trend', side_effect=AssertionError('L1 calculation forbidden')):
            for ident in ('l1_synthetic', 'unadopted_synthetic'):
                with self.subTest(drawing_id=ident), self.assertRaisesRegex(ValueError, 'saved_drawing_required'):
                    self.register(drawing_id=ident)
            self.assertEqual(self.store.read()['monitors'], [])
            self.register()
            self.bars[-1].update(open=90, close=90, high=92, low=88)
            result = self.evaluate()
        self.assertEqual({m['drawing_id'] for m in result['monitors']}, {'box'})
        self.assertEqual({e['drawing_id'] for e in result['events']}, {'box'})
        self.assertTrue(result['events'])
