"""Shared saved inputs and numeric/identity helpers for completed-close alert rules."""
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path


def number(value):
    if isinstance(value, bool) or value is None:
        raise ValueError('missing_number')
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise ValueError('invalid_number')
    return value


def stamp(value):
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('timezone_required')
    return result


def equity(p):
    return p.get('instrument_class_hint') != 'CMA_note_from_broker_label' and str(
        p.get('asset_type') or p.get('product_type') or p.get('security_type') or 'stock').lower() in (
        'stock', 'equity', 'etf', 'adr', 'common_stock', 'commonshare', 'depositary_receipt')


def indicators(rows):
    """Wilder ATR14 seeded by 14 TRs requiring a preceding close."""
    bars, trs, atr, prior = [], [], None, None
    for row in rows:
        day = row['date']
        if datetime.strptime(day, '%Y-%m-%d').date().isoformat() != day or (bars and day <= bars[-1]['date']):
            raise ValueError('invalid_bar_dates')
        high, low, close = (number(row[k]) for k in ('high', 'low', 'close'))
        if not low <= close <= high:
            raise ValueError('invalid_ohlc')
        if prior is not None:
            tr = max(high-low, abs(high-prior), abs(low-prior))
            trs.append(tr)
            if len(trs) == 14:
                atr = sum(trs)/14
            elif len(trs) > 14:
                atr = (atr*13+tr)/14
        values = [r['close'] for r in bars[-4:]]+[close]
        ten = [r['close'] for r in bars[-9:]]+[close]
        bars.append(dict(date=day, high=high, low=low, close=close, atr=atr,
                         ma5=sum(values)/5 if len(values) == 5 else None,
                         ma10=sum(ten)/10 if len(ten) == 10 else None))
        prior = close
    return bars


def packed(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True)


def holding_series(path, expected_hash, keys):
    """Read selected series from our indent=2 immutable snapshot without loading the universe."""
    with path.open('rb') as stream:
        if not expected_hash or hashlib.file_digest(stream, 'sha256').hexdigest() != expected_hash:
            raise ValueError('saved_artifact_hash_mismatch')
    # New metadata snapshots are small and hydrate only the requested holdings.
    with path.open(encoding='utf-8-sig') as stream:
        for line in stream:
            if line.rstrip() == '  "ohlcv_store": {':
                from market.prices import PriceStore
                value = json.loads(path.read_text(encoding='utf-8-sig'))
                store = PriceStore(value['ohlcv_store']['path'])
                rows = [s for s in value['series'] if s['market']+':'+s['symbol'] in keys]
                result = {s['market']+':'+s['symbol']: {**s, 'bars': store.read(s['bars_ref'])} for s in rows}
                if len(result) != len(rows):
                    raise ValueError('duplicate_market_series')
                return result
            if line.rstrip() == '  "series": [':
                break
    found, ended, item, result = False, False, [], {}
    with path.open(encoding='utf-8-sig') as stream:
        for line in stream:
            if not found:
                if line.rstrip() == '  "series": [':
                    found = True
                elif line.rstrip() in ('  "series": [],', '  "series": []'):
                    found = ended = True
                    break
                continue
            if line.rstrip() in ('  ],', '  ]'):
                ended = True
                break
            item.append(line)
            if line.rstrip() in ('    },', '    }'):
                row = json.loads(''.join(item).rstrip().rstrip(','))
                key = row['market']+':'+row['symbol']
                if key in keys:
                    if key in result:
                        raise ValueError('duplicate_market_series')
                    result[key] = row
                item = []
    if not found or not ended or item:
        raise ValueError('snapshot_series_format_unconfirmed')
    return result


def saved_inputs(root, market_run, keys=None, *, account_failed=False):
    """Verified saved run + saved accounts + selected series; never refresh providers."""
    from accounts import account_view
    from atomic_io import load_bom as load, checked_saved as checked
    import tempfile
    root = Path(root)
    run_dir = Path(market_run).resolve()
    if not run_dir.is_relative_to((root/'runtime').resolve()):
        raise ValueError('market_run_outside_runtime')
    manifest = load(run_dir/'manifest.json')
    run = checked(run_dir/'run.json', manifest['output_hashes'].get('run.json'))
    source = Path(run['market_source'])/'snapshot.json'
    with tempfile.TemporaryDirectory(prefix='alert-account-', dir=root/'runtime') as folder:
        bundle = account_view(load(root/'config.json'), Path(folder), refresh=False,
                              saved_dashboard_root=root/'runtime/dashboard')
    if account_failed:
        bundle = {**bundle, 'query_state': 'failed'}
    if keys is None:
        keys = {p['market']+':'+p.get('quote_symbol',p['symbol'])
                for a in bundle['accounts'] for p in a['positions'] if equity(p)}
    stocks = holding_series(source, manifest['input_hashes'].get(str(source)), keys)
    return bundle, stocks
