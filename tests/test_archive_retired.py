"""M01 archive contracts on disposable data; no native/operating I/O.

The synthetic lock models exclusive ownership and verifies bytes at each phase.
It is not evidence of native Windows sharing semantics: M00 forbids native I/O.
"""
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import archive_retired as archive

_HANDLE_OPEN = io.open


class SyntheticLockedFile:
    active = set()
    delete_locked = set()

    def __init__(self, path, row, *, delete_access=True):
        self.path, self.row, self.owned = path, row, False
        self.delete_access = delete_access

    def __enter__(self):
        if self.path in self.active:
            raise PermissionError('synthetic_lock_conflict')
        self.active.add(self.path)
        if self.delete_access:
            self.delete_locked.add(self.path)
        self.owned = True
        try:
            self.verify()
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def verify(self):
        archive._require(self.owned and self.path in self.active, 'synthetic_lock_missing')
        # This represents ReadFile through the owned handle, not a new CRT
        # fopen. The wrapper below rejects real code reopening DELETE handles.
        with _HANDLE_OPEN(self.path, 'rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        archive._require(self.path.stat().st_size == self.row['size']
                         and digest == self.row['sha256'], 'source_changed')

    def delete(self):
        archive._require(self.delete_access, 'read_only_lock_cannot_delete')
        self.verify()
        self.path.unlink()

    def __exit__(self, *_):
        if self.owned:
            self.active.remove(self.path)
            self.delete_locked.discard(self.path)
            self.owned = False


class ArchiveRetired(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='m01-archive-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.addCleanup(SyntheticLockedFile.active.clear)
        self.addCleanup(SyntheticLockedFile.delete_locked.clear)

    def fixture(self, name):
        folder = self.root / name
        source = folder / 'source'
        entry = source / 'completed-check'
        (entry / 'nested').mkdir(parents=True)
        data = {'one.txt': b'one\x00\xff', 'nested/two.json': b'{"result": "old fixture"}'}
        for relative, raw in data.items():
            (entry / relative).write_bytes(raw)
        manifest = {'module_id': 'M01', 'source_root': str(source), 'entries': [{
            'relative_path': 'completed-check', 'classification': 'retired_verification',
            'producer_evidence': 'synthetic completed verification fixture',
            'reference_check': {'scope': 'all synthetic reference sources',
                                'checked_at': '2026-11-17T00:00:00Z', 'unresolved': []},
            'files': [{'relative_path': relative, 'size': len(raw),
                       'sha256': hashlib.sha256(raw).hexdigest()} for relative, raw in data.items()]}]}
        return manifest, folder / 'archive', entry, data

    def snapshot(self, folder):
        return {p.relative_to(folder).as_posix(): p.read_bytes()
                for p in folder.rglob('*') if p.is_file()}

    def run_archive(self, manifest, destination, *, apply=False, backend=SyntheticLockedFile):
        def crt_open(file, mode='r', *args, **kwargs):
            if not isinstance(file, int):
                path = Path(file)
                if path in SyntheticLockedFile.delete_locked:
                    raise PermissionError('CRT read does not share DELETE access')
                if path in SyntheticLockedFile.active and any(flag in mode for flag in ('w', 'a', 'x', '+')):
                    raise PermissionError('archive read lock denies writing')
            return _HANDLE_OPEN(file, mode, *args, **kwargs)
        with patch.object(archive, '_locked_file', backend), \
             patch('builtins.open', crt_open), patch('io.open', crt_open):
            return archive.archive_retired(manifest, destination, apply=apply)

    def test_roundtrip_and_resume(self):
        manifest, destination, entry, data = self.fixture('roundtrip')
        before = self.snapshot(self.root)
        with patch.object(archive, '_locked_file', side_effect=AssertionError('plan attempted lock')):
            planned = archive.archive_retired(manifest, destination)
        self.assertEqual(planned['status'], 'planned', planned)
        self.assertEqual(self.snapshot(self.root), before)
        self.assertFalse(destination.exists())

        result = self.run_archive(manifest, destination, apply=True)
        self.assertEqual(result['status'], 'deleted', result)
        self.assertEqual(sorted(result['deleted']), ['completed-check/' + p for p in sorted(data)])
        self.assertTrue(entry.is_dir())  # No directory-wide removal.
        self.assertEqual(self.snapshot(entry), {})
        saved = Path(result['archive_path'])
        self.assertEqual(archive._digest(saved), result['archive_sha256'])
        with zipfile.ZipFile(saved) as zipped:
            restored = {name: zipped.read(name) for name in zipped.namelist()}
        self.assertEqual(restored, {'completed-check/' + name: raw for name, raw in data.items()})
        self.assertEqual(sum(len(raw) for raw in restored.values()), sum(row['size'] for row in result['files']))
        receipt = json.loads(Path(result['verification_path']).read_bytes())
        self.assertEqual(receipt['files'], result['files'])
        self.assertEqual(receipt['plan_sha256'], result['plan_sha256'])
        before = self.snapshot(destination)
        again = self.run_archive(manifest, destination, apply=True)
        self.assertEqual(again['status'], 'already_archived', again)
        self.assertEqual(again['deleted'], [])
        self.assertEqual(sorted(again['missing']), sorted(restored))
        self.assertEqual(self.snapshot(destination), before)

        # Interrupted deletion can resume only from its exact verified archive.
        manifest, destination, entry, data = self.fixture('partial')
        deleted = []
        class Interrupted(SyntheticLockedFile):
            def delete(handle):
                if deleted:
                    raise PermissionError('synthetic second deletion failed')
                super(Interrupted, handle).delete()
                deleted.append(handle.path)
        partial = self.run_archive(manifest, destination, apply=True, backend=Interrupted)
        self.assertEqual(partial['status'], 'partial', partial)
        self.assertEqual(len(partial['deleted']), 1)
        remaining = self.snapshot(entry)
        self.assertEqual(len(remaining), 1)
        completed = self.run_archive(manifest, destination, apply=True)
        self.assertEqual(completed['status'], 'deleted', completed)
        self.assertEqual(len(completed['deleted']), 1)
        self.assertEqual(len(completed['missing']), 1)
        self.assertEqual(self.snapshot(entry), {})
        self.assertEqual(SyntheticLockedFile.active, set())

    def test_reject_unsafe_or_changed_input(self):
        for index, value in enumerate(('false', '0', 1, 0, {}, {'apply': True}, [], None)):
            with self.subTest(invalid_apply=repr(value)):
                manifest, destination, entry, _ = self.fixture('invalid-apply-' + str(index))
                before = self.snapshot(entry)
                with patch.object(archive, '_plan', side_effect=AssertionError('invalid apply reached planning')):
                    result = self.run_archive(manifest, destination, apply=value)
                self.assertEqual(result['status'], 'refused', result)
                self.assertEqual(result['apply'], None)
                self.assertEqual(result['errors'], ['invalid_apply'])
                self.assertEqual(result['deleted'], [])
                self.assertEqual(self.snapshot(entry), before)
                self.assertFalse(destination.exists())

        cases = {
            'protected': lambda m: m['entries'][0].update(classification='protected'),
            'unknown': lambda m: m['entries'][0].update(classification='unknown'),
            'unresolved': lambda m: m['entries'][0]['reference_check'].update(unresolved=['unreviewed']),
            'unchecked': lambda m: m['entries'][0]['reference_check'].pop('checked_at'),
            'fixed-reference': lambda m: m['entries'][0]['reference_check'].update(fixed_references=['pinned']),
            'no-producer': lambda m: m['entries'][0].update(producer_evidence=''),
            'escape': lambda m: m['entries'][0].update(relative_path='../outside'),
            'root': lambda m: m['entries'][0].update(relative_path='.'),
            'absolute': lambda m: m['entries'][0].update(relative_path='/outside'),
            'stream': lambda m: m['entries'][0]['files'][0].update(relative_path='one.txt:hidden'),
            'duplicate': lambda m: m['entries'][0]['files'].append(deepcopy(m['entries'][0]['files'][0])),
            'case-collision': lambda m: m['entries'][0]['files'].append({**m['entries'][0]['files'][0],
                                                                       'relative_path': 'ONE.TXT'}),
            'overlap': lambda m: m['entries'].append(deepcopy(m['entries'][0])),
            'database': lambda m: m['entries'][0]['files'][0].update(relative_path='records.sqlite3'),
            'database-wal': lambda m: m['entries'][0]['files'][0].update(relative_path='records.sqlite3-wal'),
            'auth': lambda m: m['entries'][0]['files'][0].update(relative_path='auth/session.txt'),
            'token': lambda m: m['entries'][0]['files'][0].update(relative_path='token.dpapi'),
            'raw': lambda m: m['entries'][0]['files'][0].update(relative_path='raw/source.txt'),
            'snapshot': lambda m: m['entries'][0]['files'][0].update(relative_path='snapshots/saved.json'),
            'hash': lambda m: m['entries'][0]['files'][0].update(sha256='0' * 64),
            'size': lambda m: m['entries'][0]['files'][0].update(size=0),
        }
        for name, mutate in cases.items():
            with self.subTest(case=name):
                manifest, destination, entry, _ = self.fixture(name)
                before = self.snapshot(entry)
                mutate(manifest)
                result = self.run_archive(manifest, destination, apply=True)
                self.assertEqual(result['status'], 'refused', result)
                self.assertTrue(result['errors'])
                self.assertEqual(result['deleted'], [])
                self.assertEqual(self.snapshot(entry), before)

        manifest, destination, entry, _ = self.fixture('archive-below-source')
        result = self.run_archive(manifest, entry / 'archive', apply=True)
        self.assertEqual(result['errors'], ['archive_source_overlap'])

        manifest, destination, entry, _ = self.fixture('unlisted-file')
        (entry / 'unexpected.txt').write_bytes(b'unlisted')
        result = self.run_archive(manifest, destination, apply=True)
        self.assertEqual(result['errors'], ['source_listing_changed'])

        manifest, destination, entry, _ = self.fixture('recipe')
        pointer = entry / 'nested/two.json'
        pointer.write_text('{"record_artifact_ref":{"sha256":"recipe-only"}}', encoding='utf-8')
        row = manifest['entries'][0]['files'][1]
        row.update(size=pointer.stat().st_size, sha256=archive._digest(pointer))
        result = self.run_archive(manifest, destination, apply=True)
        self.assertEqual(result['errors'], ['recipe_without_original_bytes'])

        # Real symlink/junction creation is prohibited by C8. Inject their lstat
        # metadata and prove refusal before any native lock or deletion instead.
        for attributes, mode, label in ((0x400, stat.S_IFDIR, 'junction'),
                                        (0, stat.S_IFLNK, 'symlink')):
            manifest, destination, entry, _ = self.fixture(label)
            lstat = Path.lstat
            def metadata(path, *, follow=entry, original=lstat):
                if path == follow:
                    return SimpleNamespace(st_file_attributes=attributes, st_mode=mode, st_nlink=1)
                return original(path)
            with patch.object(Path, 'lstat', metadata):
                result = self.run_archive(manifest, destination, apply=True)
            self.assertEqual(result['status'], 'refused', result)
            self.assertEqual(result['errors'], ['link_forbidden'])
            self.assertEqual(len(self.snapshot(entry)), 2)

        manifest, destination, entry, _ = self.fixture('lock-failure')
        before = self.snapshot(entry)
        with patch.object(archive, '_locked_file', side_effect=PermissionError('lock denied')):
            result = archive.archive_retired(manifest, destination, apply=True)
        self.assertEqual(result['status'], 'refused', result)
        self.assertEqual(self.snapshot(entry), before)
        # Archival precedes source DELETE handles; lock failure preserves both
        # originals and the independently verified archive for a later resume.
        self.assertTrue(Path(result['archive_path']).exists())

        # Failure on a later handle releases earlier locks without removing any.
        class SecondLockFails(SyntheticLockedFile):
            def __enter__(handle):
                if handle.active:
                    raise PermissionError('second lock denied')
                return super(SecondLockFails, handle).__enter__()
        result = self.run_archive(manifest, destination, apply=True, backend=SecondLockFails)
        self.assertEqual(result['status'], 'refused', result)
        self.assertEqual(self.snapshot(entry), before)
        self.assertEqual(SyntheticLockedFile.active, set())

        manifest, destination, entry, _ = self.fixture('changed-during-archive')
        original = archive._verify_zip
        def change(path, files):
            digest = original(path, files)
            (entry / 'one.txt').write_bytes(b'changed')
            return digest
        with patch.object(archive, '_verify_zip', change):
            result = self.run_archive(manifest, destination, apply=True)
        self.assertEqual(result['status'], 'refused', result)
        self.assertEqual(result['deleted'], [])
        self.assertEqual(len(self.snapshot(entry)), 2)
        self.assertEqual((entry / 'one.txt').read_bytes(), b'changed')

        # Model a root becoming a junction after handles have been acquired.
        # Structural checks must refuse before the first source deletion.
        manifest, destination, entry, _ = self.fixture('root-changed-after-lock')
        changed = []
        lstat = Path.lstat
        class RootChanges(SyntheticLockedFile):
            def __enter__(handle):
                result = super(RootChanges, handle).__enter__()
                if handle.delete_access:
                    changed.append(True)
                return result
        def root_metadata(path):
            if changed and path == entry.parent:
                return SimpleNamespace(st_file_attributes=0x400, st_mode=stat.S_IFDIR, st_nlink=1)
            return lstat(path)
        with patch.object(Path, 'lstat', root_metadata):
            result = self.run_archive(manifest, destination, apply=True, backend=RootChanges)
        self.assertEqual(result['status'], 'refused', result)
        self.assertEqual(result['deleted'], [])
        self.assertEqual(result['errors'], ['link_forbidden'])
        self.assertEqual(len(self.snapshot(entry)), 2)

        manifest, destination, entry, _ = self.fixture('listing-changed-after-lock')
        class ListingChanges(SyntheticLockedFile):
            def __enter__(handle):
                result = super(ListingChanges, handle).__enter__()
                if handle.delete_access:
                    (entry / 'unlisted.txt').write_bytes(b'new unlisted file')
                return result
        result = self.run_archive(manifest, destination, apply=True, backend=ListingChanges)
        self.assertEqual(result['status'], 'refused', result)
        self.assertEqual(result['deleted'], [])
        self.assertEqual(result['errors'], ['source_listing_changed'])
        self.assertEqual(len(self.snapshot(entry)), 3)

        manifest, destination, entry, _ = self.fixture('corrupt-archive')
        before = self.snapshot(entry)
        def corrupt(path, files):
            path.write_bytes(b'broken zip')
            return original(path, files)
        with patch.object(archive, '_verify_zip', corrupt):
            result = self.run_archive(manifest, destination, apply=True)
        self.assertEqual(result['status'], 'refused', result)
        self.assertEqual(self.snapshot(entry), before)

        # A verified archive is necessary but not sufficient: changed receipts,
        # changed ZIP bytes or a changed remaining source all prevent resume.
        for kind in ('receipt', 'zip', 'source'):
            manifest, destination, entry, _ = self.fixture('resume-' + kind)
            before = self.snapshot(entry)
            class RefuseDeletion(SyntheticLockedFile):
                def delete(handle):
                    raise PermissionError('simulated stop before first deletion')
            stopped = self.run_archive(manifest, destination, apply=True, backend=RefuseDeletion)
            self.assertEqual(stopped['status'], 'refused', stopped)
            if kind == 'receipt':
                Path(stopped['verification_path']).write_text('{}', encoding='utf-8')
            elif kind == 'zip':
                Path(stopped['archive_path']).write_bytes(b'corrupt')
            else:
                (entry / 'one.txt').write_bytes(b'changed')
                before = self.snapshot(entry)
            result = self.run_archive(manifest, destination, apply=True)
            self.assertEqual(result['status'], 'refused', result)
            self.assertEqual(result['deleted'], [])
            self.assertEqual(self.snapshot(entry), before)

        # Never remove another invocation's exclusive plan lease.
        manifest, destination, entry, _ = self.fixture('existing-lease')
        plan = self.run_archive(manifest, destination)
        destination.mkdir()
        lease = Path(plan['archive_path']).with_suffix('.lock')
        lease.write_bytes(b'owned by another invocation')
        result = self.run_archive(manifest, destination, apply=True)
        self.assertEqual(result['status'], 'refused', result)
        self.assertEqual(lease.read_bytes(), b'owned by another invocation')
        self.assertEqual(len(self.snapshot(entry)), 2)


if __name__ == '__main__':
    unittest.main()
