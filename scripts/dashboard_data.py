"""Read-only view adapter. No market collection. Cached sector and downtrend projections on verified saved inputs."""
import hashlib
import json
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat()


def write(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x', encoding='utf-8') as stream:
        json.dump(payload, stream, ensure_ascii=False, allow_nan=False)


def checked(path, expected):
    if not expected or sha(path) != expected:
        raise ValueError('saved_artifact_hash_mismatch:' + Path(path).name)
    return load(path)


def saved_market(root=ROOT, run_dir=None):
    """Default to latest attempt; explicit prior views require a separate daily failure banner."""
    if run_dir is None:
        run_dir = load(root / 'runtime/latest.json')['output']
    run_dir = Path(run_dir).resolve()
    if not run_dir.is_relative_to((root / 'runtime').resolve()):
        raise ValueError('latest_outside_private_runtime')
    manifest = load(run_dir / 'manifest.json')
    def artifact(name):
        return checked(run_dir / name, manifest['output_hashes'].get(name) or manifest['output_hashes'].get(name.replace('/', '\\')))
    run = artifact('run.json')
    result = {'state': run['state'], 'run_id': run_dir.name, 'processing_state': run['processing_state'],
              'data_times': run.get('data_times', {}), 'quality': run.get('quality', {}),
              'approved_exclusions': run.get('approved_exclusions', []), 'analysis_pending': run.get('analysis_pending', []),
              'finished_at': run.get('finished_at'), 'coverage': run.get('sector_coverage'),
              'entries': [], 'sectors': [], 'stocks': {}, 'errors': [], 'session_dates': {},
              'candidate_count': None, 'entry_candidate_count': None}
    review = {'markets': {'KR': {'entries': []}, 'US': {'entries': []}}}
    observation = {}
    try:
        review = artifact('priority-review.json')
        if review.get('schema') != 4 or review.get('strategy') != 'pdf_momentum':
            raise ValueError('current_pdf_watch_schema_required')
        result.update(session_dates=review['session_dates'], candidate_count=review['candidate_count'],
                      entry_candidate_count=review['entry_candidate_count'], limitations=review['limitations'])
        result['entries'] = [e for m in review['markets'].values() for e in m['entries']]
    except (OSError, ValueError, KeyError) as exc:
        result['errors'].append('screening_unavailable:' + type(exc).__name__)
    try:
        signals = artifact('sectors/signals.json')
        observation = artifact('sectors/observation.json')
        observations = {(s['market'], s['group_code']): s for s in observation['observations']}
        for s in signals['sectors']:
            o = observations.get((s['market'], s['group_code']), {})
            result['sectors'].append({k: s.get(k) for k in
                ('market', 'group_code', 'group_name', 'current', 'history', 'leaders', 'weak_contributors',
                 'transitions', 'candidates', 'state_since')} | {'horizons': o.get('horizons', {})})
    except (OSError, ValueError, KeyError) as exc:
        result['errors'].append('sectors_unavailable:' + type(exc).__name__)
    source = Path(run['market_source']) / 'snapshot.json'
    snapshot = checked(source, manifest['input_hashes'].get(str(source)))
    # Keep all saved symbols accessible from sector leaders and actual holdings.
    for s in snapshot['series']:
        result['stocks'][s['market'] + ':' + s['symbol']] = {
            k: s.get(k) for k in ('market', 'symbol', 'name', 'currency', 'provider', 'price_basis',
                                  'collected_at', 'bars', *SEARCH_METADATA_FIELDS)}
    from sector_focus import cached, screen_groups
    from invest_agent.trading.store import BusyError
    try:
        groups = artifact('sectors/groups.json')
        config = load(root/'config.json') if (root/'config.json').exists() else {}
        if config.get('sector_manifest'):
            from sector_projection import cached as project
            projected = project(root, run_dir, config, source.parent, observation, review)
            groups = projected['groups']
            horizons = {(s['market'],s['group_code']):s['horizons'] for s in projected['observation']['observations']}
            result['sectors'] = [dict(s,horizons=horizons[(s['market'],s['group_code'])]) for s in projected['signals']['sectors']]
            result['coverage'] = projected['observation']['coverage']
            result['classification_policy'] = projected['policy']
        result['focus'] = cached(root, run_dir, snapshot, groups, result['sectors'], sha(source), config.get('sector_focus'))
    except (OSError, ValueError, KeyError, TypeError, BusyError) as exc:
        result['focus'] = {'items': [], 'markets': {}, 'error': '자료 또는 캐시 확인 필요 ('+type(exc).__name__+')'}
    # Describe incomplete inputs separately from successful calculation stages.
    result['quality_gaps'] = {
        'price_history': result['quality'].get('quality_issue_series'),
        'market_cap': (sum(str(row.get('market_cap_quality', '')).startswith('unavailable')
                           for row in snapshot['universe']) if 'universe' in snapshot else None),
        'classification': (sum(c['unmatched'] for c in result['coverage'].values())
                           if result.get('coverage') and all('unmatched' in c for c in result['coverage'].values()) else None),
    }
    downtrend_options = load(root/'config.json').get('priority_review',{}).get('watchlist',{}).get('downtrend_approach') if (root/'config.json').exists() else None
    if downtrend_options is not None and review.get('schema') == 4:
        from downtrend_approach import cached as downtrend_projection
        try:
            projected_review = downtrend_projection(root,review,snapshot,sha(source),downtrend_options)
            result['entries'] = [e for g in projected_review['markets'].values() for e in g['entries']]
            result['candidate_count'] = projected_review['candidate_count']
            result['downtrend_settings'] = downtrend_options
        except (OSError, ValueError, KeyError, TypeError, BusyError) as exc:
            result['errors'].append('downtrend_approach_unavailable:' + type(exc).__name__)
    result['screen_groups'] = screen_groups(result['entries'], snapshot.get('universe', []))
    return result



SEARCH_NAME_FIELDS = ('name', 'english_name', 'korean_name', 'name_en', 'name_ko')
SEARCH_METADATA_FIELDS = (*SEARCH_NAME_FIELDS[1:], 'aliases', 'provider_symbol',
                          'listing_exchange_family', 'listing_exchange', 'exchange', 'isin_code')


def search_text(value):
    """Match user spelling across case, spacing and Unicode composition."""
    return ''.join(unicodedata.normalize('NFKC', value).casefold().split())


class StockSearch:
    """Compact in-memory index of stocks already addressable by the detail view."""
    limit = 12

    def __init__(self, stocks, listings=None, policy=None):
        from market_scope import load as scope_policy, match
        policy = scope_policy() if policy is None else policy
        self.rows = []
        for key, stock in stocks.items():
            market, symbol = stock.get('market'), stock.get('symbol')
            if market not in ('KR', 'US') or not isinstance(symbol, str) or key != market + ':' + symbol:
                continue
            listing = (listings or {}).get(key, {})
            listed_isin = listing.get('isin_code') or listing.get('isinCode')
            if stock.get('isin_code') and listed_isin and stock['isin_code'] != listed_isin:
                listing = {}  # A reused ticker must not inherit a former company's names.
            row = {**listing, **stock}
            row['isin_code'] = stock.get('isin_code') or listing.get('isin_code') or listing.get('isinCode')
            if match(row, policy):
                continue
            names = [row.get(field) for field in SEARCH_NAME_FIELDS]
            names += row.get('aliases', []) if isinstance(row.get('aliases'), list) else []
            terms = [symbol, row.get('provider_symbol'), *names]
            if market == 'KR' and symbol.endswith(('.KS', '.KQ')):
                terms.append(symbol.rsplit('.', 1)[0])
            terms = tuple(dict.fromkeys(search_text(value) for value in terms if isinstance(value, str) and value.strip()))
            name = next((value for value in names if isinstance(value, str) and value.strip()), symbol)
            exchange = next((row.get(field) for field in ('listing_exchange_family', 'listing_exchange', 'exchange')
                             if isinstance(row.get(field), str) and row[field]), None)
            self.rows.append(({'key': key, 'market': market, 'symbol': symbol, 'name': name, 'exchange': exchange}, terms))

    def search(self, query):
        if not isinstance(query, str) or len(query) > 100:
            raise ValueError('invalid_search_query')
        needle = search_text(query)
        result = {'query': query, 'items': [], 'total': 0, 'limit': self.limit}
        if not needle:
            return result
        matches = []
        for item, terms in self.rows:
            rank = min((0 if term == needle else 1 if term.startswith(needle) else 2
                        for term in terms if needle in term), default=None)
            if rank is not None:
                matches.append((rank, search_text(item['symbol']), item['market'], item['key'], item))
        matches.sort(key=lambda row: row[:4])
        result.update(items=[row[-1] for row in matches[:self.limit]], total=len(matches))
        return result


def collect_balances(reader, pages, error_type):
    """Explicit three-endpoint allowlist; no order/reservation/market calls."""
    allowed = {'/n2/acctinfo', '/krstock/inquiry/v1/balance', '/gbstock/inquiry/v1/balance'}
    calls = []
    def restricted(path, inputs, continuation=None):
        if path not in allowed:
            raise ValueError('dashboard_balance_read_only')
        calls.append(path)
        return reader(path, inputs, continuation)
    started = now()
    body, _ = restricted('/n2/acctinfo', {})
    rows = body.get('Output_0')
    if not isinstance(rows, list) or any(word in str(body.get('rsp_msg', '')) for word in ('실패', '오류', '불가', '권한')):
        raise ValueError('account_list_not_confirmed')
    if not rows and not any(word in str(body.get('rsp_msg', '')) for word in ('완료', '조회되었습니다', '내역이 없습니다')):
        raise ValueError('empty_account_list_unconfirmed')
    accounts, seen = [], set()
    for row in rows:
        if row.get('acct_type') not in ('01', '02'):
            continue
        account = row.get('acct_no')
        if not isinstance(account, str) or not account.isdigit() or len(account) != 11 or account in seen:
            raise ValueError('account_identity_invalid')
        seen.add(account)
        item = {'alias': 'nh-' + hashlib.sha256(account.encode()).hexdigest()[:12],
                'account_type': row['acct_type'], 'markets': {}, 'details': {}}
        requests = {
            'KR': ('/krstock/inquiry/v1/balance', {'act_no': account, 'bnc_bse_cd': '1',
                    'ltg_aot_dit_cd': '9', 'aet_bse': '1', 'qut_dit_cd': 'UNT'}),
            'US': ('/gbstock/inquiry/v1/balance', {'act_no': account, 'qut_iqr_dit_cd': '1',
                    'fc_sec_trd_nat_cd': '200', 'cur_cd': 'USD', 'xns_dit_cd': '0'})}
        for market, (path, inputs) in requests.items():
            try:
                item['markets'][market] = {'state': 'received', 'pages': pages(path, inputs, restricted)}
            except error_type as exc:
                # Transport errors are controlled codes from the existing adapter.
                item['markets'][market] = {'state': 'unavailable', 'reason': str(exc)}
        accounts.append(item)
    return {'provider': 'NH PLUG', 'source': 'broker_export', 'as_of': started,
            'collected_until': now(), 'scope': 'KR_US_balances_only_no_orders',
            'accounts': accounts, 'endpoint_calls': calls}


def account_view(config, output, refresh=True, saved_dashboard_root=None):
    mother = Path(config['mother'])
    # Reuse account-only adapters without shadowing current market/chart modules.
    mother_scripts = str(mother / 'scripts')
    if mother_scripts not in sys.path:
        sys.path.append(mother_scripts)
    from nh_snapshot import normalize
    from nh_collect import pages
    from nh_api import read, NHError
    from nh_diagnostics import successful
    previous_paths = list((mother / 'runtime/nh-live/exports').glob('account-*.json'))
    previous_paths += list(Path(saved_dashboard_root or output.parent).glob('*/account-snapshot.private.json'))
    previous = None
    previous_meta = None
    previous_score = None
    for path in previous_paths:
        try:
            candidate = load(path)
            meta = None
            if path.name.endswith('.private.json'):
                hashes = load(path.parent / 'manifest.json')['files']
                candidate = checked(path, hashes.get(path.name))
                if hashes.get('account-view.json'):
                    meta = checked(path.parent/'account-view.json', hashes['account-view.json'])
            score = (candidate['as_of'], (meta or {}).get('attempted_at', ''))
            if previous_score is None or score > previous_score:
                previous, previous_meta, previous_score = candidate, meta, score
        except (OSError, ValueError, KeyError):
            continue
    result = {'attempted_at': now(), 'query_state': 'not_requested', 'reason': None,
              'accounts': [], 'as_of': None, 'source_mode': 'missing', 'risk': None,
              'endpoint_calls': [], 'limitations': [], 'snapshot_sha256': None}
    snapshot = None
    if refresh:
        try:
            raw = collect_balances(read, pages, NHError)
            write(output / 'account-observation.private.json', raw)
            result['endpoint_calls'] = raw['endpoint_calls']
            listings = {}
            # Reuse the verified official saved listing master; no new market fetch.
            evidence = (previous or {}).get('listing_source', {})
            if evidence.get('path') and sha(evidence['path']) == evidence.get('sha256'):
                from nh_master import parse
                listings = parse(Path(evidence['path']).read_bytes())
            snapshot = normalize(raw, listings)
            snapshot['listing_source'] = evidence
            for account, source in zip(snapshot['accounts'], raw['accounts']):
                account['query_markets'] = {m: {
                    'state': ('received' if successful(v) and isinstance(v['pages'][-1].get('Output_1'), list)
                              and isinstance(v['pages'][-1].get('Output_0'), dict) else 'unavailable'),
                    'reason': v.get('reason'), 'pages': len(v.get('pages', []))}
                    for m, v in source['markets'].items()}
                for value in account['query_markets'].values():
                    if value['state'] == 'unavailable' and not value['reason']:
                        value['reason'] = 'broker_response_or_shape_unconfirmed'
            result['query_state'] = ('received' if all(a['holdings_complete'] for a in snapshot['accounts']) else 'partial')
            result['source_mode'] = 'live_read_only'
        except (NHError, OSError, ValueError, KeyError, TypeError) as exc:
            snapshot = None
            result.update(query_state='failed', reason=str(exc) if isinstance(exc, NHError) else type(exc).__name__)
    if snapshot is None and previous:
        snapshot = previous
        result['source_mode'] = 'historical_saved'
        if not refresh and previous_meta:
            result.update({k:previous_meta.get(k) for k in ('query_state','reason','attempted_at')})
    if snapshot is None:
        return result
    normalized_path = output / 'account-snapshot.private.json'
    write(normalized_path, snapshot)
    result.update(accounts=snapshot['accounts'], as_of=snapshot['as_of'],
                  snapshot_sha256=sha(normalized_path), limitations=result['limitations'] + snapshot.get('limitations', []))
    result['stale'] = (datetime.now(timezone.utc) - datetime.fromisoformat(snapshot['as_of'])).total_seconds() > 86400
    result['valuation'] = {k: snapshot.get(k) for k in ('base_currency', 'fx_to_base')}
    result['accounts_complete'] = snapshot.get('accounts_complete', False)
    return result


def build(root=ROOT, refresh=True, *, market_run=None, daily_update=None):
    config = load(root / 'config.json')
    sys.path.insert(0, str(Path(config['engine']) / 'src'))
    output = root / 'runtime/dashboard' / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    output.mkdir(parents=True)
    try:
        market = saved_market(root, market_run)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        market = {'state': 'unavailable', 'errors': ['saved_market_unavailable:' + type(exc).__name__],
                  'entries': [], 'sectors': [], 'stocks': {}, 'session_dates': {}, 'quality': {},
                  'candidate_count': None, 'entry_candidate_count': None}
    accounts = account_view(config, output, refresh)
    if (daily_update or {}).get('account_state') == 'failed':
        accounts.update(query_state='failed', reason='daily_account_refresh_failed')
    stocks = market.pop('stocks')
    from trailing_warnings import TrailingStore
    from daily_launch import plan_for
    warnings = TrailingStore(root/'runtime/trailing-warnings.sqlite3').update(accounts, stocks, plan_for(datetime.now(timezone.utc))['targets'])
    data = {'generated_at': now(), 'market': market, 'accounts': accounts, 'trailing': warnings}
    if daily_update is not None:
        data['daily_update'] = daily_update
    write(output / 'account-view.json', accounts)
    write(output / 'dashboard.json', data)
    # Prices remain in the immutable market source, projected in memory only.
    # Do not duplicate hundreds of MB of full-universe bars on every launch.
    write(output / 'manifest.json', {'files': {p.name: sha(p) for p in output.iterdir() if p.is_file()},
                                    'market_network_calls': 0, 'authority': 'read_only_no_orders'})
    return output, data, stocks


def refresh_account_snapshot(root=ROOT):
    """One balance-only refresh; immutable receipt is reusable by scheduled and UI paths."""
    output = root/'runtime/dashboard'/('account-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    output.mkdir(parents=True)
    accounts = account_view(load(root/'config.json'), output, refresh=True)
    write(output/'account-view.json', accounts)
    write(output/'manifest.json', {'files':{p.name:sha(p) for p in output.iterdir() if p.is_file()},
                                  'authority':'read_only_no_orders'})
    return accounts
