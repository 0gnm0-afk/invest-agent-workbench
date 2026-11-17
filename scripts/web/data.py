"""Read-only view adapter over verified saved market inputs. No market collection."""
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
              'finished_at': run.get('finished_at'),
              'stocks': {}, 'errors': [], 'session_dates': {}}
    source = Path(run['market_source']) / 'snapshot.json'
    from market.price_files import PriceFiles
    from market.prices import PriceStore,default_path
    source_hash = manifest['input_hashes'].get(str(source))
    snapshot = PriceFiles(PriceStore(default_path(root))).market_snapshot(source,source_hash)
    # Keep all saved symbols accessible through search and actual holdings.
    for s in snapshot['series']:
        result['stocks'][s['market'] + ':' + s['symbol']] = {
            k: s.get(k) for k in ('market', 'symbol', 'name', 'currency', 'provider', 'price_basis',
                                  'collected_at', 'bars', 'bars_ref', *SEARCH_METADATA_FIELDS)}
    result['session_dates'] = run.get('data_times', {}).get('session_dates') or {
        market: sessions[-1] for market, sessions in snapshot.get('sessions', {}).items() if sessions}
    # Describe incomplete inputs separately from successful calculation stages.
    result['quality_gaps'] = {
        'price_history': result['quality'].get('quality_issue_series'),
        'market_cap': (sum(str(row.get('market_cap_quality', '')).startswith('unavailable')
                           for row in snapshot['universe']) if 'universe' in snapshot else None),
    }
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


def home_view(data):
    """Public market status only; saved holding context stays inside services."""
    market_fields = ('state', 'run_id', 'processing_state', 'data_times', 'quality',
                     'finished_at', 'errors', 'session_dates', 'quality_gaps')
    result = {'generated_at': data.get('generated_at'),
              'market': {key: value for key, value in data.get('market', {}).items()
                         if key in market_fields}}
    if 'daily_update' in data:
        result['daily_update'] = market_update(data['daily_update'])
    return result


def market_update(value):
    """Publish market status without internal account/monitor result payloads."""
    return {key: item for key, item in value.items()
            if not key.startswith('account')
            and key not in {'saved_accounts', 'trailing_result', 'scenario_result'}}


def build(root=ROOT, refresh=False, *, market_run=None, daily_update=None, persist_view=False):
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
                  'stocks': {}, 'session_dates': {}, 'quality': {}}
    # Compatibility callers may still supply refresh; web views only read the
    # session's saved observation and never initiate broker queries.
    accounts = account_view(config, output, False, persist_receipt=persist_view)
    stocks = market.pop('stocks')
    from alerts.trailing import TrailingStore
    from launch.daily import plan_for
    warning_store = TrailingStore(root/'runtime/trailing-warnings.sqlite3')
    warning_time = datetime.now(timezone.utc)
    warning_targets = plan_for(warning_time)['targets']
    warnings = warning_store.public(warning_time, warning_targets)
    data = {'generated_at': now(), 'market': market, 'accounts': accounts, 'trailing': warnings}
    if daily_update is not None:
        data['daily_update'] = market_update(daily_update)
    if persist_view:
        publish_receipt(output, accounts, view=data, market_network_calls=0, view_storage='export',
                        market_run=str(market_run) if market_run else None)
    return output if persist_view else None, data, stocks
