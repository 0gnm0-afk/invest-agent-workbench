"""Private market-only collection and evidence; never imports account collectors."""

if not __package__:
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from workbench_paths import ROOT
import argparse
import gzip
import hashlib
import json
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from market.toss_collection import collect, collector_version
from launch.daily import capped_sessions
from invest_agent.trading.contracts import digest
from invest_agent.trading.market import check_bars, usable_tail
from invest_agent.trading.market_provider import completed_sessions
from atomic_io import atomic_write, write_json
from atomic_io import load
from market.prices import PriceStore, freeze_snapshot, default_path
from invest_agent.trading.store import instance_lock
from market.providers.toss_client import TossMarketClient
from market.quality import error_disposition, history_usability


def audit(snapshot):
    universe = {(r['market'], r['symbol']): r for r in snapshot['universe']}
    received = {(r['market'], r['symbol']): r for r in snapshot['series']}
    failed = {(r['market'], r['symbol']) for r in snapshot['errors']}
    if (len(universe) != len(snapshot['universe']) or len(received) != len(snapshot['series'])
            or len(failed) != len(snapshot['errors']) or set(received) & failed
            or set(received) | failed != set(universe)):
        raise ValueError('identity_partition_integrity_failure')
    exchanges = {}
    for exchange, listing in snapshot['coverage']['listings'].items():
        keys = {k for k, r in universe.items() if r['listing_exchange_family'] == exchange}
        exchanges[exchange] = {'requested': len(keys), 'received': len(keys & set(received)),
                               'failed': len(keys & failed), 'listing': listing}
    quality = []
    for row in snapshot['series'] + list(snapshot['benchmarks'].values()):
        sessions = snapshot['sessions'][row['market']]
        bars = row['bars']
        tail = usable_tail(bars, sessions)
        dates = [b['date'] for b in bars]
        missing = [d for d in sessions if dates and dates[0] <= d <= dates[-1] and d not in dates]
        invalid = []
        for bar in bars:
            try:
                check_bars([bar], [bar['date']])
            except ValueError:
                invalid.append(bar['date'])
        quality.append({'market': row['market'], 'symbol': row['symbol'], 'bars': len(bars),
                        'first': dates[0], 'last': dates[-1], 'usable_tail': len(tail),
                        'missing_sessions': missing, 'invalid_sessions': invalid,
                        'non_sessions': sorted(set(dates) - set(sessions)),
                        'usability': history_usability(bars, sessions, snapshot['profile'][row['market']]),
                        'short_history': len(bars) < len(sessions)})
    quality_issues = sum(bool(r['missing_sessions'] or r['invalid_sessions'] or r['non_sessions']) for r in quality)
    dispositions = [{**r, 'disposition': error_disposition(r, universe[(r['market'], r['symbol'])],
                     snapshot['sessions'][r['market']][-1])} for r in snapshot['errors']]
    cap_issues = [{'market': r['market'], 'symbol': r['symbol'],
                   'state': r.get('market_cap_quality'), 'error': r.get('market_cap_error')}
                  for r in snapshot['series'] if str(r.get('market_cap_quality', '')).startswith('unavailable')]
    unexpected = (len(snapshot['coverage']['scope_errors'])
                  + sum(r['disposition']['state'] == 'collection_failure' for r in dispositions)
                  + sum(bool(r['invalid_sessions'] or r['non_sessions']) for r in quality))
    # OHLCV completion and optional capitalization/classification are separate.
    listing_scope = snapshot['coverage'].get('listings') or {}
    scope_complete = bool(listing_scope) and all(r.get('state') == 'available' and r.get('limit') is None
                                               for r in listing_scope.values())
    complete = scope_complete and not snapshot['coverage']['scope_errors'] and not failed and not quality_issues
    exceptions = bool(dispositions or quality_issues or cap_issues or any(r['short_history'] for r in quality))
    return {'state': 'succeeded' if complete else 'partial', 'exchanges': exchanges,
            'processing_state': 'failed' if unexpected else 'completed_with_exceptions' if exceptions else 'completed',
            'data_complete': complete,
            'metadata_complete': not cap_issues,
            'scope_basis': 'user_approved_active_market_scope' if snapshot.get('scope_exclusions') else (
                'toss_provided_active_common_stock_and_us_depositary_receipts'
                if any('DEPOSITARY_RECEIPT' in m.get('listing_types', {})
                       for m in snapshot['coverage']['listings'].values())
                else 'toss_provided_active_common_stock'),
            'approved_exclusions': snapshot.get('scope_exclusions', []),
            'exceptions': dispositions, 'market_cap_issues': cap_issues,
            'unexpected_failures': unexpected,
            'market_cap_completed_close_estimates': sum(r.get('market_cap_quality') == 'completed_close_estimate'
                                                        for r in snapshot['universe']),
            'quality_issue_series': quality_issues, 'quality': quality,
            'short_history_series': sum(r['short_history'] for r in quality),
            'identity_partition_verified': True, 'scope_errors': snapshot['coverage']['scope_errors'],
            'collection_stats': snapshot['collection_stats'],
            'completed_sessions': {m: {'first': d[0], 'last': d[-1], 'count': len(d)}
                                   for m, d in snapshot['sessions'].items()}}


class Evidence:
    def __init__(self, path):
        self.handle = gzip.open(path, 'wt', encoding='utf-8')
        self.lock = threading.Lock()
        self.counts = Counter()
        self.limits = {}

    def __call__(self, event):
        with self.lock:
            receipt = {k: v for k, v in event.items() if k != 'payload'}
            if 'payload' in event:
                receipt['payload_sha256'] = digest(event['payload'])
            self.handle.write(json.dumps(receipt, ensure_ascii=False) + '\n')
            self.handle.flush()
            self.counts[(event['path'], str(event['status']))] += 1
            self.limits.setdefault(event['path'], set()).add(json.dumps(event['headers'], sort_keys=True))

    def summary(self):
        return {'requests': sum(self.counts.values()),
                'by_endpoint_status': [{'path': p, 'status': s, 'count': n} for (p, s), n in sorted(self.counts.items())],
                'rate_headers': {p: [json.loads(v) for v in sorted(values)] for p, values in self.limits.items()}}


def run(root, config_path=None, prior_source=None, *, session_cutoff=None, evaluated_at=None, project_root=None):
    root = Path(root).resolve()
    project_root = Path(project_root or ROOT).resolve()
    runtime = project_root / 'runtime'
    if not root.is_relative_to(runtime.resolve()):
        raise ValueError('Market outputs must stay inside private runtime')
    with instance_lock(root):
        frozen_path = root / 'frozen.json'
        actual = datetime.now(timezone.utc)
        evaluated = datetime.fromisoformat(evaluated_at) if evaluated_at else actual
        if evaluated.tzinfo is None or not 0 <= (actual-evaluated).total_seconds() <= 86400:
            raise ValueError('Invalid collection evaluation time')
        version = digest({'collector': collector_version(),
                          'market_runner': Path(__file__).read_text(encoding='utf-8')})
        if frozen_path.exists():
            frozen = json.loads(frozen_path.read_text(encoding='utf-8'))
            if (config_path is not None or prior_source is not None or frozen['version'] != version
                    or digest(frozen['config']) != frozen['config_hash']):
                raise ValueError('Resume requires unchanged code and frozen config; use a new instance')
            for market, sessions in frozen['sessions'].items():
                if capped_sessions(completed_sessions(market, actual), frozen['config'].get('session_cutoff'))[-1] != sessions[-1]:
                    raise ValueError('Completed session changed; use a new instance')
            if not 0 <= (actual - datetime.fromisoformat(frozen['evaluated_at'])).total_seconds() <= 86400:
                raise ValueError('Resume window expired; use a new instance')
        else:
            if config_path is None:
                raise ValueError('New collection requires --config')
            source = json.loads(Path(config_path).read_text(encoding='utf-8-sig'))
            config = {'profile': source['profile'], 'markets': ['KR', 'US'],
                      'scope': 'market_only_toss_provided_universe', 'universe_mode': 'exchange_listings',
                      'limit_per_exchange': None}
            config['ohlcv_path'] = str(default_path(root=project_root))
            seed_store = PriceStore(config['ohlcv_path'], root=project_root)
            if seed_store.meta('archive_import_complete',False):
                from contextlib import closing
                with closing(seed_store.connect()) as db:
                    config['seed_revision'] = db.execute('SELECT max(id) FROM price_revisions').fetchone()[0]
                prior_source = None
            if source.get('user_classification'):
                config['user_classification'] = source['user_classification']
            if source.get('classification_refresh'):
                config['classification_refresh'] = source['classification_refresh']
            if session_cutoff is not None:
                config['session_cutoff'] = session_cutoff
            prior = None
            if prior_source is not None:
                prior = Path(prior_source).resolve()
                if not prior.is_relative_to(runtime.resolve()):
                    raise ValueError('Prior source must stay inside private runtime')
                manifest = json.loads((prior/'manifest.json').read_text(encoding='utf-8'))
                from atomic_io import sha
                actual_hash = sha(prior/'snapshot.json')
                if manifest.get('snapshot.json') != actual_hash:
                    raise ValueError('Prior snapshot hash mismatch')
            frozen = {'config': config, 'config_hash': digest(config), 'version': version,
                      'evaluated_at': evaluated.isoformat(),
                      'sessions': {m: capped_sessions(completed_sessions(m, evaluated), session_cutoff) for m in config['markets']},
                      'prior_source': str(prior) if prior else None,
                      'prior_snapshot_sha256': actual_hash if prior else None}
            write_json(frozen_path, frozen)
        attempt = root / 'attempts' / actual.strftime('%Y%m%dT%H%M%S%fZ')
        attempt.mkdir(parents=True)
        evidence = Evidence(attempt / 'http.jsonl.gz')
        started = time.monotonic()
        summary = {'state': 'failed', 'started_at': actual.isoformat(), 'version': version,
                   'exchanges': {e: {'requested': 0, 'received': 0, 'failed': 0,
                                     'listed': None, 'state': 'not_started'}
                                 for e in ('KOSPI', 'NASDAQ', 'NYSE')}}
        try:
            client = TossMarketClient(observer=evidence)
            prior_rows = {}
            if frozen.get('prior_source'):
                prior_path = Path(frozen['prior_source'])/'snapshot.json'
                from atomic_io import sha
                actual_hash = sha(prior_path)
                if actual_hash != frozen['prior_snapshot_sha256']:
                    raise ValueError('Prior snapshot changed after freeze')
                previous = load(prior_path)
                if previous.get('profile') == frozen['config']['profile']:
                    prior_rows = {(row['market'], row['symbol']): row for row in
                                  previous.get('series', []) + list(previous.get('benchmarks', {}).values())
                                  if row.get('provider') == 'Toss OpenAPI candles'}
                del previous
            snapshot = collect(frozen['config'], {}, root / 'cache', client,
                               now=datetime.fromisoformat(frozen['evaluated_at']),
                               prior_series=prior_rows,
                               prior_source={'path': frozen['prior_source'],
                                             'sha256': frozen['prior_snapshot_sha256']}
                               if prior_rows else None, project_root=project_root)
            store = PriceStore(frozen['config']['ohlcv_path'], root=project_root)
            write_json(attempt / 'snapshot.json', freeze_snapshot(snapshot, attempt/'snapshot.json', store))
            reloaded = load(attempt / 'snapshot.json')  # Each referenced series verifies identity/count/hash.
            for error in snapshot['errors']:
                store.failure('collection', error.get('error_code') or error['reason'],
                              market=error['market'], symbol=error['symbol'], details=error)
            summary.update(audit(reloaded))
        except Exception as exc:
            summary['error_type'] = type(exc).__name__
            summary['error_code'] = str(exc) if str(exc).startswith('market_') else type(exc).__name__
            raise
        finally:
            evidence.handle.close()
            summary.update(http=evidence.summary(), elapsed_seconds=round(time.monotonic() - started, 3),
                           finished_at=datetime.now(timezone.utc).isoformat())
            write_json(attempt / 'summary.json', summary)
            manifest = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in attempt.iterdir() if p.is_file()}
            write_json(attempt / 'manifest.json', manifest)
            write_json(root / 'latest.json', {'attempt': str(attempt), 'state': summary['state']})
        return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--instance', type=Path, required=True)
    parser.add_argument('--config', type=Path)
    args = parser.parse_args()
    try:
        summary = run(args.instance, args.config)
    except (Exception, SystemExit) as exc:
        print(json.dumps({'state': 'failed', 'error_type': type(exc).__name__}))
        return 2
    print(json.dumps({k: v for k, v in summary.items() if k != 'quality'}, ensure_ascii=True))
    return 0 if summary['state'] == 'succeeded' else 2


if __name__ == '__main__':
    raise SystemExit(main())
