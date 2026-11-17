"""M01 acceptance checks use copied code and OS-temp synthetic stores only.

Root P/scripts is outside the isolation copy. Its exact 7/33 inventory is
recorded below and audited by the implementation's outer Git/path check;
this suite checks the allowlist contract and retained callers, not live P.
Historical fixture assertions never inspect or migrate operating databases.
"""
import ast
from contextlib import closing
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'src'))

ROOT_KEEP = frozenset('nh_auth nh_api nh_master protection_sources toss_api toss_candles toss_market_client'.split())
ROOT_RETIRED = frozenset('account_stops prepare_stops adopt_protection_rule finviz_public_classification hybrid_market kr_candle_probe llm_review market_collect market_quality nh_allocation nh_collect nh_crosscheck nh_crosscheck_analyze nh_crosscheck_run nh_diagnostics nh_morning nh_snapshot public_valuation_adapter public_valuation_collect sector_basis sector_classification sector_collect sector_external_review sector_observation test_llm_review toss_connection_check toss_holdings toss_morning toss_quote toss_schema_probe toss_universe verify_toss_morning wics_classification'.split())
CYCLE_OPS = frozenset('cycle_list cycle_read cycle_context cycle_request_draft cycle_agent_input cycle_calculate cycle_save_draft cycle_approve cycle_fill'.split())
RISK_OPS = frozenset('risk_overview risk_calculate risk_save risk_review_save calculate'.split())
CYCLE_TOOLS = frozenset('analysis_cycles analysis_cycle_read analysis_cycle_input analysis_cycle_calculate analysis_cycle_save_draft'.split())
RETAINED_TOOLS = frozenset('analysis_list analysis_monitors analysis_read analysis_history analysis_report_document analysis_propose analysis_save_record analysis_holding_links analysis_refresh analysis_collaboration_read analysis_save_discussion analysis_save_plan analysis_plan_input telegram_news_status telegram_news_dialogs telegram_news_search'.split())
REMOVED_TRADING = frozenset('account_sync base_structure buy_policy capacity cash_assets chart_history charts history holding_report holding_review momentum_entry momentum_watch morning plans policy_review portfolio portfolio_changes protection report_html report_names reporting risk_budget risk_ledger risk_monitoring risk_reporting runner scenarios sell_policy sperandeo structure_math structure_narrative structure_report taver taver_structure technical_context toss_snapshot trendlines universe valuation valuation_legacy weekly_chart'.split())
# Normalized UTF-8/LF content hashes from baac458; line-ending checkout policy is immaterial.
UNCHANGED = {
    'scripts/atomic_io.py': 'bd29c8ad5c6c36441edaadd1f6602e03a1e13b474ae992e66e2f722ae1e20a64',
    'scripts/news/auth.py': '5349ee89ac455eb10dc8385785def671b3357508e2b598cd3c987e7f8f2deef1',
    'scripts/news/bridge.py': 'f3dcd2aaa81813a9d8b6fc4ac2e3829210b3af182f2c4030edb1788ad8a57de5',
    'scripts/news/search.py': 'c87f754810b086dabab1501dc128102ec6da43c6ad0b88a490c27750938dab6f',
    'scripts/news/server.py': 'ba84c9f18898315a599ffc44fa9f24b4e692c81f16ed79c9d39354c8ed3dbdfd',
    'dashboard/news/telegram_news.css': 'e0f961beb9a0f4413c53314b9fdc8b0d939ed3e1abc74ea32e19d230ee78cd3c',
    'dashboard/news/telegram_news.html': 'f9ca69660142754f32798d760d5d39dbdd55af145b4b3638957412c159017e4b',
    'dashboard/news/telegram_news_ui.js': '12537317179aa9cc152dacd17d83c80c25ffb30e505eb5984a28e7141253d28a',
}
# Pure helper AST fingerprints, before the module moves, from baac458.
HELPERS = {
    'scripts/market/adr.py': {
        'identity': '8874fe4b003c8c09e8505af2f80a13e9219fd1ea4ccecc7bdc35890431852e14',
        'page_fields': '661ed756318171b8a7176649c67cf675f128b3611c17e46ee53688e0535d25a0',
        'parse_pages': '3763bbab5a844aec37a689629a68a5335adca9f28ec3aa61dde56ef8b6aee0ba',
    },
    'scripts/market/classification_refresh.py': {
        'parse_alias_quote': 'ad174bf21784c77ace749eed52255bd8cf9135cd8f0168acd4b629e3fb143699',
        'stamp': '0a5f46b695d31a6f90d4c7ea07528d68dc501008969edc9c0af2c2448eb9b3b2',
        'classification_age_issue': 'eb006dbdf4e2909c32fcddf28ee958fe51907d5f394c93fb5eb0ef15bcc6d79b',
        'classification_freshness': '09073c33daa29ea4a2f964316c26ac76ab2c01d07919dbf869f6e05894a0a6fc',
    },
}


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')


def series(market='US', symbol='SYNTH'):
    return dict(market=market, symbol=symbol, name='합성 비후보 종목', currency='USD' if market == 'US' else 'KRW',
                provider='Toss OpenAPI candles', price_basis='adjusted', isin_code='US0000000001',
                bars=[dict(date='2026-10-05', open=10, high=12, low=9, close=11, volume=100),
                      dict(date='2026-10-06', open=11, high=13, low=10, close=12, volume=120)])


class RetiredFeatures(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        # Inject every default path used by thin record/price hydration.
        self.addCleanup(patch.stopall)
        patch('runtime_paths.ROOT', self.root).start()
        patch('records.files.ROOT', self.root).start()
        patch('market.prices.default_path', return_value=self.root / 'runtime/ohlcv.sqlite3').start()
        patch('market.price_files.default_path', return_value=self.root / 'runtime/ohlcv.sqlite3').start()
        write(self.root / 'config.json', dict(engine=str(self.root),
              records={'path': str(self.root / 'runtime/records.sqlite3'),
                       'previous_engine_roots': [str(self.root / 'previous-engine')]},
              ohlcv={'provider': 'toss', 'path': str(self.root / 'runtime/ohlcv.sqlite3')}))

    def test_no_retired_execution_edges(self):
        blocked = {'screening', 'sectors', 'cycles', 'trendln'}
        for folder in ('scripts', 'src'):
            for file in (ROOT / folder).rglob('*.py'):
                tree = ast.parse(file.read_text(encoding='utf-8-sig'))
                for node in ast.walk(tree):
                    names = [alias.name for alias in node.names] if isinstance(node, ast.Import) else [node.module or ''] if isinstance(node, ast.ImportFrom) else []
                    for name in names:
                        self.assertNotIn(name.split('.')[0], blocked, (file.relative_to(ROOT), name))
                        if name.startswith('invest_agent.trading.'):
                            self.assertNotIn(name.split('.')[2], REMOVED_TRADING, (file.relative_to(ROOT), name))
                    if isinstance(node, ast.ImportFrom) and node.module == 'invest_agent.trading':
                        self.assertFalse({a.name for a in node.names} & REMOVED_TRADING, file)
                    if isinstance(node, ast.ImportFrom) and node.level and file.parent == ROOT / 'src/invest_agent/trading':
                        self.assertNotIn((node.module or '').split('.')[0], REMOVED_TRADING, file)
                    if isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant):
                        name = node.args[0].value
                        if isinstance(name, str) and isinstance(node.func, ast.Name) and node.func.id == '__import__':
                            self.assertNotIn(name.split('.')[0], blocked)
        for folder in ('scripts/screening', 'scripts/sectors', 'scripts/cycles', 'scripts/vendor/trendln'):
            self.assertFalse(list((ROOT / folder).rglob('*.py')), folder)
        for name in REMOVED_TRADING:
            self.assertFalse((ROOT / 'src/invest_agent/trading' / (name + '.py')).exists(), name)
        mcp = ast.parse((ROOT / 'scripts/analysis/mcp.py').read_text(encoding='utf-8-sig'))
        tool_names = {n.name for n in mcp.body if isinstance(n, ast.FunctionDef) and n.decorator_list}
        self.assertEqual(tool_names, RETAINED_TOOLS)
        self.assertEqual(len(CYCLE_TOOLS), 5)
        self.assertFalse(tool_names & CYCLE_TOOLS)
        self.assertEqual({n for n in tool_names if n.startswith('telegram_news_')},
                         {'telegram_news_status', 'telegram_news_dialogs', 'telegram_news_search'})
        for relative in ('scripts/web/__init__.py', 'scripts/web/server.py', 'dashboard/shell/index.html',
                         'dashboard/shell/app.js', 'dashboard/drawings/chart-workspace.js'):
            source = (ROOT / relative).read_text(encoding='utf-8')
            for token in ('trade-cycle', 'WorkbenchTradeCycle', 'trade_cycle_protocol',
                          'auto_trendlines', 'trendln', '#screen', '#sectors', '#cycles'):
                self.assertNotIn(token, source, (relative, token))
        self.assertFalse((ROOT / 'dashboard/cycles/trade-cycle.js').exists())
        self.assertFalse((ROOT / 'dashboard/cycles/trade-cycle.css').exists())

    def test_market_without_screening(self):
        from launch.daily import DailyLaunch
        import market.daily as daily
        from market.prices import PriceStore, freeze_snapshot
        from atomic_io import load, sha as file_sha
        targets = {'KR': '2026-10-06', 'US': '2026-10-06'}
        prices = PriceStore(self.root / 'runtime/ohlcv.sqlite3')
        config = json.loads((self.root / 'config.json').read_text(encoding='utf-8'))
        snapshot = dict(series=[series('KR', '000001.KS'), series()],
                        universe=[], sessions={m: [d] for m, d in targets.items()},
                        collection_stats={'requested': 2, 'received': 2, 'failed': 0},
                        fetched_at='2026-10-07T00:00:00+00:00', collected_until='2026-10-07T00:01:00+00:00')
        quality = dict(state='succeeded', processing_state='completed', data_complete=True,
                       quality_issue_series=0, unexpected_failures=0,
                       exchanges={e: dict(requested=1, received=1, failed=0) for e in ('KOSPI', 'NASDAQ')})
        def collect(output, _config, **kwargs):
            source = output / 'source'
            path = source / 'snapshot.json'
            frozen = freeze_snapshot(deepcopy(snapshot), path, prices)
            write(path, frozen)
            write(source / 'manifest.json', {'snapshot.json': file_sha(path)})
            write(output / 'latest.json', {'attempt': str(source)})
        with patch.object(daily, 'ROOT', self.root), patch.object(daily, 'collect', side_effect=collect) as collector, \
             patch.object(daily, 'audit', return_value=quality), patch('market.exclusions.inventory', return_value={'issues': []}):
            result = daily.run(config)
        collector.assert_called_once()
        self.assertEqual(result['state'], 'succeeded', result.get('error'))
        output = Path(result['output'])
        self.assertEqual(set(result['stages']), {'inputs', 'collection', 'nh_fallback', 'adr_metadata', 'quality', 'report'})
        for name in ('priority-review.json', 'screen.json', 'screen-base.json', 'candidates.csv', 'sectors'):
            self.assertFalse((output / name).exists(), name)
        launcher = DailyLaunch(self.root)
        receipt = launcher.collection_receipt(output, targets)
        self.assertEqual(set(receipt), {'target_trade_date', 'completed', 'failed', 'status', 'provenance', 'sha256'})
        self.assertEqual(receipt['status'], 'complete')
        self.assertEqual(receipt['target_trade_date'], targets)
        self.assertEqual(receipt['completed'], {m: dict(requested=1, completed=1, failed=0) for m in targets})
        self.assertEqual(receipt['sha256'], load(output / 'manifest.json')['output_hashes']['run.json'])
        self.assertEqual(receipt['provenance'], dict(provider='toss', run_id=output.name, mode='live'))
        self.assertEqual(prices.read(prices.pin(series())), series()['bars'])
        # Use ordinary synthetic files for malformed/partial responses, leaving the
        # already-published immutable fixture above untouched.
        value = deepcopy(result)
        path = self.root / 'runtime/receipt-cases'
        def publish():
            write(path / 'run.json', value)
            write(path / 'manifest.json', {'output_hashes': {'run.json': sha((path / 'run.json').read_bytes())}})
        value['quality']['markets']['US'] = dict(requested=2, received=1, failed=1)
        publish()
        self.assertEqual(launcher.collection_receipt(path, targets)['status'], 'partial')
        value['quality']['markets']['US'] = dict(requested=2, received=0, failed=2)
        publish()
        self.assertEqual(launcher.collection_receipt(path, targets)['status'], 'failed')
        value['quality']['markets']['US'] = dict(requested=1, received=1, failed=0)
        value['data_times']['session_dates']['US'] = '2026-10-05'
        publish()
        self.assertEqual(launcher.collection_receipt(path, targets)['status'], 'partial')
        del value['quality']['markets']['US']['received']
        publish()
        with self.assertRaises((KeyError, ValueError)):
            launcher.collection_receipt(path, targets)
        (path / 'run.json').write_text('{}', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'run_hash_mismatch'):
            launcher.collection_receipt(path, targets)

    def test_saved_stock_without_screening(self):
        from market.prices import PriceStore, freeze_snapshot
        from web.data import saved_market, StockSearch
        from atomic_io import sha as file_sha
        prices = PriceStore(self.root / 'runtime/ohlcv.sqlite3')
        source = self.root / 'runtime/source/snapshot.json'
        original = series()
        write(source, freeze_snapshot(dict(series=[original], sessions={'US': ['2026-10-06']}), source, prices))
        output = self.root / 'runtime/run'
        write(output / 'run.json', dict(state='succeeded', processing_state='completed',
              quality={'received': 1, 'requested': 1, 'failed': 0}, market_source=str(source.parent)))
        write(output / 'manifest.json', dict(output_hashes={'run.json': file_sha(output / 'run.json')},
              input_hashes={str(source): file_sha(source)}))
        write(self.root / 'runtime/latest.json', {'output': str(output)})
        before = {p: sha(p.read_bytes()) for p in (source, output / 'run.json', output / 'manifest.json')}
        value = saved_market(self.root)
        self.assertEqual(value['errors'], [])
        self.assertNotIn('entries', value)
        self.assertNotIn('sectors', value)
        self.assertEqual(value['session_dates'], {'US': '2026-10-06'})
        stock = value['stocks']['US:SYNTH']
        self.assertEqual(stock['bars'], original['bars'])
        self.assertEqual(prices.read(stock['bars_ref']), original['bars'])
        index = StockSearch(value['stocks'], policy={'records': []})
        self.assertEqual(index.search('합성 비후보')['items'][0]['key'], 'US:SYNTH')
        self.assertEqual({p: sha(p.read_bytes()) for p in before}, before)

    def test_cycle_and_old_risk_refused(self):
        from analysis import Forbidden
        from analysis.direct import call
        from analysis.service import AnalysisService
        service = AnalysisService.__new__(AnalysisService)
        service.case = Mock(side_effect=AssertionError('retired operation read a case'))
        service.store = Mock()
        service.store.connect.side_effect = AssertionError('retired operation touched a database')
        with patch('analysis.direct.service_for', side_effect=AssertionError('retired operation built a service')):
            for op in CYCLE_OPS:
                with self.subTest(op=op):
                    with self.assertRaisesRegex(ValueError, 'unknown_operation'):
                        service.dispatch(op, {'analysis_key': 'an_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'}, '')
                    with self.assertRaisesRegex(Forbidden, 'operation_not_exposed'):
                        call(op, {}, root=self.root)
            for op in RISK_OPS:
                with self.subTest(op=op):
                    with self.assertRaisesRegex(ValueError, 'retired_risk_operation'):
                        service.dispatch(op, {}, '')
                    with self.assertRaisesRegex(ValueError, 'retired_risk_operation'):
                        call(op, {}, root=self.root)
        service.case.assert_not_called()
        service.store.connect.assert_not_called()
        self.assertFalse((self.root / 'runtime').exists())

    def test_retained_alerts_without_cycles(self):
        from alerts import delivery
        from alerts.trailing import TrailingStore
        from scenarios.monitor import ScenarioStore
        targets = {'KR': '2026-10-06', 'US': '2026-10-06'}
        common = dict(market='US', symbol='SYNTH', name='합성 종목', currency='USD', date=targets['US'],
                      created_at='2026-10-07T00:00:00+00:00', close=11, threshold=12, atr=1, tolerance=.5,
                      reason='합성 완료봉 사건')
        trailing = dict(common, id='trail', kind='trailing', policy='high95-approach-v2', alert_threshold=12.5, high=13)
        scenario = dict(common, id='scenario', kind='scenario', monitor_id='m1', monitor_revision=2,
                        state='reached', boundary='entry', purpose='buy', case_id='an_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa')
        technical = dict(common, id='technical', kind='technical', purpose='buy', rule_id='cci_recovery',
                         label='합성 기술 신호', stock_key='US:SYNTH')
        inactive = dict(scenario, id='inactive', monitor_id='m2')
        outdated = dict(scenario, id='outdated', monitor_revision=1)
        tested = dict(scenario, id='tested', test_record=True)
        old = dict(trailing, id='expired', date='2026-10-05')
        monitors = [dict(id='m1', active=True, revision=2, issue=None), dict(id='m2', active=False, revision=2, issue=None)]
        config = dict(enabled_at='2026-10-06T00:00:00+00:00')
        sent = []
        def sender(text):
            sent.append(text)
            return {'message_id': len(sent)}
        with patch.object(TrailingStore, 'delivery_events', side_effect=lambda _: [dict(trailing), dict(old)]), \
             patch.object(ScenarioStore, 'read', return_value={'monitors': monitors, 'events': [scenario, inactive, outdated, tested]}), \
             patch('signals.store.delivery_events', return_value=[technical]):
            result = delivery.dispatch(self.root, targets, config=config, sender=sender)
            self.assertEqual(result['counts'], {'sent': 3, 'expired': 1}, result)
            self.assertEqual(len(sent), 3)
            self.assertTrue(any('0.5 ATR' in body for body in sent))
            self.assertTrue(any('시나리오' in body for body in sent))
            self.assertTrue(any('합성 기술 신호' in body for body in sent))
            delivery.dispatch(self.root, targets, config=config, sender=sender)
            self.assertEqual(len(sent), 3)
            ledger = self.root / 'runtime/telegram-delivery/delivery.sqlite3'
            with closing(sqlite3.connect(ledger)) as db, db:
                db.execute("UPDATE deliveries SET state='sending' WHERE id='trail'")
            result = delivery.dispatch(self.root, targets, config=config, sender=sender)
            self.assertEqual(result['counts']['unknown'], 1)
            self.assertEqual(len(sent), 3)
            delivery.dispatch(self.root, targets, config=config, sender=sender)
            self.assertEqual(len(sent), 3)
            with closing(sqlite3.connect(ledger)) as db:
                self.assertEqual({row[0] for row in db.execute('SELECT id FROM deliveries')},
                                 {'trail', 'scenario', 'technical', 'expired'})

    def test_historical_records_unchanged(self):
        from drawings.store import WorkspaceStore, defaults
        from analysis.service import AnalysisService
        path = self.root / 'runtime/chart-workspaces.sqlite3'
        store = WorkspaceStore(path)
        service = AnalysisService(store, None, None, lambda: ({}, {}), self.root)
        frozen = dict(id='sn_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', revision=7, plan_version=3, body='합성 과거 본문', paths=['old/snapshot.json'])
        body = json.dumps(frozen, ensure_ascii=False)
        tables = ('trade_cycles', 'trade_cycle_drafts', 'trade_cycle_events', 'trade_cycle_ledger', 'trade_cycle_alerts')
        with closing(store.connect()) as db, db:
            for table in tables:
                db.execute('CREATE TABLE ' + table + ' (id TEXT PRIMARY KEY, revision INTEGER, payload TEXT, sha256 TEXT)')
                db.execute('INSERT INTO ' + table + ' VALUES (?,?,?,?)', ('historical-id', 7, body, sha(body.encode())))
            db.execute('INSERT INTO chart_workspaces VALUES (?,?,?,?)', ('US:SYNTH', 4, json.dumps(defaults()), '2026-10-01T00:00:00Z'))
            db.execute('INSERT INTO analysis_cases VALUES (?,?,?)', ('an_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'US:SYNTH', json.dumps({'id': 'an_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'stock_key': 'US:SYNTH'})))
            db.execute('INSERT INTO analysis_snapshots VALUES (?,?,?,?)', ('sn_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', 'an_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', body, sha(body.encode())))
            db.execute('INSERT INTO analysis_items VALUES (?,?,?,?)', ('plan_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'an_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'plan', body))
        def dump():
            with closing(sqlite3.connect(path)) as db:
                return '\n'.join(db.iterdump()).encode()
        before = dump()
        before_bytes = sha(path.read_bytes())
        # Real retained initialization and read paths must not alter old rows.
        service = AnalysisService(store, None, None, lambda: ({}, {}), self.root)
        self.assertEqual(store.read('US:SYNTH')['revision'], 4)
        self.assertEqual(service.case('an_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa')['stock_key'], 'US:SYNTH')
        self.assertEqual(service.rows('analysis_snapshots')[0], frozen)
        for op in CYCLE_OPS:
            with self.assertRaisesRegex(ValueError, 'unknown_operation'):
                service.dispatch(op, {'analysis_key': 'an_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'}, '')
        self.assertEqual(dump(), before)
        self.assertEqual(sha(path.read_bytes()), before_bytes)
        with closing(sqlite3.connect(path)) as db:
            actual = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE name LIKE 'trade_cycle%'")}
        self.assertEqual(actual, set(tables))

    def test_preserved_contracts_and_root_allowlist(self):
        from atomic_io import atomic_write
        from market import adr, classification_refresh, daily
        from invest_agent.trading.market_provider import required_daily_bars
        self.assertEqual((required_daily_bars('KR'), required_daily_bars('US')), (365, 325))
        self.assertEqual((len(ROOT_KEEP), len(ROOT_RETIRED), len(ROOT_KEEP | ROOT_RETIRED)), (7, 33, 40))
        self.assertFalse(ROOT_KEEP & ROOT_RETIRED)
        for path, expected in UNCHANGED.items():
            self.assertEqual(sha((ROOT / path).read_text(encoding='utf-8').encode()), expected, path)
        for path, functions in HELPERS.items():
            tree = ast.parse((ROOT / path).read_text(encoding='utf-8'))
            actual = {n.name: sha(ast.dump(n, include_attributes=False).encode()) for n in tree.body if isinstance(n, ast.FunctionDef)}
            for name, expected in functions.items():
                self.assertEqual(actual[name], expected, (path, name))
        row = dict(market='US', symbol='ABC', isin_code='USABC123', listing_exchange_family='NYSE', security_type='DEPOSITARY_RECEIPT')
        statistics = b'<html>NYSE: ABC &middot; Real-Time Price &middot; USD At close: Oct 5, 2026, 4:00 PM EDT<table><tr><td>Market Cap</td><td title="2,010,123,456">2.01B</td></tr></table>Last updated: Oct 6, 2026</html>'
        profile = b'<html>NYSE: ABC &middot; Real-Time Price &middot; USD<table><tr><td>Ticker Symbol</td><td>ABC</td></tr><tr><td>ISIN Number</td><td>USABC123</td></tr><tr><td>Sector</td><td>Financials</td></tr><tr><td>Industry</td><td>Banks - Regional</td></tr></table></html>'
        parsed = adr.parse_pages(statistics, profile, row, '2026-10-05')
        self.assertEqual((parsed['market_cap'], parsed['sector']), (2010123456, 'Financial'))
        with self.assertRaises(ValueError):
            adr.parse_pages(statistics, profile.replace(b'USABC123', b'OTHER'), row, '2026-10-05')
        html = '<html><title>ABC - Fixture</title><a href="?f=sec_technology">Technology</a><a href="?f=ind_software">Software</a><a href="?f=exch_nyse">NYSE</a><a href="https://www.google.com/finance/quote/ABC:NYSE">Identity</a></html>'
        self.assertEqual(classification_refresh.parse_alias_quote(html, 'ABC', 'ABC', 'NYSE')['sector'], 'Technology')
        self.assertEqual(classification_refresh.classification_freshness([{'observed_at': '2026-10-05T00:00:00Z'}], '2026-10-06T00:00:00Z')['state'], 'recent')
        target = self.root / 'atomic.txt'
        target.write_text('original', encoding='utf-8')
        import atomic_io
        replace = atomic_io.os.replace
        attempts = []
        def locked_once(source, destination):
            attempts.append(1)
            if len(attempts) == 1:
                raise PermissionError('synthetic reader lock')
            replace(source, destination)
        with patch.object(atomic_io.os, 'replace', side_effect=locked_once), patch.object(atomic_io.time, 'sleep'):
            atomic_write(target, 'replacement')
        self.assertEqual(target.read_text(), 'replacement')
        self.assertEqual(len(attempts), 2)
        with patch.object(atomic_io.os, 'replace', side_effect=PermissionError('locked')), patch.object(atomic_io.time, 'sleep'):
            with self.assertRaises(PermissionError):
                atomic_write(target, 'must not publish')
        self.assertEqual(target.read_text(), 'replacement')
        self.assertFalse(list(self.root.glob('atomic.txt.*.tmp')))
        # Current hashes name existing files only; frozen prior hashes are not rewritten.
        previous = self.root / 'past-manifest.json'
        write(previous, {'code_hashes': {'deleted/runner.py': 'historic-sha'}})
        before = previous.read_bytes()
        with patch.object(daily, 'ROOT', self.root):
            hashes = daily.code_hashes(self.root)
        self.assertTrue(hashes)
        self.assertTrue(all(Path(path).is_file() for path in hashes))
        self.assertEqual(previous.read_bytes(), before)
        self.assertEqual(json.loads((self.root / 'config.json').read_text())['records']['previous_engine_roots'], [str(self.root / 'previous-engine')])


if __name__ == '__main__':
    unittest.main()
