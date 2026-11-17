"""M06 saved drawing reads use synthetic SQLite files only."""
from contextlib import closing
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from drawings.store import WorkspaceStore, defaults


def drawing(price=100, ident='line'):
    return dict(id=ident, kind='horizontal', points=[dict(time='2026-10-01', price=price)],
                color='#218775', width=2, extend=False)


class ReadOnlyWorkspaces(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'saved.sqlite3'
        self.store = WorkspaceStore(self.path)
        self.key = 'US:FIXTURE'

    def tearDown(self):
        self.temp.cleanup()

    def save(self, revision, drawings):
        state = defaults()
        state['drawings'] = drawings
        return self.store.shared(self.key, revision=revision, state=state)

    def fingerprint(self):
        return {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                for p in self.path.parent.iterdir() if p.is_file()}

    def test_missing_legacy_and_empty_states_never_create_or_migrate(self):
        self.assertEqual(self.store.saved_workspace(self.key)['status'], 'missing_database')
        self.assertEqual(self.store.workspace_revisions(self.key, 0)['status'], 'missing_database')
        self.assertFalse(self.path.exists())
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('CREATE TABLE unrelated (value TEXT)')
        self.assertEqual(self.store.saved_workspace(self.key)['status'], 'missing_table')
        self.store.save(self.key + '|1w', 0, defaults())
        before = self.fingerprint()
        self.assertEqual(self.store.saved_workspace(self.key)['status'], 'legacy_requires_migration')
        self.assertEqual(self.store.workspace_revisions(self.key, 0)['status'], 'legacy_requires_migration')
        self.assertEqual(self.fingerprint(), before)
        self.save(1, [])
        self.assertEqual(self.store.saved_workspace(self.key)['status'], 'empty')
        self.assertEqual(self.store.saved_workspace('US:ABSENT')['status'], 'missing_workspace')
        self.assertEqual(self.store.saved_workspace(self.key, 98)['status'], 'missing_revision')

    def test_latest_and_exact_revision_keep_original_payload_and_saved_time(self):
        initial = self.save(0, [drawing(100)])
        latest = self.save(1, [drawing(120)])
        before = self.fingerprint()
        packet = self.store.saved_workspace(self.key)
        old = self.store.saved_workspace(self.key, 1)
        self.assertEqual(packet['status'], 'ok')
        self.assertEqual(packet['workspace_revision'], 2)
        self.assertEqual(packet['workspace_saved_at'], latest['updated_at'])
        self.assertEqual(packet['payload']['drawings'], [drawing(120)])
        self.assertEqual(old['workspace_revision'], 1)
        self.assertEqual(old['workspace_saved_at'], initial['updated_at'])
        self.assertEqual(old['payload']['drawings'], [drawing(100)])
        self.assertEqual(self.fingerprint(), before)

    def test_all_intermediate_change_delete_restore_revisions_are_retained(self):
        self.save(0, [drawing(100)])
        self.save(1, [drawing(120)])
        self.save(2, [])
        self.save(3, [drawing(150)])
        before = self.fingerprint()
        packet = self.store.workspace_revisions(self.key, 1)
        self.assertEqual(packet['status'], 'ok')
        self.assertTrue(packet['history_complete'])
        self.assertEqual(packet['current_revision'], 4)
        self.assertEqual(packet['baseline']['revision'], 1)
        self.assertEqual([r['revision'] for r in packet['revisions']], [2, 3, 4])
        self.assertEqual([r['changes'][0]['action'] for r in packet['revisions']],
                         ['changed', 'deleted', 'restored'])
        self.assertEqual(packet['revisions'][0]['changes'][0]['before'], drawing(100))
        self.assertEqual(packet['revisions'][2]['changes'][0]['after'], drawing(150))
        self.assertTrue(all(r['changes_complete'] for r in packet['revisions']))
        self.assertEqual(self.fingerprint(), before)
        self.assertEqual(self.store.workspace_revisions(self.key, 4)['revisions'], [])

    def test_read_only_connection_forbids_write_without_initializing_tables(self):
        self.save(0, [drawing()])
        before = self.fingerprint()
        with self.store.read_transaction() as db:
            self.assertTrue(db.in_transaction)
            self.assertEqual(self.store.saved_workspace(self.key, connection=db)['revision'], 1)
            with self.assertRaises(sqlite3.OperationalError):
                db.execute('CREATE TABLE accidental (value TEXT)')
        self.assertEqual(self.fingerprint(), before)

    def test_caller_transaction_is_reused_and_not_committed_or_rolled_back(self):
        self.save(0, [drawing()])
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('UPDATE shared_chart_workspaces SET updated_at=? WHERE identity=?',
                       ('synthetic-uncommitted', self.key))
            packet = self.store.saved_workspace(self.key, connection=db)
            self.assertEqual(packet['workspace_saved_at'], 'synthetic-uncommitted')
            self.assertTrue(db.in_transaction)
            self.assertNotEqual(self.store.saved_workspace(self.key)['workspace_saved_at'],
                                'synthetic-uncommitted')
            db.rollback()
            with self.store.read_transaction(connection=db):
                self.assertTrue(db.in_transaction)
            self.assertFalse(db.in_transaction)

    def test_read_transaction_is_consistent_across_concurrent_saved_revision(self):
        self.save(0, [drawing(100)])
        with closing(sqlite3.connect(self.path)) as writer:
            writer.execute('PRAGMA journal_mode=WAL')
        with self.store.read_transaction() as db:
            old = self.store.saved_workspace(self.key, connection=db)
            self.save(1, [drawing(130)])
            current_in_snapshot = self.store.saved_workspace(self.key, connection=db)
            history = self.store.workspace_revisions(self.key, 0, connection=db)
            self.assertEqual(current_in_snapshot, old)
            self.assertEqual(history['current_revision'], 1)
            self.assertEqual([r['revision'] for r in history['revisions']], [1])
            self.assertEqual(history['revisions'][0]['workspace_saved_at'], old['workspace_saved_at'])
        self.assertEqual(self.store.saved_workspace(self.key)['revision'], 2)

    def test_missing_intermediate_or_legacy_history_is_explicit(self):
        self.save(0, [drawing(100)])
        self.save(1, [])
        self.save(2, [drawing(130)])
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('DELETE FROM chart_workspace_versions WHERE revision=2')
        before = self.fingerprint()
        packet = self.store.workspace_revisions(self.key, 1)
        self.assertEqual(packet['status'], 'history_incomplete')
        self.assertEqual(packet['missing_revision_ranges'], [[2, 2]])
        self.assertEqual([r['revision'] for r in packet['revisions']], [3])
        self.assertFalse(packet['revisions'][0]['changes_complete'])
        self.assertEqual(self.store.saved_workspace(self.key, 2)['status'], 'missing_revision')
        self.assertEqual(self.fingerprint(), before)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('DROP TABLE chart_workspace_versions')
        before = self.fingerprint()
        packet = self.store.workspace_revisions(self.key, 1)
        self.assertEqual(packet['status'], 'history_incomplete')
        self.assertEqual(packet['missing_revision_ranges'], [[1, 2]])
        self.assertEqual(packet['revisions'][0]['revision'], 3)
        self.assertEqual(self.fingerprint(), before)

    def test_corrupt_database_json_payload_and_history_are_not_empty_success(self):
        self.path.write_bytes(b'not sqlite')
        self.assertEqual(self.store.saved_workspace(self.key)['status'], 'corrupt_data')
        self.assertEqual(self.store.workspace_revisions(self.key, 0)['status'], 'corrupt_data')
        self.path.unlink()
        self.save(0, [drawing()])
        original = self.store.saved_workspace(self.key)['payload']
        for raw in ('{', '{}', json.dumps({**original, 'drawings': [dict(drawing(), points=[])]})):
            with closing(sqlite3.connect(self.path)) as db, db:
                db.execute('UPDATE shared_chart_workspaces SET payload=?', (raw,))
            before = self.fingerprint()
            self.assertEqual(self.store.saved_workspace(self.key)['status'], 'corrupt_data')
            self.assertEqual(self.store.workspace_revisions(self.key, 0)['status'], 'corrupt_data')
            self.assertEqual(self.fingerprint(), before)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('UPDATE shared_chart_workspaces SET payload=?', (json.dumps(original),))
            altered = deepcopy(original)
            altered['drawings'] = [drawing(999)]
            db.execute('UPDATE chart_workspace_versions SET payload=? WHERE revision=1', (json.dumps(altered),))
        self.assertEqual(self.store.workspace_revisions(self.key, 0)['status'], 'corrupt_data')

    def test_invalid_keys_revisions_and_ahead_of_head_are_explicit(self):
        for key in ('', 'US:', 'US:lower', '../US:A', None):
            with self.assertRaises(ValueError):
                self.store.saved_workspace(key)
        for revision in (True, -1, '1', 1.0):
            with self.assertRaises(ValueError):
                self.store.saved_workspace(self.key, revision)
        for revision in (True, -2, None, '0'):
            with self.assertRaises(ValueError):
                self.store.workspace_revisions(self.key, revision)
        self.save(0, [drawing()])
        self.assertEqual(self.store.workspace_revisions(self.key, 2)['status'], 'revision_ahead_of_workspace')
        packet = self.store.workspace_revisions(self.key, -1)
        self.assertEqual([r['revision'] for r in packet['revisions']], [0, 1])
        self.assertEqual(packet['revisions'][1]['changes'][0]['action'], 'added')
