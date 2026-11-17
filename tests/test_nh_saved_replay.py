"""M03: saved NH evidence only; synthetic files, no provider or credential calls."""
import hashlib
import json
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from market.providers import nh_client
from market import nh_fallback, retry


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(value, sort_keys=True).encode('utf-8')
    path.write_bytes(raw)
    return hashlib.sha256(raw).hexdigest()


class SavedNHTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.evidence = self.root / 'evidence'
        self.inputs = nh_client.us_period_inputs('SYNTH', '20260605')
        self.response = {'path': nh_client.PERIOD, 'inputs': self.inputs,
                         'payload': {'rsp_cd': '00000', 'Output_1': []},
                         'fetched_at': '2026-06-06T00:00:00+00:00'}
        self.name = 'responses/' + nh_client.digest(nh_client.PERIOD, self.inputs) + '.json'
        self.response_path = self.evidence / self.name
        self.sha = save(self.response_path, self.response)
        save(self.evidence / 'manifest.json', {self.name: self.sha})

    def test_saved_bytes_request_identity_and_hash_are_preserved_without_writes(self):
        before = {p: p.read_bytes() for p in self.evidence.rglob('*') if p.is_file()}
        with patch('urllib.request.build_opener') as http:
            result, path = nh_client.MarketClient(self.root / 'unused', self.evidence).read(
                nh_client.PERIOD, self.inputs)
        self.assertEqual(result, self.response)
        self.assertEqual(path, self.response_path)
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), self.sha)
        self.assertEqual(before, {p: p.read_bytes() for p in self.evidence.rglob('*') if p.is_file()})
        self.assertFalse((self.root / 'unused').exists())
        http.assert_not_called()
        self.assertFalse(hasattr(nh_client, 'token'))

    def test_missing_response_and_live_recovery_fail_before_any_auth_or_request(self):
        with patch('urllib.request.build_opener') as http, patch.object(nh_fallback, 'PriceFiles') as prices:
            with self.assertRaisesRegex(nh_client.NHMarketError, 'requested_evidence_not_saved'):
                nh_client.MarketClient(self.root / 'absent').read(nh_client.PERIOD, self.inputs)
            with self.assertRaisesRegex(ValueError, 'single_provider_toss'):
                nh_fallback.recover(self.root / 'missing', self.root / 'output', {})
        self.assertFalse((self.root / 'absent').exists())
        self.assertFalse((self.root / 'output').exists())
        prices.assert_not_called()
        http.assert_not_called()

    def test_tampering_missing_hash_and_rebound_request_fail_closed(self):
        client = nh_client.MarketClient(self.root, self.evidence)
        self.response_path.write_bytes(self.response_path.read_bytes() + b' ')
        with self.assertRaisesRegex(nh_client.NHMarketError, 'cached_market_response_hash_mismatch'):
            client.read(nh_client.PERIOD, self.inputs)
        save(self.evidence / 'manifest.json', {})
        with self.assertRaisesRegex(nh_client.NHMarketError, 'requested_evidence_hash_unavailable'):
            client.read(nh_client.PERIOD, self.inputs)
        changed = {**self.response, 'inputs': {**self.inputs, 'iem_cd': 'OTHER'}}
        save(self.evidence / 'manifest.json', {self.name: save(self.response_path, changed)})
        with self.assertRaisesRegex(nh_client.NHMarketError, 'cached_market_request_mismatch'):
            client.read(nh_client.PERIOD, self.inputs)

    def test_saved_http_failure_and_endpoint_allowlist_are_preserved(self):
        save(self.evidence / 'manifest.json', {'output_hashes': {self.name.replace('/', '\\'): save(
            self.response_path, {**self.response, 'http_status': 404})}})
        client = nh_client.MarketClient(self.root, self.evidence)
        with self.assertRaisesRegex(nh_client.NHMarketError, 'market_http_404'):
            client.read(nh_client.PERIOD, self.inputs)
        with self.assertRaisesRegex(nh_client.NHMarketError, 'market_only_period_required'):
            client.read('/account', self.inputs)

    def replay_fixture(self, *, mismatched_isin=False, tampered=False, missing=False):
        days = [(date(2026, 1, 1) + timedelta(days=n)).isoformat() for n in range(126)]
        bars = [{'date': day, 'open': 10, 'high': 11, 'low': 9, 'close': 10, 'volume': 100000}
                for day in days]
        row = {'market': 'US', 'symbol': 'SYNTH', 'provider_symbol': 'SYNTH',
               'listing_exchange_family': 'NASDAQ', 'isin_code': 'US_SYNTHETIC',
               'currency': 'USD', 'market_cap': 1000000, 'benchmark': '^GSPC'}
        source = self.root / 'source'
        source_hash = save(source / 'snapshot.json', {
            'universe': [row], 'series': [],
            'errors': [{'market': 'US', 'symbol': 'SYNTH', 'error_code': 'market_http_404'}],
            'sessions': {'US': days}, 'collection_stats': {'requested': 1, 'received': 0, 'failed': 1},
            'coverage': {'listings': {'NASDAQ': {'state': 'available', 'limit': None}},
                         'scope_errors': [], 'market_wide': False}, 'providers': {'US': 'Toss'}})
        save(source / 'manifest.json', {'snapshot.json': source_hash})
        reference = self.root / 'reference.json'
        config = {'nh_reference_snapshot': str(reference),
                  'nh_reference_snapshot_sha256': save(reference, {'series': [
                      {**row, 'provider': 'Toss OpenAPI candles', 'bars': bars}]})}
        for market, values in [('us', [{'sNationCode': 'USA', 'sStockCode': 'NQQ', 'sSymbol': 'SYNTH',
                                        'sStandardCode': 'OTHER' if mismatched_isin else 'US_SYNTHETIC'}]),
                               ('kr', [])]:
            path = self.root / (market + '-master.json')
            config['nh_' + market + '_master'] = str(path)
            config['nh_' + market + '_master_sha256'] = save(path, values)
        inputs = nh_client.us_period_inputs('SYNTH', days[-1].replace('-', ''))
        name = 'responses/' + nh_client.digest(nh_client.PERIOD, inputs) + '.json'
        response_path = self.evidence / name
        captured_hash = save(response_path, {'path': nh_client.PERIOD, 'inputs': inputs, 'payload': {
            'rsp_cd': '00000', 'Output_1': [dict(trade_date=b['date'].replace('-', ''), open_prc=b['open'],
                                               high=b['high'], low=b['low'], close_prc=b['close'],
                                               movolume=b['volume'] / 1000) for b in reversed(bars)]}})
        save(self.evidence / 'manifest.json', {name: captured_hash})
        if tampered:
            response_path.write_bytes(response_path.read_bytes() + b' ')
        if missing:
            response_path.unlink()
        with patch.object(nh_fallback, 'audit', return_value={}), \
             patch.object(nh_fallback, 'gate', return_value={'universe_pass': True, 'universe_rejection_reasons': []}), \
             patch('urllib.request.build_opener') as http:
            output, summary, inputs = nh_fallback.recover(source, self.root / 'replayed', config,
                evidence_root=self.evidence, allow_auto_exclusions=False, root=self.root)
        http.assert_not_called()
        return json.loads((output / 'snapshot.json').read_bytes()), summary, inputs, response_path, captured_hash, bars

    def test_historical_nh_replay_preserves_provider_hash_and_values(self):
        snapshot, summary, inputs, response_path, expected, bars = self.replay_fixture()
        self.assertEqual(summary['adopted'], 1)
        self.assertEqual(summary['unrecovered'], 0)
        self.assertEqual(snapshot['series'][0]['provider'], 'NH PLUG period')
        self.assertEqual(snapshot['series'][0]['bars'], bars)
        self.assertEqual(snapshot['nh_fallback']['provenance'], 'saved_source_no_new_requests')
        self.assertFalse(summary['results'][0]['nh_requested'])
        self.assertTrue(summary['results'][0]['nh_replayed'])
        self.assertEqual(inputs[str(response_path)], expected)
        self.assertEqual(hashlib.sha256(response_path.read_bytes()).hexdigest(), expected)
        self.assertIn(str((self.evidence / 'manifest.json').resolve()), inputs)

    def test_replay_identity_conflict_does_not_replace_toss_failure(self):
        snapshot, summary, *_ = self.replay_fixture(mismatched_isin=True)
        self.assertEqual(summary['adopted'], 0)
        self.assertEqual(summary['unrecovered'], 1)
        self.assertEqual(summary['results'][0]['reason'], 'nh_identity_exchange_isin_mismatch')
        self.assertEqual(len(snapshot['errors']), 1)
        self.assertEqual(snapshot['series'], [])

    def test_replay_tampering_does_not_replace_toss_failure(self):
        snapshot, summary, *_ = self.replay_fixture(tampered=True)
        self.assertEqual(summary['adopted'], 0)
        self.assertEqual(summary['unrecovered'], 1)
        self.assertEqual(summary['results'][0]['reason'], 'cached_market_response_hash_mismatch')
        self.assertEqual(len(snapshot['errors']), 1)

    def test_replay_missing_response_does_not_replace_toss_failure(self):
        snapshot, summary, *_ = self.replay_fixture(missing=True)
        self.assertEqual(summary['adopted'], 0)
        self.assertEqual(summary['unrecovered'], 1)
        self.assertEqual(summary['results'][0]['reason'], 'requested_evidence_not_saved')
        self.assertEqual(len(snapshot['errors']), 1)


class TossRetryBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.registry = self.root / 'registry.json'

    def test_nh_retry_is_rejected_before_source_or_collector_access(self):
        job = {'id': 'job', 'provider': 'nh'}
        with patch.object(retry.review, 'registry', return_value=({'jobs': [job]}, None)), \
             patch.object(retry.review, 'latest') as latest, patch.object(retry, 'TossMarketClient') as client:
            with self.assertRaisesRegex(ValueError, 'single_provider_toss'):
                retry.apply_retry(self.root, self.registry, 'job')
        latest.assert_not_called()
        client.assert_not_called()

    def test_interrupted_toss_retry_does_not_repeat_collection(self):
        job = {'id': 'job', 'provider': 'toss', 'source_run': 'before', 'collection_started_at': 'saved'}
        with patch.object(retry.review, 'registry', return_value=({'jobs': [job]}, None)), \
             patch.object(retry.review, 'latest', return_value=('before', {}, {})), \
             patch.object(retry.review, 'same_run', return_value=True), \
             patch.object(retry.review, 'load', return_value={}), patch.object(retry.review, 'update_job'), \
             patch.object(retry, 'TossMarketClient') as client:
            with self.assertRaisesRegex(ValueError, 'retry_collection_interrupted'):
                retry.apply_retry(self.root, self.registry, 'job')
        client.assert_not_called()

    def test_completed_toss_source_resumes_without_provider_request(self):
        import market.daily as daily
        source = self.root / 'source'
        job = {'id': 'job', 'provider': 'toss', 'source_run': 'before', 'result_source': str(source)}
        with patch.object(retry.review, 'registry', return_value=({'jobs': [job]}, None)), \
             patch.object(retry.review, 'latest', return_value=('before', {}, {})), \
             patch.object(retry.review, 'same_run', return_value=True), \
             patch.object(retry.review, 'load', return_value={}), patch.object(retry.review, 'update_job') as update, \
             patch.object(retry.review, 'local_runtime_path', return_value=source), \
             patch.object(daily, 'source_check') as check, \
             patch.object(daily, 'run', return_value={'output': 'result', 'stages': {'report': 'completed'}}), \
             patch.object(retry, 'TossMarketClient') as client:
            retry.apply_retry(self.root, self.registry, 'job')
        client.assert_not_called()
        check.assert_called_once_with(source)
        self.assertEqual(update.call_args.kwargs['state'], 'computed')


if __name__ == '__main__':
    unittest.main()
