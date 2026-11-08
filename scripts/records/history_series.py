"""Store new history-series metadata in the existing records DB.

Keep the collector's checked JSON envelope and every retry version unchanged.
Price values stay in OHLCV; original files containing inline bars are never
removed or overwritten by this path. The caller owns the collection lock.
"""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import re

from records.checkpoints import KIND, MAX_RAW, _regular, _reference, _verify
from records.store import RecordStore, default_path, _has_prices


def _packet(raw, path, market):
    if not isinstance(raw, bytes) or len(raw) > MAX_RAW:
        raise ValueError('history_series_packet_invalid')
    packet = json.loads(raw)
    if not isinstance(packet, dict) or set(packet) != {'sha256', 'data'}:
        raise ValueError('history_series_packet_invalid')
    value = packet['data']
    if not isinstance(value, dict):
        raise ValueError('history_series_packet_invalid')
    if value.get('bars') or _has_prices(value):
        raise ValueError('history_series_inline_source_preserved')
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode('utf-8')
    symbol = value.get('symbol')
    if (hashlib.sha256(encoded).hexdigest() != packet['sha256']
            or not isinstance(symbol, str) or not symbol
            or hashlib.sha256(symbol.encode()).hexdigest() != path.stem
            or value.get('market') != market
            or value.get('state') not in ('received', 'empty', 'failed')):
        raise ValueError('history_series_packet_invalid')
    if 'bars_ref' in value:
        if ('bars' in value or not isinstance(value['bars_ref'], dict) or not value['bars_ref']
                or not isinstance(value.get('ohlcv_path'), str) or not value['ohlcv_path']
                or not re.fullmatch('[0-9a-f]{64}', str(value.get('data_sha256', '')))):
            raise ValueError('history_series_price_reference_invalid')
    elif value.get('bars') != [] or value['state'] not in ('empty', 'failed'):
        raise ValueError('history_series_price_reference_required')


def save_series_checkpoint(path, raw, *, root=None):
    """Return False only for standalone/no-DB file compatibility.

    A pre-existing thin file is retired only after both versions are preserved
    and normal original-path reads return the new exact bytes. An inline legacy
    file requires its existing price-recipe migration before a DB-backed retry.
    """
    path = Path(path).absolute()
    if root is None:
        runtime = next((p for p in path.parent.parent.parents if p.name == 'runtime'), None)
        if runtime is None:
            return False
        root = runtime.parent
    root = Path(root).resolve()
    runtime = root / 'runtime'
    if not path.is_relative_to(runtime):
        return False
    parts = path.relative_to(runtime).parts
    if len(parts) != 4 or parts[0] not in ('kr-history', 'us-history') or parts[2] != 'series':
        return False
    database = default_path(root)
    if not database.is_file():
        return False
    if not re.fullmatch(r'[0-9a-f]{64}\.json', path.name):
        raise ValueError('history_series_path_invalid')
    path = _regular(path, runtime)
    market = 'KR' if parts[0] == 'kr-history' else 'US'
    _packet(raw, path, market)
    previous = path.read_bytes() if path.is_file() else None
    if previous is not None:
        _packet(previous, path, market)
    store = RecordStore(database)
    with closing(store.connect(write=True)) as db, db:
        db.execute('BEGIN IMMEDIATE')
        if previous is not None:
            store._insert_version(db, path, previous, kind=KIND, root=root,
                                  metadata={'family': 'history_series', 'source': 'prior_physical'})
        version = store._insert_version(db, path, raw, kind=KIND, root=root,
                                        metadata={'family': 'history_series', 'source': 'collector'})
        reference = _reference(store, db, version)
    _verify(store, reference, path, raw)
    if previous is not None:
        _regular(path, runtime)
        if path.read_bytes() != previous:
            raise ValueError('history_series_source_changed')
        path.unlink()
    return True
