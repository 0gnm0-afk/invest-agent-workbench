"""Verified record references for new runs and recovery of migrated old paths."""
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
