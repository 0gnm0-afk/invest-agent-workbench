"""Boundary tests: stored truth, broker isolation, missing values and HTTP scope."""
import copy
import hashlib
import http.client
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import web.data as d
import accounts as account_data
from web.server import handler_for, ThreadingHTTPServer


class StoredData(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        # Recovery helpers otherwise fall back through module-level/default
        # arguments to another root. Inject the synthetic stores end-to-end.
        self.addCleanup(patch.stopall)
        patch('runtime_paths.ROOT', self.root).start()
        patch('records.files.ROOT', self.root).start()
        patch('market.prices.default_path', return_value=self.root / 'runtime/ohlcv.sqlite3').start()
        patch('market.price_files.default_path', return_value=self.root / 'runtime/ohlcv.sqlite3').start()
        from records.store import RecordStore
        from market.prices import PriceStore
        self.records = RecordStore(self.root / 'runtime/records.sqlite3')
        self.prices = PriceStore(self.root / 'runtime/ohlcv.sqlite3')
        connection = self.prices.connect(write=True)
        connection.close()
        self.run = self.root / 'runtime/run'
        self.source = self.root / 'runtime/source/snapshot.json'
        d.write(self.source, {'sessions': {'US': ['2026-09-24']},
                              'series': [{'market': 'US', 'symbol': 'ABC', 'bars': []}]})
        d.write(self.root / 'runtime/latest.json', {'output': str(self.run), 'state': 'failed'})
        files = {
            'run.json': {'state': 'failed', 'processing_state': 'failed', 'quality': {'state': 'partial', 'received': 0}, 'market_source': str(self.source.parent)},
            'priority-review.json': {'schema': 4, 'strategy': 'pdf_momentum', 'session_dates': {'US': '2026-09-24'}, 'candidate_count': 0, 'entry_candidate_count': 0, 'limitations': [], 'markets': {'US': {'entries': []}}},
            'sectors/signals.json': {'sectors': []}, 'sectors/observation.json': {'observations': []}}
        for name, data in files.items():
            d.write(self.run / name, data)
        d.write(self.run / 'manifest.json', {'output_hashes': {name.replace('/', '\\'): d.sha(self.run / name) for name in files}, 'input_hashes': {str(self.source): d.sha(self.source)}})

    def tearDown(self):
        self.temp.cleanup()

    def test_zero_is_valid_and_failed_is_preserved(self):
        value = d.saved_market(self.root)
        self.assertEqual((value['state'], value['quality']['state'], value['quality']['received']), ('failed', 'partial', 0))
        self.assertNotIn('candidate_count', value)
        self.assertEqual(value['errors'], [])

    def test_missing_quality_details_are_unknown_not_zero(self):
        self.assertEqual(d.saved_market(self.root)['quality_gaps'],
                         dict(price_history=None, market_cap=None))

    def test_quality_details_preserve_partial_and_failure(self):
        source = d.load(self.source)
        source['universe'] = [dict(market='US', symbol='ABC', market_cap_quality='unavailable_shares'),
                              dict(market='US', symbol='DEF', market_cap_quality='quoted_price')]
        self.source.write_text(json.dumps(source), encoding='utf-8')
        run = d.load(self.run/'run.json')
        run['quality']['quality_issue_series'] = 4
        run['sector_coverage'] = {'KR':{'unmatched':0}, 'US':{'unmatched':2}}
        (self.run/'run.json').write_text(json.dumps(run), encoding='utf-8')
        manifest = d.load(self.run/'manifest.json')
        manifest['input_hashes'][str(self.source)] = d.sha(self.source)
        manifest['output_hashes']['run.json'] = d.sha(self.run/'run.json')
        (self.run/'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
        value = d.saved_market(self.root)
        self.assertEqual(value['quality_gaps'], dict(price_history=4, market_cap=1))
        self.assertEqual(value['state'], 'failed')
        self.assertEqual(value['quality']['state'], 'partial')

    def test_hash_does_not_allocate_a_whole_file_buffer(self):
        expected = hashlib.sha256(self.source.read_bytes()).hexdigest()
        with patch.object(Path, 'read_bytes', side_effect=MemoryError('whole_file_buffer')):
            self.assertEqual(d.sha(self.source), expected)

    def test_build_reuses_db_without_receipt_files_and_exports_only_on_request(self):
        d.write(self.root/'config.json', {'engine': str(self.root/'engine')})
        accounts = dict(query_state='saved', holdings=[])
        with patch.object(d, 'account_view', return_value=accounts), patch('alerts.trailing.TrailingStore.update', side_effect=AssertionError('saved view must not rewrite warnings')):
            for _ in range(20):
                output, data, stocks = d.build(self.root, refresh=False, market_run=self.run)
        self.assertIn('US:ABC', stocks)
        self.assertEqual(data['accounts'], accounts)
        self.assertIsNone(output)
        self.assertFalse((self.root/'runtime/dashboard').exists())
        with patch.object(d, 'account_view', return_value=accounts), patch('alerts.trailing.TrailingStore.update', return_value={}):
            exported, _, _ = d.build(self.root, refresh=False, market_run=self.run, persist_view=True)
        self.assertTrue((exported/'dashboard.json').exists())
        self.assertEqual(d.load(exported/'manifest.json')['files']['dashboard.json'], d.sha(exported/'dashboard.json'))

    def test_price_tampering_is_rejected(self):
        self.source.write_text('{}')
        with self.assertRaisesRegex(ValueError, 'hash_mismatch'):
            d.saved_market(self.root)

    # Preserve the M00 manifest IDs while checking that retired artifacts are ignored.
    def test_missing_screen_is_not_zero(self):
        (self.run / 'priority-review.json').unlink()
        value = d.saved_market(self.root)
        self.assertNotIn('candidate_count', value)
        self.assertEqual(value['errors'], [])
        self.assertIn('US:ABC', value['stocks'])
        self.assertEqual(value['session_dates'], {'US': '2026-09-24'})

    def test_missing_sector_does_not_hide_screen(self):
        (self.run / 'sectors/signals.json').unlink()
        value = d.saved_market(self.root)
        self.assertNotIn('sectors', value)
        self.assertEqual(value['errors'], [])
        self.assertIn('US:ABC', value['stocks'])

    def test_unsupported_screen_does_not_reactivate_legacy(self):
        f = self.run / 'priority-review.json'
        v = d.load(f); v['strategy'] = 'taver'; f.write_text(json.dumps(v))
        m = d.load(self.run / 'manifest.json');m['output_hashes'][f.name] = d.sha(f)
        (self.run / 'manifest.json').write_text(json.dumps(m))
        value = d.saved_market(self.root)
        self.assertNotIn('candidate_count', value)
        self.assertEqual(value['errors'], [])

    def test_latest_outside_runtime_is_rejected(self):
        (self.root / 'runtime/latest.json').write_text(json.dumps({'output': str(self.root.parent)}))
        with self.assertRaisesRegex(ValueError, 'outside'):
            d.saved_market(self.root)

    def test_latest_in_previous_workspace_replays_current_saved_market(self):
        expected = d.saved_market(self.root)
        previous = self.root / 'previous-workspace'
        d.write(self.root / 'config.json', {'records': {'previous_roots': [str(previous)]}})
        latest = self.root / 'runtime/latest.json'
        latest.write_text(json.dumps({'output': str(previous / 'runtime/run')}), encoding='utf-8')
        self.assertEqual(d.saved_market(self.root), expected)
        latest.write_text(json.dumps({'output': str(previous / 'runtime/../../outside')}), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'outside_runtime'):
            d.saved_market(self.root)


class BrokerBoundary(unittest.TestCase):
    """M08 handoff: persist session results; no app-owned broker transport."""

    def setUp(self):
        from accounts.store import AccountStore
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = AccountStore(Path(self.temp.name) / 'runtime/accounts.sqlite3')

    def observation(self):
        page = {'Output_0': {}, 'Output_1': [], 'rsp_msg': '조회완료',
                '_pagination': {'cts_flag': 'N', 'cts': None, 'request_cts': None}}
        return {'as_of': '2026-11-01T01:00:00+00:00', 'accounts_complete': True,
                'accounts': [{'alias': 'nh-synthetic', 'markets': {
                    market: {'state': 'received', 'pages': [copy.deepcopy(page)]}
                    for market in ('KR', 'US')}}]}

    def save(self, observation, request_id='synthetic'):
        from accounts.brokers import query_state
        from accounts.record import normalized
        snapshot = normalized('nh', observation)
        claim = self.store.begin('nh', request_id, 'mcp')
        self.assertTrue(claim['execute'])
        return self.store.complete('nh', request_id, claim['lease'],
            metadata={'query_state': query_state(snapshot)}, snapshot=snapshot, raw=observation)

    def test_session_observation_preserves_refs_and_timestamp(self):
        observation = self.observation()
        receipt = self.save(observation)
        self.assertEqual(receipt['origin'], 'mcp')
        self.assertEqual(receipt['query_state'], 'received')
        self.assertEqual(self.store.read(receipt['raw_ref']), observation)
        snapshot = self.store.read(receipt['snapshot_ref'])
        self.assertEqual(snapshot['as_of'], observation['as_of'])
        self.assertTrue(snapshot['accounts'][0]['holdings_complete'])
        self.assertEqual(self.store.latest('nh'), receipt)

    def test_completed_request_does_not_grant_second_lease(self):
        receipt = self.save(self.observation())
        repeated = self.store.begin('nh', receipt['request_id'], 'mcp')
        self.assertFalse(repeated['execute'])
        self.assertNotIn('lease', repeated)
        self.assertEqual(repeated['result'], receipt)

    def test_one_market_error_is_not_empty_success(self):
        observation = self.observation()
        observation['accounts'][0]['markets']['US'] = {
            'state': 'unavailable', 'reason': 'synthetic_http_503', 'pages': []}
        receipt = self.save(observation)
        self.assertEqual(receipt['query_state'], 'partial')
        account = self.store.read(receipt['snapshot_ref'])['accounts'][0]
        self.assertFalse(account['holdings_complete'])
        self.assertEqual(account['query_markets']['KR']['state'], 'received')
        self.assertEqual(account['query_markets']['US'], {
            'state': 'unavailable', 'reason': 'synthetic_http_503', 'pages': 0})

    def test_unconfirmed_empty_account_list_is_partial(self):
        observation = self.observation()
        observation.update(accounts=[], accounts_complete=False)
        receipt = self.save(observation)
        self.assertEqual(receipt['query_state'], 'partial')
        snapshot = self.store.read(receipt['snapshot_ref'])
        self.assertEqual(snapshot['accounts'], [])
        self.assertFalse(snapshot['accounts_complete'])

    def test_confirmed_zero_accounts(self):
        observation = self.observation()
        observation['accounts'] = []
        receipt = self.save(observation)
        self.assertEqual(receipt['query_state'], 'received')
        snapshot = self.store.read(receipt['snapshot_ref'])
        self.assertEqual(snapshot['accounts'], [])
        self.assertTrue(snapshot['accounts_complete'])

    def test_duplicate_account_rejected(self):
        from accounts.record import normalized
        observation = self.observation()
        observation['accounts'].append(copy.deepcopy(observation['accounts'][0]))
        with self.assertRaisesRegex(ValueError, 'duplicate_account_alias'):
            normalized('nh', observation)
        self.assertFalse(self.store.path.exists())

    def test_unconfirmed_nonempty_account_list_is_partial(self):
        observation = self.observation()
        observation.pop('accounts_complete')
        receipt = self.save(observation)
        self.assertEqual(receipt['query_state'], 'partial')
        snapshot = self.store.read(receipt['snapshot_ref'])
        self.assertEqual(len(snapshot['accounts']), 1)
        self.assertFalse(snapshot['accounts_complete'])

    def test_non_dict_account_row_rejected(self):
        from accounts.record import normalized
        observation = self.observation()
        observation['accounts'] = ['not-an-account']
        with self.assertRaisesRegex(ValueError, 'observation_shape_invalid'):
            normalized('nh', observation)
        self.assertFalse(self.store.path.exists())

    def test_failed_attempt_preserves_error_pages_and_observation(self):
        observation = self.observation()
        original = self.save(observation)
        claim = self.store.begin('nh', 'synthetic-failed', 'mcp')
        error = {'pages': [{'rsp_msg': 'synthetic unavailable'}]}
        failed = self.store.complete('nh', 'synthetic-failed', claim['lease'],
            metadata={'query_state': 'failed', 'reason': 'synthetic_http_503'}, raw=error)
        self.assertIsNone(failed['snapshot_ref'])
        self.assertEqual(self.store.read(failed['raw_ref']), error)
        self.assertEqual(self.store.latest_attempt('nh'), failed)
        self.assertEqual(self.store.latest('nh'), original)
        self.assertEqual(self.store.read(original['raw_ref']), observation)
        self.assertEqual(self.store.read(original['snapshot_ref'])['as_of'], observation['as_of'])

class HttpBoundary(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        dependencies = {'histories': SimpleNamespace(saved=SimpleNamespace(stocks={}, markets={})),
                        'financials': SimpleNamespace()}
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), handler_for({'market': {'state':'failed'}}, {}, Path(__file__).resolve().parents[1]/'dashboard', 'test',
            Path(cls.temp.name)/'chart.sqlite3', analysis_dependencies=dependencies))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True);cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.server.server_close();cls.thread.join()

    def get(self, path, headers=None, method='GET'):
        c = http.client.HTTPConnection('127.0.0.1', self.server.server_port)
        c.request(method, path, headers=headers or {});r=c.getresponse();body=r.read();status=r.status;c.close();return status,body

    def test_failed_data_is_served(self):
        status, body=self.get('/api/home');self.assertEqual(status,200);self.assertEqual(json.loads(body)['market']['state'],'failed')

    def test_feature_assets_keep_existing_urls_after_folder_move(self):
        assets=Path(__file__).resolve().parents[1]/'dashboard'
        groups={'shell': ('app.js','style.css','format.js','tooltips.js'),
                'charts': ('charts.js','stock-chart.js','chart-events.js'),
                'accounts': ('risk-workbench.js','risk-workbench.css'),
                'financials': ('financial-focus.js','financial-focus.css','financials.css'),
                'market': ('market-exclusions.js','market-exclusions.css'),
                'alerts': ('trailing.js',),
                'cycles': ('trade-cycle.js','trade-cycle.css'),
                'drawings': ('drawing-tools.js','chart-workspace.js','chart-tools.css'),
                'analysis': ('analysis.js','collaboration.js','report-reader.js','report-reader.css'),
                'scenarios': ('scenario-preview.js','scenario-monitor.js','scenario-monitor.css')}
        for folder,names in groups.items():
            for name in names:
                status,body=self.get('/'+name)
                self.assertEqual(status,200,name)
                self.assertEqual(body,(assets/folder/name).read_bytes())

    def test_no_raw_account_or_filesystem_access(self):
        for path in ['/account-observation.private.json','/../config.json','/scripts/nh_api.py','/runtime/latest.json']:
            self.assertEqual(self.get(path)[0],404)

    def test_dns_rebinding_host_rejected(self):
        self.assertEqual(self.get('/api/home', {'Host':'evil.example'})[0],403)

    def test_cross_origin_rejected(self):
        self.assertEqual(self.get('/api/home', {'Origin':'https://evil.example'})[0],403)

    def test_post_disabled(self):
        self.assertEqual(self.get('/api/home',method='POST')[0],405)

    def test_missing_stock_explicit(self):
        status, body=self.get('/api/stock/US%3ANOPE');self.assertEqual(status,200);self.assertEqual(json.loads(body)['state'],'unavailable')


class AccountFallback(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        config = d.load(d.ROOT / 'config.json')
        cls.addClassCleanup(setattr, sys, 'path', sys.path.copy())
        sys.path.insert(0, str(Path(config['engine']) / 'src'))
        sys.path.append(str(Path(config['mother']) / 'scripts'))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.output = self.root / 'dashboard/attempt'
        self.output.mkdir(parents=True)
        self.config = {'mother': str(self.root)}

    def tearDown(self): self.temp.cleanup()

    def test_credentials_failure_without_cache_is_missing(self):
        import nh_api
        with patch('nh_api.read', side_effect=nh_api.NHError('credentials_missing')):
            value = d.account_view(self.config, self.output)
        self.assertEqual((value['query_state'],value['source_mode'],value['as_of']),('failed','missing',None))
        self.assertEqual(value['accounts'],[])

    def test_failure_with_cache_remains_failed_and_stale(self):
        import nh_api
        snapshot={'schema_version':1,'source':'broker_export','as_of':'2020-01-01T00:00:00+00:00','max_age_hours':'24','base_currency':'KRW','fx_to_base':{'KRW':'1'},'accounts':[]}
        d.write(self.root / 'runtime/nh-live/exports/account-20200101.json', snapshot)
        with patch('nh_api.read', side_effect=nh_api.NHError('http_503')):
            value = d.account_view(self.config, self.output)
        self.assertEqual((value['query_state'],value['source_mode'],value['reason']),('failed','historical_saved','http_503'))
        self.assertTrue(value['stale'])
        self.assertEqual(value['as_of'], snapshot['as_of'])

    def test_persisted_account_failure_remains_failed_on_saved_read(self):
        import nh_api
        d.write(self.root/'config.json',self.config)
        snapshot={'as_of':'2026-10-06T00:00:00+00:00','accounts':[],'limitations':[]}
        d.write(self.root/'runtime/nh-live/exports/account-test.json',snapshot)
        with patch('nh_api.read', side_effect=nh_api.NHError('http_503')):
            value=account_data.refresh_account_snapshot(self.root)
        self.assertEqual(value['query_state'],'failed')
        output=self.root/'runtime/dashboard/read';output.mkdir()
        with patch('nh_api.read',side_effect=AssertionError('unexpected duplicate read')):
            saved=d.account_view(self.config,output,refresh=False)
        self.assertEqual(saved['query_state'],'failed');self.assertEqual(saved['reason'],'http_503')
        self.assertEqual(saved['as_of'],snapshot['as_of'])

    def test_saved_mode_never_calls_broker(self):
        with patch('nh_api.read', side_effect=AssertionError('must not call')):
            value=d.account_view(self.config,self.output,refresh=False)
        self.assertEqual(value['query_state'],'not_requested')


if __name__ == '__main__': unittest.main()
