import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import atomic_io
from market.daily import seed_check
from records.classification import BASELINE, BASELINE_INPUTS, REFRESH_AUTHORITIES, REFRESH_INPUTS, migrate
from records.store import RecordStore
from sectors.basis import CONTRACT, FAQ_URL, load_basis
from sectors.classification_refresh import apply
from sectors.observation import age_issue


class ClassificationRecordsTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        self.mother = self.root / 'mother'
        self.old_mother = self.root / 'previous-mother'
        self.old_root = self.root / 'previous-workbench'
        self.runtime = self.root / 'runtime'
        self.store = RecordStore(self.runtime / 'records.sqlite3')
        self.applied = '2026-10-02T00:00:00+00:00'
        self.policies = {}
        for target in ('runtime_paths.ROOT', 'records.files.ROOT'):
            guard = patch(target, self.root)
            guard.start()
            self.addCleanup(guard.stop)
        guard = patch('socket.socket.connect', side_effect=AssertionError('fixture_external_network_forbidden'))
        guard.start()
        self.addCleanup(guard.stop)

        def put(path, value, *, policy=True):
            path.parent.mkdir(parents=True, exist_ok=True)
            raw = json.dumps(value, ensure_ascii=False).encode()
            path.write_bytes(raw)
            if policy:
                self.policies[path] = raw
            return hashlib.sha256(raw).hexdigest()

        baseline_pins = {}
        for relative, kind in BASELINE_INPUTS.items():
            value = {'rows': []} if kind == 'classification_table' else {}
            if kind == 'classification_calculation_basis':
                self.basis = self.old_mother / 'runtime' / relative
                doc = self.mother / 'runtime' / relative.parent / 'sources/toss-faq.md'
                doc.parent.mkdir(parents=True, exist_ok=True)
                raw = b'# Saved FAQ fixture'
                doc.write_bytes(raw)
                digest = hashlib.sha256(raw).hexdigest()
                put(doc.with_suffix('.md.meta.json'), {
                    'url': FAQ_URL, 'final_url': FAQ_URL, 'sha256': digest, 'status': 200,
                    'observed_at': self.applied,
                }, policy=False)
                value = {'contract': CONTRACT, 'kr_reference': 'saved_universe_equal_weight',
                         'documents': [{'file': doc.name, 'url': FAQ_URL, 'sha256': digest}]}
            physical = self.mother / 'runtime' / relative
            baseline_pins[str(self.old_mother / 'runtime' / relative)] = put(physical, value)
        baseline = self.mother / 'runtime' / BASELINE
        baseline_hash = put(baseline, {'schema': 1, 'roots': [str(self.old_mother / 'runtime/old-market')],
                                      'input_hashes': baseline_pins})
        identity = self.runtime / 'source/identity.json'
        identity_hash = put(identity, {'universe': []}, policy=False)
        refresh_pins = {}
        for relative in REFRESH_INPUTS:
            path = self.runtime / relative
            refresh_pins[str(self.old_root / 'runtime' / relative)] = put(path, {
                'schema': 1, 'records': [],
                'identity_snapshot': {'path': str(self.old_root / 'runtime/source/identity.json'), 'sha256': identity_hash},
            })
        self.reference = {'path': str(self.old_root / 'runtime' / REFRESH_INPUTS[-1]),
                          'sha256': list(refresh_pins.values())[-1]}
        self.settings = {'mother': str(self.mother), 'sector_manifest': str(baseline),
                         'sector_manifest_sha256': baseline_hash, 'classification_refresh': self.reference,
                         'records': {'previous_root': str(self.old_root), 'previous_mother_root': str(self.old_mother)}}
        put(self.root / 'config.json', self.settings, policy=False)
        for relative, (source, digest) in zip(REFRESH_AUTHORITIES, refresh_pins.items()):
            run = self.runtime / relative
            run_hash = put(run / 'run.json', {'state': 'partial', 'finished_at': self.applied}, policy=False)
            put(run / 'manifest.json', {'input_hashes': {source: digest},
                                        'output_hashes': {'run.json': run_hash}}, policy=False)
            self.store.publish_run(run, root=self.root)

    def test_pinned_inputs_are_missing_only_and_normal_readers_restore_originals(self):
        self.assertEqual([r['state'] for r in migrate(self.root)], ['missing'] * 7)
        self.assertEqual([r['state'] for r in migrate(self.root, apply=True)], ['published'] * 7)
        self.assertEqual([r['state'] for r in migrate(self.root, apply=True)], ['existing'] * 7)
        for path, raw in self.policies.items():
            self.assertEqual(path.read_bytes(), raw)
            path.unlink()
        self.assertEqual([r['state'] for r in migrate(self.root)], ['existing'] * 7)
        self.assertTrue(atomic_io.available(self.basis))
        self.assertEqual(seed_check(self.settings)[0]['schema'], 1)
        self.assertEqual(load_basis(self.basis.parent, atomic_io.checked, atomic_io.load, {},
                                    self.applied, age_issue)['contract'], CONTRACT)
        self.assertEqual(apply([], {'classification_refresh': self.reference}, atomic_io.checked,
                               {}, self.applied, age_issue, []), [])
        self.assertIsNone(self.store.lookup(self.runtime / 'source/identity.json'))

    def test_tampered_original_stops_migration_before_writing(self):
        (self.runtime / REFRESH_INPUTS[0]).write_bytes(b'{}')
        with self.assertRaisesRegex(ValueError, 'classification_policy_hash_mismatch'):
            migrate(self.root, apply=True)
        for path in self.policies:
            self.assertIsNone(self.store.lookup(path))


if __name__ == '__main__':
    unittest.main()
