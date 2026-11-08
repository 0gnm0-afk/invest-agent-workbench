"""Keep daily collection checkpoints in the existing records database.

The original path and exact JSON bytes remain readable through records.files.
Standalone collectors keep their file layout. Only metadata checkpoints in a
dated daily run are eligible; inline price history is deliberately excluded.
"""
from contextlib import closing
from datetime import date, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import zlib

from invest_agent.trading.contracts import digest
from invest_agent.trading.store import instance_lock
from records.store import RecordStore, default_path, _has_prices
from records.paths import runtime_lookup_paths


KIND = 'collection_checkpoint'
FAMILIES = ('series', 'cap-prices')
MAX_RAW = 1024 * 1024


def _regular(path, boundary):
    path, boundary = Path(path).absolute(), Path(boundary).resolve()
    if not path.is_relative_to(boundary) or not path.resolve().is_relative_to(boundary):
        raise ValueError('checkpoint_outside_runtime')
    for part in (path, *path.parents):
        if part.is_symlink() or part.is_junction():
            raise ValueError('checkpoint_link_forbidden')
        if part == boundary:
            break
    return path


def _scope(cache, root=None):
    cache = Path(cache).absolute()
    if root is None:
        runtime = next((p for p in cache.parents if p.name == 'runtime'), None)
        if runtime is None:
            return None
        root = runtime.parent
    root = Path(root).resolve()
    runtime = root / 'runtime'
    if not cache.is_relative_to(runtime):
        return None
    parts = cache.relative_to(runtime).parts
    if len(parts) != 4 or parts[2:] != ('collection', 'cache'):
        return None
    try:
        if date.fromisoformat(parts[0]).isoformat() != parts[0]:
            return None
    except ValueError:
        return None
    _regular(cache, runtime)
    return root, cache


def _source(path, cache, root):
    path = Path(path).absolute()
    if (path.parent.parent != cache or path.parent.name not in FAMILIES
            or not re.fullmatch(r'[0-9a-f]{64}\.json', path.name)):
        raise ValueError('checkpoint_source_invalid')
    return _regular(path, root / 'runtime')


def _packet(raw, path):
    if len(raw) > MAX_RAW:
        raise ValueError('checkpoint_too_large')
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError('checkpoint_packet_invalid')
    payload = value.get('payload')
    if isinstance(payload, dict) and ('bars' in payload or _has_prices(payload)):
        raise ValueError('checkpoint_inline_prices')
    fields = {'key', 'observed_at', 'payload', 'hash'}
    if path.parent.name == 'series':
        fields.add('ohlcv_path')
    if (set(value) != fields or value['key'] != path.stem or not isinstance(payload, dict)
            or digest(payload) != value['hash']):
        raise ValueError('checkpoint_packet_invalid')
    if datetime.fromisoformat(value['observed_at']).tzinfo is None:
        raise ValueError('checkpoint_timestamp_invalid')
    if path.parent.name == 'series':
        ref = payload.get('bars_ref')
        if (not isinstance(ref, dict) or not ref
                or not isinstance(value['ohlcv_path'], str) or not value['ohlcv_path']):
            raise ValueError('checkpoint_price_reference_required')
    elif payload.get('market_cap_quality') != 'completed_close_estimate':
        raise ValueError('checkpoint_cap_quality_invalid')
    return value


def _reference(store, db, version):
    row = db.execute('''SELECT source_path,sha256,raw_size,blob_sha256,encoding,kind,id
                        FROM record_versions WHERE id=?''', (version,)).fetchone()
    return store._reference(row)


def _verify(store, reference, path, raw):
    # Check both the pinned historical version and the normal latest-path
    # resolver before removing any file. This also tests configured DB routing.
    from records.files import resolve
    if (reference['encoding'] != 'raw' or store.read(reference) != raw
            or resolve(path) != raw):
        raise ValueError('checkpoint_database_roundtrip_failed')


def save_checkpoint(path, raw, *, root=None):
    """Save under the caller's collection lock; return False for file fixtures.

    A refresh preserves both the previous physical bytes and the new version.
    Removing the verified physical copy prevents it shadowing a newer DB entry.
    Legacy checkpoints containing inline bars retain the original file behavior.
    """
    path = Path(path).absolute()
    scope = _scope(path.parent.parent, root)
    if scope is None:
        return False
    root, cache = scope
    database = default_path(root)
    if not database.is_file():
        return False
    path = _source(path, cache, root)
    _packet(raw, path)
    previous = path.read_bytes() if path.is_file() else None
    if previous is not None:
        try:
            old = json.loads(previous)
        except (ValueError, UnicodeError):
            old = None
        if _has_prices(old) or (isinstance(old, dict) and isinstance(old.get('payload'), dict)
                               and 'bars' in old['payload']):
            return False
    store = RecordStore(database)
    with closing(store.connect(write=True)) as db, db:
        db.execute('BEGIN IMMEDIATE')
        if previous is not None:
            store._insert_version(db, path, previous, kind=KIND, root=root,
                                  metadata={'family': path.parent.name, 'source': 'prior_physical'})
        version = store._insert_version(db, path, raw, kind=KIND, root=root,
                                        metadata={'family': path.parent.name, 'source': 'collector'})
        reference = _reference(store, db, version)
    _verify(store, reference, path, raw)
    if previous is not None:
        if path.read_bytes() != previous:
            raise ValueError('checkpoint_source_changed')
        path.unlink()
    return True


def _batch_source(folder, name):
    """The folder ancestry is checked once under the collector lock/batch."""
    if not re.fullmatch(r'[0-9a-f]{64}\.json', name):
        raise ValueError('checkpoint_source_invalid')
    path = folder / name
    info = path.lstat()
    if (getattr(info, 'st_file_attributes', 0) & 0x400
            or not stat.S_ISREG(info.st_mode)):
        raise ValueError('checkpoint_link_forbidden')
    return path


def _prefixes(cache, root):
    # Resolve relocation aliases/config once, never once per checkpoint.
    aliases = runtime_lookup_paths(cache, root=root)
    return {family: tuple(os.path.normcase(str((alias / family).resolve())) + os.sep
                         for alias in aliases)
            for family in FAMILIES}


_VERSION_COLUMNS = 'source_key,source_path,sha256,raw_size,blob_sha256,encoding,kind,id'


def _latest_versions(db, prefixes):
    ranges = list(dict.fromkeys(prefix for values in prefixes.values() for prefix in values))
    where = ' OR '.join('(source_key>=? AND source_key<?)' for _ in ranges)
    bounds = tuple(value for prefix in ranges for value in (prefix, prefix + '\uffff'))
    latest = {}
    for row in db.execute('SELECT ' + _VERSION_COLUMNS + ' FROM record_versions WHERE ' + where, bounds):
        if row[0] not in latest or row[7] > latest[row[0]][7]:
            latest[row[0]] = row
    return latest


def _read_version(db, row):
    key, source, expected, size, blob_sha, encoding, _, _ = row
    if (encoding != 'raw' or blob_sha != expected or not isinstance(size, int)
            or not 0 <= size <= MAX_RAW or os.path.normcase(os.path.abspath(source)) != key):
        raise ValueError('checkpoint_database_roundtrip_failed')
    blob = db.execute('SELECT raw_size,payload FROM record_blobs WHERE sha256=?', (blob_sha,)).fetchone()
    if blob is None or blob[0] != size:
        raise ValueError('checkpoint_database_roundtrip_failed')
    try:
        decoder = zlib.decompressobj()
        raw = decoder.decompress(blob[1], size + 1)
    except zlib.error as exc:
        raise ValueError('checkpoint_database_roundtrip_failed') from exc
    if (len(raw) != size or not decoder.eof or decoder.unused_data
            or hashlib.sha256(raw).hexdigest() != expected):
        raise ValueError('checkpoint_database_roundtrip_failed')
    return raw


def _verify_batch(db, batch, references, prefixes):
    for (path, raw), reference in zip(batch, references):
        key = prefixes[path.parent.name][0] + path.name
        row = db.execute('SELECT ' + _VERSION_COLUMNS +
                         ' FROM record_versions WHERE source_key=? ORDER BY id DESC LIMIT 1',
                         (key,)).fetchone()
        if (row is None or row[7] != reference['version_id']
                or _read_version(db, row) != raw):
            raise ValueError('checkpoint_database_roundtrip_failed')


def compact(cache, *, root=None, apply=False, progress=None):
    """Validate, optionally preserve and retire only thin daily checkpoints.

    The default does not write the DB or remove files. A resumed applied pass
    sees only remaining physical files; completed entries are already readable
    by their original paths. The collector's own lock excludes active writes.
    No directory, universe file, inline history, or unrelated output is removed.
    """
    scope = _scope(cache, root)
    if scope is None:
        raise ValueError('checkpoint_cache_scope_invalid')
    root, cache = scope
    database = default_path(root)
    if not database.is_file():
        raise ValueError('checkpoint_existing_database_required')
    store = RecordStore(database)
    result = {'cache': str(cache), 'files': 0, 'bytes': 0, 'removed': 0,
              'skipped_inline': 0, 'applied': apply}
    manifest = hashlib.sha256()
    with instance_lock(cache), closing(store.connect(write=apply)) as db:
        prefixes = _prefixes(cache, root)
        latest = _latest_versions(db, prefixes)
        pending = []
        for family in FAMILIES:
            folder = _regular(cache / family, root / 'runtime')
            for path in sorted(folder.glob('*.json')):
                path = _batch_source(folder, path.name)
                raw = path.read_bytes()
                try:
                    _packet(raw, path)
                except ValueError as exc:
                    if str(exc) != 'checkpoint_inline_prices':
                        raise
                    result['skipped_inline'] += 1
                    continue
                existing = next((latest[prefix + path.name] for prefix in prefixes[family]
                                 if prefix + path.name in latest), None)
                if existing and _read_version(db, existing) != raw:
                    raise ValueError('checkpoint_existing_version_conflict')
                pending.append((path, raw))
                result['files'] += 1
                result['bytes'] += len(raw)
                manifest.update((path.relative_to(cache).as_posix() + '\0' +
                                 hashlib.sha256(raw).hexdigest() + '\n').encode())
        result['manifest_sha256'] = manifest.hexdigest()
        if not apply:
            return result
        # Bounded transactions preserve all entries in a batch before deletion.
        # Keep exact source bytes; generic record recipes are unnecessary here.
        for start in range(0, len(pending), 200):
            batch = pending[start:start + 200]
            references = []
            for family in {path.parent.name for path, _ in batch}:
                _regular(cache / family, root / 'runtime')
            with db:
                db.execute('BEGIN IMMEDIATE')
                for path, raw in batch:
                    _batch_source(path.parent, path.name)
                    if path.read_bytes() != raw:
                        raise ValueError('checkpoint_source_changed')
                    key = prefixes[path.parent.name][0] + path.name
                    version = store._insert_version(db, path, raw, kind=KIND, root=root,
                        metadata={'family': path.parent.name, 'source': 'compaction'},
                        validated_address=(key, str(root)))
                    references.append(_reference(store, db, version))
            _verify_batch(db, batch, references, prefixes)
            # Exercise normal path/config routing once per batch as well as
            # verifying every independent blob above on the shared connection.
            _verify(store, references[0], batch[0][0], batch[0][1])
            for family in {path.parent.name for path, _ in batch}:
                _regular(cache / family, root / 'runtime')
            for path, raw in batch:
                _batch_source(path.parent, path.name)
                if path.read_bytes() != raw:
                    raise ValueError('checkpoint_source_changed')
                path.unlink()
                result['removed'] += 1
            if progress:
                progress({'removed': result['removed'], 'files': result['files']})
    return result
