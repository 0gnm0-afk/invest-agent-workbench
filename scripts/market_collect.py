"""Private market-only collection and evidence; never imports account collectors."""
import argparse
import gzip
import hashlib
import json
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from hybrid_market import collect, collector_version
from invest_agent.trading.contracts import digest
from invest_agent.trading.market import check_bars, usable_tail
from invest_agent.trading.market_provider import completed_sessions
from invest_agent.trading.runner import atomic_write
from invest_agent.trading.store import instance_lock
from toss_market_client import TossMarketClient
from market_quality import error_disposition, history_usability


def write_json(path, value):
    atomic_write(path, json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))


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
    complete = snapshot['coverage']['market_wide'] and not quality_issues
    dispositions = [{**r, 'disposition': error_disposition(r, universe[(r['market'], r['symbol'])],
                     snapshot['sessions'][r['market']][-1])} for r in snapshot['errors']]
    cap_issues = [{'market': r['market'], 'symbol': r['symbol'],
                   'state': r.get('market_cap_quality'), 'error': r.get('market_cap_error')}
                  for r in snapshot['series'] if str(r.get('market_cap_quality', '')).startswith('unavailable')]
    unexpected = (len(snapshot['coverage']['scope_errors'])
                  + sum(r['disposition']['state'] == 'collection_failure' for r in dispositions)
                  + sum(bool(r['invalid_sessions'] or r['non_sessions']) for r in quality)
                  + sum(bool(r['error']) for r in cap_issues))
    complete = complete and not cap_issues
    exceptions = bool(dispositions or quality_issues or cap_issues or any(r['short_history'] for r in quality))
    return {'state': 'succeeded' if complete else 'partial', 'exchanges': exchanges,
            'processing_state': 'failed' if unexpected else 'completed_with_exceptions' if exceptions else 'completed',
            'data_complete': complete,
            'scope_basis': 'toss_provided_active_common_stock',
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
            self.handle.write(json.dumps(event, ensure_ascii=False) + '\n')
            self.handle.flush()
            self.counts[(event['path'], str(event['status']))] += 1
            self.limits.setdefault(event['path'], set()).add(json.dumps(event['headers'], sort_keys=True))

    def summary(self):
        return {'requests': sum(self.counts.values()),
                'by_endpoint_status': [{'path': p, 'status': s, 'count': n} for (p, s), n in sorted(self.counts.items())],
                'rate_headers': {p: [json.loads(v) for v in sorted(values)] for p, values in self.limits.items()}}


def run(root, config_path=None, prior_source=None):
    root = Path(root).resolve()
    runtime = Path(__file__).resolve().parents[1] / 'runtime'
    if not root.is_relative_to(runtime.resolve()):
        raise ValueError('Market outputs must stay inside private runtime')
    with instance_lock(root):
        frozen_path = root / 'frozen.json'
        actual = datetime.now(timezone.utc)
        version = digest({'collector': collector_version(),
                          'market_runner': Path(__file__).read_text(encoding='utf-8')})
        if frozen_path.exists():
            frozen = json.loads(frozen_path.read_text(encoding='utf-8'))
            if (config_path is not None or prior_source is not None or frozen['version'] != version
                    or digest(frozen['config']) != frozen['config_hash']):
                raise ValueError('Resume requires unchanged code and frozen config; use a new instance')
            for market, sessions in frozen['sessions'].items():
                if completed_sessions(market, actual)[-1] != sessions[-1]:
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
            prior = None
            if prior_source is not None:
                prior = Path(prior_source).resolve()
                if not prior.is_relative_to(runtime.resolve()):
                    raise ValueError('Prior source must stay inside private runtime')
                manifest = json.loads((prior/'manifest.json').read_text(encoding='utf-8'))
                with (prior/'snapshot.json').open('rb') as stream:
                    actual_hash = hashlib.file_digest(stream, 'sha256').hexdigest()
                if manifest.get('snapshot.json') != actual_hash:
                    raise ValueError('Prior snapshot hash mismatch')
            frozen = {'config': config, 'config_hash': digest(config), 'version': version,
                      'evaluated_at': actual.isoformat(),
                      'sessions': {m: completed_sessions(m, actual) for m in config['markets']},
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
                with prior_path.open('rb') as stream:
                    actual_hash = hashlib.file_digest(stream, 'sha256').hexdigest()
                if actual_hash != frozen['prior_snapshot_sha256']:
                    raise ValueError('Prior snapshot changed after freeze')
                previous = json.loads(prior_path.read_text(encoding='utf-8'))
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
                               if prior_rows else None)
            write_json(attempt / 'snapshot.json', snapshot)
            reloaded = json.loads((attempt / 'snapshot.json').read_text(encoding='utf-8'))
            if digest(reloaded) != digest(snapshot):
                raise ValueError('snapshot_roundtrip_failure')
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
