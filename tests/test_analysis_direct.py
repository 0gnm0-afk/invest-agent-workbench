"""Direct MCP service integration, only temporary DBs and synthetic observations."""
from contextlib import closing, ExitStack
from copy import deepcopy
from pathlib import Path
import json
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from analysis import Forbidden
from analysis.direct import call, SelectedHistories, SelectedArchive
from analysis.direct_accounts import saved_accounts
from analysis.service import AnalysisService
from accounts.store import AccountStore
from drawings.store import WorkspaceStore, Conflict
from market.prices import PriceStore
from market.history.chart import aggregate

FIXTURE = json.loads((Path(__file__).parent / 'fixtures/collaboration-v1.json').read_text(encoding='utf-8'))


class DirectAnalysisTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.prices = PriceStore(self.root / 'runtime/ohlcv.sqlite3')
        self.bars = [dict(date=d, open=100., high=112., low=90., close=105., volume=1000.)
                     for d in ('2026-09-18', '2026-09-21', '2026-09-22', '2026-09-23')]
        self.stock = dict(market='US', symbol='SYNTH', name='Synthetic only', currency='USD',
                          provider='Toss OpenAPI candles', price_basis='adjusted', isin_code='US0000000001', bars=self.bars)
        self.prices.put(self.stock, source='synthetic', kind='series')
        self.prices.put({**self.stock, 'symbol': 'OTHER', 'isin_code': 'US0000000002'}, source='synthetic-other', kind='series')
        hist = SimpleNamespace(saved=SimpleNamespace(stocks={}), prices=self.prices,
                               view=lambda s, f: {**s, 'bars': aggregate(s['bars'], f), 'timeframe': f, 'history': {'state': 'complete'}})
        self.fin = SimpleNamespace(read=lambda k: {'state': 'not_requested', 'result': None})
        view = lambda: ({'accounts': {'as_of': None, 'accounts': [], 'stale': True}, 'market': {'state': 'saved_only'}}, {'US:SYNTH': self.stock})
        self.service = AnalysisService(WorkspaceStore(self.root / 'runtime/chart-workspaces.sqlite3'), hist, self.fin, view, self.root)
        detail = self.service.dispatch('create', {'stock_key': 'US:SYNTH', 'test_record': True}, 'http://fixture')
        self.cid, self.sid = detail['case']['id'], detail['snapshots'][0]['id']
        self.guards = ExitStack()
        self.addCleanup(self.guards.close)
        for name in ('subprocess.Popen', 'urllib.request.urlopen', 'market.history.chart.HistoryStore.__init__',
                     'market.history.saved.SavedMarketHistories.__init__'):
            self.guards.enter_context(patch(name, side_effect=AssertionError('external/dashboard/all-market dependency')))
        self.guards.enter_context(patch('market.scope.match', return_value=None))

    def invoke(self, op, args, **options):
        return call(op, args, root=self.root, **options)

    def test_historical_reads_preserve_ids_without_loading_current_prices(self):
        before = self.service.snapshot(self.cid, self.sid)
        with patch.object(SelectedHistories, 'select', side_effect=AssertionError('current prices loaded')):
            history = self.invoke('detail', {'analysis_key': self.cid})
            value = self.invoke('read', {'analysis_key': self.cid, 'snapshot_id': self.sid, 'section': 'chart'})
            discussion = self.invoke('collaboration_read', {'analysis_key': self.cid})
            listed = self.invoke('list', {'stock_key': 'US:SYNTH'})
        self.assertIsNone(history['links']['dashboard_url'])
        self.assertEqual(history['links']['transport'], 'local_db')
        self.assertEqual(value['chart']['bars'], before['chart']['bars'])
        self.assertEqual(value['snapshot_id'], self.sid)
        self.assertEqual(discussion['revision'], 0)
        self.assertEqual(listed['cases'][0]['id'], self.cid)

    def test_current_capture_loads_one_stock_and_pins_without_price_writes(self):
        before = self.prices.path.read_bytes()
        seen = []
        original = SelectedHistories.select
        def select(instance, key):
            seen.append(key)
            return original(instance, key)
        with patch.object(SelectedHistories, 'select', select), patch('analysis.direct.LazyFinancials.__getattr__', lambda _, name: getattr(self.fin, name)):
            value = self.invoke('read', {'analysis_key': self.cid, 'section': 'chart', 'limit': 2})
        self.assertEqual(seen, ['US:SYNTH'])
        self.assertEqual(value['total'], 4)
        self.assertEqual(value['next_offset'], 2)
        self.assertNotEqual(value['snapshot_id'], self.sid)
        self.assertEqual(before, self.prices.path.read_bytes())
        replay = self.invoke('read', {'analysis_key': self.cid, 'snapshot_id': value['snapshot_id'], 'section': 'chart', 'offset': 2})
        self.assertEqual([b['date'] for b in replay['chart']['bars']], ['2026-09-22', '2026-09-23'])
        self.assertEqual(self.service.snapshot(self.cid, self.sid)['chart']['bars'], self.bars)

    def test_discussion_and_plan_use_existing_revision_and_never_adopt(self):
        envelope = dict(analysis_key=self.cid, snapshot_id=self.sid, expected_revision=0, contract_version='collaboration.v1')
        discussion = self.invoke('discussion_save', {**envelope, **deepcopy(FIXTURE['discussion'])})
        self.assertTrue(discussion['verified'])
        with self.assertRaises(Conflict):
            self.invoke('discussion_save', {**envelope, **deepcopy(FIXTURE['discussion'])})
        current = self.invoke('collaboration_read', {'analysis_key': self.cid})
        plan = self.invoke('plan_save', {**envelope, 'expected_revision': current['revision'], 'plan': deepcopy(FIXTURE['plan']),
                                      'references': [], 'plan_id': None, 'expected_plan_version': 0})
        self.assertTrue(plan['verified'])
        reopened = self.invoke('collaboration_read', {'analysis_key': self.cid})
        self.assertEqual(reopened['active_plans'], [])
        self.assertEqual(reopened['plans'][0]['id'], plan['record']['id'])

    def test_unexposed_authority_and_risk_writes_fail_before_store_access(self):
        with patch('analysis.direct.service_for', side_effect=AssertionError('store accessed')):
            for op in ('decide', 'holding', 'plan_decide', 'monitor_register', 'cycle_fill', 'cycle_approve', 'order'):
                with self.assertRaises(Forbidden):
                    self.invoke(op, {})
            with self.assertRaises(Forbidden):
                self.invoke('read', {'analysis_key': self.cid}, role='risk')
            with self.assertRaises(Forbidden):
                self.invoke('discussion_save', {}, role='risk')
            with self.assertRaisesRegex(ValueError, 'retired_risk_operation'):
                self.invoke('risk_calculate', {})
        value = self.invoke('read', {'analysis_key': self.cid, 'snapshot_id': self.sid}, role='risk')
        self.assertEqual(value['snapshot_id'], self.sid)

    def test_explicit_refresh_only_uses_existing_selected_contract(self):
        service = SimpleNamespace(case=lambda _: {'stock_key': 'US:SYNTH'},
                                  histories=SimpleNamespace(select=lambda key: self.assertEqual(key, 'US:SYNTH')),
                                  dispatch=lambda op, args, origin, principal: {'op': op, 'mode': args['mode'], 'role': principal})
        with patch('analysis.direct.service_for', return_value=service):
            for mode in ('financial_sources', 'financial_price_recalculate', 'price_history'):
                value = self.invoke('refresh', {'analysis_key': self.cid, 'mode': mode})
                self.assertEqual(value, {'op': 'refresh', 'mode': mode, 'role': 'collaboration'})

    def test_selected_archive_extracts_only_requested_catalog_entries(self):
        catalog = dict(source='synthetic-archive', config={'end': '2026-09-23'},
                       listings={'SYNTH': {'name': 'One'}, 'OTHER': {'name': 'Two'}},
                       coverage={'SYNTH': {'bars': 4}, 'OTHER': {'bars': 999}}, references={})
        self.prices.set_meta('archive_US', catalog)
        selected = SelectedArchive(self.prices, 'US', 'SYNTH')
        self.assertEqual(selected.listings, {'SYNTH': {'name': 'One'}})
        self.assertEqual(selected.coverage, {'SYNTH': {'bars': 4}})

    def test_saved_accounts_preserve_failed_attempt_and_never_read_raw(self):
        store = AccountStore(self.root / 'runtime/accounts.sqlite3')
        snapshot = dict(provider='NH PLUG', as_of='2026-09-18T00:00:00+00:00', accounts_complete=True,
                        accounts=[{'alias': 'synthetic', 'positions': [], 'holdings_complete': True, 'account_number': 'fixture-private'}])
        request = store.begin('nh', 'synthetic-success', 'mcp')
        store.complete('nh', 'synthetic-success', request['lease'], metadata={'query_state': 'received', 'attempted_at': snapshot['as_of']}, snapshot=snapshot,
                       raw={'private': 'synthetic raw data must not appear'})
        request = store.begin('nh', 'synthetic-failed', 'mcp')
        store.complete('nh', 'synthetic-failed', request['lease'], metadata={'query_state': 'failed', 'attempted_at': '2026-09-19T00:00:00+00:00', 'reason': 'fixture_failure'})
        before = store.path.read_bytes()
        result = saved_accounts(self.root)
        self.assertEqual(result['as_of'], snapshot['as_of'])
        self.assertEqual(result['query_state'], 'failed')
        self.assertEqual(result['attempted_at'], '2026-09-19T00:00:00+00:00')
        self.assertTrue(result['stale'])
        self.assertNotIn('fixture-private', json.dumps(result))
        self.assertNotIn('synthetic raw', json.dumps(result))
        self.assertEqual(before, store.path.read_bytes())

    def test_both_saved_providers_merge_read_only_without_hiding_partial_failure(self):
        store = AccountStore(self.root / 'runtime/accounts.sqlite3')
        times = {'nh': '2026-09-18T00:00:00+00:00', 'meritz': '2026-09-19T00:00:00+00:00'}
        for provider, label, rate in (('nh', 'NH PLUG', '1400'), ('meritz', 'Meritz', '1500')):
            snapshot = dict(provider=label, as_of=times[provider], accounts_complete=True,
                            base_currency='KRW', fx_to_base={'KRW': '1', 'USD': rate},
                            accounts=[{'alias': 'synthetic-' + provider, 'positions': [],
                                       'holdings_complete': True, 'cash_complete': True}])
            request = store.begin(provider, 'synthetic-success', 'mcp')
            store.complete(provider, 'synthetic-success', request['lease'], snapshot=snapshot,
                           metadata={'query_state': 'received', 'attempted_at': times[provider]})
        request = store.begin('meritz', 'synthetic-failed', 'mcp')
        attempted = '2026-09-20T00:00:00+00:00'
        store.complete('meritz', 'synthetic-failed', request['lease'],
                       metadata={'query_state': 'failed', 'attempted_at': attempted, 'reason': 'synthetic_transport'})
        before = store.path.read_bytes()
        with patch('analysis.direct_accounts.ReadOnlyAccountStore.save', side_effect=AssertionError('read-only merge attempted a write')):
            result = saved_accounts(self.root)
        self.assertEqual(before, store.path.read_bytes())
        accounts = {account['alias']: account for account in result['accounts']}
        self.assertEqual(set(accounts), {'synthetic-nh', 'synthetic-meritz'})
        self.assertEqual(result['as_of'], times['nh'])
        self.assertEqual(result['query_state'], 'partial')
        self.assertNotIn('snapshot_ref', result)
        self.assertNotIn('USD', result['valuation']['fx_to_base'])
        self.assertEqual(accounts['synthetic-nh']['fx_to_base']['USD'], '1400')
        self.assertEqual(accounts['synthetic-meritz']['fx_to_base']['USD'], '1500')
        self.assertEqual(accounts['synthetic-nh']['as_of'], times['nh'])
        self.assertEqual(accounts['synthetic-meritz']['as_of'], times['meritz'])
        self.assertTrue(accounts['synthetic-nh']['holdings_complete'])
        self.assertFalse(accounts['synthetic-meritz']['holdings_complete'])
        self.assertEqual(result['providers']['meritz']['query_state'], 'failed')
        self.assertEqual(result['providers']['meritz']['attempted_at'], attempted)
        original = store.read(store.latest('meritz')['snapshot_ref'])
        self.assertTrue(original['accounts'][0]['holdings_complete'])


if __name__ == '__main__':
    unittest.main()
