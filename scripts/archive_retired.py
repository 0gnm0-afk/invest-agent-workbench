"""Explicit retired-file archival; never discovers or reads operating defaults.

The caller supplies a reviewed manifest and a separate archive directory. The
default is a read-only plan. Eligible classifications are retired_cache,
retired_work and retired_verification; reference_check requires scope,
checked_at and an empty unresolved list. No DB/reference hydration is used.

Apply uses Windows same-handle verification/deletion and fails closed elsewhere.
C8 tests inject a synthetic lock backend: native locks require separate operating
verification because M00 deliberately prohibits ctypes and native file handles.
"""
from contextlib import ExitStack, contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import zipfile


CLASSIFICATIONS = frozenset({'retired_cache', 'retired_work', 'retired_verification'})
_PROTECTED = frozenset({'auth', 'secrets', 'credentials', 'raw', 'originals',
                        'snapshots', 'snapshot', 'judgments', 'ma-dd-describe-20261017'})
_FORBIDDEN_SUFFIXES = ('.db', '.db-wal', '.db-shm', '.sqlite', '.sqlite3',
                       '.sqlite-wal', '.sqlite-shm', '.sqlite3-wal', '.sqlite3-shm',
                       '.db-journal', '.sqlite-journal', '.sqlite3-journal',
                       '.dpapi', '.pem', '.key', '.pdf')


class ArchiveError(ValueError):
    pass


def _require(condition, reason):
    if not condition:
        raise ArchiveError(reason)


def _json_bytes(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def _digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _relative(value):
    _require(isinstance(value, str) and value and '\\' not in value
             and ':' not in value, 'invalid_relative_path')
    parts = value.split('/')
    _require(not any(p in ('', '.', '..') or p.endswith((' ', '.')) for p in parts)
             and not PurePosixPath(value).is_absolute(), 'invalid_relative_path')
    _require(not any(re.fullmatch(r'(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?', p, re.I)
                     for p in parts), 'invalid_relative_path')
    return PurePosixPath(value)


def _plain(path, *, directory=False, missing=False):
    if missing and not path.exists() and not path.is_symlink():
        return None
    info = path.lstat()
    _require(not stat.S_ISLNK(info.st_mode)
             and not getattr(info, 'st_file_attributes', 0) & 0x400, 'link_forbidden')
    _require(stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode),
             'directory_required' if directory else 'regular_file_required')
    if not directory:
        _require(info.st_nlink == 1, 'hardlink_forbidden')
    return info


def _inside(root, relative, *, missing=False):
    path = root.joinpath(*relative.parts)
    _require(path != root and path.resolve().is_relative_to(root), 'path_escape')
    for parent in list(path.parents)[:len(relative.parts) - 1]:
        _plain(parent, directory=True)
    _plain(path, missing=missing)
    return path


def _protected(relative):
    lower = relative.as_posix().casefold()
    return (bool(set(p.casefold() for p in relative.parts) & _PROTECTED)
            or lower.endswith(_FORBIDDEN_SUFFIXES)
            or any(word in relative.name.casefold() for word in ('token', 'credential', 'secret')))


def _recipe(path):
    # Recipe/pointer bytes are not an independent archival copy of their input.
    if path.suffix.casefold() != '.json':
        return False
    with path.open('rb') as stream:
        try:
            value = json.load(stream)
        except (ValueError, UnicodeError):
            return False
    def has_pointer(item):
        if isinstance(item, dict):
            return bool(set(item) & {'record_artifact_ref', 'bars_ref', 'price_recipe',
                                    'ohlcv_store', 'account_store_ref', 'account_view_ref'}) or any(
                                        has_pointer(v) for v in item.values())
        return isinstance(item, list) and any(has_pointer(v) for v in item)
    return has_pointer(value)


def _inventory(root, entries, *, allow_missing=False, verify_contents=True):
    """Validate full entry listings, including unlisted files and links."""
    _require(root == root.resolve(), 'source_root_link')
    _plain(root, directory=True)
    present = []
    for entry in entries:
        relative = _relative(entry['relative_path'])
        folder = root.joinpath(*relative.parts)
        for parent in list(folder.parents)[:len(relative.parts) - 1]:
            _plain(parent, directory=True)
        _plain(folder, directory=True)
        expected = {row['relative_path']: row for row in entry['files']}
        actual = set()
        for parent, dirs, files in os.walk(folder, followlinks=False):
            for directory in dirs:
                _plain(Path(parent) / directory, directory=True)
            for name in files:
                path = Path(parent) / name
                _plain(path)
                actual.add(path.relative_to(folder).as_posix())
        _require(actual <= set(expected) and (allow_missing or actual == set(expected)),
                 'source_listing_changed')
        for name in sorted(actual):
            row = expected[name]
            full_relative = relative / _relative(name)
            path = _inside(root, full_relative)
            _require(not _protected(full_relative), 'protected_file')
            _require(path.stat().st_size == row['size'], 'source_changed')
            if verify_contents:
                _require(_digest(path) == row['sha256'], 'source_changed')
                _require(not _recipe(path), 'recipe_without_original_bytes')
            present.append((full_relative.as_posix(), path, row))
    return present


def _plan(manifest, archive_dir):
    # JSON roundtrip pins the caller's mutable object before any file access.
    manifest = json.loads(_json_bytes(manifest))
    _require(manifest.get('module_id') in ('M01', 'M13'), 'invalid_module')
    root = Path(manifest.get('source_root', ''))
    _require(root.is_absolute() and root != Path(root.anchor), 'invalid_source_root')
    _require(root == root.resolve(), 'source_root_link')
    _require(not set(p.casefold() for p in root.parts) & _PROTECTED, 'protected_source_root')
    _plain(root, directory=True)
    archive = Path(archive_dir)
    _require(archive.is_absolute() and archive == archive.resolve(), 'invalid_archive_directory')
    _require(not archive.is_relative_to(root) and not root.is_relative_to(archive),
             'archive_source_overlap')
    if archive.exists():
        _plain(archive, directory=True)
    entries = manifest.get('entries')
    _require(isinstance(entries, list) and entries, 'empty_manifest')
    names, folders, files = set(), set(), []
    for entry in entries:
        relative = _relative(entry['relative_path'])
        _require(not _protected(relative), 'protected_entry')
        folder = relative.as_posix().casefold()
        _require(not any(folder == old or folder.startswith(old + '/') or old.startswith(folder + '/')
                         for old in folders), 'duplicate_or_overlapping_entry')
        folders.add(folder)
        _require(entry.get('classification') in CLASSIFICATIONS, 'ineligible_classification')
        _require(isinstance(entry.get('producer_evidence'), str) and entry['producer_evidence'].strip(),
                 'producer_evidence_missing')
        reference = entry.get('reference_check', {})
        _require(isinstance(reference, dict) and reference.get('scope') and reference.get('checked_at')
                 and reference.get('unresolved') == []
                 and not reference.get('fixed_references') and not reference.get('protected'),
                 'references_unverified')
        rows = entry.get('files')
        _require(isinstance(rows, list) and rows, 'empty_entry')
        for row in rows:
            name = (relative / _relative(row['relative_path'])).as_posix()
            _require(name.casefold() not in names, 'duplicate_or_case_collision')
            names.add(name.casefold())
            _require(not _protected(PurePosixPath(name)), 'protected_file')
            _require(type(row.get('size')) is int and row['size'] >= 0
                     and isinstance(row.get('sha256'), str)
                     and re.fullmatch('[0-9a-f]{64}', row['sha256']), 'invalid_file_evidence')
            files.append({'relative_path': name, 'size': row['size'], 'sha256': row['sha256']})
    files.sort(key=lambda row: row['relative_path'])
    identifier = hashlib.sha256(_json_bytes(manifest)).hexdigest()
    base = archive / (manifest['module_id'] + '-' + identifier)
    return manifest, root, files, identifier, base


def _verify_zip(path, files):
    expected = {row['relative_path']: row for row in files}
    with zipfile.ZipFile(path, 'r') as saved:
        items = saved.infolist()
        _require(len(items) == len(expected) and {i.filename for i in items} == set(expected),
                 'archive_listing_mismatch')
        for item in items:
            row = expected[item.filename]
            _require(not item.is_dir() and item.file_size == row['size'], 'archive_size_mismatch')
            with saved.open(item, 'r') as stream:
                actual = hashlib.file_digest(stream, 'sha256').hexdigest()
            _require(actual == row['sha256'], 'archive_hash_mismatch')
    return _digest(path)


def _publish_json(path, value):
    # Exclusive creation preserves previous evidence. Incomplete publication is
    # a refusal on resume, requiring an operator to inspect it; never overwrite.
    with path.open('xb') as stream:
        stream.write(_json_bytes(value))
        stream.flush()
        os.fsync(stream.fileno())


class _WindowsLockedFile:
    """Deny write/rename/delete sharing, verify, and retire the same handle."""
    def __init__(self, path, row, *, delete_access=True):
        _require(os.name == 'nt', 'safe_lock_backend_unavailable')
        # Lazy import keeps read-only planning independent of native APIs.
        from records import windows_retire as native
        self.native, self.path, self.row, self.handle = native, path, row, None
        self.delete_access = delete_access

    def __enter__(self):
        native = self.native
        access = native._READ_DELETE if self.delete_access else 0x80000000  # GENERIC_READ
        self.handle = native._api.CreateFileW(str(self.path), access,
            native._SHARE_READ, None, native._OPEN_EXISTING, native._OPEN_REPARSE_POINT, None)
        if self.handle in (None, native._INVALID_HANDLE):
            self.handle = None
            raise native._win_error()
        try:
            self.verify()
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def verify(self):
        native, handle = self.native, self.handle
        _require(native._information(handle)[1] == self.row['size'], 'source_changed')
        _require(native._normal_path(native._final_path(handle)) == native._normal_path(str(self.path)),
                 'locked_path_changed')
        info = native._FileInformation()
        if not native._api.GetFileInformationByHandle(handle, native.ctypes.byref(info)):
            raise native._win_error()
        _require(info.links == 1, 'hardlink_forbidden')
        if not native._api.SetFilePointerEx(handle, 0, None, 0):
            raise native._win_error()
        digest, size = hashlib.sha256(), 0
        buffer = native.ctypes.create_string_buffer(1024 * 1024)
        while True:
            count = native.wintypes.DWORD()
            if not native._api.ReadFile(handle, buffer, len(buffer), native.ctypes.byref(count), None):
                raise native._win_error()
            if not count.value:
                break
            digest.update(buffer.raw[:count.value])
            size += count.value
        _require(size == self.row['size'] and digest.hexdigest() == self.row['sha256'], 'source_changed')
        _require(native._normal_path(native._final_path(handle)) == native._normal_path(str(self.path)),
                 'locked_path_changed')

    def delete(self):
        _require(self.delete_access, 'read_only_lock_cannot_delete')
        self.verify()
        self.native._mark_delete(self.handle)
        self.__exit__(None, None, None)

    def __exit__(self, *_):
        if self.handle is not None:
            handle, self.handle = self.handle, None
            if not self.native._api.CloseHandle(handle):
                raise self.native._win_error()


_locked_file = _WindowsLockedFile


@contextmanager
def _plan_lease(path):
    # A failed exclusive create never gives ownership of somebody else's lock.
    handle = path.open('xb')
    try:
        yield
    finally:
        handle.close()
        path.unlink()


def archive_retired(manifest, archive_dir, apply=False):
    """Return a read-only plan or explicit archive/deletion/refusal evidence.

    Never delete directories or unlisted files. Resume requires the exact same
    manifest, independently verified ZIP and immutable verification receipt.
    Caller is responsible for approving operational reference checks beforehand.
    """
    result = {'status': 'refused', 'apply': apply if type(apply) is bool else None,
              'deleted': [], 'missing': [],
              'files': [], 'errors': []}
    try:
        _require(type(apply) is bool, 'invalid_apply')
        manifest, root, files, identifier, base = _plan(manifest, archive_dir)
        archive = base.with_suffix('.zip')
        manifest_path = base.with_suffix('.manifest.json')
        verification = base.with_suffix('.verification.json')
        result.update(plan_sha256=identifier, archive_path=str(archive),
                      manifest_path=str(manifest_path), verification_path=str(verification), files=files)
        existing = [p.exists() for p in (archive, manifest_path, verification)]
        _require(not any(existing) or all(existing), 'incomplete_archive_publication')
        resumed = all(existing)
        if resumed:
            for path in (archive, manifest_path, verification):
                _plain(path)
            _require(manifest_path.read_bytes() == _json_bytes(manifest), 'resume_manifest_mismatch')
            receipt = json.loads(verification.read_bytes())
            archive_sha = _verify_zip(archive, files)
            _require(receipt == {'plan_sha256': identifier, 'archive_sha256': archive_sha,
                                 'files': files}, 'resume_verification_mismatch')
            result['archive_sha256'] = archive_sha
        present = _inventory(root, manifest['entries'], allow_missing=resumed)
        present_names = {name for name, _, _ in present}
        result['missing'] = [row['relative_path'] for row in files if row['relative_path'] not in present_names]
        if not apply:
            result['status'] = 'planned'
            return result
        base.parent.mkdir(parents=True, exist_ok=True)
        _plain(base.parent, directory=True)
        lock_path = base.with_suffix('.lock')
        # Serialize this plan's archive publication/resume, never replace a lock.
        with _plan_lease(lock_path):
            # Python/CRT reads do not share DELETE access on Windows. Finish
            # source reads before acquiring READ|DELETE source handles. Any
            # concurrent change is caught by ZIP and later same-handle hashes.
            if not resumed:
                temporary = base.with_suffix('.zip.pending')
                with zipfile.ZipFile(temporary, 'x', compression=zipfile.ZIP_DEFLATED) as saved:
                    for name, path, _ in present:
                        saved.write(path, name)
                archive_sha = _verify_zip(temporary, files)
                # Windows rename refuses replacement. There is no
                # os.replace fallback if a concurrent file is present.
                _require(not archive.exists(), 'archive_already_exists')
                temporary.rename(archive)
                _publish_json(manifest_path, manifest)
                _publish_json(verification, {'plan_sha256': identifier,
                    'archive_sha256': archive_sha, 'files': files})
                result['archive_sha256'] = archive_sha
            with ExitStack() as stack:
                archive_row = {'size': archive.stat().st_size, 'sha256': result['archive_sha256']}
                # READ only still denies writes/rename/deletion, while allowing
                # ZipFile's independent normal read under FILE_SHARE_READ.
                stack.enter_context(_locked_file(archive, archive_row, delete_access=False))
                _require(_verify_zip(archive, files) == result['archive_sha256'], 'archive_changed')
                locked = [(name, stack.enter_context(_locked_file(path, row)))
                          for name, path, row in present]
                # All handles are acquired and all names rechecked before
                # the first deletion. Do not reopen locked source bytes here:
                # the same owned handles perform every remaining hash check.
                current = _inventory(root, manifest['entries'], allow_missing=resumed,
                                     verify_contents=False)
                _require({name for name, _, _ in current} == present_names, 'source_listing_changed')
                for _, handle in locked:
                    handle.verify()
                for name, handle in locked:
                    handle.delete()
                    result['deleted'].append(name)
            result['status'] = 'deleted' if result['deleted'] else 'already_archived'
    except (OSError, ValueError, TypeError, KeyError, zipfile.BadZipFile) as exc:
        result['status'] = 'partial' if result['deleted'] else 'refused'
        # Do not leak arbitrary input paths, source content or exception text.
        result['errors'].append(str(exc) if isinstance(exc, ArchiveError) else type(exc).__name__)
    return result
