"""Append-only knowledge in the shared records DB; no network or dashboard.

Raw bytes are stored once by SHA256. Sources and statements retain attribution,
while summaries are versioned interpretations, never adopted trading policies.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3

from .search import KeywordSearch


AUTHORS = frozenset({'user', 'source', 'ai'})
KINDS = frozenset({'learning', 'interpretation', 'hypothesis', 'question', 'interest',
                   'decision', 'chart', 'response', 'change', 'next_step', 'session',
                   'observation', 'defer'})
OPEN_KINDS = frozenset({'question', 'hypothesis', 'next_step'})
FILE_TYPES = frozenset({'.md', '.txt', '.pdf', '.png', '.jpg', '.jpeg', '.webp', '.gif',
                        '.csv', '.tsv', '.docx', '.pptx', '.xlsx', '.html'})
TEXT_TYPES = frozenset({'.md', '.txt', '.csv', '.tsv', '.html'})
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_TEXT_CHARS = 2_000_000
MIME_TYPES = {
    '.md': 'text/markdown', '.txt': 'text/plain', '.pdf': 'application/pdf',
    '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
    '.webp': 'image/webp', '.gif': 'image/gif', '.csv': 'text/csv',
    '.tsv': 'text/tab-separated-values', '.html': 'text/html',
    '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    '.pptx': 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
    '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
}
BLOCKED_PARTS = frozenset({'.git', '.venv', 'secrets', 'auth', '.aws', '.codex', '.agents'})


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _text(value, name, *, optional=False, limit=MAX_TEXT_CHARS):
    if optional and value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError('knowledge_invalid_' + name)
    return value


def _list(values, name):
    if values is None:
        return []
    if not isinstance(values, list) or len(values) > 200:
        raise ValueError('knowledge_invalid_' + name)
    return sorted({_text(v, name, limit=500) for v in values})


def _page(offset, limit):
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError('knowledge_invalid_pagination')


def _now():
    return datetime.now(timezone.utc).isoformat()


def _text_page(value, offset, limit):
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 24000:
        raise ValueError('knowledge_invalid_text_pagination')
    return value[offset:offset + limit], dict(total=len(value), offset=offset,
        next_offset=offset + limit if offset + limit < len(value) else None)


def _linked(path):
    return path.is_symlink() or path.is_junction()


def _source_media(source):
    suffix = Path(source['original_name']).suffix.lower() if source.get('original_name') else '.txt'
    if suffix not in MIME_TYPES:
        raise ValueError('knowledge_source_file_type_unsupported')
    return MIME_TYPES[suffix], '.jpg' if suffix == '.jpeg' else suffix


class KnowledgeStore:
    def __init__(self, path, *, search_backend=None):
        self.path = Path(path).resolve()
        self.search_backend = KeywordSearch() if search_backend is None else search_backend

    @contextmanager
    def _read(self):
        if not self.path.exists():
            yield None
            return
        db = sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='knowledge_notes'").fetchone()
            yield db if exists else None
        finally:
            db.close()

    @contextmanager
    def _write(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA foreign_keys=ON')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS knowledge_blobs (
                    sha256 TEXT PRIMARY KEY, raw BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS knowledge_sources (
                    id TEXT PRIMARY KEY, payload TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS knowledge_notes (
                    id TEXT PRIMARY KEY, payload TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS knowledge_links (
                    item_type TEXT NOT NULL, item_id TEXT NOT NULL, subject TEXT NOT NULL,
                    PRIMARY KEY (item_type, item_id, subject));
                CREATE INDEX IF NOT EXISTS knowledge_subjects ON knowledge_links(subject, item_type);
                CREATE TABLE IF NOT EXISTS knowledge_sessions (
                    session_id TEXT NOT NULL, item_type TEXT NOT NULL, item_id TEXT NOT NULL,
                    recorded_at TEXT NOT NULL, PRIMARY KEY (session_id, item_type, item_id));
                CREATE TABLE IF NOT EXISTS knowledge_source_locations (
                    source_id TEXT NOT NULL, path TEXT NOT NULL,
                    PRIMARY KEY (source_id, path));
                CREATE TABLE IF NOT EXISTS knowledge_summaries (
                    subject TEXT NOT NULL, revision INTEGER NOT NULL, payload TEXT NOT NULL,
                    created_at TEXT NOT NULL, PRIMARY KEY(subject, revision));
            ''')
            for table in ('knowledge_blobs', 'knowledge_sources', 'knowledge_notes', 'knowledge_summaries'):
                for action in ('UPDATE', 'DELETE'):
                    db.execute(f'''CREATE TRIGGER IF NOT EXISTS {table}_{action.lower()}_guard
                        BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT, 'knowledge_immutable'); END''')
            db.commit()
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _subjects(topics, stock_keys):
        topics, stocks = _list(topics, 'topics'), _list(stock_keys, 'stock_keys')
        if any(not re.fullmatch(r'(KR|US):[A-Za-z0-9._-]+', s) for s in stocks):
            raise ValueError('knowledge_invalid_stock_key')
        return ['topic:' + t for t in topics] + stocks

    @staticmethod
    def _subject(subject):
        _text(subject, 'subject', limit=500)
        if not (subject.startswith('topic:') and subject[6:].strip()) and not re.fullmatch(r'(KR|US):[A-Za-z0-9._-]+', subject):
            raise ValueError('knowledge_invalid_subject')
        return subject

    @staticmethod
    def _attach(db, item_type, item_id, subjects, session_id):
        for subject in subjects:
            db.execute('INSERT OR IGNORE INTO knowledge_links VALUES (?, ?, ?)', (item_type, item_id, subject))
        if session_id:
            db.execute('INSERT OR IGNORE INTO knowledge_sessions VALUES (?, ?, ?, ?)',
                       (session_id, item_type, item_id, _now()))

    @staticmethod
    def _require(db, table, ids):
        for item_id in ids:
            if db.execute('SELECT 1 FROM ' + table + ' WHERE id=?', (item_id,)).fetchone() is None:
                raise ValueError('knowledge_missing_reference:' + item_id)

    def ingest(self, title, text='', file_path=None, source_kind='document', source_uri=None,
               as_of=None, topics=None, stock_keys=None, session_id=None):
        """Preserve provided bytes/text; repeats reuse content and add subject/session links."""
        _text(title, 'title', limit=1000)
        _text(source_kind, 'source_kind', limit=100)
        _text(source_uri, 'source_uri', optional=True, limit=4000)
        _text(as_of, 'as_of', optional=True, limit=100)
        _text(session_id, 'session_id', optional=True, limit=500)
        subjects = self._subjects(topics, stock_keys)
        if not isinstance(text, str) or len(text) > MAX_TEXT_CHARS:
            raise ValueError('knowledge_invalid_text')
        original_name, original_path = None, None
        if file_path is not None:
            path = Path(_text(file_path, 'file_path', limit=4000)).resolve()
            if path.suffix.lower() not in FILE_TYPES or any(p.lower() in BLOCKED_PARTS for p in path.parts):
                raise ValueError('knowledge_disallowed_source_file')
            if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
                raise ValueError('knowledge_source_file_missing_or_too_large')
            with path.open('rb') as handle:
                raw = handle.read(MAX_FILE_BYTES + 1)
            if len(raw) > MAX_FILE_BYTES:
                raise ValueError('knowledge_source_file_too_large')
            original_name, original_path = path.name, str(path)
            if not text and path.suffix.lower() in TEXT_TYPES:
                text = raw.decode('utf-8-sig')
                if len(text) > MAX_TEXT_CHARS:
                    raise ValueError('knowledge_invalid_text')
        else:
            _text(text, 'text')
            raw = text.encode('utf-8')
        if not raw:
            raise ValueError('knowledge_empty_source')
        payload = dict(title=title, text=text, source_kind=source_kind, source_uri=source_uri,
                       as_of=as_of, sha256=_sha(raw), byte_length=len(raw), original_name=original_name)
        # The same source moved to a new path keeps its identity and original bytes.
        item_id = 'ks_' + _sha(_json(payload).encode())
        with self._write() as db:
            existing = db.execute('SELECT 1 FROM knowledge_sources WHERE id=?', (item_id,)).fetchone()
            db.execute('INSERT OR IGNORE INTO knowledge_blobs VALUES (?, ?)', (payload['sha256'], raw))
            if not existing:
                db.execute('INSERT INTO knowledge_sources VALUES (?, ?, ?)',
                           (item_id, _json(payload), _now()))
            self._attach(db, 'source', item_id, subjects, session_id)
            if original_path:
                db.execute('INSERT OR IGNORE INTO knowledge_source_locations VALUES (?, ?)', (item_id, original_path))
        result = self.source(item_id)
        result.update(verified=True, idempotent=bool(existing), supplied_path=original_path)
        return result

    def append_note(self, kind, body, author, topics=None, stock_keys=None, source_ids=None,
                    references=None, session_id=None, supersedes=None, resolves=None, as_of=None):
        """Record actual supplied statements, not operational decisions or execution."""
        if kind not in KINDS or author not in AUTHORS:
            raise ValueError('knowledge_invalid_note_kind_or_author')
        _text(body, 'body')
        _text(session_id, 'session_id', optional=True, limit=500)
        _text(as_of, 'as_of', optional=True, limit=100)
        _text(supersedes, 'supersedes', optional=True, limit=100)
        source_ids, resolves = _list(source_ids, 'source_ids'), _list(resolves, 'resolves')
        subjects = self._subjects(topics, stock_keys)
        references = [] if references is None else references
        if not isinstance(references, list) or len(references) > 100:
            raise ValueError('knowledge_invalid_references')
        for ref in references:
            if not isinstance(ref, dict) or set(ref) - {'kind', 'id', 'label'} or not {'kind', 'id'} <= ref.keys():
                raise ValueError('knowledge_invalid_reference')
            for key, value in ref.items():
                _text(value, 'reference_' + key, limit=4000)
        payload = dict(kind=kind, body=body, author=author, source_ids=source_ids, references=references,
                       supersedes=supersedes, resolves=resolves, as_of=as_of)
        # Different subjects can mean different judgments even with identical wording.
        item_id = 'kn_' + _sha(_json([payload, subjects]).encode())
        with self._write() as db:
            self._require(db, 'knowledge_sources', source_ids)
            self._require(db, 'knowledge_notes', resolves + ([supersedes] if supersedes else []))
            for target in resolves + ([supersedes] if supersedes else []):
                prior = json.loads(db.execute('SELECT payload FROM knowledge_notes WHERE id=?', (target,)).fetchone()[0])
                if prior['author'] != author:
                    raise ValueError('knowledge_cannot_close_another_authors_statement')
            for target in resolves:
                item = json.loads(db.execute('SELECT payload FROM knowledge_notes WHERE id=?', (target,)).fetchone()[0])
                if item['kind'] not in OPEN_KINDS:
                    raise ValueError('knowledge_target_is_not_open_question_or_action')
            existing = db.execute('SELECT 1 FROM knowledge_notes WHERE id=?', (item_id,)).fetchone()
            if not existing:
                db.execute('INSERT INTO knowledge_notes VALUES (?, ?, ?)', (item_id, _json(payload), _now()))
            self._attach(db, 'note', item_id, subjects, session_id)
        result = self.note(item_id)
        result.update(verified=True, idempotent=bool(existing))
        return result

    @staticmethod
    def _item(db, item_type, row, include_text=True, preview=False):
        payload = json.loads(row['payload'])
        if item_type == 'source' and not include_text:
            original = payload.pop('text', '')
            payload['text_preview'] = original[:4000]
            payload['text_length'] = len(original)
            payload['text_truncated'] = len(original) > 4000
        if item_type == 'note' and preview:
            original = payload['body']
            payload['body'] = original[:4000]
            payload['body_length'] = len(original)
            payload['body_truncated'] = len(original) > 4000
        payload.update(id=row['id'], item_type=item_type, created_at=row['created_at'],
                       subjects=[r[0] for r in db.execute('SELECT subject FROM knowledge_links WHERE item_type=? AND item_id=? ORDER BY subject', (item_type, row['id']))],
                       session_ids=[r[0] for r in db.execute('SELECT session_id FROM knowledge_sessions WHERE item_type=? AND item_id=? ORDER BY recorded_at', (item_type, row['id']))])
        if item_type == 'source':
            payload['original_paths'] = [r[0] for r in db.execute('SELECT path FROM knowledge_source_locations WHERE source_id=? ORDER BY path', (row['id'],))]
        return payload

    def source(self, source_id, include_text=True, offset=0, limit=12000):
        with self._read() as db:
            row = db.execute('SELECT * FROM knowledge_sources WHERE id=?', (source_id,)).fetchone() if db else None
            if row is None:
                raise ValueError('knowledge_source_not_found')
            result = self._item(db, 'source', row, include_text)
            if include_text:
                result['text'], result['text_page'] = _text_page(result['text'], offset, limit)
            raw = db.execute('SELECT raw FROM knowledge_blobs WHERE sha256=?', (result['sha256'],)).fetchone()
            if raw is None or _sha(raw[0]) != result['sha256']:
                raise ValueError('knowledge_source_integrity_failure')
            result['verified'] = True
            result['mime_type'], _ = _source_media(result)
            result['mime_basis'] = 'original_filename_extension' if result['original_name'] else 'provided_utf8_text'
            return result

    def export_source_bytes(self, source_id):
        """Return verified originals; the caller chooses an explicitly requested destination."""
        result = self.source(source_id, include_text=False)
        with self._read() as db:
            raw = db.execute('SELECT raw FROM knowledge_blobs WHERE sha256=?', (result['sha256'],)).fetchone()[0]
        if _sha(raw) != result['sha256']:
            raise ValueError('knowledge_source_integrity_failure')
        return raw

    def export_source(self, source_id):
        """Materialize an explicit export in a fixed cache; never overwrite or execute."""
        source = self.source(source_id, include_text=False)
        raw = self.export_source_bytes(source_id)
        mime_type, suffix = _source_media(source)
        directory = self.path.parent / '01.원본내보내기'
        if any(part.lower() in BLOCKED_PARTS for part in directory.parts) or _linked(directory):
            raise ValueError('knowledge_unsafe_export_directory')
        directory.mkdir(exist_ok=True)
        if _linked(directory) or directory.resolve() != directory:
            raise ValueError('knowledge_unsafe_export_directory')
        target = directory / (source['sha256'] + suffix)
        if _linked(target) or target.resolve() != target:
            raise ValueError('knowledge_unsafe_export_target')
        created = False
        identity = None
        try:
            try:
                handle = target.open('xb')
            except FileExistsError:
                # A duplicate reuses only the same complete bytes. A partial write
                # from another process is a visible conflict, never overwritten.
                if not target.is_file() or target.stat().st_size != len(raw):
                    raise ValueError('knowledge_export_conflict_or_incomplete') from None
            else:
                created = True
                with handle:
                    identity = os.fstat(handle.fileno())
                    handle.write(raw)
                    handle.flush()
                    os.fsync(handle.fileno())
            if _linked(target) or target.resolve() != target or target.stat().st_size != len(raw):
                raise ValueError('knowledge_unsafe_or_incomplete_export')
            if _sha(target.read_bytes()) != source['sha256']:
                raise ValueError('knowledge_export_conflict_or_incomplete')
        except BaseException:
            if created and identity is not None and target.exists() and not _linked(target):
                current = target.stat()
                if (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino):
                    target.unlink()
            raise
        return dict(source_id=source_id, path=str(target), filename=target.name,
                    original_name=source['original_name'], mime_type=mime_type,
                    mime_basis=source['mime_basis'], sha256=source['sha256'], byte_length=len(raw),
                    verified=True, idempotent=not created, source=source,
                    action='exported_original_bytes_not_executed', database_modified=False)

    def note(self, note_id, offset=0, limit=12000):
        with self._read() as db:
            row = db.execute('SELECT * FROM knowledge_notes WHERE id=?', (note_id,)).fetchone() if db else None
            if row is None:
                raise ValueError('knowledge_note_not_found')
            result = self._item(db, 'note', row)
            result['body'], result['body_page'] = _text_page(result['body'], offset, limit)
            return result

    def save_summary(self, subject, body, expected_revision, note_ids=None, source_ids=None, next_step=None):
        """Append a synthesis with optimistic concurrency; prior versions remain readable."""
        self._subject(subject)
        _text(body, 'body', limit=12000)
        _text(next_step, 'next_step', optional=True, limit=4000)
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError('knowledge_invalid_revision')
        notes, sources = _list(note_ids, 'note_ids'), _list(source_ids, 'source_ids')
        if not notes and not sources:
            raise ValueError('knowledge_summary_requires_evidence')
        payload = dict(body=body, note_ids=notes, source_ids=sources, next_step=next_step, author='ai',
                       status='synthesis_not_user_adoption')
        encoded = _json(payload)
        with self._write() as db:
            self._require(db, 'knowledge_notes', notes)
            self._require(db, 'knowledge_sources', sources)
            last = db.execute('SELECT * FROM knowledge_summaries WHERE subject=? ORDER BY revision DESC LIMIT 1', (subject,)).fetchone()
            revision = last['revision'] if last else 0
            if last and last['payload'] == encoded and expected_revision in (revision, revision - 1):
                return dict(payload, subject=subject, revision=revision, created_at=last['created_at'], verified=True, idempotent=True)
            if expected_revision != revision:
                raise ValueError('knowledge_revision_conflict')
            created_at = _now()
            db.execute('INSERT INTO knowledge_summaries VALUES (?, ?, ?, ?)', (subject, revision + 1, encoded, created_at))
        return dict(payload, subject=subject, revision=revision + 1, created_at=created_at, verified=True, idempotent=False)

    def summaries(self, subject):
        self._subject(subject)
        with self._read() as db:
            rows = list(db.execute('SELECT * FROM knowledge_summaries WHERE subject=? ORDER BY revision DESC', (subject,))) if db else []
        return [dict(json.loads(r['payload']), subject=subject, revision=r['revision'], created_at=r['created_at']) for r in rows]

    def summary_history(self, subject, offset=0, limit=30):
        """Newest-first bounded previews; exact revisions open via summary_read."""
        self._subject(subject)
        _page(offset, limit)
        with self._read() as db:
            if db is None:
                rows, total = [], 0
            else:
                total = db.execute('SELECT count(*) FROM knowledge_summaries WHERE subject=?', (subject,)).fetchone()[0]
                rows = db.execute('SELECT * FROM knowledge_summaries WHERE subject=? ORDER BY revision DESC LIMIT ? OFFSET ?',
                                  (subject, limit, offset)).fetchall()
        items = []
        for row in rows:
            payload = json.loads(row['payload'])
            body, next_step = payload['body'], payload['next_step']
            items.append(dict(subject=subject, revision=row['revision'], created_at=row['created_at'],
                              author=payload['author'], status=payload['status'],
                              body_preview=body[:2000], body_length=len(body), body_truncated=len(body) > 2000,
                              next_step_preview=next_step[:1000] if next_step else None,
                              next_step_truncated=bool(next_step and len(next_step) > 1000),
                              note_count=len(payload['note_ids']), source_count=len(payload['source_ids']),
                              sha256=_sha(row['payload'].encode('utf-8'))))
        return dict(subject=subject, items=items, total=total, offset=offset,
                    next_offset=offset + limit if offset + limit < total else None,
                    order='revision_desc', authority='AI_synthesis_history_not_user_adoption')

    def summary_read(self, subject, revision, offset=0, limit=12000):
        """Read one immutable version; paginated body retains exact stored text."""
        self._subject(subject)
        if type(revision) is not int or revision < 1:
            raise ValueError('knowledge_invalid_revision')
        with self._read() as db:
            row = db.execute('SELECT * FROM knowledge_summaries WHERE subject=? AND revision=?',
                             (subject, revision)).fetchone() if db else None
        if row is None:
            raise ValueError('knowledge_summary_not_found')
        result = json.loads(row['payload'])
        result['body'], result['body_page'] = _text_page(result['body'], offset, limit)
        result.update(subject=subject, revision=revision, created_at=row['created_at'],
                      sha256=_sha(row['payload'].encode('utf-8')), mode='exact_saved_revision')
        return result

    def search(self, query='', stock_key=None, topic=None, offset=0, limit=30):
        """Literal text search with exact stock/topic filters, no fuzzy entity assumptions."""
        _page(offset, limit)
        if not isinstance(query, str) or len(query) > 500:
            raise ValueError('knowledge_invalid_query')
        subjects = self._subjects([topic] if topic else [], [stock_key] if stock_key else [])
        rows = []
        with self._read() as db:
            if db:
                for kind, row in self.search_backend.search_rows(db, query=query, subjects=subjects):
                    rows.append(self._item(db, kind, row, include_text=False, preview=True))
        rows.sort(key=lambda r: (r['created_at'], r['id']), reverse=True)
        count = len(rows)
        return dict(items=rows[offset:offset + limit], total=count, offset=offset,
                    next_offset=offset + limit if offset + limit < count else None)

    def context(self, stock_key=None, topic=None, offset=0, limit=50, unresolved_offset=0, next_steps_offset=0):
        """Relevant original history + current synthesis; timestamps never imply freshness."""
        if not stock_key and not topic:
            raise ValueError('knowledge_context_requires_subject')
        _page(offset, limit)
        _page(unresolved_offset, limit)
        _page(next_steps_offset, limit)
        subjects = self._subjects([topic] if topic else [], [stock_key] if stock_key else [])
        history = self.search(stock_key=stock_key, topic=topic, offset=offset, limit=limit)
        current = {}
        for subject in subjects:
            versions = self.summary_history(subject, limit=1)
            if versions['items']:
                current[subject] = self.summary_read(subject, versions['items'][0]['revision'])
        related_topics = set()
        unresolved, next_steps = [], []
        with self._read() as db:
            if db:
                for subject in subjects:
                    related_topics.update(row[0] for row in db.execute('''
                        SELECT DISTINCT b.subject FROM knowledge_links a JOIN knowledge_links b
                        ON a.item_type=b.item_type AND a.item_id=b.item_id
                        WHERE a.subject=? AND b.subject LIKE 'topic:%' AND b.subject!=?
                    ''', (subject, subject)))
                # Evaluate state from the full relevant history, never just the returned page.
                candidates = {}
                closed = set()
                for row in db.execute('SELECT * FROM knowledge_notes ORDER BY created_at'):
                    payload = json.loads(row['payload'])
                    closed.update(payload['resolves'])
                    if payload['supersedes']:
                        closed.add(payload['supersedes'])
                    if payload['kind'] in OPEN_KINDS:
                        item = self._item(db, 'note', row, preview=True)
                        if all(s in item['subjects'] for s in subjects):
                            candidates[row['id']] = item
                active = [item for key, item in candidates.items() if key not in closed]
                unresolved = [item for item in active if item['kind'] != 'next_step']
                next_steps = [item for item in active if item['kind'] == 'next_step']
        def page_info(items, start):
            return dict(total=len(items), offset=start, next_offset=start + limit if start + limit < len(items) else None)
        return dict(subjects=subjects, found=bool(history['total'] or current), summaries=current,
                    related_topics=sorted(related_topics),
                    history=history, unresolved=unresolved[unresolved_offset:unresolved_offset + limit],
                    unresolved_page=page_info(unresolved, unresolved_offset),
                    next_steps=next_steps[next_steps_offset:next_steps_offset + limit],
                    next_steps_page=page_info(next_steps, next_steps_offset),
                    read_at=_now(), freshness='saved_history_not_live_validation',
                    authority='Statements and AI summaries are evidence, never orders, fills or adopted plans.')
