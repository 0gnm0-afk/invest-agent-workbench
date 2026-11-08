"""Verified record references for new runs and recovery of migrated old paths."""
from contextlib import nullcontext
import hashlib
import json
import os
from pathlib import Path

from .paths import local_runtime_path
from workbench_paths import ROOT


MARKER = 'record_artifact_ref'


def _key(path):
    return os.path.normcase(str(Path(path).resolve()))


def _store(path):
    from .store import RecordStore, default_path
    local = local_runtime_path(path).resolve()
    for parent in local.parents:
        if parent.name == 'runtime':
            database = default_path(parent.parent)
            if database.is_file():
                return RecordStore(database)
            break
    config = ROOT / 'config.json'
    if config.is_file():
        mother = json.loads(config.read_bytes()).get('mother')
        if mother and Path(mother).is_absolute() and local.is_relative_to((Path(mother) / 'runtime').resolve()):
            database = default_path(ROOT)
            if database.is_file():
                return RecordStore(database)
    return None


def resolve(path, raw=None):
    """Existing corrupt files are never silently replaced by a saved version."""
    from .store import RecordStore
    if raw is not None:
        if len(raw) > 8192 or MARKER.encode() not in raw:
            return raw
        value = json.loads(raw.decode('utf-8-sig'))
        if not isinstance(value, dict) or set(value) != {MARKER}:
            return raw
        reference = value[MARKER]
        if not isinstance(reference, dict) or _key(local_runtime_path(reference.get('source_path', ''))) != _key(local_runtime_path(path)):
            raise ValueError('record_reference_path_mismatch')
        restored = RecordStore(local_runtime_path(reference['path'])).read(reference)
        if restored is None:
            raise ValueError('record_reference_missing')
        return restored
    store = _store(path)
    return store.read(path) if store else None


def available(path):
    if Path(path).is_file() or local_runtime_path(path).is_file():
        return True
    store = _store(path)
    return bool(store and store.lookup(path))


def publish_thin(run_dir, *, root, store=None):
    """Only for a newly generated run, after its immutable DB publication.

    A crash may leave a mix of original JSON and references; both read identically.
    Existing run migration deliberately never calls this function.
    """
    from .store import RecordStore, default_path
    from atomic_io import atomic_write, read_bytes
    store = store or RecordStore(default_path(root))
    references = store.artifacts(run_dir)
    run_dir = Path(run_dir).resolve()
    pending = []
    for reference in references:
        path = local_runtime_path(reference['source_path']).resolve()
        if path.name == 'manifest.json':
            continue
        if not path.is_relative_to(run_dir):
            raise ValueError('record_reference_outside_run')
        content = store.read(reference)
        if read_bytes(path) != content:
            raise ValueError('record_source_changed_before_publication')
        pending.append((path, reference))
    for path, reference in pending:
        atomic_write(path, json.dumps({MARKER: reference}, ensure_ascii=False, sort_keys=True))
    return len(pending)


def compact_run(run_dir, *, root, store=None, apply=False):
    """Verify an already published market run, then optionally replace duplicates.

    The records/price databases are read-only. Original manifests and outputs not
    owned by RecordStore stay untouched. Every original byte must be recoverable
    independently from the DB before any file is replaced; interrupted runs can
    be resumed because original files and verified references read identically.
    """
    from .store import RecordStore, default_path, allowed_role, TERMINAL_STATES, _role
    from atomic_io import atomic_write
    from invest_agent.trading.store import instance_lock

    root = Path(root).resolve()
    runtime = root / 'runtime'
    run_dir = local_runtime_path(run_dir, root=root).resolve()
    if not run_dir.is_relative_to(runtime) or run_dir == runtime:
        raise ValueError('record_compaction_outside_runtime')
    if run_dir.relative_to(runtime).parts[0] in ('dashboard', '90_작업보관'):
        raise ValueError('record_compaction_protected_path')
    store = store or RecordStore(default_path(root))
    with instance_lock(runtime) if apply else nullcontext():
        published = [run for run in store.runs()
                     if local_runtime_path(run['run_path'], root=root).resolve() == run_dir]
        if len(published) != 1:
            raise ValueError('record_compaction_run_not_published')
        published = published[0]
        manifest_path = run_dir / 'manifest.json'
        manifest_raw = manifest_path.read_bytes()
        if hashlib.sha256(manifest_raw).hexdigest() != published['manifest_sha256']:
            raise ValueError('record_compaction_manifest_changed')
        references = store.artifacts(published['run_path'])
        by_role = {reference['role']: reference for reference in references}
        if len(by_role) != len(references) or not {'manifest.json', 'run.json'} <= by_role.keys():
            raise ValueError('record_compaction_incomplete_publication')
        if store.read(by_role['manifest.json']) != manifest_raw:
            raise ValueError('record_compaction_manifest_changed')
        manifest = json.loads(manifest_raw)
        run = json.loads(store.read(by_role['run.json']))
        if run.get('state') not in TERMINAL_STATES or not run.get('finished_at'):
            raise ValueError('record_compaction_run_not_terminal')
        expected_roles = {name.replace('\\', '/') for name in manifest['output_hashes']
                          if allowed_role(name.replace('\\', '/'))} | {'manifest.json'}
        if set(by_role) != expected_roles:
            raise ValueError('record_compaction_incomplete_publication')
        pending, skipped = [], {'already_reference': 0, 'missing': 0, 'not_smaller': 0}
        for reference in references:
            role, path = _role(reference['role'], run_dir)
            if path != local_runtime_path(reference['source_path'], root=root).resolve():
                raise ValueError('record_compaction_path_mismatch')
            if role == 'manifest.json':
                continue
            expected = next(value for name, value in manifest['output_hashes'].items()
                            if name.replace('\\', '/') == role)
            if reference['sha256'] != expected:
                raise ValueError('record_compaction_reference_hash_mismatch')
            # This read reconstructs screen price recipes using pinned OHLCV
            # versions and validates both the recipe and the original byte hash.
            restored = store.read(reference)
            if not path.is_file():
                skipped['missing'] += 1
                continue
            physical = path.read_bytes()
            if resolve(path, physical) != restored:
                raise ValueError('record_source_changed_before_compaction')
            marker = json.dumps({MARKER: reference}, ensure_ascii=False, sort_keys=True)
            if physical != restored:
                skipped['already_reference'] += 1
                continue
            marker_size = len(marker.encode('utf-8'))
            if marker_size >= len(physical):
                skipped['not_smaller'] += 1
                continue
            pending.append((path, marker, len(physical), marker_size, hashlib.sha256(physical).hexdigest()))
        result = {'run_path': str(run_dir), 'manifest_sha256': published['manifest_sha256'],
                  'state': 'verified_plan', 'verified_artifacts': len(references), 'skipped': skipped,
                  'files': [{'path': str(p), 'original_bytes': size, 'reference_bytes': small, 'sha256': digest}
                            for p, _, size, small, digest in pending],
                  'reclaimed_bytes': sum(size - small for _, _, size, small, _ in pending),
                  'replaced_files': 0, 'database_written': False}
        if apply:
            for path, marker, _, _, expected in pending:
                # The shared pipeline lock excludes daily writers. Check again
                # immediately before each replacement for unrelated file edits.
                if manifest_path.read_bytes() != manifest_raw:
                    raise ValueError('record_compaction_manifest_changed')
                if not path.resolve().is_relative_to(run_dir):
                    raise ValueError('record_compaction_path_mismatch')
                with path.open('rb') as stream:
                    if hashlib.file_digest(stream, 'sha256').hexdigest() != expected:
                        raise ValueError('record_source_changed_before_compaction')
                atomic_write(path, marker)
                result['replaced_files'] += 1
            result['state'] = 'compacted'
        return result
