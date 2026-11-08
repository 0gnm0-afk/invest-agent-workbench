"""Verified non-price run artifacts, addressed by original source bytes and SHA.

Original files are never removed or replaced here. A published run is immutable;
price-bearing artifacts must supply a reference recipe rather than inline bars.
"""
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import sqlite3
import time
import zlib

from workbench_paths import ROOT
from .paths import local_runtime_path, runtime_lookup_paths


ROLES = frozenset({
    'run.json', 'config.json', 'quality.json', 'market-failures.json',
    'screen-base.json', 'screen.json', 'priority-review.json', 'priority-charts.json',
    'policy.json', 'scope-policy.json', 'classification-policy.json', 'registry.json',
})
TERMINAL_STATES = frozenset({'succeeded', 'partial', 'failed'})


def default_path(root=ROOT):
    root = Path(root).resolve()
    config = root / 'config.json'
    settings = json.loads(config.read_text(encoding='utf-8-sig')) if config.exists() else {}
    path = Path(settings.get('records', {}).get('path', 'runtime/records.sqlite3'))
    return path if path.is_absolute() else root / path


def _key(path):
    return os.path.normcase(str(Path(path).resolve()))


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _inside(path, root):
    path = Path(path).resolve()
    if not path.is_relative_to((Path(root).resolve() / 'runtime').resolve()):
        raise ValueError('record_source_outside_runtime')
    return path


def _hash(value):
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{64}', value):
        raise ValueError('record_hash_invalid')
    return value


def _has_prices(value):
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            if ({'date', 'open', 'high', 'low', 'close', 'volume'} <= item.keys()
                    or {'timestamp', 'openPrice', 'highPrice', 'lowPrice', 'closePrice', 'volume'} <= item.keys()):
                return True
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    return False


def _raw_guard(raw, role):
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        return
    if _has_prices(value):
        raise ValueError('record_price_arrays_require_reference:' + role)


def _role(name, run_dir):
    if not isinstance(name, str):
        raise ValueError('record_output_name_invalid')
    normalized = name.replace('\\', '/')
    pure = PurePosixPath(normalized)
    if pure.is_absolute() or PureWindowsPath(name).drive or '..' in pure.parts:
        raise ValueError('record_output_outside_run')
    target = (run_dir / normalized).resolve()
    if not target.is_relative_to(run_dir):
        raise ValueError('record_output_outside_run')
    return pure.as_posix(), target


def allowed_role(role):
    path = PurePosixPath(role)
    return role in ROLES or (len(path.parts) > 1 and path.parts[0] in ('sectors', 'policies')
                             and path.suffix == '.json' and path.name != 'snapshot.json')


class RecordStore:
    def __init__(self, path=None):
        self.path = Path(path or default_path()).resolve()

    def connect(self, *, write=False):
        if not write:
            db = sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True, timeout=30)
            db.execute('PRAGMA query_only=ON')
            return db
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=30)
        try:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables and 'record_meta' not in tables:
                raise ValueError('record_database_belongs_to_another_store')
            db.execute('PRAGMA foreign_keys=ON')
            # journal_mode changes can report SQLITE_BUSY immediately even with
            # busy_timeout set, when several first publishers initialize at once.
            for attempt in range(8):
                try:
                    db.execute('PRAGMA journal_mode=WAL')
                    break
                except sqlite3.OperationalError as exc:
                    if 'locked' not in str(exc).lower() or attempt == 7:
                        raise
                    time.sleep(min(.02 * 2 ** attempt, .25))
            db.executescript('''
            CREATE TABLE IF NOT EXISTS record_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            INSERT OR IGNORE INTO record_meta VALUES ('schema','1');
            CREATE TABLE IF NOT EXISTS record_blobs(
                sha256 TEXT PRIMARY KEY,raw_size INTEGER NOT NULL,payload BLOB NOT NULL);
            CREATE TABLE IF NOT EXISTS record_versions(
                id INTEGER PRIMARY KEY,source_path TEXT NOT NULL,source_key TEXT NOT NULL,
                root_path TEXT NOT NULL,sha256 TEXT NOT NULL,raw_size INTEGER NOT NULL,
                blob_sha256 TEXT NOT NULL REFERENCES record_blobs(sha256),encoding TEXT NOT NULL,
                kind TEXT NOT NULL,metadata TEXT NOT NULL,created_at TEXT NOT NULL,
                UNIQUE(source_key,sha256));
            CREATE INDEX IF NOT EXISTS record_source ON record_versions(source_key,id);
            CREATE TABLE IF NOT EXISTS record_runs(
                run_path TEXT PRIMARY KEY,run_id TEXT NOT NULL,manifest_sha256 TEXT NOT NULL,
                state TEXT NOT NULL,processing_state TEXT,mode TEXT,data_times TEXT NOT NULL,
                metadata TEXT NOT NULL,published_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS record_run_artifacts(
                run_path TEXT NOT NULL REFERENCES record_runs(run_path),role TEXT NOT NULL,
                version_id INTEGER NOT NULL REFERENCES record_versions(id),
                PRIMARY KEY(run_path,role));
            CREATE TABLE IF NOT EXISTS record_subjects(
                version_id INTEGER NOT NULL REFERENCES record_versions(id),row_number INTEGER NOT NULL,
                market TEXT NOT NULL,symbol TEXT NOT NULL,PRIMARY KEY(version_id,row_number));
            CREATE INDEX IF NOT EXISTS record_subject_lookup ON record_subjects(market,symbol);
            ''')
        except BaseException:
            db.close()
            raise
        return db

    def _reference(self, row):
        return {'path': str(self.path), 'source_path': row[0], 'sha256': row[1],
                'size': row[2], 'blob_sha256': row[3], 'encoding': row[4],
                'kind': row[5], 'version_id': row[6]}

    def lookup(self, source, expected_sha=None):
        explicit = isinstance(source, dict)
        if explicit:
            if local_runtime_path(source['path']).resolve() != self.path:
                raise ValueError('record_reference_database_mismatch')
            expected_sha = expected_sha or source['sha256']
            source = source['source_path']
        if expected_sha is not None:
            _hash(expected_sha)
        if not self.path.is_file():
            if explicit:
                raise ValueError('record_reference_missing')
            return None
        with closing(self.connect()) as db:
            registered = False
            for path in runtime_lookup_paths(source):
                key = _key(path)
                clause = ' AND sha256=?' if expected_sha else ''
                args = (key, expected_sha) if expected_sha else (key,)
                row = db.execute('''SELECT source_path,sha256,raw_size,blob_sha256,encoding,kind,id
                    FROM record_versions WHERE source_key=?''' + clause + ' ORDER BY id DESC LIMIT 1', args).fetchone()
                if row:
                    return self._reference(row)
                registered |= db.execute(
                    'SELECT 1 FROM record_versions WHERE source_key=? LIMIT 1', (key,)).fetchone() is not None
        if registered or explicit:
            raise ValueError('record_reference_hash_mismatch' if registered else 'record_reference_missing')
        return None

    reference = lookup

    def read(self, source, expected_sha=None, *, decoder=None):
        reference = self.lookup(source, expected_sha)
        if reference is None:
            return None
        with closing(self.connect()) as db:
            blob = db.execute('SELECT raw_size,payload FROM record_blobs WHERE sha256=?',
                              (reference['blob_sha256'],)).fetchone()
        if blob is None:
            raise ValueError('record_blob_missing')
        if not isinstance(blob[0], int) or not 0 <= blob[0] <= 2 ** 31:
            raise ValueError('record_blob_size_invalid')
        try:
            decompressor = zlib.decompressobj()
            payload = decompressor.decompress(blob[1], blob[0] + 1)
            if len(payload) != blob[0] or not decompressor.eof or decompressor.unused_data:
                raise ValueError('record_blob_size_mismatch')
        except zlib.error as exc:
            raise ValueError('record_blob_invalid') from exc
        if _sha(payload) != reference['blob_sha256']:
            raise ValueError('record_blob_hash_mismatch')
        if reference['encoding'] == 'raw':
            raw = payload
        else:
            if decoder is None:
                from .prices import decode as decoder
            raw = decoder(reference['encoding'], payload, reference)
        if not isinstance(raw, bytes) or len(raw) != reference['size'] or _sha(raw) != reference['sha256']:
            raise ValueError('record_source_integrity_failed')
        return raw

    def _put(self, db, source, raw, *, kind, root, metadata=None, encoded=None):
        source = _inside(source, root)
        return self._insert_version(db, source, raw, kind=kind, root=root, metadata=metadata, encoded=encoded)

    def _insert_version(self, db, source, raw, *, kind, root, metadata=None, encoded=None):
        """Insert into a caller-owned transaction after source authority validation."""
        if not isinstance(raw, bytes):
            raise ValueError('record_source_bytes_required')
        sha = _sha(raw)
        existing = db.execute('SELECT id FROM record_versions WHERE source_key=? AND sha256=?',
                              (_key(source), sha)).fetchone()
        if existing:
            return existing[0]
        if encoded is None:
            encoding, payload = 'raw', raw
            _raw_guard(raw, kind)
        else:
            encoding, payload = encoded['encoding'], encoded['payload']
            if not isinstance(encoding, str) or not encoding or not isinstance(payload, bytes):
                raise ValueError('record_encoding_invalid')
            _raw_guard(payload, kind)
        blob_sha = _sha(payload)
        if db.execute('SELECT 1 FROM record_blobs WHERE sha256=?', (blob_sha,)).fetchone() is None:
            db.execute('INSERT INTO record_blobs VALUES (?,?,?)', (blob_sha, len(payload), zlib.compress(payload, 6)))
        version = db.execute('''INSERT INTO record_versions(
            source_path,source_key,root_path,sha256,raw_size,blob_sha256,encoding,kind,metadata,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)''', (str(source), _key(source), str(Path(root).resolve()), sha, len(raw),
            blob_sha, encoding, kind, _json(metadata or {}), datetime.now(timezone.utc).isoformat())).lastrowid
        # Subject keys index original rows, including rows whose prices live only
        # in a verified recipe. Never materialize prices merely to build an index.
        if encoding in ('raw', 'screen_prices_v1'):
            try:
                parsed = json.loads(payload)
            except (ValueError, UnicodeError):
                parsed = None
            if encoding == 'screen_prices_v1':
                parsed = parsed.get('value') if isinstance(parsed, dict) else None
            rows = parsed.get('rows') if isinstance(parsed, dict) else parsed
            if isinstance(rows, list):
                keys = [(version, i, row['market'], row['symbol']) for i, row in enumerate(rows)
                        if isinstance(row, dict) and isinstance(row.get('market'), str)
                        and isinstance(row.get('symbol'), str)]
                db.executemany('INSERT INTO record_subjects VALUES (?,?,?,?)', keys)
        return version

    def put(self, source, raw, *, kind='artifact', root=ROOT, metadata=None, encoded=None):
        with closing(self.connect(write=True)) as db, db:
            db.execute('BEGIN IMMEDIATE')
            self._put(db, source, raw, kind=kind, root=root, metadata=metadata, encoded=encoded)
        return self.lookup(source, _sha(raw))

    def runs(self):
        if not self.path.is_file():
            return []
        with closing(self.connect()) as db:
            rows = db.execute('SELECT run_path,run_id,manifest_sha256,state,processing_state,mode,data_times,metadata FROM record_runs ORDER BY published_at,run_path').fetchall()
        return [dict(zip(('run_path', 'run_id', 'manifest_sha256', 'state', 'processing_state', 'mode', 'data_times', 'metadata'),
                         (*row[:6], json.loads(row[6]), json.loads(row[7])))) for row in rows]

    def artifacts(self, run_dir):
        if not self.path.is_file():
            return []
        with closing(self.connect()) as db:
            rows = []
            for path in runtime_lookup_paths(run_dir):
                rows = db.execute('''SELECT v.source_path,v.sha256,v.raw_size,v.blob_sha256,v.encoding,v.kind,v.id,a.role
                    FROM record_run_artifacts a JOIN record_versions v ON v.id=a.version_id
                    WHERE a.run_path=? ORDER BY a.role''', (str(path),)).fetchall()
                if rows:
                    break
        return [{**self._reference(row[:7]), 'role': row[7]} for row in rows]

    def subjects(self, market, symbol, kind=None):
        """Find immutable artifact rows for an exact instrument, without decoding bodies.

        Results include every stored version; row numbers refer to that version's
        original ``rows`` array. A standalone ``put`` has an empty ``run_paths``.
        """
        if not isinstance(market, str) or not isinstance(symbol, str):
            raise ValueError('record_subject_identity_invalid')
        if kind is not None and not isinstance(kind, str):
            raise ValueError('record_subject_kind_invalid')
        if not self.path.is_file():
            return []
        args = (market, symbol)
        clause = ''
        if kind is not None:
            clause = ' AND v.kind=?'
            args += (kind,)
        with closing(self.connect()) as db:
            rows = db.execute('''SELECT v.source_path,v.sha256,v.raw_size,v.blob_sha256,
                v.encoding,v.kind,v.id,s.row_number,a.run_path
                FROM record_subjects s JOIN record_versions v ON v.id=s.version_id
                LEFT JOIN record_run_artifacts a ON a.version_id=v.id
                WHERE s.market=? AND s.symbol=?''' + clause +
                ' ORDER BY v.id,s.row_number,a.run_path', args).fetchall()
        results = {}
        for row in rows:
            item = results.setdefault((row[6], row[7]),
                {**self._reference(row[:7]), 'row_number': row[7], 'run_paths': []})
            if row[8] is not None and row[8] not in item['run_paths']:
                item['run_paths'].append(row[8])
        return list(results.values())

    def reindex_subjects(self):
        """Rebuild missing derived keys without changing any immutable artifact."""
        if not self.path.is_file():
            return 0
        with closing(self.connect()) as db:
            versions = db.execute('''SELECT v.id,v.encoding,v.blob_sha256,b.payload
                FROM record_versions v JOIN record_blobs b ON b.sha256=v.blob_sha256
                WHERE v.encoding IN ('raw','screen_prices_v1')
                AND NOT EXISTS(SELECT 1 FROM record_subjects s WHERE s.version_id=v.id)''').fetchall()
        added = 0
        for version, encoding, expected, compressed in versions:
            payload = zlib.decompress(compressed)
            if _sha(payload) != expected:
                raise ValueError('record_blob_hash_mismatch')
            parsed = json.loads(payload)
            if encoding == 'screen_prices_v1':
                parsed = parsed['value']
            rows = parsed.get('rows') if isinstance(parsed, dict) else parsed
            if not isinstance(rows, list):
                continue
            keys = [(version, i, row['market'], row['symbol']) for i, row in enumerate(rows)
                    if isinstance(row, dict) and isinstance(row.get('market'), str)
                    and isinstance(row.get('symbol'), str)]
            if keys:
                with closing(self.connect(write=True)) as db, db:
                    db.executemany('INSERT OR IGNORE INTO record_subjects VALUES (?,?,?,?)', keys)
                added += len(keys)
        return added

    @staticmethod
    def _published_run(db, run_dir):
        for path in runtime_lookup_paths(run_dir):
            row = db.execute('SELECT manifest_sha256,run_id,state FROM record_runs WHERE run_path=?',
                             (str(path),)).fetchone()
            if row:
                return path, row
        return run_dir, None

    def publish_run(self, run_dir, *, root=ROOT, serializer=None):
        run_dir = _inside(run_dir, root)
        manifest_raw = (run_dir / 'manifest.json').read_bytes()
        manifest_sha = _sha(manifest_raw)
        manifest = json.loads(manifest_raw)
        outputs = manifest['output_hashes']
        if not isinstance(outputs, dict):
            raise ValueError('record_output_hashes_invalid')
        # Check before reading outputs: already-published artifacts may be thin references.
        if self.path.is_file():
            with closing(self.connect()) as db:
                published_path, row = self._published_run(db, run_dir)
            if row:
                if row[0] != manifest_sha:
                    raise ValueError('record_published_run_conflict')
                return {'run_path': str(published_path), 'run_id': row[1], 'state': row[2], 'manifest_sha256': manifest_sha,
                        'artifact_count': len(self.artifacts(published_path)), 'idempotent': True}
        selected = {}
        for name, expected in outputs.items():
            role, source = _role(name, run_dir)
            if allowed_role(role):
                _hash(expected)
                if role in selected:
                    raise ValueError('record_duplicate_output_role')
                selected[role] = (source, expected)
        if 'run.json' not in selected:
            raise ValueError('record_run_output_missing')
        run_raw = selected['run.json'][0].read_bytes()
        if _sha(run_raw) != selected['run.json'][1]:
            raise ValueError('record_output_hash_mismatch:run.json')
        run = json.loads(run_raw)
        if run.get('state') not in TERMINAL_STATES or not run.get('finished_at'):
            raise ValueError('record_run_not_terminal')
        run_id = run.get('run_id') or run_dir.name
        if serializer is None:
            from .prices import encode as serializer
        with closing(self.connect(write=True)) as db, db:
            db.execute('BEGIN IMMEDIATE')
            published_path, old = self._published_run(db, run_dir)
            if old:
                if old[0] != manifest_sha:
                    raise ValueError('record_published_run_conflict')
                run_dir = published_path
                idempotent = True
            else:
                idempotent = False
                db.execute('INSERT INTO record_runs VALUES (?,?,?,?,?,?,?,?,?)',
                    (str(run_dir), run_id, manifest_sha, run['state'], run.get('processing_state'), run.get('mode'),
                     _json(run.get('data_times')), _json({k: run.get(k) for k in
                        ('started_at', 'finished_at', 'market_source', 'session_cutoff', 'evaluated_at')}),
                     datetime.now(timezone.utc).isoformat()))
                for role, source, raw in [('manifest.json', run_dir / 'manifest.json', manifest_raw)] + [
                        (role, source, None) for role, (source, _) in sorted(selected.items())]:
                    raw = source.read_bytes() if raw is None else raw
                    if role != 'manifest.json' and _sha(raw) != selected[role][1]:
                        raise ValueError('record_output_hash_mismatch:' + role)
                    encoded = serializer(role, raw, run_dir, Path(root).resolve())
                    version = self._put(db, source, raw, kind=role, root=root, encoded=encoded,
                                        metadata={'run_path': str(run_dir), 'run_id': run_id})
                    db.execute('INSERT INTO record_run_artifacts VALUES (?,?,?)', (str(run_dir), role, version))
        return {'run_path': str(run_dir), 'run_id': run_id, 'state': run['state'], 'manifest_sha256': manifest_sha,
                'artifact_count': len(self.artifacts(run_dir)), 'idempotent': idempotent}


def publish_run(run_dir, *, root=ROOT, serializer=None, store=None):
    return (store or RecordStore(default_path(root))).publish_run(run_dir, root=root, serializer=serializer)
