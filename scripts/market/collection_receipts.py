"""C9 collection-only receipts. The digest pins run.json, not this receipt."""
from datetime import datetime
from pathlib import Path
import re

from atomic_io import checked, load


ROLE = 'collection-receipt.json'
MARKETS = ('KR', 'US')


def collection_receipt(run, run_sha256, *, targets=None):
    """Match DailyLaunch.collection_receipt counts, dates and worst-market status.

    Runs that failed before producing quality have zero observed completions;
    they remain failed rather than becoming a successful empty collection.
    """
    if not isinstance(run_sha256, str) or not re.fullmatch('[0-9a-f]{64}', run_sha256):
        raise ValueError('collection_run_hash_invalid')
    dates = (run.get('data_times') or {}).get('session_dates') or {}
    targets = targets or run.get('target_trade_date') or dates
    if set(targets) != set(MARKETS):
        raise ValueError('collection_targets_missing')
    for date in targets.values():
        datetime.strptime(date, '%Y-%m-%d')
    counts = (run.get('quality') or {}).get('markets')
    if counts is None:
        if run.get('state') != 'failed':
            raise ValueError('collection_counts_missing')
        counts = {m: dict(requested=0, received=0, failed=0) for m in MARKETS}
    completed, failed, statuses = {}, {}, []
    for market in MARKETS:
        row = counts[market]
        requested, received, missing = (row[k] for k in ('requested', 'received', 'failed'))
        if any(type(n) is not int or n < 0 for n in (requested, received, missing)) or received + missing != requested:
            raise ValueError('collection_counts_invalid')
        if dates.get(market) is not None:
            datetime.strptime(dates[market], '%Y-%m-%d')
        completed[market] = {'requested': requested, 'completed': received, 'failed': missing}
        failed[market] = missing
        statuses.append('failed' if received == 0 else 'complete'
                        if received == requested and dates.get(market) == targets[market] else 'partial')
    mode = run.get('mode')
    if mode not in ('live', 'recover', 'replay'):
        raise ValueError('collection_mode_missing')
    return {'target_trade_date': dict(targets), 'completed': completed, 'failed': failed,
            'status': max(statuses, key={'complete': 0, 'partial': 1, 'failed': 2}.get),
            'provenance': {'provider': 'toss', 'run_id': Path(run['output']).name, 'mode': mode},
            'sha256': run_sha256}


def read_collection_receipt(output):
    """Read and verify a published C9 receipt without collection or database writes."""
    output = Path(output)
    manifest = load(output / 'manifest.json')
    hashes = manifest['output_hashes']
    run = load(checked(output / 'run.json', hashes['run.json'], {}))
    receipt = load(checked(output / ROLE, hashes[ROLE], {}))
    if receipt != collection_receipt(run, hashes['run.json']):
        raise ValueError('collection_receipt_mismatch')
    return receipt
