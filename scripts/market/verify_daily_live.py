"""Explicit, bounded live Toss -> canonical OHLCV DB verification (no launch/alerts).

Run with the configured engine Python. Source responses and pinned references are
retained under runtime; the dashboard's latest run pointer is never changed.
"""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from workbench_paths import ROOT
from atomic_io import atomic_write
from market.prices import PriceStore, default_path, freeze_snapshot, hydrate_snapshot, digest, normalized
from market.providers.toss_client import TossMarketClient
from market.toss_collection import collect
from invest_agent.trading.market_provider import completed_sessions


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))


def verify(path):
    manifest = json.loads(path.read_text(encoding='utf-8'))
    store = PriceStore(manifest['database'])
    checked = 0
    for item in manifest['references']:
        bars = store.read(item['reference'])
        assert digest(normalized(bars)) == item['numeric_sha256'], item['label']
        checked += 1
    return {'verified_references': checked, 'database': str(store.path),
            'collection_completed': manifest.get('state') == 'completed'}


def run(folder):
    folder = folder.resolve()
    folder.mkdir(parents=True, exist_ok=True)
    if (folder / 'manifest.json').exists():
        raise ValueError('Use a fresh output folder; evidence is immutable')
    store = PriceStore(default_path())
    now = datetime.now(timezone.utc)
    config = json.loads((ROOT / 'config.json').read_text(encoding='utf-8-sig'))
    targets = [('KR', '005930.KS', 'KR7005930003'), ('KR', '000660.KS', 'KR7000660001'),
               ('US', 'AAPL', 'US0378331005'), ('US', 'NVDA', 'US67066G1040')]
    refs, universe = [], []
    for market, symbol, isin in targets:
        seed = store.daily_seed({'market': market, 'symbol': symbol, 'isin_code': isin,
            'price_basis': 'toss_adjusted_ohlc_no_order_authority'}, '2026-10-15')
        if not seed:
            raise ValueError('Missing saved baseline: ' + symbol)
        refs.append({'label': 'baseline/' + symbol, 'reference': seed['bars_ref'],
                     'numeric_sha256': digest(normalized(seed['bars']))})
        universe.append({k: seed[k] for k in ('market', 'symbol', 'provider_symbol', 'benchmark',
                                             'isin_code', 'listing_exchange_family') if k in seed})
    request = {'profile': config['profile'], 'universe': universe, 'ohlcv_path': str(store.path)}
    with closing(store.connect()) as db:
        request['seed_revision'] = db.execute('SELECT max(id) FROM price_revisions').fetchone()[0]
    manifest = {'state': 'running', 'started_at': now.isoformat(), 'database': str(store.path), 'universe': universe,
                'initial_revision': request['seed_revision'], 'references': refs, 'stages': []}
    save(folder / 'manifest.json', manifest)
    events = []
    event_lock = threading.Lock()

    def observe(event):
        with event_lock:
            events.append(event)
            save(folder / 'wire.json', events)

    try:
        client = TossMarketClient(observer=observe)
    except (Exception, SystemExit) as exc:
        manifest.update(state='failed', failure={'stage': 'client_initialization', 'type': type(exc).__name__})
        save(folder / 'manifest.json', manifest)
        raise
    latest = max(completed_sessions(m, now)[-1] for m in ('KR', 'US'))
    cutoffs = list(dict.fromkeys(['2026-10-16', '2026-10-19', latest]))
    for cutoff in cutoffs:
        stage = folder / ('stage-' + cutoff)
        before = len(events)
        result = collect({**request, 'session_cutoff': cutoff}, {}, stage, client, now=now)
        # Persist the same thin format used by the normal collector, then hydrate it.
        snapshot_path = stage / 'snapshot.json'
        frozen = freeze_snapshot(result, snapshot_path, store)
        save(snapshot_path, frozen)
        restored = hydrate_snapshot(frozen)
        if result['errors'] or result['coverage']['scope_errors'] or len(restored['series']) != len(universe):
            manifest.update(state='failed', failure={'cutoff': cutoff, 'errors': result['errors'],
                'scope_errors': result['coverage']['scope_errors'], 'received': len(restored['series'])})
            save(folder / 'manifest.json', manifest)
            raise ValueError('Live collection incomplete; inspect manifest and wire evidence')
        rows = []
        for row in restored['series'] + list(restored['benchmarks'].values()):
            assert row['bars'][-1]['date'] == result['sessions'][row['market']][-1]
            ref = row['bars_ref']
            assert normalized(row['bars']) == normalized(store.read(ref))
            refs.append({'label': cutoff + '/' + row['symbol'], 'reference': ref,
                         'numeric_sha256': digest(normalized(row['bars']))})
            rows.append({'symbol': row['symbol'], 'last': row['bars'][-1],
                         'reference': ref, 'collection': row['collection']})
        stage_info = {'cutoff': cutoff, 'stats': result['collection_stats'], 'rows': rows,
                      'wire_requests': len(events) - before}
        # A same-stage resume must hydrate checkpoints without another GET.
        before_resume = len(events)
        resumed = collect({**request, 'session_cutoff': cutoff}, {}, stage, client, now=now)
        assert len(events) == before_resume
        assert resumed['collection_stats']['cache_hits'] == len(universe)
        assert {r['symbol']: normalized(r['bars']) for r in resumed['series']} == {
            r['symbol']: normalized(r['bars']) for r in result['series']}
        stage_info['resume_no_network'] = True
        manifest['stages'].append(stage_info)
        with closing(store.connect()) as db:
            request['seed_revision'] = db.execute('SELECT max(id) FROM price_revisions').fetchone()[0]
        manifest['references'] = refs
        save(folder / 'manifest.json', manifest)
        verify(folder / 'manifest.json')  # Includes every old reference after each append/correction.
        print(json.dumps({'cutoff': cutoff, 'stats': result['collection_stats'],
                          'wire_requests': stage_info['wire_requests']}, ensure_ascii=False), flush=True)
    manifest.update(state='completed', completed_at=datetime.now(timezone.utc).isoformat())
    save(folder / 'manifest.json', manifest)
    manifest['verification'] = verify(folder / 'manifest.json')
    save(folder / 'manifest.json', manifest)
    return manifest['verification']


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--verify-only', type=Path)
    args = parser.parse_args()
    if args.verify_only:
        print(json.dumps(verify(args.verify_only), ensure_ascii=False))
    elif args.output:
        print(json.dumps(run(args.output), ensure_ascii=False))
    else:
        parser.error('--output or --verify-only is required')
