"""Private Toss-only KR/US observations with per-series checkpoints and no orders."""
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from invest_agent.trading.contracts import digest
from invest_agent.trading.market import check_bars, validate_profile, usable_tail
from invest_agent.trading.market_provider import completed_sessions
from invest_agent.trading.runner import atomic_write
from invest_agent.trading.store import instance_lock
from toss_candles import daily_series
from daily_launch import capped_sessions
from toss_universe import discover
from market_quality import complete_cap, error_disposition
import market_scope

COLLECTOR_ID = 'toss_openapi_kr_us_v1'


def failure_code(exc):
    # Only expose known local validation codes, never arbitrary response bodies.
    message = str(exc)
    if message.startswith('market_') or message in (
        'stale_or_invalid_last_session', 'no_completed_session_bars', 'duplicate_or_unordered_dates',
        'completed_sessions_unavailable', 'not_equity', 'Invalid candle envelope',
        'Unexpected candle currency', 'Invalid candle value',
        'Conflicting overlapping candle pages', 'Candle cursor does not progress',
        'Candle cursor does not move backwards', 'Candle page budget exhausted before history boundary',
    ):
        return message
    return type(exc).__name__


def collector_version():
    folder = Path(__file__).parent
    return digest([folder.joinpath(name).read_text(encoding='utf-8') for name in (
        'hybrid_market.py', 'toss_universe.py', 'toss_candles.py',
        'toss_market_client.py', 'toss_api.py', 'market_quality.py', 'market_lifecycle.json', 'daily_launch.py', 'market_scope.py', 'market_exclusions.json', 'automatic_scope.py',
    )])


def make_collector(root):
    from invest_agent.trading.morning import CollectorUnavailableError
    from toss_market_client import TossMarketClient
    client = None

    def callback(request, evaluated_at):
        nonlocal client
        actual = datetime.now(timezone.utc)
        markets = set(request.get('markets', [])) | {
            row['market'] for row in request.get('universe', []) + request.get('additional_universe', [])
        }
        if any(completed_sessions(market, actual)[-1] != completed_sessions(market, evaluated_at)[-1]
               for market in markets):
            raise CollectorUnavailableError('Completed sessions changed; start a fresh morning run')
        if request.get('reuse_us_from_run'):
            raise CollectorUnavailableError('Yahoo-era US seed reuse is not valid for the Toss-only collector')
        if client is None:
            client = TossMarketClient()
        return collect(request, {}, Path(root) / 'market-cache', client, now=evaluated_at,
                       refresh=request.get('refresh_market', False))

    callback.collector_id = COLLECTOR_ID
    callback.collector_version = collector_version()
    return callback


def collect(config, account, cache_root, get, *, now=None, refresh=False,
            prior_series=None, prior_source=None):
    now = now or datetime.now(timezone.utc)
    validate_profile(config['profile'])
    root = Path(cache_root).resolve()
    with instance_lock(root):
        return _collect(config, account, root, get, now, refresh, prior_series or {}, prior_source)


def _collect(config, account, root, get, now, refresh, prior_series, prior_source):
    scope_policy = market_scope.load()
    if config.get('universe_mode') == 'exchange_listings':
        universe, listings = discover(
            get, config['markets'], config.get('limit_per_exchange'),
            cache_root=root / 'universe', now=now, exclusions=scope_policy,
        )
    else:
        universe, listings = config['universe'], None
    unique = {(row['market'], row['symbol']): dict(row) for row in universe}
    if len(unique) != len(universe):
        raise ValueError('Duplicate configured market identity')
    for row in config.get('additional_universe', []):
        key = (row['market'], row['symbol'])
        unique[key] = {**row, **unique.get(key, {})}
    for account_row in account.get('accounts', []):
        for position in account_row.get('positions', []):
            if position.get('market') in ('KR', 'US') and position.get('quote_symbol') and position.get('benchmark'):
                unique.setdefault(
                    (position['market'], position['quote_symbol']),
                    {'market': position['market'], 'symbol': position['quote_symbol'],
                     'provider_symbol': position.get('provider_symbol'), 'benchmark': position['benchmark']},
                )
    universe, scope_exclusions = market_scope.partition(list(unique.values()), scope_policy)
    scope_exclusions.extend(r for meta in (listings or {}).values() for r in meta.get('approved_exclusions',[]))
    if not universe and not listings:
        raise ValueError('Explicit universe must not be empty')
    if any(row['market'] not in ('KR', 'US') for row in universe):
        raise ValueError('Unsupported market')
    scope_errors = [
        {'scope': exchange, 'stage': 'listing', 'reason': row.get('reason', 'listing_unavailable'),
         'state': row.get('state'), 'batch_failures': row.get('batch_failures', [])}
        for exchange, row in (listings or {}).items() if row.get('state') != 'available'
    ]
    sessions = {}
    risk_markets = {row['market'] for row in config.get('risk_universe', [])}
    risk_markets.update(
        position['market'] for account_row in account.get('accounts', [])
        for position in account_row.get('positions', []) if position.get('market') in ('KR', 'US')
    )
    for market in sorted({row['market'] for row in universe} | risk_markets):
        try:
            sessions[market] = capped_sessions(completed_sessions(market, now), config.get('session_cutoff'))
        except (ValueError, KeyError, IndexError):
            scope_errors.append({'scope': market, 'stage': 'calendar', 'reason': 'completed_sessions_unavailable'})

    version = digest({
        'schema': 2,
        'normalizers': [
            Path(__file__).with_name('toss_candles.py').read_text(encoding='utf-8'),
            Path(__file__).with_name('toss_universe.py').read_text(encoding='utf-8'),
        ],
    })
    stats = {
        'state': 'running', 'requested': len(universe), 'completed': 0, 'received': 0, 'failed': 0,
        'cache_hits': 0, 'incremental_updates': 0, 'full_downloads': 0,
        'overlap_refreshes': 0, 'progress_write_failures': 0,
    }

    def progress():
        try:
            atomic_write(root / 'progress.json', json.dumps({
                **stats, 'updated_at': datetime.now(timezone.utc).isoformat(),
            }))
        except PermissionError:
            stats['progress_write_failures'] += 1

    progress()

    def read_series(market, symbol, benchmark=None, provider_symbol=None, *, raw=False, listing=None):
        if market_scope.match({'market':market,'symbol':symbol}, scope_policy):
            raise ValueError('approved_market_exclusion')
        if market not in sessions:
            raise ValueError('completed_sessions_unavailable')
        identity = {
            'market': market, 'symbol': symbol, 'provider_symbol': provider_symbol,
            'benchmark': benchmark, 'sessions': sessions[market],
            'provider': 'Toss raw' if raw else 'Toss adjusted',
        }
        key = digest({'version': version, **identity})
        path = root / 'series' / (key + '.json')
        if not refresh and path.is_file():
            try:
                cached = json.loads(path.read_text(encoding='utf-8'))
                observed = datetime.fromisoformat(cached['observed_at'])
                if (cached['key'] == key and digest(cached['payload']) == cached['hash']
                        and 0 <= (now - observed).total_seconds() <= 86400):
                    usable_tail(cached['payload']['bars'], sessions[market])
                    return cached['payload'], 'cache'
            except (OSError, ValueError, KeyError, TypeError):
                pass
        previous = prior_series.pop((market, symbol), None) if not raw and not refresh else None
        previous_bars = previous.get('bars', []) if isinstance(previous, dict) else []
        if isinstance(previous_bars, list):
            previous_bars = [bar for bar in previous_bars if bar.get('date') in sessions[market]]
        prior_dates = [bar.get('date') for bar in previous_bars] if isinstance(previous_bars, list) else []
        expected_basis = ('toss_index_points_observation_only' if symbol in ('^KS11', '^KQ11')
                          else 'toss_adjusted_ohlc_no_order_authority')
        identity_ok = (previous and previous.get('provider') == 'Toss OpenAPI candles'
                       and previous.get('market') == market and previous.get('symbol') == symbol
                       and previous.get('benchmark') == benchmark
                       and previous.get('provider_symbol') == (provider_symbol or
                           ('KOSPI' if symbol == '^KS11' else 'KOSDAQ' if symbol == '^KQ11' else
                            'SPY' if symbol == '^GSPC' else symbol.split('.')[0] if market == 'KR' else symbol))
                       and previous.get('price_basis') == expected_basis
                       and previous.get('currency') == ('KRW' if market == 'KR' else 'USD')
                       and (listing is None or all(previous.get(field) == listing.get(field)
                           for field in ('isin_code', 'listing_exchange_family')))
                       and prior_dates and prior_dates == sorted(set(prior_dates))
                       and prior_dates[-1] in sessions[market]
                       and all(day in sessions[market] for day in prior_dates))
        if identity_ok:
            try:
                check_bars(previous_bars, [day for day in sessions[market] if day <= prior_dates[-1]])
                full_at = datetime.fromisoformat(previous.get('collection', {}).get(
                    'full_history_collected_at') or previous['collected_at'])
                identity_ok = 0 <= (now - full_at).total_seconds() < 30 * 86400
            except (ValueError, KeyError, TypeError):
                identity_ok = False
        mode = 'full'
        if identity_ok:
            boundary = prior_dates[-1]
            gap = len([day for day in sessions[market] if day > boundary])
            recent = daily_series(get, symbol, market, sessions[market], benchmark=benchmark,
                                  provider_symbol=provider_symbol, adjusted=True,
                                  stop_on_or_before=boundary, page_size=min(200, max(32, gap + 32)))
            recent_by_date = {bar['date']: bar for bar in recent['bars']}
            old_by_date = {bar['date']: bar for bar in previous_bars}
            overlap = set(recent_by_date) & set(old_by_date)
            if boundary in overlap and len(overlap) >= min(5, len(old_by_date)) and all(
                    old_by_date[day] == recent_by_date[day] for day in overlap):
                merged = {**old_by_date, **recent_by_date}
                value = {**recent, 'bars': [merged[day] for day in sorted(merged)]}
                value.setdefault('collection', {}).update(mode='incremental', overlap_bars=len(overlap),
                    prior_source=prior_source, full_history_collected_at=full_at.isoformat(),
                    new_bars=sum(day not in old_by_date for day in recent_by_date))
                mode = 'incremental'
            else:
                mode = 'overlap_refresh'
        if mode != 'incremental':
            value = daily_series(get, symbol, market, sessions[market], benchmark=benchmark,
                                 provider_symbol=provider_symbol, adjusted=not raw)
            value.setdefault('collection', {}).update(mode=mode, full_history_collected_at=now.isoformat())
        if benchmark is not None and value.get('instrument_type') != 'EQUITY':
            raise ValueError('not_equity')
        value['benchmark'] = benchmark
        if not value['bars']:
            raise ValueError('no_completed_session_bars')
        usable_tail(value['bars'], sessions[market])
        value['collected_at'] = now.isoformat()
        atomic_write(path, json.dumps({
            'key': key, 'observed_at': now.isoformat(), 'payload': value, 'hash': digest(value),
        }))
        return value, mode

    benchmarks = {}
    benchmark_markets = {}
    for row in universe:
        prior = benchmark_markets.setdefault(row['benchmark'], row['market'])
        if prior != row['market']:
            raise ValueError('Benchmark cannot cross markets')
    for symbol, market in benchmark_markets.items():
        try:
            benchmarks[symbol] = read_series(market, symbol)[0]
        except (Exception, SystemExit) as exc:
            scope_errors.append({
                'scope': market, 'stage': 'benchmark', 'reason': 'benchmark_unavailable',
                'error_type': type(exc).__name__,
            })

    series, errors = [], []
    cap_version = collector_version()
    def read_equity(row):
        value, origin = read_series(row['market'], row['symbol'], row['benchmark'],
                                    row.get('provider_symbol'), listing=row)
        cap_fields = {}
        if row.get('quote_time_state') == 'not_provided':
            cap_key = digest({'row': row, 'session': sessions[row['market']][-1],
                              'version': cap_version})
            cap_path = root / 'cap-prices' / (cap_key + '.json')
            try:
                cached = json.loads(cap_path.read_text(encoding='utf-8')) if not refresh else {}
                if (cached.get('key') != cap_key or digest(cached['payload']) != cached['hash']
                        or not 0 <= (now - datetime.fromisoformat(cached['observed_at'])).total_seconds() <= 86400):
                    raise ValueError('invalid_cap_cache')
                cap_fields = cached['payload']
            except (OSError, ValueError, KeyError, TypeError):
                try:
                    cap_fields = complete_cap(get, row, sessions[row['market']])
                    if cap_fields.get('market_cap_quality') == 'completed_close_estimate':
                        atomic_write(cap_path, json.dumps({'key': cap_key, 'observed_at': now.isoformat(),
                                                          'payload': cap_fields, 'hash': digest(cap_fields)}))
                except (Exception, SystemExit) as exc:
                    cap_fields = {'market_cap': None, 'market_cap_as_of': None,
                                  'market_cap_quality': 'unavailable_completed_close',
                                  'market_cap_error': failure_code(exc)}
        return value, origin, cap_fields

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {
            pool.submit(read_equity, row): row for row in universe
        }
        for future in as_completed(futures):
            row = futures[future]
            try:
                value, origin, cap_fields = future.result()
                row.update(cap_fields)
                series.append({**row, **value})
                stats['received'] += 1
                stats['cache_hits'] += origin == 'cache'
                stats['incremental_updates'] += origin == 'incremental'
                stats['full_downloads'] += origin in ('full', 'overlap_refresh')
                stats['overlap_refreshes'] += origin == 'overlap_refresh'
            except (Exception, SystemExit) as exc:
                error = {
                    'market': row['market'], 'symbol': row['symbol'],
                    'reason': type(exc).__name__ + ': provider_unavailable_or_ineligible',
                    'error_code': failure_code(exc),
                }
                error['disposition'] = error_disposition(error, row, sessions.get(row['market'], [''])[-1])
                errors.append(error)
                stats['failed'] += 1
            stats['completed'] += 1
            progress()

    risk_series, risk_errors = [], []
    risk_targets = {
        (row['market'], row['symbol'], row.get('provider_symbol'))
        for row in config.get('risk_universe', [])
    }
    risk_targets.update(
        (position['market'], position['quote_symbol'], position.get('provider_symbol'))
        for account_row in account.get('accounts', []) for position in account_row.get('positions', [])
        if position.get('market') in ('KR', 'US') and position.get('quote_symbol')
    )
    for market, symbol, provider_symbol in sorted(risk_targets, key=lambda item: (item[0], item[1], item[2] or '')):
        try:
            risk_series.append(read_series(market, symbol, provider_symbol=provider_symbol, raw=True)[0])
        except (Exception, SystemExit) as exc:
            risk_errors.append({
                'market': market, 'symbol': symbol,
                'reason': type(exc).__name__ + ': raw_price_unavailable',
            })
    stats['state'] = 'finished'
    progress()
    result = {
        'scope_exclusions': scope_exclusions,
        'classification_refresh': config.get('classification_refresh'),
        'schema_version': 1,
        'source': 'live_public',
        'fetched_at': now.isoformat(),
        'collected_until': datetime.now(timezone.utc).isoformat(),
        'collection_mode': COLLECTOR_ID,
        'profile': config['profile'],
        'sessions': sessions,
        'series': sorted(series, key=lambda row: (row['market'], row['symbol'])),
        'risk_series': risk_series,
        'risk_errors': risk_errors,
        'benchmarks': benchmarks,
        'universe': universe,
        'errors': errors,
        'coverage': {
            'scope': config.get('scope', 'configured_universe'),
            'requested': len(universe),
            'received': len(series),
            'listings': listings,
            'scope_errors': scope_errors,
            'market_wide': bool(listings and not scope_errors and not errors
                                and not any(str(r.get('market_cap_quality', '')).startswith('unavailable') for r in universe)
                                and len(series) == len(universe)
                                and config.get('limit_per_exchange') is None),
        },
        'collection_stats': stats,
        'providers': {'KR': 'Toss adjusted candles', 'US': 'Toss adjusted candles (SPY benchmark proxy)'},
    }
    return market_scope.apply_snapshot(result, scope_policy)
