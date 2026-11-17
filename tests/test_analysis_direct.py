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
from analysis import Forbidden, digest
from analysis.direct import call, SelectedHistories, SelectedArchive
from analysis.direct_accounts import saved_accounts
from analysis.service import AnalysisService
from accounts import saved as account_saved
from accounts.store import AccountStore
from drawings.store import WorkspaceStore, Conflict
from market.prices import PriceStore, saved_bars
from market.history.chart import aggregate

# Inline synthetic fixture so the M00 source-only copy needs no data files.
FIXTURE = json.loads(r'''
{
  "contract_version": "collaboration.v1",
  "synthetic": true,
  "discussion": {
    "title": "합성 회사의 보유 이유 논의",
    "body": "사용자: 성장 논리가 유지되면 보유하되 하락 시 절반 축소를 검토하고 싶다. 에이전트: 어떤 봉의 확인가격과 어떤 물량의 절반인지 아직 정하지 않았다.",
    "sections": {"business_evidence": "분석 가설: 신규 제품의 반복 매출 증가. 실제 기업 사실 아님.", "judgment_invalidation": "성장 논리 훼손은 정성 재검토 조건.", "purpose_plan": "보유 목적 논의 중", "responses": "하락 시 절반 축소 검토. 판정 시점과 수량 기준 미정."},
    "open_questions": ["완료 일봉 기준인지, 최초 또는 잔여 물량의 절반인지?"],
    "references": [{"kind": "utterance", "reference": "synthetic-session:turn-1", "text": "성장 논리가 유지되면 보유하되 하락 시 절반 축소를 검토하고 싶다.", "speaker": "user"}]
  },
  "plan": {
    "title": "합성 USD 조건 대응 대안", "purpose": "매매", "account_alias": "synthetic-us",
    "allocation": {"quantity": "10", "basis": "units"},
    "conditions": [{"id": "c1", "kind": "price", "statement": "완료 일봉 종가가 90 USD 미만이면 최초 배정 물량 절반 축소", "action": "reduce", "evaluation": "daily_close", "operator": "lt", "price": "90", "currency": "USD", "quantity": {"value": "0.5", "basis": "initial_fraction"}, "priority": 1, "execution_price": "89", "drawing_ref": null}, {"id": "c2", "kind": "qualitative", "statement": "성장 논리 훼손 시 재검토", "action": "review", "evaluation": null, "operator": null, "price": null, "currency": null, "quantity": {"value": null, "basis": null}, "priority": 2, "execution_price": null, "drawing_ref": null}],
    "scenarios": [{"id": "s1", "evaluation_at": "2026-10-30", "assumed_price": "80", "currency": "USD", "response": "plan", "price_path": [{"at": "2026-10-06", "price": "89"}, {"at": "2026-10-30", "price": "80"}], "fx_to_base": null, "fx_as_of": null, "cost": null}]
  },
  "risk_review": {"title": "합성 계산 미정 사항", "body": "FX와 비용은 계좌 논의에서 보완. 실제 최대손실 아님.", "calculation": null, "references": []}
}
''')


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
        with patch('analysis.direct.LazyFinancials.__getattr__', lambda _, name: getattr(self.fin, name)):
            for _ in range(5):
                again = self.invoke('read', {'analysis_key': self.cid, 'section': 'chart', 'limit': 2})
                self.assertEqual(again['snapshot_id'], value['snapshot_id'])
                self.assertEqual(again['captured_at'], value['captured_at'])
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

    def test_current_read_uses_price_store_only(self):
        histories = SelectedHistories(self.root)
        with patch('analysis.direct.saved_bars', wraps=saved_bars) as read, \
             patch.object(histories.prices, 'archive_entry', wraps=histories.prices.archive_entry) as archive:
            first = histories.select('US:SYNTH')
            self.assertIs(histories.select('US:SYNTH'), first)
        read.assert_called_once_with('US:SYNTH', root=self.root)
        archive.assert_called_once_with('US', 'SYNTH')
        # Direct analysis owns no price SQL, including archive metadata lookup.
        source = (Path(__file__).resolve().parents[1] / 'scripts/analysis/direct.py').read_text(encoding='utf-8')
        self.assertNotIn('.execute(', source)
        with patch('analysis.direct.saved_bars', side_effect=AssertionError('current price lookup')):
            value = self.invoke('read', {'analysis_key': self.cid, 'snapshot_id': self.sid, 'section': 'chart'})
        self.assertEqual(value['chart']['bars'], self.bars)

    def test_saved_price_adapter_matches_previous_sql_and_fixed_reference(self):
        def previous_select(stock_key):
            # Frozen pre-M03 adapter oracle, over a synthetic store only.
            market, symbol = stock_key.split(':', 1)
            sid = self.prices.find(dict(market=market, symbol=symbol))
            with closing(self.prices.connect()) as db:
                db.execute('BEGIN')
                identity, metadata = map(json.loads, db.execute(
                    'SELECT identity,metadata FROM price_series WHERE id=?', (sid,)).fetchone())
                observed = json.loads(db.execute('SELECT metadata FROM price_observations WHERE series_id=? ORDER BY id DESC LIMIT 1', (sid,)).fetchone()[0])
                bars = [dict(zip(('date', 'open', 'high', 'low', 'close', 'volume'), row)) for row in db.execute(
                    'SELECT day,open,high,low,close,volume FROM prices WHERE series_id=? ORDER BY day', (sid,))]
            entry = self.prices.archive_entry(market, symbol)
            listing, quality = entry['listings'][symbol], entry['coverage'][symbol]
            excluded = set(quality['invalid_ohlc']) | (set(quality['non_sessions']) if market == 'US' else set())
            stock = {**listing, **metadata, **observed, **identity,
                     'bars': [b for b in bars if b['date'] not in excluded],
                     'name': observed.get('name') or metadata.get('name') or listing.get('name') or symbol,
                     'isin_code': identity.get('isin') or listing.get('isinCode'),
                     'listing_exchange_family': observed.get('listing_exchange_family') or listing.get('listing_exchange')}
            stock['history'] = {'quality': {'excluded_dates': sorted(excluded)}}
            stock['bars_ref'] = self.prices.pin(stock)
            return stock

        for market, symbol, currency, isin in [('US', 'SYNTH', 'USD', 'US0000000001'), ('KR', '000001.KS', 'KRW', 'KR0000000001')]:
            with self.subTest(market=market):
                stock = {**self.stock, 'market': market, 'symbol': symbol, 'currency': currency, 'isin_code': isin}
                self.prices.put(stock, source='initial-' + market, kind='series')
                updated = {**stock, 'name': 'Newest observation', 'bars': deepcopy(self.bars)}
                updated['bars'][-1]['close'] = 106.
                self.prices.put(updated, source='corrected-' + market, kind='series')
                catalog = dict(source='synthetic-archive', config={'end': '2026-09-23'},
                               listings={symbol: {'name': 'Listing name', 'isinCode': isin, 'listing_exchange': 'synthetic-exchange'}},
                               coverage={symbol: {'invalid_ohlc': ['2026-09-21'], 'non_sessions': ['2026-09-22']}}, references={})
                self.prices.set_meta('archive_' + market, catalog)
                key = market + ':' + symbol
                expected = previous_select(key)
                before = self.prices.path.read_bytes()
                actual = SelectedHistories(self.root).select(key)
                self.assertEqual(actual, expected)
                self.assertEqual(self.prices.path.read_bytes(), before)
                old_ref = actual['bars_ref']
                changed = deepcopy(updated)
                changed['bars'][-1]['close'] = 107.
                self.prices.put(changed, source='later-' + market, kind='series')
                self.assertEqual(saved_bars(key, root=self.root, reference=old_ref)['bars'], actual['bars'])
                catalog['listings'][symbol]['isinCode'] = 'DIFFERENT'
                self.prices.set_meta('archive_' + market, catalog)
                with self.assertRaisesRegex(ValueError, 'saved_listing_identity_changed'):
                    SelectedHistories(self.root).select(key)
        with self.assertRaisesRegex(ValueError, 'saved_price_identity_missing_or_ambiguous'):
            SelectedHistories(self.root).select('US:ABSENT')
        with self.assertRaisesRegex(ValueError, 'exact_stock_key_required'):
            SelectedHistories(self.root).select('SYNTH')

    def test_account_wrapper_uses_m08_allowlist_for_arbitrary_nested_fields(self):
        stamp = '2026-09-18T00:00:00+00:00'
        secret = 'synthetic-private-marker'
        source = dict(as_of=stamp, attempted_at=stamp, query_state='partial', accounts_complete=False,
                      account_number=secret, unexpected_response={'nested': [secret]}, lease=secret,
                      accounts=[dict(alias='nh-synthetic', provider='nh', as_of=stamp, holdings_complete=False,
                                     lots=[{'original': secret}], raw_json={'nested': secret},
                                     cash={'USD': '123.4500', 'unexpected': secret},
                                     positions=[dict(market='US', symbol='SYNTH', currency='USD', quantity='0.2500', price='105.00',
                                                     account_number=secret, original_lot={'nested': [secret]})])],
                      providers={'nh': dict(as_of=stamp, query_state='partial', raw_ref={'sha256': secret}, token=secret)})
        original = deepcopy(source)
        with patch.object(account_saved, 'saved_accounts', return_value=source) as read, \
             patch.object(account_saved, 'session_projection', wraps=account_saved.session_projection) as project:
            result = saved_accounts(self.root)
        read.assert_called_once_with(self.root)
        project.assert_called_once_with(source)
        self.assertEqual(source, original)
        self.assertEqual(result['as_of'], stamp)
        self.assertEqual(result['query_state'], 'partial')
        position = result['accounts'][0]['positions'][0]
        self.assertEqual((position['quantity'], position['price']), ('0.2500', '105.00'))
        self.assertNotIn(secret, json.dumps(result))

    def test_account_wrapper_provider_states_are_read_only(self):
        for providers, state in [((), 'not_requested'), (('nh',), 'received'), (('meritz',), 'received'),
                                 (('nh', 'meritz'), 'partial'), (('nh',), 'failed')]:
            with self.subTest(providers=providers, state=state), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                store = AccountStore(root / 'runtime/accounts.sqlite3')
                raw_hashes = set()
                stamp, attempt = '2026-09-18T00:00:00+00:00', '2026-09-19T00:00:00+00:00'
                for provider in providers:
                    snapshot = dict(provider='NH PLUG' if provider == 'nh' else 'Meritz Open API', as_of=stamp,
                                    accounts_complete=True, accounts=[dict(alias=provider + '-synthetic', holdings_complete=True,
                                        positions=[dict(market='US', symbol='SYNTH', currency='USD', quantity='1.2500', price='105.00',
                                                        original_lots=[{'data': 'private-lot'}])])])
                    request = store.begin(provider, 'success', 'mcp')
                    receipt = store.complete(provider, 'success', request['lease'], snapshot=snapshot,
                        metadata=dict(query_state='received', attempted_at=stamp), raw={'synthetic': 'raw-broker-private'})
                    raw_hashes.add(receipt['raw_ref']['sha256'])
                    if state in ('partial', 'failed') and provider == providers[-1]:
                        request = store.begin(provider, 'failure', 'mcp')
                        store.complete(provider, 'failure', request['lease'], metadata=dict(
                            query_state='failed', attempted_at=attempt, reason='transport_failed'))
                # Fixture setup ends before the exact file comparison. Real WAL
                # read consistency is covered by the snapshot decoder test.
                if store.path.exists():
                    with closing(sqlite3.connect(store.path)) as db:
                        db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
                        self.assertEqual(db.execute('PRAGMA journal_mode=DELETE').fetchone()[0], 'delete')
                before = {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob('*') if p.is_file()}
                original_read = account_saved.ReadOnlyAccountStore.read
                def read_snapshot(instance, ref):
                    self.assertNotIn(ref['sha256'], raw_hashes)
                    return original_read(instance, ref)
                with patch.object(account_saved.ReadOnlyAccountStore, 'read', read_snapshot), \
                     patch.object(AccountStore, 'save', side_effect=AssertionError('account write')), \
                     patch.object(AccountStore, 'begin', side_effect=AssertionError('account observation begin')), \
                     patch.object(AccountStore, 'complete', side_effect=AssertionError('account observation complete')):
                    result = saved_accounts(root)
                self.assertEqual(result['query_state'], state)
                self.assertEqual(result['as_of'], stamp if providers else None)
                if providers:
                    self.assertEqual(result['accounts'][0]['positions'][0]['quantity'], '1.2500')
                if state in ('partial', 'failed'):
                    self.assertEqual(result['attempted_at'], attempt)
                    self.assertFalse(result['accounts'][-1]['holdings_complete'])
                self.assertNotIn('private', json.dumps(result))
                after = {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob('*') if p.is_file()}
                self.assertEqual(before, after)

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
                            accounts=[{'alias': provider + '-synthetic', 'positions': [],
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
        self.assertEqual(set(accounts), {'nh-synthetic', 'meritz-synthetic'})
        self.assertEqual(result['as_of'], times['nh'])
        self.assertEqual(result['query_state'], 'partial')
        self.assertNotIn('snapshot_ref', result)
        self.assertNotIn('USD', result['valuation']['fx_to_base'])
        self.assertEqual(accounts['nh-synthetic']['fx_to_base']['USD'], '1400')
        self.assertEqual(accounts['meritz-synthetic']['fx_to_base']['USD'], '1500')
        self.assertEqual(accounts['nh-synthetic']['as_of'], times['nh'])
        self.assertEqual(accounts['meritz-synthetic']['as_of'], times['meritz'])
        self.assertTrue(accounts['nh-synthetic']['holdings_complete'])
        self.assertFalse(accounts['meritz-synthetic']['holdings_complete'])
        self.assertEqual(result['providers']['meritz']['query_state'], 'failed')
        self.assertEqual(result['providers']['meritz']['attempted_at'], attempted)
        original = store.read(store.latest('meritz')['snapshot_ref'])
        self.assertTrue(original['accounts'][0]['holdings_complete'])

    def test_historical_account_exports_project_without_changing_original_hashes(self):
        private = 'historical-private-broker-marker'
        account_alias = 'SYNTH_ACCOUNT_2a33349e7e60'
        accounts = dict(as_of='2026-09-18T00:00:00+00:00', accounts_complete=True,
                        raw_response={'private': private}, accounts=[dict(alias=account_alias,
                            account_number=private, holdings_complete=True, positions=[dict(
                                market='US', symbol='SYNTH', currency='USD', quantity='1.2500', price='105.00',
                                raw_lots=[{'private': private}], broker_response={'private': private})])])
        self.service.view = lambda: ({'accounts': accounts, 'market': {'state': 'saved_only'}}, {'US:SYNTH': self.stock})
        snap = self.service.capture(self.service.case(self.cid))
        sid = snap['id']
        plan = self.invoke('plan_save', dict(analysis_key=self.cid, snapshot_id=sid, expected_revision=0,
            contract_version='collaboration.v1', plan=deepcopy(FIXTURE['plan']), references=[], plan_id=None,
            expected_plan_version=0))['record']
        holding = self.service.dispatch('holding', dict(analysis_key=self.cid, snapshot_id=sid,
            adopted_snapshot_id=sid, account_alias=account_alias, confirmed_by_user=True,
            thesis='User-authored thesis preserved', invalidation='User-authored condition', confirmation='Synthetic fixture'),
            'http://fixture', principal='browser')
        before = {p.relative_to(self.root).as_posix(): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        stored_hash = digest(snap)
        with patch.object(SelectedHistories, 'select', side_effect=AssertionError('current prices')):
            read = self.invoke('read', dict(analysis_key=self.cid, snapshot_id=sid, section='accounts'))
            packet = self.invoke('plan_input', dict(analysis_key=self.cid, plan_record_id=plan['id'], mode='hypothetical'))
            linked = self.invoke('holding_links', {})
            detail = self.invoke('detail', {'analysis_key': self.cid})
            collaboration = self.invoke('collaboration_read', {'analysis_key': self.cid})
        for value in (read, packet, linked, detail, collaboration):
            self.assertNotIn(private, json.dumps(value))
            self.assertNotIn(account_alias, json.dumps(value))
            self.assertEqual(value['account_projection'], 'session_allowlist')
            self.assertEqual(value['hashes_refer_to'], 'stored_original')
        self.assertEqual(read['accounts']['accounts'][0]['positions'][0]['quantity'], '1.2500')
        self.assertEqual(packet['snapshot']['accounts'], read['accounts'])
        self.assertEqual(packet['snapshot_sha256'], stored_hash)
        self.assertEqual(packet['plan_sha256'], plan['plan_sha256'])
        self.assertEqual(packet['plan_record'], plan)  # User-authored alias is unchanged.
        self.assertEqual(packet['snapshot']['component_hashes'], snap['component_hashes'])
        self.assertNotEqual(digest(packet['snapshot']), stored_hash)  # Explicit projection, original hash basis.
        for collection in (linked['links'], detail['items'], collaboration['legacy_records']):
            projected = next(record for record in collection if record['id'] == holding['id'])
            self.assertEqual(projected['thesis'], holding['thesis'])
            self.assertIsNone(projected['account_alias'])
            self.assertEqual(projected['observed_position']['quantity'], '1.2500')
        self.assertEqual(self.service.snapshot(self.cid, sid), snap)
        self.assertEqual(self.service.item(self.cid, holding['id']), holding)
        self.assertEqual(self.service.item(self.cid, plan['id']), plan)
        self.assertEqual(digest(self.service.snapshot(self.cid, sid)), stored_hash)
        after = {p.relative_to(self.root).as_posix(): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual(before, after)


if __name__ == '__main__':
    unittest.main()
