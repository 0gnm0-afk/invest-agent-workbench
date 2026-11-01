"""One owner for immutable account observations and API/MCP refresh receipts.

No broker transport or credentials live here. A failed refresh never changes the
timestamp of a prior successful snapshot. Independent direct MCP calls must join
begin/complete to participate in coordination.
"""
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import secrets
import sqlite3

from workbench_paths import ROOT


def packed(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(packed(value).encode()).hexdigest()


class AccountStore:
    def __init__(self, path=None):
        self.path = Path(path or ROOT/'runtime/accounts.sqlite3').resolve()

    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=30)
        db.execute('PRAGMA journal_mode=WAL')
        db.executescript('''
            CREATE TABLE IF NOT EXISTS account_objects(sha256 TEXT PRIMARY KEY,payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS account_queries(
                provider TEXT NOT NULL,request_id TEXT NOT NULL,token TEXT NOT NULL,
                started_at TEXT NOT NULL,finished_at TEXT,state TEXT NOT NULL,
                snapshot_sha256 TEXT,raw_sha256 TEXT,metadata TEXT,
                PRIMARY KEY(provider,request_id));
            CREATE UNIQUE INDEX IF NOT EXISTS one_account_refresh
                ON account_queries(provider) WHERE state='pending';
        ''')
        return db

    def save(self, value):
        # Auth material must never be stored, even through an MCP caller.
        def validate(item):
            if isinstance(item, dict):
                for k,v in item.items():
                    if k.lower().replace('-','_') in {'authorization','access_token','refresh_token','app_secret',
                            'app_key','secret','client_secret','token'}:
                        raise ValueError('account_credentials_forbidden')
                    validate(v)
            elif isinstance(item, list):
                for v in item: validate(v)
        validate(value)
        key = digest(value)
        with closing(self.connect()) as db, db:
            db.execute('INSERT OR IGNORE INTO account_objects VALUES (?,?)',(key,packed(value)))
        return {'schema':1,'path':str(self.path),'sha256':key}

    def read(self, ref):
        with closing(self.connect()) as db:
            row = db.execute('SELECT payload FROM account_objects WHERE sha256=?',(ref['sha256'],)).fetchone()
        if not row or digest(json.loads(row[0])) != ref['sha256']:
            raise ValueError('account_reference_integrity_failed')
        return json.loads(row[0])

    def begin(self, provider, request_id, origin):
        if provider not in ('nh','meritz') or origin not in ('api','mcp'):
            raise ValueError('account_query_owner_invalid')
        if not isinstance(request_id,str) or not 1 <= len(request_id) <= 200:
            raise ValueError('account_request_id_invalid')
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT state,metadata FROM account_queries WHERE provider=? AND request_id=?',
                             (provider,request_id)).fetchone()
            if old:
                return {'state':old[0], 'request_id':request_id, 'execute':False,
                        'result':json.loads(old[1]) if old[1] else None}
            active = db.execute("SELECT request_id,started_at FROM account_queries WHERE provider=? AND state='pending'",(provider,)).fetchone()
            if active:
                # Do not silently expire/retry an uncertain external operation.
                return {'state':'pending','request_id':active[0], 'started_at':active[1], 'execute':False}
            token = secrets.token_urlsafe(32)
            db.execute('INSERT INTO account_queries VALUES (?,?,?,?,?,?,?,?,?)',
                       (provider,request_id,token,datetime.now(timezone.utc).isoformat(),None,'pending',None,None,packed({'origin':origin})))
            return {'state':'pending','request_id':request_id,'execute':True,'lease':token}

    def complete(self, provider, request_id, lease, *, metadata, snapshot=None, raw=None):
        state = metadata['query_state']
        if state not in ('received','partial','failed'):
            raise ValueError('account_query_state_invalid')
        if snapshot is not None:
            label=str(snapshot.get('provider','')).lower()
            if not (label.startswith('nh') if provider=='nh' else label.startswith(('meritz','메리츠'))):
                raise ValueError('account_snapshot_provider_mismatch')
            if (not isinstance(snapshot.get('accounts'),list) or not snapshot.get('as_of')
                    or datetime.fromisoformat(snapshot['as_of'].replace('Z','+00:00')).tzinfo is None
                    or state == 'failed'):
                raise ValueError('account_snapshot_shape_invalid')
            for account in snapshot['accounts']:
                if not isinstance(account.get('positions'),list) or not account.get('alias'):
                    raise ValueError('account_snapshot_shape_invalid')
            aliases=[a['alias'] for a in snapshot['accounts']]
            if len(aliases)!=len(set(aliases)):
                raise ValueError('account_snapshot_duplicate_alias')
        elif state != 'failed':
            raise ValueError('account_snapshot_required')
        # Reject an invalid lease before accepting account payloads.
        with closing(self.connect()) as db:
            row = db.execute("SELECT token,state,started_at,metadata FROM account_queries WHERE provider=? AND request_id=?",(provider,request_id)).fetchone()
        if not row or row[:2] != (lease,'pending'):
            raise ValueError('account_query_lease_invalid')
        snap_ref = self.save(snapshot) if snapshot is not None else None
        raw_ref = self.save(raw) if raw is not None else None
        result = {'attempted_at':row[2],**metadata,'origin':json.loads(row[3])['origin'],
                  'provider':provider,'request_id':request_id,'snapshot_ref':snap_ref,'raw_ref':raw_ref}
        # Also validate/sanitize metadata, without a second price/account payload.
        self.save(result)
        with closing(self.connect()) as db,db:
            changed = db.execute("UPDATE account_queries SET finished_at=?,state=?,snapshot_sha256=?,raw_sha256=?,metadata=? "
                       "WHERE provider=? AND request_id=? AND token=? AND state='pending'",
                       (datetime.now(timezone.utc).isoformat(),state,(snap_ref or {}).get('sha256'),
                        (raw_ref or {}).get('sha256'),packed(result),provider,request_id,lease)).rowcount
            if changed != 1: raise ValueError('account_query_lease_invalid')
        return result

    def latest(self, provider):
        with closing(self.connect()) as db:
            row = db.execute("SELECT metadata FROM account_queries WHERE provider=? AND snapshot_sha256 IS NOT NULL "
                             "ORDER BY started_at DESC LIMIT 1",(provider,)).fetchone()
        return json.loads(row[0]) if row else None

    def latest_attempt(self, provider):
        with closing(self.connect()) as db:
            row=db.execute('SELECT metadata FROM account_queries WHERE provider=? AND finished_at IS NOT NULL '
                           'ORDER BY started_at DESC LIMIT 1',(provider,)).fetchone()
        return json.loads(row[0]) if row else None


def hydrate(value):
    if isinstance(value,dict) and set(value) == {'account_view_ref'}:
        ref=value['account_view_ref'];snapshot=AccountStore(ref['snapshot_ref']['path']).read(ref['snapshot_ref'])
        return {**ref['metadata'],'accounts':snapshot['accounts']}
    if isinstance(value,dict) and set(value) == {'account_store_ref'}:
        ref = value['account_store_ref']
        return AccountStore(ref['path']).read(ref)
    return value
