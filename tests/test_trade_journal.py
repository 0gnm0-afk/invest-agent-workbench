from contextlib import closing
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

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


if __name__ == '__main__':
    unittest.main()
