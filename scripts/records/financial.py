"""Archive the verified financial calculation input and outputs."""
if not __package__:
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import argparse
from collections import defaultdict
from contextlib import closing
import json
from pathlib import Path
import re

from atomic_io import atomic_write
from records.files import resolve
from records.paths import local_runtime_path, runtime_lookup_paths
from records.store import RecordStore, default_path, _hash, _key, _sha
from workbench_paths import ROOT


ARTIFACTS = {
    'manifest.json': 'financial_manifest',
    'result.json': 'financial_result',
    'normalized.json': 'financial_normalized',
    'source.json': 'financial_source',
}
BUNDLE = frozenset(ARTIFACTS)
LEGACY_BUNDLE = BUNDLE - {'source.json'}


def read_artifact(path):
    """Recover from the DB only if the original is absent."""
    path = Path(path)
    try:
        return path.read_bytes()
    except FileNotFoundError:
        raw = resolve(path)
        if raw is None:
            raise
        return raw


def _identity(base, root):
    financials = (Path(root).resolve() / 'runtime' / 'financials').resolve()
    try:
        symbol, folder, snapshot = Path(base).resolve().relative_to(financials).parts
    except ValueError as exc:
        raise ValueError('financial_record_outside_snapshots') from exc
    key = symbol.replace('-', ':', 1)
    if (folder != 'snapshots' or not re.fullmatch('[a-f0-9]{32}', snapshot)
            or not re.fullmatch(r'(KR:[0-9]{6}(?:\.KS|\.KQ)?|US:[A-Z0-9][A-Z0-9.\-]{0,14})', key)):
        raise ValueError('invalid_financial_record_snapshot')
    return key, snapshot


def _validate(base, root, payloads, manifest_sha=None):
    key, snapshot = _identity(base, root)
    raw = payloads['manifest.json']
    digest = _sha(raw)
    if manifest_sha is not None and digest != manifest_sha:
        raise ValueError('financial_record_manifest_hash_mismatch')
    manifest = json.loads(raw)
    if (not isinstance(manifest, dict) or manifest.get('schema') != 1
            or manifest.get('key') != key or not isinstance(manifest.get('version'), str)
            or not isinstance(manifest.get('files'), dict)):
        raise ValueError('invalid_financial_record_manifest')
    _hash(manifest['files'].get('source.json'))
    for name in ('result.json', 'normalized.json', 'source.json'):
        if name == 'source.json' and name not in payloads:
            continue
        raw = payloads[name]
        if _sha(raw) != manifest['files'].get(name):
            raise ValueError('financial_record_artifact_hash_mismatch:' + name)
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError('invalid_financial_record_artifact:' + name)
        if name == 'result.json' and (value.get('key'), value.get('snapshot_id')) != (key, snapshot):
            raise ValueError('financial_record_identity_mismatch')
    return dict(key=key, snapshot_id=snapshot, manifest_sha256=digest, version=manifest['version'])


def _existing_bundle(db, base, payloads, metadata):
    keys = tuple({_key(path / name) for path in runtime_lookup_paths(base) for name in ARTIFACTS})
    sql = 'SELECT source_path,kind,sha256,metadata FROM record_versions WHERE source_key IN ({})'
    rows = db.execute(sql.format(','.join('?' for _ in keys)), keys).fetchall()
    if not rows:
        return frozenset()
    present = frozenset(Path(path).name for path, _, _, _ in rows)
    if present not in (LEGACY_BUNDLE, BUNDLE):
        raise ValueError('financial_record_bundle_conflict')
    actual = sorted((Path(path).name, kind, digest) for path, kind, digest, _ in rows)
    expected = sorted((name, ARTIFACTS[name], _sha(payloads[name])) for name in present)
    if actual != expected or any(json.loads(meta) != metadata for _, _, _, meta in rows):
        raise ValueError('financial_record_bundle_conflict')
    return present


def publish_snapshot(base, *, root=ROOT, store=None, manifest_sha=None):
    """Register only missing verified artifacts in one snapshot transaction."""
    base = Path(base).resolve()
    _identity(base, root)
    paths = {name: base / name for name in ARTIFACTS}
    if any(path.resolve().parent != base for path in paths.values()):
        raise ValueError('financial_record_artifact_outside_snapshot')
    payloads = {name: read_artifact(path) for name, path in paths.items()}
    metadata = _validate(base, root, payloads, manifest_sha)
    store = store or RecordStore(default_path(root))
    present = frozenset()
    if store.path.is_file():
        with closing(store.connect()) as db:
            present = _existing_bundle(db, base, payloads, metadata)
    missing = BUNDLE - present
    if missing:
        with closing(store.connect(write=True)) as db, db:
            db.execute('BEGIN IMMEDIATE')
            missing = BUNDLE - _existing_bundle(db, base, payloads, metadata)
            for name, kind in ARTIFACTS.items():
                if name in missing:
                    store._put(db, paths[name], payloads[name], kind=kind, root=root, metadata=metadata)
    return dict(metadata, path=str(base), reused=not missing,
                artifacts=len(payloads), raw_bytes=sum(map(len, payloads.values())),
                inserted_artifacts=len(missing), inserted_bytes=sum(len(payloads[name]) for name in missing))


def verify(store, *, root=ROOT):
    """Decode all DB bundles and report earlier bundles awaiting source backfill."""
    with closing(store.connect()) as db:
        check = db.execute('PRAGMA quick_check').fetchone()[0]
        rows = db.execute(
            'SELECT source_path,kind,sha256,metadata FROM record_versions WHERE kind IN (?,?,?,?)',
            tuple(ARTIFACTS.values())).fetchall()
    if check != 'ok':
        raise ValueError('financial_record_database_integrity_failed')
    grouped = defaultdict(list)
    for row in rows:
        grouped[local_runtime_path(row[0]).parent].append(row)
    verified_bytes = 0
    legacy = 0
    for base, bundle in grouped.items():
        actual = sorted((Path(path).name, kind) for path, kind, _, _ in bundle)
        if actual == sorted((name, ARTIFACTS[name]) for name in LEGACY_BUNDLE):
            legacy += 1
        elif actual != sorted(ARTIFACTS.items()):
            raise ValueError('financial_record_incomplete_bundle:' + str(base))
        payloads = {}
        for path, _, digest, _ in bundle:
            raw = store.read(path, digest)
            if raw is None:
                raise ValueError('financial_record_artifact_missing:' + path)
            payloads[Path(path).name] = raw
        metadata = _validate(base, root, payloads)
        if any(json.loads(meta) != metadata for _, _, _, meta in bundle):
            raise ValueError('financial_record_metadata_mismatch:' + str(base))
        verified_bytes += sum(map(len, payloads.values()))
    return dict(snapshots=len(grouped), artifacts=len(rows), verified_bytes=verified_bytes,
                legacy_snapshots_missing_source=legacy, quick_check=check)


def _latest_sha(base):
    try:
        pointer = json.loads((base.parent.parent / 'latest.json').read_bytes())
    except FileNotFoundError:
        return None
    return pointer['manifest_sha256'] if pointer['snapshot'] == base.name else None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--output', type=Path)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--all', action='store_true', help='Archive snapshots with saved manifests')
    mode.add_argument('--snapshot', action='append', type=Path)
    mode.add_argument('--verify', action='store_true', help='Read every published bundle from the DB')
    args = parser.parse_args()
    root = args.root.resolve()
    store = RecordStore(default_path(root))
    if args.verify:
        result = verify(store, root=root)
    else:
        if args.output is None:
            parser.error('--output is required for publication evidence')
        snapshots = (sorted(p.parent for p in (root / 'runtime' / 'financials').glob('*/snapshots/*/manifest.json'))
                     if args.all else [p.resolve() for p in args.snapshot])
        result = {'planned': len(snapshots), 'receipts': [], 'errors': []}
        for base in snapshots:
            try:
                receipt = publish_snapshot(base, root=root, store=store, manifest_sha=_latest_sha(base))
                result['receipts'].append(receipt)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                result['errors'].append(dict(path=str(base), reason=str(exc)))
            atomic_write(args.output, json.dumps(result, ensure_ascii=False, indent=2))
    if args.output:
        atomic_write(args.output, json.dumps(result, ensure_ascii=False, indent=2))
    summary = {k: v for k, v in result.items() if k not in ('receipts', 'errors')}
    if 'receipts' in result:
        summary.update(published=len(result['receipts']), errors=len(result['errors']),
                       inserted_artifacts=sum(item['inserted_artifacts'] for item in result['receipts']),
                       inserted_bytes=sum(item['inserted_bytes'] for item in result['receipts']))
    print(json.dumps(summary, ensure_ascii=False))
    return int(bool(result.get('errors')))


if __name__ == '__main__':
    raise SystemExit(main())
