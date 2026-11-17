"""Reuse identical analysis inputs; preserve every existing immutable ID and hash."""
from contextlib import closing
import json
from . import digest, packed


def decode_saved(payload, expected, *, root):
    """Verify and hydrate a fixed input without initializing or writing stores.

    This internal reader preserves historical account contents exactly. Session
    callers apply M08's projection after verifying the original snapshot hash.
    Use ``partial(decode_saved, root=root)`` as the M06 snapshot reader.
    """
    value = json.loads(payload)
    if digest(value) != expected:
        raise ValueError('snapshot_integrity_failed')
    if 'chart_price_ref' in value:
        from market.prices import PriceStore
        from market.history.chart import aggregate
        ref = value.pop('chart_price_ref')
        value['chart']['bars'] = aggregate(
            PriceStore(ref['path'], root=root).read(ref['reference']), ref['frame'])
        if digest(value['chart']) != value['component_hashes']['chart']:
            raise ValueError('snapshot_chart_integrity_failed')
    if set(value.get('accounts', {})) == {'account_store_ref'}:
        from accounts.saved import ReadOnlyAccountStore
        from runtime_paths import local_runtime_path
        ref = value['accounts']['account_store_ref']
        value['accounts'] = ReadOnlyAccountStore(local_runtime_path(ref['path'], root=root)).read(ref)
    return value


def input_hash(value):
    # All fields matter, including context/identity/market state and source dates.
    # Only the generated capture envelope is excluded.
    return digest({k: v for k, v in value.items() if k not in ('id', 'captured_at')})


def save_once(connect, value, stored, decode):
    fingerprint = input_hash(value)
    with closing(connect()) as db, db:
        # Cross-process serialization covers lookup AND insert.
        db.execute('BEGIN IMMEDIATE')
        db.execute("""CREATE TABLE IF NOT EXISTS analysis_snapshot_inputs (
            snapshot_id TEXT PRIMARY KEY, case_id TEXT NOT NULL, input_sha256 TEXT NOT NULL)""")
        db.execute('CREATE INDEX IF NOT EXISTS analysis_snapshot_input_lookup ON analysis_snapshot_inputs(case_id,input_sha256)')
        # Index legacy snapshots once without rewriting any original body/hash/ID.
        for sid, payload, sha in db.execute("""SELECT s.id,s.payload,s.sha256
                FROM analysis_snapshots s LEFT JOIN analysis_snapshot_inputs i ON s.id=i.snapshot_id
                WHERE s.case_id=? AND i.snapshot_id IS NULL ORDER BY s.rowid""", (value['case_id'],)).fetchall():
            prior = decode(payload, sha)
            db.execute('INSERT INTO analysis_snapshot_inputs VALUES (?,?,?)',
                       (sid, value['case_id'], input_hash(prior)))
        row = db.execute("""SELECT s.payload,s.sha256 FROM analysis_snapshot_inputs i
            JOIN analysis_snapshots s ON s.id=i.snapshot_id
            WHERE i.case_id=? AND i.input_sha256=? ORDER BY i.rowid LIMIT 1""",
            (value['case_id'], fingerprint)).fetchone()
        if row:
            prior = decode(*row)
            if input_hash(prior) != fingerprint:
                raise ValueError('snapshot_input_integrity_failed')
            return prior
        db.execute('INSERT INTO analysis_snapshots VALUES (?,?,?,?)',
                   (value['id'], value['case_id'], packed(stored), digest(stored)))
        db.execute('INSERT INTO analysis_snapshot_inputs VALUES (?,?,?)',
                   (value['id'], value['case_id'], fingerprint))
        return value
