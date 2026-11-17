"""Read exact adopted-plan drawing references without initializing any store.

M06 supplies an explicit read-only connection factory and a frozen-snapshot
decoder: ``functools.partial(analysis.snapshots.decode_saved, root=root)``.
The decoder resolves only pinned saved inputs without initializing stores.
No current holdings, latest chart, capture, migration or geometry is requested.
"""
from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import re

from . import digest, key
from .collaboration import Collaboration, KINDS, VERSION, identity, validate_plan


class _BorrowedConnection:
    """Keep plan_basis's nested reads in the caller's one read transaction."""
    def __init__(self, connection):
        self.connection = connection

    def execute(self, *args, **kwargs):
        return self.connection.execute(*args, **kwargs)

    def close(self):
        pass


def _read_only(*args, **kwargs):
    raise ValueError('adopted_refs_read_only')


def adopted_plan_refs(stock_key, *, root, connect, snapshot_reader):
    """Return active adoptions and their frozen drawing evidence for one stock.

    ``root`` is the explicit Markdown root. ``connect()`` supplies a SQLite
    connection opened read-only; ``snapshot_reader(payload, stored_sha256)``
    verifies and hydrates the immutable snapshot, including pinned references;
    pass ``functools.partial(analysis.snapshots.decode_saved, root=root)``.
    Both are mandatory so this entry point cannot infer an operating database.
    Missing/unsupported stores and broken evidence raise; an initialized store
    with no applicable adoption returns ``state='available', adopted_plans=[]``.
    Account payloads are used only by the supplied decoder for snapshot integrity
    and are never included in this drawing-reference projection.
    """
    if not isinstance(stock_key, str) or not re.fullmatch(r'(KR|US):[A-Z0-9.\-]{1,24}', stock_key):
        raise ValueError('exact_stock_key_required')
    if not callable(connect) or not callable(snapshot_reader):
        raise ValueError('adopted_refs_readers_required')
    root = Path(root)
    result = []
    with closing(connect()) as db:
        # Defense in depth for an accidentally writable injected connection.
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        # Require the current ledger even when it contains no matching cases.
        for table, columns in (
            ('analysis_cases', 'id,stock_key,payload'),
            ('analysis_items', 'id,case_id,kind,payload'),
            ('analysis_snapshots', 'id,case_id,payload,sha256'),
            ('analysis_collaboration_heads', 'case_id,revision'),
        ):
            db.execute('SELECT ' + columns + ' FROM ' + table + ' LIMIT 0')

        borrowed = _BorrowedConnection(db)

        def item(cid, rid):
            key(rid, 'it_')
            row = db.execute('SELECT payload FROM analysis_items WHERE case_id=? AND id=?', (cid, rid)).fetchone()
            return json.loads(row[0]) if row else None

        def snapshot(cid, sid):
            key(sid, 'sn_')
            row = db.execute('SELECT payload,sha256 FROM analysis_snapshots WHERE case_id=? AND id=?', (cid, sid)).fetchone()
            if row is None:
                raise ValueError('snapshot_not_found_for_analysis')
            stored = json.loads(row[0])
            if digest(stored) != row[1]:
                raise ValueError('snapshot_integrity_failed')
            value = snapshot_reader(*row)
            if value.get('id') != sid or value.get('case_id') != cid or value.get('stock_key') != stock_key:
                raise ValueError('adopted_refs_snapshot_identity_mismatch')
            return value

        reader = Collaboration(connect=lambda: borrowed, root=root, case=_read_only,
            snapshot=snapshot, item=item, rows=_read_only, snapshots=_read_only,
            capture=_read_only, links=_read_only)
        cases = db.execute('SELECT id,payload FROM analysis_cases WHERE stock_key=? ORDER BY rowid', (stock_key,)).fetchall()
        for cid, payload in cases:
            key(cid, 'an_')
            case = json.loads(payload)
            if case.get('id') != cid or case.get('stock_key') != stock_key:
                raise ValueError('adopted_refs_case_identity_mismatch')
            rows = []
            for rid, kind, raw in db.execute('SELECT id,kind,payload FROM analysis_items WHERE case_id=? ORDER BY rowid', (cid,)):
                record = json.loads(raw)
                if (record.get('id') != rid or record.get('kind') != kind or record.get('case_id') != cid):
                    raise ValueError('adopted_refs_record_identity_mismatch')
                if kind not in KINDS:
                    continue
                key(rid, 'it_')
                if record.get('contract_version') != VERSION:
                    raise ValueError('unsupported_collaboration_contract')
                if record.get('stock_key') != stock_key:
                    raise ValueError('adopted_refs_record_identity_mismatch')
                expected_path = root / 'judgments' / 'analyses' / cid / (rid + '.md')
                path = record.get('markdown_path')
                if (not isinstance(path, str) or (root / path).resolve() != expected_path.resolve()
                        or not expected_path.resolve().is_relative_to(root.resolve())):
                    raise ValueError('adopted_refs_markdown_path_mismatch')
                reader.verify(record)
                if kind == 'plan_decision' and (
                    record.get('action') not in ('adopt', 'revoke')
                    or record.get('principal') != 'browser'
                    or record.get('subject') != 'local_ui_confirmation'
                ):
                    raise ValueError('adopted_refs_invalid_adoption')
                rows.append(record)
            # A corrupt revoke must not turn broken evidence into an empty
            # successful result. Validate every historical decision against the
            # exact version that existed and was active at that point in time.
            known_plans, latest_plans, historical_active = {}, {}, {}
            for record in rows:
                if record['kind'] == 'plan_version':
                    known_plans[record['id']] = record
                    latest_plans[record['plan_id']] = record['id']
                    continue
                if record['kind'] != 'plan_decision':
                    continue
                rid = record['plan_record_id']
                if rid not in known_plans:
                    raise ValueError('adopted_refs_decision_plan_missing')
                plan, snap, _ = reader.plan_basis(cid, rid, require_adopted=False)
                if plan.get('stock_key') != stock_key or plan.get('identity') != identity(snap):
                    raise ValueError('adopted_refs_plan_identity_mismatch')
                if digest(plan['plan']) != plan['plan_sha256']:
                    raise ValueError('adopted_refs_plan_integrity_failed')
                for field in ('plan_id', 'plan_version', 'plan_sha256', 'snapshot_id', 'snapshot_sha256'):
                    if record.get(field) != plan.get(field):
                        raise ValueError('adopted_refs_adoption_basis_mismatch')
                validate_plan(plan['plan'], snap)
                pid = plan['plan_id']
                if record['action'] == 'adopt':
                    if latest_plans.get(pid) != rid or historical_active.get(pid) == rid:
                        raise ValueError('adopted_refs_invalid_adoption_history')
                    historical_active[pid] = rid
                else:
                    if historical_active.get(pid) != rid:
                        raise ValueError('adopted_refs_invalid_revocation_history')
                    historical_active.pop(pid)
            for decision in reader.active(rows).values():
                plan, snap, adoption = reader.plan_basis(cid, decision['plan_record_id'], require_adopted=True)
                if (plan.get('contract_version') != VERSION or plan.get('case_id') != cid
                        or plan.get('stock_key') != stock_key or plan.get('identity') != identity(snap)):
                    raise ValueError('adopted_refs_plan_identity_mismatch')
                if digest(plan['plan']) != plan['plan_sha256']:
                    raise ValueError('adopted_refs_plan_integrity_failed')
                for field in ('plan_id', 'plan_version', 'plan_sha256', 'snapshot_id', 'snapshot_sha256'):
                    if adoption.get(field) != plan.get(field):
                        raise ValueError('adopted_refs_adoption_basis_mismatch')
                validate_plan(plan['plan'], snap)
                frozen = []
                for condition in plan['plan']['conditions']:
                    ref = condition['drawing_ref']
                    if ref is None:
                        continue
                    drawing = next(d for d in snap['workspace']['state']['drawings'] if d['id'] == ref['drawing_id'])
                    frozen.append(dict(condition_id=condition['id'], drawing_ref=deepcopy(ref), drawing=deepcopy(drawing)))
                result.append(dict(
                    analysis_key=cid, stock_key=stock_key, plan_record_id=plan['id'],
                    plan_id=plan['plan_id'], plan_version=plan['plan_version'], plan_sha256=plan['plan_sha256'],
                    snapshot_id=snap['id'], snapshot_sha256=plan['snapshot_sha256'],
                    adoption_record_id=adoption['id'], identity=deepcopy(plan['identity']),
                    drawing_refs=[deepcopy(row['drawing_ref']) for row in frozen], frozen_drawings=frozen,
                    snapshot_basis=dict(captured_at=snap['captured_at'],
                        workspace_revision=snap['workspace']['revision'],
                        workspace_saved_at=snap['workspace'].get('updated_at'),
                        chart=deepcopy(snap['chart']), context=deepcopy(snap['context'])),
                ))
    return dict(contract_version=VERSION, stock_key=stock_key, state='available', adopted_plans=result)
