"""Copy private vault trade journals into the verified records database."""
if not __package__:
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import argparse
from contextlib import closing
import json
from pathlib import Path
import re

from records.store import RecordStore, _key, _sha, default_path
from runtime_paths import (LEGACY_JOURNAL_DIR, journal_relative_path, local_runtime_path,
                           previous_roots, runtime_lookup_paths)
from workbench_paths import ROOT


KIND = 'trade_journal'
VAULT_DIR = LEGACY_JOURNAL_DIR


def journal_folder(root):
    settings = json.loads((Path(root) / 'config.json').read_text(encoding='utf-8-sig'))
    mother = Path(settings['mother'])
    if not mother.is_absolute():
        raise ValueError('trade_journal_mother_must_be_absolute')
    return mother.resolve() / journal_relative_path(settings)


def _source(path, folder, *, existing=False):
    path = Path(path).absolute()
    if path.parent != folder or path.suffix.lower() != '.md':
        raise ValueError('trade_journal_source_outside_scope')
    if existing and (path.resolve() != path or not path.is_file()):
        raise ValueError('trade_journal_source_not_regular_file')
    return path


def _validate(raw):
    try:
        first, separator, rest = raw.decode('utf-8-sig').partition('\n')
    except UnicodeError:
        raise ValueError('trade_journal_utf8_required') from None
    if not separator or first.rstrip('\r') != '---':
        raise ValueError('trade_journal_frontmatter_required')
    header = []
    for line in rest.splitlines():
        if line == '---':
            types = [field for field in header if field.startswith('type:')]
            if len(types) == 1 and re.fullmatch(r'type:[ \t]*일지[ \t]*', types[0]):
                return
            break
        header.append(line)
    raise ValueError('trade_journal_type_required')


def _metadata(path, root):
    settings = json.loads((Path(root) / 'config.json').read_text(encoding='utf-8-sig'))
    mothers = [Path(settings['mother'])] + previous_roots(
        settings.get('records', {}), 'previous_mother_root', 'previous_mother_roots')
    for mother in mothers:
        if path.is_relative_to(mother):
            return {'mother_relative_path': path.relative_to(mother).as_posix()}
    raise ValueError('trade_journal_source_outside_scope')


def migrate(root=ROOT, *, store=None, sources=None):
    """Publish only missing path/SHA versions in one transaction."""
    folder = journal_folder(root)
    if folder.resolve() != folder or not folder.is_dir():
        raise ValueError('trade_journal_folder_unavailable')
    if sources is None:
        sources = sorted(path for path in folder.iterdir() if path.suffix.lower() == '.md')
    payloads = {}
    for source in sources:
        path = _source(source, folder, existing=True)
        raw = path.read_bytes()
        _validate(raw)
        payloads[path] = raw
    inserted = 0
    if payloads:
        store = store or RecordStore(default_path(root))
        with closing(store.connect(write=True)) as db, db:
            db.execute('BEGIN IMMEDIATE')
            for path, raw in payloads.items():
                row = None
                for old in runtime_lookup_paths(path, root=root):
                    row = db.execute(
                        'SELECT kind,metadata FROM record_versions WHERE source_key=? AND sha256=?',
                        (_key(old), _sha(raw)),
                    ).fetchone()
                    if row:
                        if row[0] != KIND or json.loads(row[1]) != _metadata(old, root):
                            raise ValueError('trade_journal_version_conflict')
                        break
                metadata = _metadata(path, root)
                if row:
                    continue
                store._insert_version(db, path, raw, kind=KIND, root=root, metadata=metadata)
                inserted += 1
    return dict(planned=len(payloads), inserted=inserted, reused=len(payloads) - inserted)


def verify(root=ROOT, *, store=None):
    """Verify registered versions and raw bytes without reading the source notes."""
    folder = journal_folder(root)
    store = store or RecordStore(default_path(root))
    with closing(store.connect()) as db:
        rows = db.execute(
            'SELECT source_path,sha256,metadata FROM record_versions WHERE kind=? ORDER BY id',
            (KIND,),
        ).fetchall()
    paths, total = set(), 0
    for source, digest, metadata in rows:
        path = _source(local_runtime_path(source, root=root), folder)
        if json.loads(metadata) != _metadata(Path(source), root):
            raise ValueError('trade_journal_metadata_mismatch')
        raw = store.read(source, digest)
        if raw is None:
            raise ValueError('trade_journal_version_missing')
        _validate(raw)
        total += len(raw)
        paths.add(path)
    return dict(sources=len(paths), versions=len(rows), verified_bytes=total)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--all', action='store_true', help='Register missing private vault journals')
    mode.add_argument('--verify', action='store_true', help='Verify DB bytes without reading the notes')
    args = parser.parse_args()
    result = verify(args.root.resolve()) if args.verify else migrate(args.root.resolve())
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
