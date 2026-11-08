"""Preserve exact market response bytes once in the existing records DB.

Only manifest-pinned KR/US history responses are eligible. The original
collection manifest and path remain the authority; collection/verification
readers recover a retired response through the normal saved-file resolver.
"""
if not __package__:
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import argparse
from contextlib import closing
from datetime import datetime
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat as stat_types
import zlib

from invest_agent.trading.store import instance_lock
from records.store import RecordStore, default_path, _key
from records.paths import runtime_lookup_paths
from workbench_paths import ROOT


KIND = 'history_response'
MAX_RAW = 8 * 1024 * 1024


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _regular(path, boundary):
    path = Path(path).absolute()
    boundary = Path(boundary).resolve()
    if not path.resolve().is_relative_to(boundary):
        raise ValueError('history_response_outside_runtime')
    for part in (path, *path.parents):
        if part.is_symlink() or part.is_junction():
            raise ValueError('history_response_link_forbidden')
        if part == boundary:
            break
    return path.resolve()


def _scope(collection, root):
    root = Path(root).resolve()
    collection = _regular(collection, root / 'runtime')
    relative = collection.relative_to(root / 'runtime')
    if len(relative.parts) != 2 or relative.parts[0] not in ('kr-history', 'us-history'):
        raise ValueError('history_response_collection_invalid')
    return collection


def _collection(collection, root):
    collection = _scope(collection, root)
    raw = (collection / 'manifest.json').read_bytes()
    manifest = json.loads(raw)
    if not isinstance(manifest, dict):
        raise ValueError('history_response_manifest_invalid')
    names = {}
    for name, digest in manifest.items():
        match = re.fullmatch(r'responses[/\\]([0-9a-f]{64}\.json\.gz)', name)
        if not match:
            continue
        if not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest):
            raise ValueError('history_response_manifest_hash_invalid')
        filename = match[1]
        if filename in names and names[filename] != digest:
            raise ValueError('history_response_manifest_conflict')
        names[filename] = digest
    return collection, names, _sha(raw)


def _source(collection, name, entry=None):
    if not re.fullmatch(r'[0-9a-f]{64}\.json\.gz', name):
        raise ValueError('history_response_name_invalid')
    path = collection / 'responses' / name
    # The directory ancestry is checked once under the collection lock.
    try:
        if entry is not None:
            if os.path.normcase(os.path.abspath(entry.path)) != os.path.normcase(str(path)):
                raise ValueError('history_response_database_path_invalid')
            # Windows DirEntry retains FindFirstFile metadata. Unlike path.lstat,
            # this does not search the large exFAT directory again for each name.
            stat = entry.stat(follow_symlinks=False)
        else:
            stat = path.lstat()
    except FileNotFoundError:
        return path, None
    if getattr(stat, 'st_file_attributes', 0) & 0x400 or stat_types.S_ISLNK(stat.st_mode):
        raise ValueError('history_response_link_forbidden')
    if not stat_types.S_ISREG(stat.st_mode):
        raise ValueError('history_response_regular_file_required')
    return path, stat


def _responses(collection, names):
    """Read directory entries in physical order, then verify retired DB sources.

    The caller owns the collection lock. Consume each cached entry immediately,
    rather than keeping stale file metadata for the entire collection.
    """
    seen = set()
    try:
        entries = os.scandir(collection / 'responses')
    except FileNotFoundError:
        entries = None
    if entries is not None:
        with entries:
            for entry in entries:
                name = os.path.normcase(entry.name)
                if name not in names:
                    continue
                if name in seen:
                    raise ValueError('history_response_duplicate_name')
                seen.add(name)
                path, stat = _source(collection, name, entry)
                yield name, path, stat
    for name in sorted(set(names) - seen):
        yield name, collection / 'responses' / name, None


def _packet(raw, name, expected):
    if len(raw) > MAX_RAW or _sha(raw) != expected:
        raise ValueError('history_response_source_hash_mismatch')
    with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:
        unpacked = stream.read(64 * 1024 * 1024 + 1)
    if len(unpacked) > 64 * 1024 * 1024:
        raise ValueError('history_response_payload_too_large')
    packet = json.loads(unpacked)
    if not isinstance(packet, dict) or set(packet) != {'request', 'payload', 'sha256', 'fetched_at'}:
        raise ValueError('history_response_packet_invalid')
    request = packet['request']
    if (not isinstance(request, list) or len(request) != 2 or not isinstance(request[1], dict)
            or request[0] not in ('/api/v1/candles', '/api/v1/stocks/all')):
        raise ValueError('history_response_request_invalid')
    def packed(value):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()
    if _sha(packed(request)) + '.json.gz' != name or _sha(packed(packet['payload'])) != packet['sha256']:
        raise ValueError('history_response_packet_hash_mismatch')
    if datetime.fromisoformat(packet['fetched_at']).tzinfo is None:
        raise ValueError('history_response_timestamp_invalid')


def _prefixes(collection, root):
    return tuple(_key(path / 'responses') + os.sep
                 for path in runtime_lookup_paths(collection, root=root))


def _existing_store(root, store):
    store = store or RecordStore(default_path(root))
    if not store.path.is_file():
        raise ValueError('history_response_existing_database_required')
    return store


def _blob(payload, size, expected):
    if not isinstance(size, int) or not 0 <= size <= MAX_RAW:
        raise ValueError('history_response_database_corrupt')
    decoder = zlib.decompressobj()
    raw = decoder.decompress(payload, size + 1)
    if len(raw) != size or not decoder.eof or decoder.unused_data or _sha(raw) != expected:
        raise ValueError('history_response_database_corrupt')
    return raw


def save_response(collection, path, raw, *, root=None):
    """Save a new checked response in an existing workbench DB, or return False.

    Standalone collectors without that DB retain their original file format.
    A running collection has no final manifest yet; request/payload identities
    and the original bytes are pinned now and later included in its manifest.
    The caller owns the collector lock, just as for its original file writes.
    """
    collection = Path(collection).resolve()
    if root is None:
        runtime = next((p for p in collection.parents if p.name == 'runtime'), None)
        if runtime is None:
            return False
        relative = collection.relative_to(runtime)
        if len(relative.parts) != 2 or relative.parts[0] not in ('kr-history', 'us-history'):
            return False
        root = runtime.parent
    root = Path(root).resolve()
    database = default_path(root)
    if not database.is_file():
        return False
    collection = _scope(collection, root)
    _regular(collection / 'responses', root / 'runtime')
    expected_path, existing = _source(collection, Path(path).name)
    if Path(path).absolute() != expected_path:
        raise ValueError('history_response_collection_invalid')
    digest = _sha(raw)
    _packet(raw, expected_path.name, digest)
    if existing and expected_path.read_bytes() != raw:
        raise ValueError('history_response_original_changed')
    store = RecordStore(database)
    keys = [prefix + expected_path.name for prefix in _prefixes(collection, root)]
    with closing(store.connect(write=True)) as db, db:
        db.execute('BEGIN IMMEDIATE')
        version = store._insert_version(db, expected_path, raw, kind=KIND, root=root,
                                        metadata={'collection': str(collection), 'source': 'collector'},
                                        immutable_keys=keys, validated_address=(keys[0], str(root)))
        row = db.execute('''SELECT source_path,sha256,raw_size,blob_sha256,encoding,kind,id
                            FROM record_versions WHERE id=?''', (version,)).fetchone()
        reference = store._reference(row)
    if reference['encoding'] != 'raw' or store.read(reference) != raw:
        raise ValueError('history_response_database_corrupt')
    return True


def migrate(collection, *, root=ROOT, store=None, progress=None):
    """Copy pinned originals in bounded transactions; never delete here."""
    root = Path(root).resolve()
    collection, names, manifest_sha = _collection(collection, root)
    store = _existing_store(root, store)
    prefixes = _prefixes(collection, root)
    imported = reused = total = 0
    with instance_lock(collection / 'lock'), closing(store.connect(write=True)) as db:
        _regular(collection / 'responses', root / 'runtime')
        batch = []
        for name, path, stat in _responses(collection, names):
            expected = names[name]
            # A resumed migration may have already retired this physical file.
            if stat:
                raw = path.read_bytes()
            else:
                keys = [prefix + name for prefix in prefixes]
                prior = db.execute('''SELECT v.sha256,v.kind,v.encoding,v.blob_sha256,b.raw_size,b.payload
                                      FROM record_versions v LEFT JOIN record_blobs b ON b.sha256=v.blob_sha256
                                      WHERE v.source_key IN (''' + ','.join('?' for _ in keys) + ')', keys).fetchall()
                if not prior or any(row[:4] != (expected, KIND, 'raw', expected) for row in prior):
                    raise ValueError('history_response_immutable_conflict')
                raw = _blob(prior[0][5], prior[0][4], expected)
            if raw is None:
                raise ValueError('history_response_missing')
            _packet(raw, name, expected)
            batch.append((path, raw, expected))
            if len(batch) == 256:
                new, old = _publish(db, store, batch, root, collection, manifest_sha, prefixes)
                imported += new; reused += old; total += sum(len(r[1]) for r in batch)
                batch.clear()
                if progress:
                    progress({'imported': imported, 'reused': reused, 'bytes': total})
        if batch:
            new, old = _publish(db, store, batch, root, collection, manifest_sha, prefixes)
            imported += new; reused += old; total += sum(len(r[1]) for r in batch)
    return dict(collection=str(collection), imported=imported, reused=reused, bytes=total,
                original_files_removed=0, manifest_sha256=manifest_sha)


def _publish(db, store, batch, root, collection, manifest_sha, prefixes):
    new = old = 0
    with db:
        db.execute('BEGIN IMMEDIATE')
        if _sha((collection / 'manifest.json').read_bytes()) != manifest_sha:
            raise ValueError('history_response_manifest_changed')
        for path, raw, expected in batch:
            keys = [prefix + path.name for prefix in prefixes]
            prior = db.execute('SELECT sha256,kind,encoding,blob_sha256 FROM record_versions WHERE source_key IN (' +
                               ','.join('?' for _ in keys) + ')', keys).fetchall()
            if prior and any(row != (expected, KIND, 'raw', expected) for row in prior):
                raise ValueError('history_response_immutable_conflict')
            store._insert_version(db, path, raw, kind=KIND, root=root,
                                  metadata={'collection': str(collection), 'manifest_sha256': manifest_sha},
                                  immutable_keys=keys, validated_address=(keys[0], str(root)))
            old += bool(prior); new += not prior
    return new, old


def compact(collection, *, root=ROOT, store=None, apply=False, progress=None):
    """Verify DB payloads independently, then remove only matching source files.

    Takes the collector's own lock. Partial completion is safe and resumable:
    every removed file has an independently verified DB copy at its old path.
    Unmanifested files and the manifest itself are never removed.
    """
    root = Path(root).resolve()
    collection, names, manifest_sha = _collection(collection, root)
    store = _existing_store(root, store)
    prefixes = _prefixes(collection, root)
    where = ' OR '.join('(v.source_key>=? AND v.source_key<?)' for _ in prefixes)
    bounds = tuple(value for prefix in prefixes for value in (prefix, prefix + '\uffff'))
    def source_name(key):
        return next((key[len(prefix):] for prefix in prefixes if key.startswith(prefix)), None)
    checked = removed = physical_bytes = 0
    seen = set()
    with instance_lock(collection / 'lock'), closing(store.connect()) as db:
        _regular(collection / 'responses', root / 'runtime')
        saved = db.execute('''SELECT v.source_key,v.source_path,v.sha256,v.encoding,v.kind,v.raw_size,
                            v.blob_sha256,b.raw_size FROM record_versions v
                            LEFT JOIN record_blobs b ON b.sha256=v.blob_sha256 WHERE ''' + where, bounds).fetchall()
        pinned = {}
        for key, source, digest, encoding, kind, size, blob_sha, blob_size in saved:
            name = source_name(key)
            if name not in names:
                continue
            if (digest, encoding, kind, blob_sha) != (names[name], 'raw', KIND, names[name]):
                raise ValueError('history_response_database_conflict')
            if (os.path.normcase(os.path.abspath(source)) != key or not isinstance(size, int)
                    or not 0 <= size <= MAX_RAW or blob_size != size):
                raise ValueError('history_response_database_corrupt')
            pinned[name] = size
        if set(pinned) != set(names):
            raise ValueError('history_response_migration_incomplete')
        del saved
        for name, path, stat in _responses(collection, names):
            expected, size = names[name], pinned[name]
            blob = db.execute('SELECT raw_size,payload FROM record_blobs WHERE sha256=?', (expected,)).fetchone()
            if blob is None:
                raise ValueError('history_response_database_corrupt')
            blob_size, payload = blob
            raw = _blob(payload, blob_size, expected)
            if len(raw) != size:
                raise ValueError('history_response_database_corrupt')
            _packet(raw, path.name, expected)
            seen.add(path.name); checked += 1
            if checked % 256 == 1 and _sha((collection / 'manifest.json').read_bytes()) != manifest_sha:
                raise ValueError('history_response_manifest_changed')
            if stat:
                if path.read_bytes() != raw:
                    raise ValueError('history_response_original_changed')
                physical_bytes += size
                if apply:
                    # exFAT timestamps have coarser resolution than nanoseconds;
                    # a same-size edit can otherwise pass the metadata check.
                    if path.read_bytes() != raw:
                        raise ValueError('history_response_original_changed')
                    # Keep the non-follow stat/reparse check immediately before
                    # removal; only the earlier enumeration stat is cached.
                    _, final_stat = _source(collection, name)
                    if final_stat is None or (final_stat.st_mtime_ns, final_stat.st_size) != (stat.st_mtime_ns, size):
                        raise ValueError('history_response_original_changed')
                    path.unlink()
                    removed += 1
            if progress and checked % 5000 == 0:
                progress({'verified': checked, 'removed': removed})
        if seen != set(names):
            raise ValueError('history_response_migration_incomplete')
        if _sha((collection / 'manifest.json').read_bytes()) != manifest_sha:
            raise ValueError('history_response_manifest_changed')
    return dict(collection=str(collection), verified=checked, original_files_removed=removed,
                eligible_bytes=physical_bytes, apply=apply, manifest_sha256=manifest_sha)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--collection', type=Path, required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--import-originals', action='store_true')
    mode.add_argument('--compact', action='store_true')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if args.apply and not args.compact:
        parser.error('--apply requires --compact')
    progress = lambda value: print(json.dumps(value), flush=True)
    result = (migrate(args.collection, root=args.root, progress=progress) if args.import_originals
              else compact(args.collection, root=args.root, apply=args.apply, progress=progress))
    print(json.dumps(result), flush=True)


if __name__ == '__main__':
    main()
