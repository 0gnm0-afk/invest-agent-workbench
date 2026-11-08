from contextlib import closing
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from records import journal
from records.store import RecordStore


class TradeJournalTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1] / 'runtime')
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.mother = self.root / 'mother'
        self.folder = self.mother / journal.VAULT_DIR
        self.folder.mkdir(parents=True)
        (self.root / 'config.json').write_text(json.dumps({'mother': str(self.mother)}), encoding='utf-8')
        self.store = RecordStore(self.root / 'runtime' / 'records.sqlite3')
        self.source = self.folder / 'sample.md'
        self.raw = '\ufeff---\r\ntype: 일지\r\n---\r\nprivate fixture\r\n'.encode('utf-8')
        self.source.write_bytes(self.raw)

    def test_actual_path_round_trip_when_original_is_absent(self):
        self.assertEqual(journal.migrate(self.root), {'planned': 1, 'inserted': 1, 'reused': 0})
        self.assertEqual(journal.migrate(self.root), {'planned': 1, 'inserted': 0, 'reused': 1})
        self.assertEqual(self.source.read_bytes(), self.raw)
        self.source.unlink()
        digest = hashlib.sha256(self.raw).hexdigest()
        self.assertEqual(self.store.read(self.source, digest), self.raw)
        self.assertEqual(journal.verify(self.root), {'sources': 1, 'versions': 1, 'verified_bytes': len(self.raw)})

    def test_other_vault_paths_and_types_cannot_be_registered(self):
        outside = self.folder.parent / 'other.md'
        outside.write_bytes(self.raw)
        with self.assertRaisesRegex(ValueError, 'trade_journal_source_outside_scope'):
            journal.migrate(self.root, sources=[self.source, outside])
        with self.assertRaisesRegex(ValueError, 'record_source_outside_runtime'):
            self.store.put(self.source, self.raw, root=self.root, kind=journal.KIND)
        invalid = self.folder / 'template.md'
        invalid.write_text('---\ntype: template\n---\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'trade_journal_type_required'):
            journal.migrate(self.root)
        with closing(self.store.connect()) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM record_versions').fetchone()[0], 0)

    def test_added_post_trade_section_preserves_previous_version(self):
        journal.migrate(self.root)
        amended = self.raw + b'\r\nReview fixture\r\n'
        self.source.write_bytes(amended)
        self.assertEqual(journal.migrate(self.root)['inserted'], 1)
        self.assertEqual(self.store.read(self.source), amended)
        self.assertEqual(self.store.read(self.source, hashlib.sha256(self.raw).hexdigest()), self.raw)
        self.assertEqual(journal.verify(self.root)['versions'], 2)

    def test_relocated_journals_read_original_keys_and_reuse_original_metadata(self):
        journal.migrate(self.root)
        old_source = self.source
        old_mother = self.mother
        intermediate = self.root / 'usb-mother'
        current = self.root / 'consolidated-mother'
        relative = Path('02.지식/20.매매일지/03.일지')
        new_folder = current / relative
        new_folder.parent.mkdir(parents=True)
        self.folder.replace(new_folder)
        (self.root / 'config.json').write_text(json.dumps({
            'mother': str(current), 'records': {
                'previous_mother_root': str(old_mother),
                'previous_mother_roots': [str(intermediate)],
                'journal_relative_path': relative.as_posix(),
                'previous_journal_folders': [str(old_mother / journal.VAULT_DIR),
                                             str(intermediate / journal.VAULT_DIR)]}}), encoding='utf-8')
        new_source = new_folder / self.source.name
        digest = hashlib.sha256(self.raw).hexdigest()
        before = self.store.path.read_bytes()
        with patch('runtime_paths.ROOT', self.root):
            for source in (old_source, intermediate / journal.VAULT_DIR / self.source.name, new_source):
                self.assertEqual(self.store.read(source, digest), self.raw)
            self.assertEqual(journal.verify(self.root)['versions'], 1)
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(journal.migrate(self.root), {'planned': 1, 'inserted': 0, 'reused': 1})
        new_source.unlink()
        self.assertEqual(journal.verify(self.root)['versions'], 1)
        with closing(self.store.connect()) as db:
            saved = db.execute('SELECT source_path,metadata FROM record_versions').fetchone()
            self.assertEqual(Path(saved[0]), old_source)
            self.assertEqual(json.loads(saved[1]), {
                'mother_relative_path': (journal.VAULT_DIR / self.source.name).as_posix()})

    def test_relocated_journal_still_rejects_changed_metadata_and_path_escape(self):
        journal.migrate(self.root)
        with closing(self.store.connect(write=True)) as db, db:
            db.execute('UPDATE record_versions SET metadata=?', ('{}',))
        with self.assertRaisesRegex(ValueError, 'trade_journal_metadata_mismatch'):
            journal.verify(self.root)
        (self.root / 'config.json').write_text(json.dumps({'mother': str(self.mother),
            'records': {'journal_relative_path': '../outside'}}), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'trade_journal_relative_path_invalid'):
            journal.verify(self.root)


if __name__ == '__main__':
    unittest.main()
