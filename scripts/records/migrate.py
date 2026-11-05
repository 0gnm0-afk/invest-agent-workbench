"""Offline migration and verified lookup of operational market records. No deletion."""
if not __package__:
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from contextlib import closing
import argparse
import hashlib
import json
from pathlib import Path
import re
import socket
from unittest.mock import patch

from atomic_io import atomic_write
from workbench_paths import ROOT
from records.store import RecordStore, default_path, TERMINAL_STATES
from records.paths import local_runtime_path


def save(path, value):
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))


def discover(root):
    runtime = root / 'runtime'
    parents = [p for p in runtime.iterdir() if p.is_dir() and re.fullmatch(r'\d{4}-\d{2}-\d{2}', p.name)]
    parents += [runtime / name for name in ('policy-migrations', 'policy-repair', 'scope-repair',
                                           'market-scope-auto', 'market-scope-user') if (runtime / name).is_dir()]
    selected, skipped = [], []
    for parent in sorted(parents):
        for path in sorted(parent.rglob('run.json')):
            manifest = path.parent / 'manifest.json'
            if not manifest.is_file():
                continue
            try:
                run = json.loads(path.read_bytes())
                raw = manifest.read_bytes()
                value = json.loads(raw)
                if not isinstance(value.get('output_hashes'), dict):
                    continue
                if run.get('state') not in TERMINAL_STATES or not run.get('finished_at'):
                    skipped.append({'run': str(path.parent), 'reason': 'not_terminal'})
                    continue
                selected.append({'run': str(path.parent.resolve()), 'state': run['state'],
                                 'manifest_sha256': hashlib.sha256(raw).hexdigest()})
            except (OSError, ValueError, KeyError, TypeError) as exc:
                skipped.append({'run': str(path.parent), 'reason': type(exc).__name__})
    return {'root': str(root), 'runs': selected, 'skipped': skipped}


def migrate(root, folder, runs=None):
    folder.mkdir(parents=True, exist_ok=True)
    plan = discover(root) if runs is None else {'root': str(root), 'runs': [{'run': str(p.resolve())} for p in runs], 'skipped': []}
    save(folder / 'plan.json', plan)
    store = RecordStore(default_path(root))
    receipts, errors = [], []
    for entry in plan['runs']:
        run = Path(entry['run'])
        try:
            expected = entry.get('manifest_sha256')
            if expected and hashlib.sha256((run / 'manifest.json').read_bytes()).hexdigest() != expected:
                raise ValueError('record_planned_manifest_changed')
            receipt = store.publish_run(run, root=root)
            receipts.append(receipt)
            print(json.dumps({'run': run.name, 'state': receipt['state'], 'artifacts': receipt['artifact_count'],
                              'idempotent': receipt['idempotent']}), flush=True)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            errors.append({'run': str(run), 'type': type(exc).__name__, 'reason': str(exc)})
            print(json.dumps({'run': run.name, 'error': str(exc)}), flush=True)
        result = {'database': str(store.path), 'planned': len(plan['runs']), 'published': len(receipts),
                  'receipts': receipts, 'errors': errors, 'original_files_removed': 0}
        save(folder / 'migration.json', result)
    return result if plan['runs'] else {'published': 0, 'errors': [], 'original_files_removed': 0}


def verify(store, *, forbid_original_reads=False):
    runs = store.runs()
    references = [reference for run in runs for reference in store.artifacts(run['run_path'])]
    protected = {str(local_runtime_path(r['source_path']).resolve()).casefold() for r in references}
    original_read = Path.read_bytes

    def guarded(path):
        if str(path.resolve()).casefold() in protected:
            raise AssertionError('migrated_original_read_forbidden')
        return original_read(path)

    checked, recovered_bytes = 0, 0
    with patch.object(Path, 'read_bytes', guarded) if forbid_original_reads else patch.object(Path, 'read_bytes', original_read):
        for reference in references:
            raw = store.read(reference)
            if hashlib.sha256(raw).hexdigest() != reference['sha256']:
                raise ValueError('record_verification_failed')
            if not forbid_original_reads:
                path = local_runtime_path(reference['source_path'])
                if path.is_file():
                    from atomic_io import read_bytes
                    if read_bytes(path) != raw:
                        raise ValueError('record_original_differs')
            checked += 1
            recovered_bytes += len(raw)
    with closing(store.connect()) as db:
        quick = db.execute('PRAGMA quick_check').fetchone()[0]
        subjects = db.execute('SELECT count(*) FROM record_subjects').fetchone()[0]
        blobs = db.execute('SELECT count(*),coalesce(sum(length(payload)),0) FROM record_blobs').fetchone()
    if quick != 'ok':
        raise ValueError('record_database_integrity_failed')
    return dict(runs=len(runs), verified_artifacts=checked, recovered_bytes=recovered_bytes,
                subjects=subjects, blobs=blobs[0], compressed_payload_bytes=blobs[1], quick_check=quick,
                original_reads_forbidden=forbid_original_reads)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--all', action='store_true', help='All terminal date/policy/scope market runs; no raw-source/financial scan')
    mode.add_argument('--run', type=Path, action='append')
    mode.add_argument('--verify', action='store_true')
    mode.add_argument('--list', action='store_true')
    mode.add_argument('--read', type=Path, help='Print a verified artifact recovered from DB')
    parser.add_argument('--sha256')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--forbid-original-reads', action='store_true')
    args = parser.parse_args()
    root = args.root.resolve()
    store = RecordStore(default_path(root))
    with patch.object(socket.socket, 'connect', side_effect=AssertionError('records_network_forbidden')):
        if args.verify:
            result = verify(store, forbid_original_reads=args.forbid_original_reads)
        elif args.list:
            result = store.runs()
        elif args.read:
            raw = store.read(args.read, args.sha256)
            if raw is None:
                raise ValueError('record_not_found')
            print(raw.decode('utf-8-sig'))
            return 0
        else:
            if not args.output:
                parser.error('--output is required for migration evidence')
            result = migrate(root, args.output.resolve(), None if args.all else args.run)
        if args.output and args.verify:
            save(args.output, result)
        print(json.dumps(result, ensure_ascii=False), flush=True)
        return 1 if isinstance(result, dict) and result.get('errors') else 0


if __name__ == '__main__':
    raise SystemExit(main())
