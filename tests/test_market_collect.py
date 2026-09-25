import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import market_collect as m
import test_hybrid_market as base_tests


class MarketOnlyTests(base_tests.TossMarketPipelineTests):
    def full_config(self):
        rows = self.config['universe']
        rows[0]['listing_exchange_family'] = 'KOSPI'
        rows[1]['listing_exchange_family'] = 'NASDAQ'
        self.config.update(universe_mode='exchange_listings', markets=['KR', 'US'], limit_per_exchange=None)
        meta = {e: {'state': 'available', 'listed': int(e != 'NYSE')}
                for e in ('KOSPI', 'NASDAQ', 'NYSE')}
        return rows, meta

    def test_one_failed_series_never_reports_full_market_success(self):
        rows, meta = self.full_config()
        original = self.candle_series
        def candles(*args, **kwargs):
            if args[1] == 'EXAMPLE':
                raise ValueError('stale_or_invalid_last_session')
            return original(*args, **kwargs)
        self.candles.side_effect = candles
        with patch('hybrid_market.discover', return_value=(rows, meta)):
            result = self.run_collect()
        self.assertFalse(result['coverage']['market_wide'])
        self.assertEqual(m.audit(result)['state'], 'partial')
        self.assertEqual(result['errors'][0]['error_code'], 'stale_or_invalid_last_session')

    def test_quality_holes_are_visible_even_with_valid_latest_bar(self):
        rows, meta = self.full_config()
        with patch('hybrid_market.discover', return_value=(rows, meta)):
            result = self.run_collect()
        result['series'][0]['bars'].pop(1)
        audited = m.audit(result)
        self.assertEqual(audited['state'], 'partial')
        self.assertEqual(audited['quality_issue_series'], 1)
        result['errors'].append({'market': 'US', 'symbol': 'EXAMPLE'})
        with self.assertRaisesRegex(ValueError, 'identity_partition'):
            m.audit(result)

    def test_no_completed_bars_is_distinct_from_stale_history(self):
        rows, meta = self.full_config()
        for empty, expected in ((True, 'no_completed_session_bars'),
                                (False, 'stale_or_invalid_last_session')):
            with self.subTest(empty=empty):
                def candles(*args, **kwargs):
                    value = self.candle_series(*args, **kwargs)
                    if args[1] == 'EXAMPLE':
                        value['bars'] = [] if empty else value['bars'][:-1]
                    return value
                self.candles.side_effect = candles
                with patch('hybrid_market.discover', return_value=(rows, meta)):
                    result = self.run_collect()
                self.assertEqual(result['errors'][0]['error_code'], expected)
                self.assertEqual(result['collection_stats']['received'], 1)
                self.assertFalse(result['coverage']['market_wide'])
                self.assertEqual(m.audit(result)['state'], 'partial')

    def test_market_only_resume_and_manifest_without_account_calls(self):
        rows, meta = self.full_config()
        with patch('hybrid_market.discover', return_value=(rows, meta)):
            snapshot = self.run_collect()
        project = Path(m.__file__).resolve().parents[1]
        (project / 'runtime').mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=project / 'runtime') as directory:
            root = Path(directory)
            config = root / 'input.json'
            config.write_text(json.dumps(self.config))
            with patch.object(m, 'completed_sessions', return_value=self.days), \
                    patch.object(m, 'TossMarketClient') as client, \
                    patch.object(m, 'collect', return_value=snapshot) as collector:
                first = m.run(root, config)
                second = m.run(root)
                self.assertEqual((first['state'], second['state']), ('succeeded', 'succeeded'))
                self.assertEqual(collector.call_args.args[1], {})
                self.assertNotIn('risk_universe', collector.call_args.args[0])
                self.assertNotIn('additional_universe', collector.call_args.args[0])
                with patch.object(m, 'collector_version', return_value='changed'):
                    with self.assertRaises(ValueError):
                        m.run(root)
                self.assertEqual(client.call_count, 2)
            latest = json.loads((root / 'latest.json').read_text(encoding='utf-8'))
            attempt = Path(latest['attempt'])
            manifest = json.loads((attempt / 'manifest.json').read_text(encoding='utf-8'))
            for name, expected in manifest.items():
                self.assertEqual(hashlib.sha256((attempt / name).read_bytes()).hexdigest(), expected)

    def test_completed_cap_cache_and_preserved_null_quote(self):
        rows, meta = self.full_config()
        rows[1].update(quote_time_state='not_provided', quote_timestamp=None,
                       market_cap=None, market_cap_as_of=None, shares_outstanding=100)
        fields = dict(market_cap=10000, market_cap_as_of=self.days[-1],
                      market_cap_quality='completed_close_estimate')
        with patch('hybrid_market.discover', side_effect=lambda *a, **k: ([dict(r) for r in rows], meta)), \
                patch('hybrid_market.complete_cap', return_value=fields) as cap:
            first = self.run_collect()
            second = self.run_collect()
        self.assertEqual(cap.call_count, 1)
        for result in (first, second):
            self.assertEqual(result['series'][1]['market_cap_as_of'], self.days[-1])
            self.assertIsNone(result['series'][1]['quote_timestamp'])
            self.assertEqual(m.audit(result)['market_cap_completed_close_estimates'], 1)

    def test_fallback_transport_failure_remains_partial_not_handled_success(self):
        rows, meta = self.full_config()
        rows[1].update(quote_time_state='not_provided', shares_outstanding=100)
        with patch('hybrid_market.discover', return_value=(rows, meta)), \
                patch('hybrid_market.complete_cap', side_effect=ValueError('market_http_500')):
            result = self.run_collect()
        self.assertEqual(len(result['series']), 2)
        self.assertFalse(result['coverage']['market_wide'])
        quality = m.audit(result)
        self.assertEqual(quality['state'], 'partial')
        self.assertEqual(quality['processing_state'], 'failed')
        self.assertEqual(quality['unexpected_failures'], 1)


if __name__ == '__main__':
    unittest.main()
