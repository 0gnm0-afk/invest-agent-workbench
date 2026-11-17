"""Session MCP adapter over existing stores; no HTTP endpoint or dashboard process."""
from pathlib import Path
import re
import sqlite3
import threading

from analysis import Forbidden
from analysis.service import AnalysisService
from drawings.store import WorkspaceStore
from market.prices import PriceStore, default_path, saved_bars
from market.history.chart import HistoryStore
from market.history.saved import DBHistory

OPERATIONS = frozenset({
    'list', 'detail', 'read', 'report_document', 'propose', 'record', 'holding_links', 'refresh',
    'collaboration_read', 'discussion_save', 'plan_save', 'plan_input', 'monitor_list',
})
RETIRED = frozenset({'risk_overview', 'risk_calculate', 'risk_save', 'risk_review_save', 'calculate'})


class ExistingWorkspace(WorkspaceStore):
    def connect(self):
        # Never silently initialize a different empty database.
        return sqlite3.connect(self.path.resolve().as_uri() + '?mode=rw', uri=True, timeout=10)


class SelectedArchive(DBHistory):
    """Reuse archive verification/quality logic for one requested symbol."""
    def __init__(self, store, market, symbol):
        self.store, self.market = store, market
        self.currency = {'KR': 'KRW', 'US': 'USD'}[market]
        self.enabled, self.error, self.stocks = False, False, {}
        self.listings, self.coverage, self.references = {}, {}, {}
        entry = store.archive_entry(market, symbol)
        if entry is None:
            return
        self.enabled = True
        self.root, self.config = Path(entry['source']), entry['config']
        self.listings, self.coverage, self.references = (entry[k] for k in ('listings', 'coverage', 'references'))


class SelectedCatalog:
    def __init__(self, prices):
        self.prices = prices
        self.stocks, self.markets = {}, {}

    def archive(self, market, symbol):
        key = market + ':' + symbol
        if key not in self.markets:
            self.markets[key] = SelectedArchive(self.prices, market, symbol)
        return self.markets[key]

    def packet(self, stock):
        return self.archive(stock['market'], stock['symbol']).packet(stock)


class SelectedHistories(HistoryStore):
    def __init__(self, root):
        # Skip the eager all-market catalog; reuse selected view/refresh methods.
        self.root = Path(root)
        self.path = Path(root) / 'runtime/chart-history.sqlite3'
        self.lock = threading.Lock()
        self.client_factory = self.client = None
        self.prices = PriceStore(default_path(root), root=root)
        self.saved = SelectedCatalog(self.prices)

    def select(self, stock_key):
        if not isinstance(stock_key, str) or not re.fullmatch(r'(KR|US):[A-Z0-9.\-]{1,24}', stock_key):
            raise ValueError('exact_stock_key_required')
        if stock_key in self.saved.stocks:
            return self.saved.stocks[stock_key]
        market, symbol = stock_key.split(':', 1)
        current = saved_bars(stock_key, root=self.root)
        archive = self.saved.archive(market, symbol)
        identity, metadata = current['identity'], current['metadata']
        observed, bars = current['observation_metadata'], current['bars']
        listing = archive.listings.get(symbol, {})
        if identity.get('isin') and listing.get('isinCode') and identity['isin'] != listing['isinCode']:
            raise ValueError('saved_listing_identity_changed')
        quality = archive.coverage.get(symbol, {})
        excluded = set(quality.get('invalid_ohlc', [])) | (set(quality.get('non_sessions', [])) if market == 'US' else set())
        bars = [b for b in bars if b['date'] not in excluded]
        stock = {**listing, **metadata, **observed, **identity, 'bars': bars,
                 'name': observed.get('name') or metadata.get('name') or listing.get('name') or symbol,
                 'isin_code': identity.get('isin') or listing.get('isinCode'),
                 'listing_exchange_family': observed.get('listing_exchange_family') or listing.get('listing_exchange')}
        stock['history'] = {'quality': {'excluded_dates': sorted(excluded)}}
        stock['bars_ref'] = self.prices.pin(stock)
        self.saved.stocks[stock_key] = stock
        return stock


class LazyFinancials:
    def __init__(self, root, prices):
        self.root, self.prices, self.instance = Path(root), prices, None

    def __getattr__(self, name):
        if self.instance is None:
            from financials.store import FinancialStore
            self.instance = FinancialStore(self.root / 'runtime/financials', price_store=self.prices)
        return getattr(self.instance, name)


class DirectService(AnalysisService):
    def links(self, case, origin):
        key = case['id']
        prompt = f"analysis_collaboration_read(analysis_key='{key}')로 {case['stock_key']}의 저장 논의·정확한 채택 버전·미정 질문을 먼저 읽고 이어가줘."
        return {'analysis_key': key, 'dashboard_url': None, 'transport': 'local_db',
                'reopen_tool': {'name': 'analysis_collaboration_read', 'arguments': {'analysis_key': key}},
                'reopen_command': f'.\\analysis.ps1 -AnalysisKey {key} -NoBrowser',
                'resume_prompt': prompt, 'copy_text': prompt, 'prompt': prompt,
                'qualitative_skill': 'eps-research'}


def service_for(root):
    root = Path(root)
    histories = SelectedHistories(root)
    financials = LazyFinancials(root, histories.prices)
    view_cache = None

    def view():
        nonlocal view_cache
        if view_cache is None:
            from analysis.direct_accounts import saved_accounts
            view_cache = {'accounts': saved_accounts(root), 'market': {
                'state': 'saved_only', 'session_dates': {}, 'source': 'local_db', 'errors': []}}
        return view_cache, histories.saved.stocks

    return DirectService(ExistingWorkspace(root / 'runtime/chart-workspaces.sqlite3'), histories,
                         financials, view, root, initialize=False)


def call(op, arguments, *, root, role='collaboration'):
    if op in RETIRED:
        raise ValueError('retired_risk_operation')
    if role not in ('collaboration', 'risk'):
        raise ValueError('invalid_analysis_role')
    if not isinstance(arguments, dict):
        raise ValueError('object_required')
    if op not in OPERATIONS:
        raise Forbidden('operation_not_exposed_to_session_mcp')
    if role == 'risk' and op not in {'list', 'detail', 'holding_links', 'read', 'monitor_list', 'collaboration_read', 'plan_input'}:
        raise Forbidden('risk_cannot_write_collaboration_or_legacy_records')
    if role == 'risk' and op == 'read' and not arguments.get('snapshot_id'):
        raise Forbidden('risk_read_requires_frozen_snapshot')
    service = service_for(root)
    if op == 'refresh' or op == 'read' and not arguments.get('snapshot_id'):
        case = service.case(arguments['analysis_key'])
        service.histories.select(case['stock_key'])
        if op == 'refresh' and arguments.get('mode') == 'financial_sources' and case['stock_key'] == 'KR:005930.KS':
            try:
                service.histories.select('KR:005935.KS')
            except ValueError as exc:
                if str(exc) != 'saved_price_identity_missing_or_ambiguous':
                    raise
    result = service.dispatch(op, arguments, '', principal=role)
    from analysis.direct_accounts import project_response
    return project_response(op, result)
