"""M03 synthetic Toss orchestration, approved scope and durable C9 receipts."""
from copy import deepcopy
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import atomic_io
import market.daily as daily
import market.toss_collection as toss
from market.collect import audit
from market.collection_receipts import ROLE, collection_receipt, read_collection_receipt
from launch.daily import DailyLaunch


class SingleProviderTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.days = ['2026-09-09', '2026-09-10', '2026-09-11']
        rule = dict(ma_period=2, slope_window=1, transition_window=1, rs_period=1,
                    activity_window=1, min_rs=0, min_activity_ratio=1, min_average_value=0)
        # An obsolete provider setting must never reactivate NH requests.
        self.config = {'engine': str(self.root), 'profile': {'status': 'review_only', 'KR': rule, 'US': rule},
                       'ohlcv': {'provider': 'legacy', 'path': 'runtime/ohlcv.sqlite3'},
                       'records': {'path': 'runtime/records.sqlite3'}}
        atomic_io.write_json(self.root / 'config.json', self.config)
        self.calendar = patch.object(daily, 'completed_sessions', return_value=self.days).start()
        self.nh = patch.object(daily.nh_fallback, 'recover', side_effect=AssertionError('new_nh_forbidden')).start()
        self.addCleanup(patch.stopall)

    def snapshot(self, failed=()):
        universe = [dict(market=market, symbol=symbol, provider_symbol=symbol, isin_code='ISIN-' + symbol,
                         listing_exchange_family=exchange, benchmark='^KS11' if market == 'KR' else '^GSPC')
                    for market, symbol, exchange in [('KR', 'KR-FIXTURE', 'KOSPI'),
                                                     ('US', 'OUTSIDE-WATCH', 'NASDAQ'),
                                                     ('US', 'ADR-FIXTURE', 'NYSE')]]
        universe[-1].update(security_type='DEPOSITARY_RECEIPT', market_cap=None,
                            market_cap_quality='unavailable_adr_share_basis')
        series = [{**row, 'currency': 'KRW' if row['market'] == 'KR' else 'USD',
                   'provider': 'Toss OpenAPI candles', 'price_basis': 'toss_adjusted_ohlc_no_order_authority',
                   'bars': [dict(date=day, open=10., high=12., low=9., close=11., volume=1000.) for day in self.days]}
                  for row in universe if row['symbol'] not in failed]
        errors = [dict(market=row['market'], symbol=row['symbol'], error_code='stale_or_invalid_last_session')
                  for row in universe if row['symbol'] in failed]
        return dict(universe=universe, series=series, errors=errors, benchmarks={},
                    sessions={m: self.days for m in ('KR', 'US')}, profile=self.config['profile'],
                    price_collection_provider='toss', fetched_at='2026-09-12T00:00:00+00:00',
                    collected_until='2026-09-12T00:00:00+00:00',
                    providers={m: 'Toss adjusted candles' for m in ('KR', 'US')},
                    collection_stats=dict(requested=3, received=len(series), failed=len(errors)),
                    scope_exclusions=[], coverage=dict(scope_errors=[], market_wide=not errors,
                        listings={e: {'state': 'available', 'limit': None} for e in ('KOSPI', 'NASDAQ', 'NYSE')}))

    def source(self, snapshot, name='source'):
        folder = self.root / 'runtime' / name
        folder.mkdir(parents=True)
        atomic_io.write_json(folder / 'snapshot.json', snapshot)
        atomic_io.write_json(folder / 'manifest.json', {'snapshot.json': atomic_io.sha(folder / 'snapshot.json')})
        return folder

    def execute(self, snapshot, *, mode='replay'):
        source = self.source(snapshot, mode)
        if mode == 'live':
            def collect(folder, *args, **kwargs):
                self.assertEqual(kwargs['project_root'], self.root)
                atomic_io.write_json(folder / 'latest.json', {'attempt': str(source)})
            with patch.object(daily, 'collect', side_effect=collect) as collector:
                result = daily.run(self.config, root=self.root)
                collector.assert_called_once()
        else:
            with patch.object(daily, 'collect', side_effect=AssertionError('saved_collection_forbidden')):
                result = daily.run(self.config, root=self.root, **{('source' if mode == 'replay' else 'recover_source'): source})
        return result

    def check_receipt(self, result, expected):
        output = Path(result['output'])
        receipt = read_collection_receipt(output)
        manifest = atomic_io.load(output / 'manifest.json')
        self.assertEqual(set(receipt), {'target_trade_date', 'completed', 'failed', 'status', 'provenance', 'sha256'})
        self.assertEqual(receipt['status'], expected)
        self.assertEqual(receipt['sha256'], manifest['output_hashes']['run.json'])
        self.assertEqual(receipt['sha256'], hashlib.sha256(atomic_io.read_bytes(output / 'run.json')).hexdigest())
        self.assertEqual(receipt['target_trade_date'], dict(KR=self.days[-1], US=self.days[-1]))
        self.assertEqual(receipt['provenance'], dict(provider='toss', run_id=output.name, mode=result['mode']))
        self.assertFalse((output / 'priority-review.json').exists())
        self.assertEqual(receipt, DailyLaunch(self.root).collection_receipt(output))
        self.assertIn(ROLE, manifest['output_hashes'])
        # The C1 publisher durably stores the new role before latest is visible.
        self.assertIn('record_artifact_ref', json.loads((output / ROLE).read_bytes()))
        return receipt

    def test_live_and_recover_ignore_legacy_nh_setting(self):
        for mode in ('live', 'recover'):
            with self.subTest(mode=mode):
                result = self.execute(self.snapshot(), mode=mode)
                self.assertEqual(result['state'], 'succeeded')
                self.assertEqual(result['nh_fallback']['reason'], 'single_provider_toss')
                self.check_receipt(result, 'complete')
                report = (Path(result['output']) / 'report.md').read_text(encoding='utf-8')
                self.assertIn('신규 가격 공급자: Toss', report)
        self.nh.assert_not_called()

    def test_partial_and_failed_receipts_keep_market_counts(self):
        for missing, expected in [(('OUTSIDE-WATCH',), 'partial'),
                                   (('OUTSIDE-WATCH', 'ADR-FIXTURE'), 'failed')]:
            with self.subTest(missing=missing):
                source = self.source(self.snapshot(missing), expected)
                result = daily.run(self.config, source=source, root=self.root)
                receipt = self.check_receipt(result, expected)
                self.assertEqual(receipt['completed']['US'], dict(requested=2, completed=2-len(missing), failed=len(missing)))
                self.assertEqual(receipt['failed']['US'], len(missing))
        self.nh.assert_not_called()

    def test_collection_exception_has_failed_receipt_without_nh_substitution(self):
        with patch.object(daily, 'collect', side_effect=ValueError('fixture_toss_unavailable')):
            result = daily.run(self.config, root=self.root)
        self.assertEqual(result['state'], 'failed')
        receipt = read_collection_receipt(result['output'])
        self.assertEqual(receipt['status'], 'failed')
        self.assertEqual(receipt['completed']['US'], dict(requested=0, completed=0, failed=0))
        self.nh.assert_not_called()

    def test_legacy_nh_snapshot_replay_preserves_source_and_never_collects(self):
        snapshot = self.snapshot()
        snapshot['series'][1]['provider'] = 'NH PLUG period'
        snapshot['nh_fallback'] = dict(attempted=1, adopted=1, unrecovered=0)
        self.calendar.side_effect = AssertionError('saved_replay_calendar_forbidden')
        result = self.execute(snapshot)
        self.check_receipt(result, 'complete')
        self.assertEqual(atomic_io.load(Path(result['market_source']) / 'snapshot.json')['series'][1]['provider'], 'NH PLUG period')
        self.assertEqual(result['nh_fallback']['provenance'], 'saved_source_no_new_requests')
        self.assertIn('과거 NH 자료 포함 · 새 NH 요청 없음', (Path(result['output']) / 'report.md').read_text(encoding='utf-8'))
        self.nh.assert_not_called()

    def test_receipt_rejects_counts_hash_target_and_record_tampering(self):
        result = self.execute(self.snapshot())
        receipt = self.check_receipt(result, 'complete')
        changed = deepcopy(result)
        changed['quality']['markets']['US']['failed'] = 9
        with self.assertRaisesRegex(ValueError, 'collection_counts_invalid'):
            collection_receipt(changed, receipt['sha256'])
        with self.assertRaisesRegex(ValueError, 'collection_run_hash_invalid'):
            collection_receipt(result, 'bad')
        later = dict(receipt['target_trade_date'], US='2026-09-14')
        self.assertEqual(collection_receipt(result, receipt['sha256'], targets=later)['status'], 'partial')
        path = Path(result['output']) / ROLE
        path.write_text('{}', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'input_hash_mismatch'):
            read_collection_receipt(path.parent)

    def test_corrupt_saved_input_is_failed_and_not_replaced(self):
        source = self.source(self.snapshot())
        (source / 'snapshot.json').write_text('{}', encoding='utf-8')
        with patch.object(daily, 'collect', side_effect=AssertionError('saved_collection_forbidden')):
            result = daily.run(self.config, source=source, root=self.root)
        self.assertEqual(result['state'], 'failed')
        self.assertEqual(read_collection_receipt(result['output'])['status'], 'failed')
        self.nh.assert_not_called()

    def test_approved_scope_adr_missing_cap_and_failed_price_remain_distinct(self):
        snapshot = self.snapshot()
        rows = deepcopy(snapshot['universe'])
        rows += [dict(rows[1], symbol='EXCLUDED'), dict(rows[1], symbol='PRICE-FAILED')]
        approved = dict(market='US', symbol='EXCLUDED', isin_code=rows[-2]['isin_code'], exchange='NASDAQ',
                        reason='user_excluded_nh_unrecovered', identity_bound=True, approval_id='fixture-approval',
                        approval='fixture', approved_on='2026-09-12', evidence_run='fixture')
        atomic_io.write_json(self.root / 'runtime/market-scope-user/records.json',
                             dict(schema=1, records=[approved], jobs=[]))
        import market.exclusions as exclusions
        wrong_registry = self.root / 'wrong-root.json'
        wrong_registry.write_text('invalid json: default root must not be read', encoding='utf-8')
        config = dict(self.config, universe_mode='exchange_listings', markets=['KR', 'US'],
                      limit_per_exchange=None, ohlcv_path=str(self.root / 'runtime/ohlcv.sqlite3'))
        requests = []
        def candles(get, symbol, market, days, **kwargs):
            requests.append(symbol)
            if symbol == 'PRICE-FAILED':
                raise ValueError('stale_or_invalid_last_session')
            return dict(market=market, symbol=symbol, provider='Toss OpenAPI candles', instrument_type='EQUITY',
                        currency='KRW' if market == 'KR' else 'USD',
                        price_basis='toss_adjusted_ohlc_no_order_authority',
                        bars=deepcopy(snapshot['series'][0]['bars']))
        with patch.object(toss, 'completed_sessions', return_value=self.days), \
                patch.object(toss.market_scope, 'REGISTRY', wrong_registry), \
                patch.object(exclusions, 'REGISTRY', wrong_registry), \
                patch.object(toss, 'discover', return_value=(rows, snapshot['coverage']['listings'])), \
                patch.object(toss, 'daily_series', side_effect=candles):
            result = toss.collect(config, {}, self.root / 'runtime/cache', lambda *args: None,
                                  now=datetime(2026, 9, 12, tzinfo=timezone.utc), project_root=self.root)
        quality = audit(result)
        self.assertNotIn('EXCLUDED', requests)
        self.assertIn('OUTSIDE-WATCH', requests)
        self.assertIn('ADR-FIXTURE', requests)
        self.assertEqual(result['scope_exclusions'][0]['symbol'], 'EXCLUDED')
        self.assertEqual(result['errors'][0]['symbol'], 'PRICE-FAILED')
        self.assertEqual(result['collection_stats']['requested'], 4)
        self.assertEqual(quality['market_cap_issues'][0]['symbol'], 'ADR-FIXTURE')
        adr = next(row for row in result['series'] if row['symbol'] == 'ADR-FIXTURE')
        self.assertIsNone(adr['market_cap'])
        # Collection only creates OHLCV/checkpoint data, never monitoring tables.
        import sqlite3
        for path in (self.root / 'runtime').glob('*.sqlite3'):
            with closing(sqlite3.connect(path)) as db:
                tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
                self.assertFalse(any(name.startswith(('technical_', 'scenario_monitor')) for name in tables))
        self.nh.assert_not_called()
