"""M10 HTTP and refresh contracts. Every server/store uses synthetic OS temp data."""
import copy
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
import http.client
import json
import sqlite3
from pathlib import Path
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from analysis.service import AnalysisService
from drawings.store import WorkspaceStore, defaults
from market.prices import PriceStore
from web.server import handler_for, ThreadingHTTPServer
from web.refresh import DashboardDaily
import web.data as data_module

ASSET_ROOT = Path(__file__).resolve().parents[1] / 'dashboard'


def synthetic_bars():
    rows, day = [], date(2026, 1, 5)
    while len(rows) < 40:
        if day.weekday() < 5:
            close = 100 + len(rows) * 2
            rows.append(dict(date=day.isoformat(), open=close, high=close+3,
                             low=close-2, close=close, volume=100))
        day += timedelta(days=1)
    return rows


class DashboardContract(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='m10-contract-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'chart-workspaces.sqlite3'
        self.rows = synthetic_bars()
        self.stock = dict(market='US', symbol='SYNTH', name='합성 <회사>', currency='USD',
                          price_basis='adjusted', provider='synthetic', isin_code='US0000000001',
                          bars=self.rows)
        self.accounts = dict(as_of='2026-01-03T00:00:00+00:00', query_state='failed',
                             reason='saved_attempt_failed', stale=True,
                             accounts=[dict(alias='synthetic', account_number='private-sentinel', positions=[])])
        self.data = dict(generated_at='2026-03-01T00:00:00+00:00',
                         market=dict(state='partial', session_dates={'US': self.rows[-1]['date']},
                                     quality={'received': 1}, entries=['retired-candidate'],
                                     sectors=['retired-sector'], accounts=['private-sentinel']),
                         accounts=self.accounts, candidates=['retired-candidate'],
                         trailing={'state': 'saved'}, daily_update={'state': 'saved'})
        self.stocks = {'US:SYNTH': self.stock}
        self.views = {'view': (self.data, self.stocks)}
        self.hist = SimpleNamespace(saved=SimpleNamespace(stocks={}, markets={}),
                                    view=Mock(side_effect=lambda stock, frame: {**copy.deepcopy(stock), 'timeframe': frame}),
                                    ensure=Mock(side_effect=AssertionError('external history forbidden')))
        self.fin = SimpleNamespace(read=Mock(return_value={'state':'unavailable', 'result':None}),
                                   start=Mock(side_effect=AssertionError('external financial forbidden')))
        self.retry = Mock(return_value={'state': 'running'})
        self.launch = Mock(return_value={'state': 'saved'})
        self.server = None

    def start(self, *, offline=False):
        handler = handler_for(self.data, self.stocks, ASSET_ROOT, 'fixture-token', self.path,
                              views=self.views, root=self.root, offline=offline,
                              daily_retry=self.retry, launch=self.launch,
                              analysis_dependencies={'histories':self.hist, 'financials':self.fin})
        # The fixture has a numeric loopback address; skip HTTPServer reverse DNS.
        with patch('socket.getfqdn', return_value='127.0.0.1'):
            self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)
        self.origin = 'http://127.0.0.1:' + str(self.server.server_port)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)

    def request(self, path, *, method='GET', value=None, raw=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=10)
        body = raw if raw is not None else (json.dumps(value, ensure_ascii=False).encode('utf-8') if value is not None else None)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        result = response.status, response.read(), response.getheaders()
        connection.close()
        return result

    def post(self, path, value=None, *, raw=None, headers=None):
        supplied = {'Origin':self.origin, 'X-Workbench-Token':'fixture-token', 'Content-Type':'application/json'}
        supplied.update(headers or {})
        return self.request(path, method='POST', value=value, raw=raw, headers=supplied)

    def test_home_is_market_projection_and_keeps_internal_observation(self):
        self.start()
        before = copy.deepcopy(self.views)
        status, body, _ = self.request('/api/home')
        self.assertEqual(status, 200)
        value = json.loads(body)
        self.assertEqual(set(value), {'generated_at', 'market', 'daily_update'})
        self.assertEqual(value['market']['session_dates'], {'US':self.rows[-1]['date']})
        self.assertEqual(value['market']['quality'], {'received':1})
        self.assertEqual(value['generated_at'], self.data['generated_at'])
        for retired in ('private-sentinel', 'retired-candidate', 'retired-sector', 'accounts', 'trailing'):
            self.assertNotIn(retired, body.decode())
        self.assertEqual(self.views, before)

    def test_retired_apis_and_assets_are_unreachable(self):
        self.start()
        paths = ('/api/screen', '/api/screening', '/api/sectors', '/api/sector/US', '/api/focus',
                 '/api/cycles', '/api/cycle/old', '/api/accounts', '/api/account/old', '/api/risk',
                 '/api/market-exclusions', '/risk-workbench.js', '/risk-workbench.css',
                 '/market-exclusions.js', '/market-exclusions.css', '/trade-cycle.js', '/trade-cycle.css')
        for path in paths:
            with self.subTest(path=path):
                self.assertEqual(self.request(path)[0], 404)
                self.assertEqual(self.post(path, {})[0], 405)
        for op in ('cycle_list', 'cycle_read', 'risk_overview', 'risk_calculate', 'risk_save'):
            self.assertEqual(self.post('/api/analysis-rpc', {'op':op, 'arguments':{}})[0], 400)

    def test_l1_parameters_and_service_states_pass_through(self):
        self.start()
        for state in ('ok', 'insufficient_data', 'invalid_data', 'solver_failed'):
            result = dict(status=state, stored=False, signal=False, lines=[],
                          window={'start':'2026-01-05', 'end':'2026-02-27', 'timeframe':'1w'})
            with patch('web.server.l1_trendlines', return_value=result) as l1, \
                    patch('scenarios.monitor.ScenarioStore.sync_workspace', side_effect=AssertionError('monitor write')):
                status, body, _ = self.request('/api/l1-trendlines/US%3ASYNTH?start=2026-01-05&end=2026-02-27&timeframe=1w')
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(body), result)
                l1.assert_called_once_with('US:SYNTH', root=self.root, start='2026-01-05', end='2026-02-27', timeframe='1w')
        self.hist.view.assert_not_called()
        self.hist.ensure.assert_not_called()
        self.fin.start.assert_not_called()

    def test_l1_rejects_invalid_or_duplicate_windows_before_service(self):
        self.start()
        bad = ('start=bad', 'end=2026-02-30', 'start=2026-1-05', 'start=2026-02-20&end=2026-01-05',
               'start=2026-01-05&start=2026-01-06', 'timeframe=1d&timeframe=1w', 'timeframe=4h',
               'end=', 'start=', 'timeframe=', 'extra=true')
        with patch('web.server.l1_trendlines') as l1:
            for query in bad:
                with self.subTest(query=query):
                    self.assertEqual(self.request('/api/l1-trendlines/US%3ASYNTH?' + query)[0], 400)
            self.assertEqual(self.request('/api/l1-trendlines/US%3AMISSING')[0], 404)
            self.assertEqual(self.post('/api/l1-trendlines/US%3ASYNTH', {})[0], 405)
            l1.assert_not_called()

    def test_l1_real_saved_read_does_not_change_any_fixture_file(self):
        prices = PriceStore(self.root/'runtime/ohlcv.sqlite3', root=self.root)
        prices.put(self.stock, source='synthetic-m10')
        # Make the synthetic DB byte-stable: SQLite mode=ro on a WAL database
        # can create shared-memory/empty-WAL coordination files without data writes.
        with closing(sqlite3.connect(prices.path)) as db:
            db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            db.execute('PRAGMA journal_mode=DELETE')
        self.start()
        def files():
            return {str(p.relative_to(self.root)):p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        before = files()
        real_connect = PriceStore.connect
        def read_only(store, *, write=False):
            self.assertFalse(write)
            return real_connect(store, write=False)
        from drawings.l1_trendlines import l1_trendlines
        def fixed_l1(*args, **kwargs):
            return l1_trendlines(*args, now=datetime(2026, 3, 1, tzinfo=timezone.utc), **kwargs)
        with patch('web.server.l1_trendlines', side_effect=fixed_l1), \
                patch.object(PriceStore, 'connect', read_only), \
                patch('drawings.store.WorkspaceStore.shared', side_effect=AssertionError('workspace read/write not needed')), \
                patch('scenarios.monitor.ScenarioStore.sync_workspace', side_effect=AssertionError('monitor write')):
            status, body, _ = self.request('/api/l1-trendlines/US%3ASYNTH?start=2026-01-05&end=2026-02-27&timeframe=1d')
        self.assertEqual(status, 200)
        value = json.loads(body)
        self.assertEqual(value['status'], 'ok')
        self.assertFalse(value['stored'])
        self.assertFalse(value['signal'])
        self.assertEqual(value['window']['timeframe'], '1d')
        self.assertGreater(len(value['lines']), 0)
        self.assertTrue(value['lines'][-1]['in_progress'])
        self.assertEqual(value['lines'][-1]['line_style'], 'dashed')
        after = files()
        changed = sorted(path for path in set(before) | set(after) if before.get(path) != after.get(path))
        self.assertEqual(changed, [], 'L1 changed fixture files: ' + ', '.join(changed))
        self.hist.ensure.assert_not_called()
        self.hist.view.assert_not_called()

    def test_write_security_json_size_and_confirmation_boundaries(self):
        self.start()
        path = '/api/chart-state/US%3ASYNTH'
        body = {'revision':0, 'state':defaults()}
        for headers in ({'Origin':'https://evil.example'}, {'Host':'evil.example'}, {'X-Workbench-Token':'wrong'}):
            self.assertEqual(self.post(path, body, headers=headers)[0], 403)
        self.assertEqual(self.post(path, body, headers={'Content-Type':'text/plain'})[0], 415)
        self.assertEqual(self.post(path, raw=b'x'*131073)[0], 413)
        self.assertEqual(self.post(path, raw=b'')[0], 413)
        self.assertEqual(self.post(path, raw=b'{broken')[0], 400)
        fake_dispatch = Mock(return_value={'confirmed':True})
        args = {'op':'decide', 'arguments':{'author':'user'}}
        with patch('web.server.AnalysisService.dispatch', fake_dispatch):
            self.assertEqual(self.post('/api/analysis-rpc', args)[0], 403)
            self.assertEqual(self.post('/api/analysis-rpc', args, headers={'Sec-Fetch-Site':'same-origin', 'Cookie':'workbench_confirm_'+str(self.server.server_port)+'=forged'})[0], 403)
            _, _, headers = self.request('/health', headers={'Sec-Fetch-Site':'same-origin'})
            cookies = '; '.join(v.split(';', 1)[0] for k,v in headers if k.lower() == 'set-cookie')
            self.assertEqual(self.post('/api/analysis-rpc', args, headers={'Sec-Fetch-Site':'same-origin', 'Cookie':cookies})[0], 200)
            fake_dispatch.assert_called_once()
            self.assertEqual(fake_dispatch.call_args.kwargs['principal'], 'browser')

    def test_successful_drawing_syncs_once_and_conflict_never_syncs(self):
        self.start()
        state = defaults()
        state['drawings'] = [dict(id='fixture-line', kind='horizontal', points=[{'time':'2026-01-05','price':100}],
                                  color='#218775', width=2, extend=False)]
        path = '/api/chart-state/US%3ASYNTH'
        revision = json.loads(self.request(path)[1])['revision']
        original = WorkspaceStore.shared
        calls = []
        def saved(store, *args, **kwargs):
            calls.append((args, kwargs))
            return original(store, *args, **kwargs)
        with patch.object(WorkspaceStore, 'shared', saved), \
                patch('scenarios.monitor.ScenarioStore.sync_workspace') as sync:
            status, body, _ = self.post(path, {'revision':revision, 'state':state})
            self.assertEqual(status, 200)
            self.assertEqual(len(calls), 1)
            sync.assert_called_once_with('US:SYNTH')
            saved_revision = json.loads(body)['revision']
            sync.reset_mock()
            self.assertEqual(self.post(path, {'revision':revision, 'state':defaults()})[0], 409)
            sync.assert_not_called()
        stored = json.loads(self.request(path)[1])
        self.assertEqual(stored['revision'], saved_revision)
        self.assertEqual(stored['state'], state)

    def test_offline_blocks_refresh_but_keeps_saved_drawings(self):
        self.start(offline=True)
        self.assertEqual(self.post('/api/financials/US%3ASYNTH', {'refresh':True})[0], 403)
        self.assertEqual(self.post('/api/analysis-rpc', {'op':'refresh','arguments':{}})[0], 403)
        self.assertIn(self.post('/api/launch', {'daily_refresh':True,'retry_market':False,'saved_accounts':False})[0], (403,409))
        self.assertIn(self.post('/api/daily-update', {'retry':True,'attempt_id':'fixture'})[0], (403,409))
        self.launch.assert_not_called()
        self.retry.assert_not_called()
        revision = json.loads(self.request('/api/chart-state/US%3ASYNTH')[1])['revision']
        with patch('scenarios.monitor.ScenarioStore.sync_workspace'):
            self.assertEqual(self.post('/api/chart-state/US%3ASYNTH', {'revision':revision,'state':defaults()})[0], 200)
        self.assertEqual(self.post('/api/chart-history/US%3ASYNTH', {'retry':True})[0], 200)
        self.hist.ensure.assert_not_called()
        self.fin.start.assert_not_called()

    def test_launch_cannot_forward_account_refresh(self):
        self.start()
        for saved_accounts in (False, True):
            self.assertEqual(self.post('/api/launch', {'daily_refresh':True, 'retry_market':False, 'saved_accounts':saved_accounts})[0], 202)
            self.launch.assert_called_with(daily_refresh=True, retry_market=False, saved_accounts=True)

    def test_chart_financial_reads_preserve_snapshot_plan_and_monitor_refs(self):
        from analysis.collaboration import VERSION
        from scenarios.monitor import ScenarioStore
        self.start()
        store = WorkspaceStore(self.path)
        state = defaults()
        state['drawings'] = [dict(id='fixture-line', kind='horizontal', points=[{'time':'2026-01-05','price':100}],
                                  color='#218775', width=2, extend=False)]
        workspace = store.shared('US:SYNTH')
        workspace = store.shared('US:SYNTH', revision=workspace['revision'], state=state)
        service = AnalysisService(store, self.hist, self.fin, lambda:self.views['view'], self.root)
        detail = service.dispatch('create', {'stock_key':'US:SYNTH','test_record':True}, self.origin)
        cid, sid = detail['case']['id'], detail['snapshots'][0]['id']
        hypothesis = dict(verification_date=None, invalidation_statement='반증 <조건> & 원문',
                          pre_verification_allocation='전체 투자자산(현금 포함)의 5% 이내')
        plan = dict(title='합성 계획', purpose='매매', account_alias=None,
                    allocation={'quantity':None,'basis':None}, conditions=[], scenarios=[], hypothesis=hypothesis)
        record = service.dispatch('plan_save', dict(analysis_key=cid, contract_version=VERSION,
                    snapshot_id=sid, expected_revision=0, plan_id=None, expected_plan_version=0,
                    plan=plan, references=[]), self.origin, principal='collaboration')['record']
        review = service.dispatch('plan_review', dict(analysis_key=cid, plan_record_id=record['id'], expected_revision=1),
                    self.origin, principal='browser', session='fixture')
        service.dispatch('plan_decide', dict(analysis_key=cid, contract_version=VERSION, expected_revision=1,
                    confirmation=review['confirmation'], action='adopt', reason='synthetic'),
                    self.origin, principal='browser', session='fixture')
        monitor = ScenarioStore(self.path)
        identity = dict(market='US',symbol='SYNTH',currency='USD',stable_id='US0000000001',price_basis='adjusted')
        monitor.register(stock_key='US:SYNTH', identity=identity, case_id=cid, plan_record_id=record['id'],
                         plan_id=record['plan_id'], plan_version=1, drawing_id='fixture-line',
                         workspace_revision=workspace['revision'], test_record=True, chart=self.stock)
        tables = ('analysis_snapshots','analysis_items','shared_chart_workspaces','scenario_monitors','scenario_monitor_history')
        def rows():
            with closing(store.connect()) as db:
                return {table:db.execute('SELECT * FROM '+table).fetchall() for table in tables}
        before = rows()
        with patch.object(ScenarioStore, 'register', side_effect=AssertionError('implicit monitor registration')):
            for path in ('/api/home','/api/stock/US%3ASYNTH','/api/chart/US%3ASYNTH','/api/chart-state/US%3ASYNTH','/api/financials/US%3ASYNTH'):
                self.assertEqual(self.request(path)[0], 200, path)
            status, body, _ = self.post('/api/analysis-rpc', {'op':'collaboration_read','arguments':{'analysis_key':cid}})
            self.assertEqual(status, 200)
            value = json.loads(body)
            self.assertEqual(value['active_plans'][0]['record']['plan']['hypothesis'], hypothesis)
            self.assertIn('resume_prompt', value['links'])
            self.assertNotIn('codex://', body.decode().lower())
        self.assertEqual(rows(), before)
        self.assertEqual(store.shared('US:SYNTH')['revision'], workspace['revision'])


class SavedAccountRefresh(unittest.TestCase):
    def test_build_never_refreshes_accounts_or_rewrites_warning_state(self):
        with tempfile.TemporaryDirectory(prefix='m10-build-') as temporary:
            root = Path(temporary)
            (root/'config.json').write_text(json.dumps({'engine':str(root), 'mother':str(root)}), encoding='utf-8')
            observation = dict(query_state='failed', as_of='2026-01-03T00:00:00+00:00',
                               stale=True, reason='saved_failure', accounts=[])
            warnings = dict(as_of='2026-01-04T00:00:00+00:00', account_as_of=observation['as_of'], state='saved')
            with patch('web.data.account_view', return_value=observation) as account, \
                    patch('web.data.saved_market', side_effect=lambda *a, **k: {'state':'partial','stocks':{}}), \
                    patch('alerts.trailing.TrailingStore') as store:
                store.return_value.public.return_value = warnings
                for refresh in (True, False):
                    _, data, _ = data_module.build(root, refresh=refresh, daily_update={'account_state':'failed'})
                    self.assertEqual(data['accounts'], observation)
                    self.assertEqual(data['trailing'], warnings)
                for call in account.call_args_list:
                    actual = call.kwargs.get('refresh', call.args[2] if len(call.args)>2 else False)
                    self.assertIs(actual, False)
                self.assertEqual(store.return_value.public.call_count, 2)
                store.return_value.update.assert_not_called()

    def test_start_reopen_and_receipt_following_never_request_accounts(self):
        now = datetime(2026, 3, 2, tzinfo=timezone.utc)
        record = dict(day='2026-03-02', state='succeeded', stage='finished', targets={},
                      attempt_id='synthetic', display_run='saved', account_state='failed', account_attempted=True)
        gate = SimpleNamespace(accounts=True, read=Mock(return_value=record),
                               once=Mock(return_value=record), display_run=Mock(return_value='saved'),
                               public=lambda row:dict(row))
        builder = Mock(return_value=(None, {'market':{'state':'succeeded'}, 'accounts':{'as_of':'old','query_state':'failed'}}, {}))
        views = {'view': ({'market':{'state':'succeeded'}, 'accounts':{'as_of':'old','query_state':'failed','stale':True}}, {})}
        controller = DashboardDaily(gate, views, builder, 'old', saved_accounts=False, now=lambda:now)
        controller.start()
        controller.thread.join(5)
        self.assertFalse(controller.thread.is_alive())
        self.assertFalse(gate.accounts)
        self.assertFalse(controller.status()['retry_available'])
        self.assertEqual(views['view'][0]['accounts']['as_of'], 'old')
        count = gate.once.call_count
        controller.launch(daily_refresh=False, retry_market=False, saved_accounts=False)
        if controller.thread: controller.thread.join(5)
        self.assertEqual(gate.once.call_count, count)
        self.assertFalse(gate.accounts)
        for call in builder.call_args_list:
            self.assertFalse(call.kwargs.get('refresh'))
        controller.launch(daily_refresh=True, retry_market=False, saved_accounts=False)
        controller.thread.join(5)
        self.assertFalse(gate.accounts)
        for call in builder.call_args_list:
            self.assertFalse(call.kwargs.get('refresh'))
