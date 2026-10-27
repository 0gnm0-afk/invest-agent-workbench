"""Offline coverage for expanded US listing scope and immutable legacy runs."""
import gzip
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from market.history.collect import Collector, packed, read_checked, run, save_checked
from market.history.verify import verify


class ADRHistoryTests(unittest.TestCase):
    @staticmethod
    def listing(params):
        if params['securityType'] == 'DEPOSITARY_RECEIPT':
            return ([] if params['market'] == 'NASDAQ' else [
                {'symbol': 'NVO', 'securityType': 'DEPOSITARY_RECEIPT',
                 'isCommonShare': False, 'isinCode': 'US6701002056'}])
        return [{'symbol': 'AAPL' if params['market'] == 'NASDAQ' else 'IBM',
                 'securityType': 'STOCK', 'isCommonShare': True}]

    @classmethod
    def client(cls, calls):
        def fake(path, params):
            calls.append((path, dict(params)))
            if path.endswith('/stocks/all'):
                return {'result': cls.listing(params)}
            return {'result': {'candles': [dict(
                timestamp='2026-09-30T00:00:00-04:00', currency='USD',
                openPrice=10, highPrice=12, lowPrice=9, closePrice=11, volume=100)],
                'nextBefore': None}}
        return fake

    @staticmethod
    def config(root, schema=2):
        value = dict(schema=schema, markets=['US'], end='2026-09-30', scope='synthetic')
        if schema == 2:
            value['security_types'] = ['STOCK', 'DEPOSITARY_RECEIPT']
        save_checked(root/'collection.json', value)

    def test_new_run_records_both_listing_types_and_audits_nvo_candles(self):
        calls = []
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch('market.history.collect.TossMarketClient', return_value=self.client(calls)), \
                    patch('market.history.collect.completed_sessions', return_value=['2026-09-30']), \
                    patch('builtins.print'):
                run(root, workers=1)
                summary = verify(root, require_finished=True)
            config = read_checked(root/'collection.json')
            self.assertEqual(config['schema'], 2)
            self.assertEqual(config['security_types'], ['STOCK', 'DEPOSITARY_RECEIPT'])
            self.assertIn('DEPOSITARY_RECEIPT (no commonShare filter)', config['scope'])
            universe = read_checked(root/'universe.json')
            self.assertEqual([r['symbol'] for r in universe['rows']], ['AAPL', 'IBM', 'NVO'])
            self.assertEqual(universe['exchanges']['NASDAQ']['requests']['DEPOSITARY_RECEIPT']['count'], 0)
            listings = [params for path, params in calls if path.endswith('/stocks/all')]
            self.assertEqual(len(listings), 4)
            for params in listings:
                self.assertEqual(params['status'], 'ACTIVE')
                if params['securityType'] == 'DEPOSITARY_RECEIPT':
                    self.assertNotIn('commonShare', params)
                else:
                    self.assertEqual(params['commonShare'], 'true')
            self.assertEqual(summary['counts'], {'received': 3})
            self.assertEqual(summary['requested'], 3)
            self.assertTrue(summary['all_attempted'])
            manifest = json.loads((root/'manifest.json').read_bytes())
            self.assertEqual(len([p for p in manifest if Path(p).parts[0] == 'responses']), 7)
            nvo = read_checked(root/'series'/(hashlib.sha256(b'NVO').hexdigest()+'.json'))
            self.assertEqual(nvo['listing']['securityType'], 'DEPOSITARY_RECEIPT')
            self.assertFalse(nvo['listing']['isCommonShare'])
            self.assertEqual(nvo['bars'][0]['date'], '2026-09-30')

    def test_failed_adr_listing_does_not_publish_partial_universe(self):
        for failure in ('transport', 'malformed'):
            calls = []
            def fake(path, params):
                calls.append(dict(params))
                if params['securityType'] == 'DEPOSITARY_RECEIPT':
                    if failure == 'transport':
                        raise ValueError('synthetic_adr_transport_failure')
                    return {'result': None}
                return {'result': self.listing(params)}
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as td:
                with self.assertRaises(ValueError):
                    Collector(td, fake).universe()
                self.assertFalse((Path(td)/'universe.json').exists())
                self.assertFalse((Path(td)/'progress.json').exists())
                self.assertEqual(len(calls), 2)

    def test_duplicate_or_conflicting_symbols_across_types_fail_closed(self):
        def fake(path, params):
            return {'result': [{'symbol': 'NVO', 'securityType': params['securityType'],
                                'isCommonShare': params['securityType'] == 'STOCK',
                                'isinCode': 'US0000000001' if params['securityType'] == 'STOCK' else 'US0000000002'}]}
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaisesRegex(ValueError, 'cross_exchange_symbol_collision'):
                Collector(td, fake).universe()
            self.assertFalse((Path(td)/'universe.json').exists())

    def test_legacy_scope_resume_preserves_stock_only_collection_and_raw_bytes(self):
        calls = []
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.config(root, schema=1)
            collector = Collector(root, self.client(calls))
            self.assertFalse(collector.include_adr)
            universe = collector.universe()
            self.assertEqual([r['symbol'] for r in universe['rows']], ['AAPL', 'IBM'])
            self.assertNotIn('security_types', universe)
            self.assertIn('response', universe['exchanges']['NYSE'])
            for item in universe['rows']:
                collector.collect_one(item, '2026-09-30')
            before = {p.relative_to(root): p.read_bytes() for p in root.rglob('*') if p.is_file()}
            with patch('market.history.collect.TossMarketClient', side_effect=AssertionError('unexpected network')), patch('builtins.print'):
                run(root, workers=1)
                summary = verify(root, require_finished=True)
            for name, data in before.items():
                self.assertEqual((root/name).read_bytes(), data)
            self.assertEqual(len(calls), 4)
            self.assertEqual(summary['counts'], {'received': 2})
            self.assertNotIn('DEPOSITARY_RECEIPT', summary['scope'])

    def test_schema2_cannot_reuse_stock_only_universe(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.config(root, schema=1)
            Collector(root, self.client([])).universe()
            self.config(root, schema=2)
            with self.assertRaisesRegex(ValueError, 'saved_listing_scope_mismatch'):
                Collector(root, self.client([])).universe()
            with self.assertRaisesRegex(AssertionError, 'listing_scope_mismatch'):
                verify(root)

    def test_verifier_requires_both_raw_listing_requests_even_when_adr_list_empty(self):
        for mutation in ('missing_type', 'wrong_request', 'missing_scope', 'wrong_count'):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                self.config(root)
                universe = Collector(root, self.client([])).universe()
                meta = universe['exchanges']['NASDAQ']
                if mutation == 'missing_type':
                    meta['requests'].pop('DEPOSITARY_RECEIPT')
                elif mutation == 'wrong_request':
                    key = meta['requests']['DEPOSITARY_RECEIPT']['response']
                    path = root/'responses'/(key+'.json.gz')
                    packet = json.loads(gzip.decompress(path.read_bytes()))
                    packet['request'][1]['commonShare'] = 'true'
                    path.write_bytes(gzip.compress(packed(packet)))
                elif mutation == 'missing_scope':
                    universe.pop('security_types')
                else:
                    meta['requests']['DEPOSITARY_RECEIPT']['count'] = 1
                    meta['count'] += 1
                save_checked(root/'universe.json', universe)
                with self.assertRaises(AssertionError):
                    verify(root)
                self.assertFalse((root/'verification.json').exists())

    def test_verifier_rejects_duplicate_identity_in_raw_listing_union(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.config(root)
            universe = Collector(root, self.client([])).universe()
            meta = universe['exchanges']['NYSE']['requests']['DEPOSITARY_RECEIPT']
            path = root/'responses'/(meta['response']+'.json.gz')
            packet = json.loads(gzip.decompress(path.read_bytes()))
            packet['payload']['result'][0]['symbol'] = 'AAPL'
            packet['sha256'] = hashlib.sha256(packed(packet['payload'])).hexdigest()
            path.write_bytes(gzip.compress(packed(packet)))
            with self.assertRaisesRegex(AssertionError, 'duplicate_listing'):
                verify(root)


if __name__ == '__main__':
    unittest.main()
