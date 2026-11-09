"""Read-only view adapter. No market collection. Cached sector and downtrend projections on verified saved inputs."""
import json
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from workbench_paths import ROOT
from runtime_paths import local_runtime_path
from atomic_io import load_bom as load, sha, write_new_json as write, checked_saved as checked, utc_now as now
from accounts import account_view, publish_receipt


def saved_market(root=ROOT, run_dir=None):
    """Default to latest attempt; explicit prior views require a separate daily failure banner."""
    if run_dir is None:
        run_dir = load(root / 'runtime/latest.json')['output']
    run_dir = local_runtime_path(run_dir, root=root).resolve()
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
    from market.price_files import PriceFiles
    from market.prices import PriceStore,default_path
    source_hash = manifest['input_hashes'].get(str(source))
    snapshot = PriceFiles(PriceStore(default_path(root))).market_snapshot(source,source_hash)
    # Keep all saved symbols accessible from sector leaders and actual holdings.
    for s in snapshot['series']:
        result['stocks'][s['market'] + ':' + s['symbol']] = {
            k: s.get(k) for k in ('market', 'symbol', 'name', 'currency', 'provider', 'price_basis',
                                  'collected_at', 'bars', 'bars_ref', *SEARCH_METADATA_FIELDS)}
    from sectors.focus import cached, screen_groups
    from invest_agent.trading.store import BusyError
    try:
        groups = artifact('sectors/groups.json')
        config = load(root/'config.json') if (root/'config.json').exists() else {}
        if config.get('sector_manifest'):
            from sectors.projection import cached as project
            projected = project(root, run_dir, config, source.parent, observation, review, snapshot=snapshot)
            groups = projected['groups']
            horizons = {(s['market'],s['group_code']):s['horizons'] for s in projected['observation']['observations']}
            result['sectors'] = [dict(s,horizons=horizons[(s['market'],s['group_code'])]) for s in projected['signals']['sectors']]
            result['coverage'] = projected['observation']['coverage']
            result['classification_policy'] = projected['policy']
        result['focus'] = cached(root, run_dir, snapshot, groups, result['sectors'], source_hash, config.get('sector_focus'))
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
        from screening.projection import cached as trendline_projection
        try:
            projected_review = trendline_projection(root,artifact('screen.json'),snapshot,review,source_hash,downtrend_options)
            result['entries'] = [e for g in projected_review['markets'].values() for e in g['entries']]
            result['candidate_count'] = projected_review['candidate_count']
            result['entry_candidate_count'] = projected_review['entry_candidate_count']
            result['trendline_policy'] = projected_review['config']['trendline_policy']
            result['downtrend_settings'] = projected_review['config']['watchlist']['downtrend_approach']
        except (OSError, ValueError, KeyError, TypeError, BusyError) as exc:
            result['errors'].append('recent_trendlines_unavailable:' + type(exc).__name__)
            result.update(entries=[],candidate_count=None,entry_candidate_count=None,trendline_policy='unavailable')
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
        from market.scope import load as scope_policy, match
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


def build(root=ROOT, refresh=True, *, market_run=None, daily_update=None, persist_view=False):
    config = load(root / 'config.json')
    sys.path.insert(0, str(Path(config['engine']) / 'src'))
    # Ordinary views are memory projections of the existing DBs. Only an
    # explicitly requested export owns a new immutable output directory.
    output = root / 'runtime/dashboard' / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') if persist_view else 'view')
    if persist_view:
        output.mkdir(parents=True)
    try:
        market = saved_market(root, market_run)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        market = {'state': 'unavailable', 'errors': ['saved_market_unavailable:' + type(exc).__name__],
                  'entries': [], 'sectors': [], 'stocks': {}, 'session_dates': {}, 'quality': {},
                  'candidate_count': None, 'entry_candidate_count': None}
    accounts = account_view(config, output, refresh, persist_receipt=persist_view)
    if (daily_update or {}).get('account_state') == 'failed':
        accounts.update(query_state='failed', reason='daily_account_refresh_failed')
    stocks = market.pop('stocks')
    from alerts.trailing import TrailingStore
    from launch.daily import plan_for
    warning_store = TrailingStore(root/'runtime/trailing-warnings.sqlite3')
    warning_time = datetime.now(timezone.utc)
    warning_targets = plan_for(warning_time)['targets']
    # Saved-only viewing must not rewrite monitor timestamps/state. The daily
    # collection path already evaluates warnings after its account refresh.
    warnings = (warning_store.update(accounts, stocks, warning_targets, warning_time) if refresh
                else warning_store.public(warning_time, warning_targets))
    data = {'generated_at': now(), 'market': market, 'accounts': accounts, 'trailing': warnings}
    if daily_update is not None:
        data['daily_update'] = daily_update
    if persist_view:
        publish_receipt(output, accounts, view=data, market_network_calls=0, view_storage='export',
                        market_run=str(market_run) if market_run else None)
    return output if persist_view else None, data, stocks
