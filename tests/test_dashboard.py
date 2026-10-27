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
import dashboard_data as d
from dashboard import handler_for, ThreadingHTTPServer


class StoredData(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.run = self.root / 'runtime/run'
        self.source = self.root / 'runtime/source/snapshot.json'
        d.write(self.source, {'series': [{'market': 'US', 'symbol': 'ABC', 'bars': []}]})
        d.write(self.root / 'runtime/latest.json', {'output': str(self.run), 'state': 'failed'})
        files = {
            'run.json': {'state': 'failed', 'processing_state': 'failed', 'quality': {'state': 'partial'}, 'market_source': str(self.source.parent)},
            'priority-review.json': {'schema': 4, 'strategy': 'pdf_momentum', 'session_dates': {'US': '2026-09-24'}, 'candidate_count': 0, 'entry_candidate_count': 0, 'limitations': [], 'markets': {'US': {'entries': []}}},
            'sectors/signals.json': {'sectors': []}, 'sectors/observation.json': {'observations': []}}
        for name, data in files.items():
            d.write(self.run / name, data)
        d.write(self.run / 'manifest.json', {'output_hashes': {name.replace('/', '\\'): d.sha(self.run / name) for name in files}, 'input_hashes': {str(self.source): d.sha(self.source)}})

    def tearDown(self):
        self.temp.cleanup()

    def test_zero_is_valid_and_failed_is_preserved(self):
        value = d.saved_market(self.root)
        self.assertEqual((value['state'], value['quality']['state'], value['candidate_count'], value['entry_candidate_count']), ('failed','partial',0,0))
        self.assertEqual(value['errors'], [])

    def test_missing_quality_details_are_unknown_not_zero(self):
        self.assertEqual(d.saved_market(self.root)['quality_gaps'],
                         dict(price_history=None, market_cap=None, classification=None))

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
        self.assertEqual(value['quality_gaps'], dict(price_history=4, market_cap=1, classification=2))
        self.assertEqual(value['state'], 'failed')
        self.assertEqual(value['quality']['state'], 'partial')

    def test_hash_does_not_allocate_a_whole_file_buffer(self):
        expected = hashlib.sha256(self.source.read_bytes()).hexdigest()
        with patch.object(Path, 'read_bytes', side_effect=MemoryError('whole_file_buffer')):
            self.assertEqual(d.sha(self.source), expected)

    def test_build_persists_account_receipt_without_large_view_copies(self):
        d.write(self.root/'config.json', {'engine': str(self.root/'engine')})
        accounts = dict(query_state='saved', holdings=[])
        with patch.object(d, 'account_view', return_value=accounts), patch('trailing_warnings.TrailingStore.update', side_effect=AssertionError('saved view must not rewrite warnings')):
            output, data, stocks = d.build(self.root, refresh=False, market_run=self.run)
        self.assertIn('US:ABC', stocks)
        self.assertEqual(data['accounts'], accounts)
        self.assertFalse((output/'dashboard.json').exists())
        self.assertFalse((output/'stocks.json').exists())
        receipt = d.load(output/'manifest.json')
        self.assertEqual(receipt['files']['account-view.json'], d.sha(output/'account-view.json'))
        self.assertEqual(receipt['view_storage'], 'memory')
        with patch.object(d, 'account_view', return_value=accounts), patch('trailing_warnings.TrailingStore.update', return_value={}):
            exported, _, _ = d.build(self.root, refresh=False, market_run=self.run, persist_view=True)
        self.assertTrue((exported/'dashboard.json').exists())
        self.assertEqual(d.load(exported/'manifest.json')['files']['dashboard.json'], d.sha(exported/'dashboard.json'))

    def test_price_tampering_is_rejected(self):
        self.source.write_text('{}')
        with self.assertRaisesRegex(ValueError, 'hash_mismatch'):
            d.saved_market(self.root)

    def test_missing_screen_is_not_zero(self):
        (self.run / 'priority-review.json').unlink()
        value = d.saved_market(self.root)
        self.assertIsNone(value['candidate_count'])
        self.assertTrue(value['errors'])

    def test_missing_sector_does_not_hide_screen(self):
        (self.run / 'sectors/signals.json').unlink()
        value = d.saved_market(self.root)
        self.assertEqual(value['candidate_count'], 0)
        self.assertTrue(value['errors'])

    def test_unsupported_screen_does_not_reactivate_legacy(self):
        f = self.run / 'priority-review.json'
        v = d.load(f); v['strategy'] = 'taver'; f.write_text(json.dumps(v))
        m = d.load(self.run / 'manifest.json');m['output_hashes'][f.name] = d.sha(f)
        (self.run / 'manifest.json').write_text(json.dumps(m))
        self.assertIsNone(d.saved_market(self.root)['candidate_count'])

    def test_latest_outside_runtime_is_rejected(self):
        (self.root / 'runtime/latest.json').write_text(json.dumps({'output': str(self.root.parent)}))
        with self.assertRaisesRegex(ValueError, 'outside'):
            d.saved_market(self.root)


class BrokerBoundary(unittest.TestCase):
    class Error(Exception): pass

    def reader(self, path, inputs, continuation=None):
        if path == '/n2/acctinfo':
            return {'Output_0': [{'acct_type': '01', 'acct_no': 'SYNTH_ACCOUNT_254aa248acb4'}]}, {}
        return {'Output_0': {}, 'Output_1': [], 'rsp_msg': '조회완료'}, {'cts_flag': 'N'}

    def pages(self, path, inputs, reader):
        return [reader(path, inputs)[0]]

    def test_exact_account_balance_allowlist(self):
        raw = d.collect_balances(self.reader, self.pages, self.Error)
        self.assertEqual(raw['endpoint_calls'], ['/n2/acctinfo','/krstock/inquiry/v1/balance','/gbstock/inquiry/v1/balance'])
        self.assertNotIn('SYNTH_ACCOUNT_254aa248acb4', json.dumps(raw))
        self.assertEqual(raw['accounts'][0]['markets']['KR']['state'], 'received')

    def test_order_request_refused_before_transport(self):
        def pages(path, inputs, reader):
            return reader('/krstock/inquiry/v1/dailyOrderExecution', inputs)
        with self.assertRaisesRegex(ValueError, 'read_only'):
            d.collect_balances(self.reader, pages, self.Error)

    def test_one_market_error_is_not_empty_success(self):
        def reader(path, inputs, continuation=None):
            if 'gbstock' in path: raise self.Error('http_503')
            return self.reader(path, inputs, continuation)
        raw = d.collect_balances(reader, self.pages, self.Error)
        self.assertEqual(raw['accounts'][0]['markets']['US'], {'state':'unavailable','reason':'http_503'})
        self.assertEqual(raw['accounts'][0]['markets']['KR']['state'],'received')

    def test_unconfirmed_empty_account_list_rejected(self):
        with self.assertRaisesRegex(ValueError, 'empty_account_list'):
            d.collect_balances(lambda *args: ({'Output_0': []}, {}), self.pages, self.Error)

    def test_confirmed_zero_accounts(self):
        raw = d.collect_balances(lambda *args: ({'Output_0': [], 'rsp_msg': '조회완료'}, {}), self.pages, self.Error)
        self.assertEqual(raw['accounts'], [])

    def test_duplicate_account_rejected(self):
        row = {'acct_type': '01', 'acct_no': 'SYNTH_ACCOUNT_254aa248acb4'}
        with self.assertRaisesRegex(ValueError, 'identity'):
            d.collect_balances(lambda *args: ({'Output_0': [row, row]}, {}), self.pages, self.Error)


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
        groups={'cycles': ('trade-cycle.js','trade-cycle.css'),
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
            value=d.refresh_account_snapshot(self.root)
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
