"""Lossless record recipes must not copy or modify canonical prices."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from records.prices import encode, decode, completed_weeks_v1, _contains_prices
from market.prices import PriceStore


class RecordPriceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.run = self.root / 'runtime/day/run'
        self.source = self.root / 'runtime/day/source/snapshot.json'
        self.run.mkdir(parents=True)
        self.source.parent.mkdir(parents=True)
        self.store = PriceStore(self.root / 'runtime/ohlcv.sqlite3')
        self.bars = [dict(date=day, open=10., high=12., low=9., close=11., volume=100.)
                     for day in ('2026-10-12', '2026-10-13', '2026-10-14', '2026-10-15',
                                 '2026-10-16', '2026-10-19')]
        self.series = dict(market='US', symbol='AAA', currency='USD', provider='Toss OpenAPI candles',
                           price_basis='toss_adjusted_ohlc_no_order_authority', bars=self.bars)
        self.ref = self.store.put(self.series, source='fixture', kind='series')
        self.source.write_text(json.dumps(dict(ohlcv_store={'path': str(self.store.path)},
            series=[{**{k: v for k, v in self.series.items() if k != 'bars'}, 'bars_ref': self.ref}])), encoding='utf-8')
        (self.run / 'run.json').write_text(json.dumps({'market_source': str(self.source.parent)}), encoding='utf-8')
        (self.run / 'manifest.json').write_text(json.dumps({'input_hashes': {
            str(self.source.resolve()): hashlib.sha256(self.source.read_bytes()).hexdigest()}}), encoding='utf-8')

    def value(self):
        context = {k: v for k, v in self.series.items() if k != 'bars'}
        context.update(daily_bars=json.loads(json.dumps(self.bars)),
                       weekly_bars=completed_weeks_v1(self.bars, self.bars[-1]['date']))
        return dict(rows=[dict(market='US', symbol='AAA', technical_context=context)], memo='자료 원문')

    def test_exact_roundtrip_formats_and_no_price_copy_or_database_write(self):
        value = self.value()
        value['rows'][0]['technical_context']['daily_bars'][1]['volume'] = 100
        changed_order = value['rows'][0]['technical_context']['daily_bars'][1]
        value['rows'][0]['technical_context']['daily_bars'][1] = dict(reversed(list(changed_order.items())))
        before = hashlib.sha256(self.store.path.read_bytes()).hexdigest()
        for options, prefix, newline, suffix in (
            ({'ensure_ascii': False, 'indent': 2}, b'', '\n', '\n'),
            ({'ensure_ascii': True, 'indent': 4}, b'\xef\xbb\xbf', '\r\n', '\r\n'),
            ({'ensure_ascii': False, 'separators': (',', ':')}, b'', '\n', ''),
        ):
            raw = prefix + (json.dumps(value, **options) + suffix.replace('\r\n', '\n')).replace('\n', newline).encode('utf-8')
            packed = encode('screen.json', raw, self.run, self.root)
            self.assertEqual(decode(packed['encoding'], packed['payload'], {}), raw)
            self.assertFalse(_contains_prices(json.loads(packed['payload'])))
        self.assertEqual(hashlib.sha256(self.store.path.read_bytes()).hexdigest(), before)

    def test_prior_revision_remains_readable_after_correction(self):
        raw = json.dumps(self.value()).encode()
        packed = encode('screen-base.json', raw, self.run, self.root)
        revised = {**self.series, 'bars': json.loads(json.dumps(self.bars))}
        revised['bars'][-1]['close'] = 11.5
        self.store.put(revised, source='correction', kind='series')
        self.assertEqual(decode(packed['encoding'], packed['payload'], {}), raw)

    def test_source_identity_and_values_are_required(self):
        value = self.value()
        value['rows'][0]['technical_context']['currency'] = 'KRW'
        with self.assertRaisesRegex(ValueError, 'identity_mismatch'):
            encode('screen.json', json.dumps(value).encode(), self.run, self.root)
        value = self.value()
        value['rows'][0]['technical_context']['daily_bars'][0]['close'] = 20
        with self.assertRaisesRegex(ValueError, 'values_mismatch'):
            encode('screen.json', json.dumps(value).encode(), self.run, self.root)

    def test_source_manifest_hash_and_supported_paths_fail_closed(self):
        raw = json.dumps(self.value()).encode()
        with self.assertRaisesRegex(ValueError, 'unhandled_price_array'):
            encode('other.json', raw, self.run, self.root)
        value = self.value()
        value['another_price_copy'] = self.bars
        with self.assertRaisesRegex(ValueError, 'unhandled_price_array'):
            encode('screen.json', json.dumps(value).encode(), self.run, self.root)
        self.source.write_bytes(self.source.read_bytes() + b' ')
        with self.assertRaisesRegex(ValueError, 'source_integrity_failed'):
            encode('screen.json', raw, self.run, self.root)

    def test_weekly_policy_is_frozen_and_omits_unfinished_week(self):
        self.assertEqual(len(completed_weeks_v1(self.bars[:4], '2026-10-15')), 0)
        packed = encode('screen.json', json.dumps(self.value()).encode(), self.run, self.root)
        recipe = json.loads(packed['payload'])
        recipe['patches'][0]['weekly_policy'] = 'future_policy'
        with self.assertRaisesRegex(ValueError, 'weekly_policy_unsupported'):
            decode(packed['encoding'], json.dumps(recipe).encode(), {})

    def test_payload_or_external_original_hash_tampering_is_rejected(self):
        packed = encode('screen.json', json.dumps(self.value()).encode(), self.run, self.root)
        with self.assertRaisesRegex(ValueError, 'reference_hash_mismatch'):
            decode(packed['encoding'], packed['payload'], {'original_sha256': '0' * 64})
        recipe = json.loads(packed['payload'])
        recipe['value']['memo'] = 'rewritten'
        with self.assertRaisesRegex(ValueError, 'recovery_hash_mismatch'):
            decode(packed['encoding'], json.dumps(recipe).encode(), {})

    def test_nonprice_record_is_not_encoded(self):
        self.assertIsNone(encode('quality.json', b'{"quality": "partial"}', self.run, self.root))
        self.assertIsNone(encode('report.md', b'# Plain report', self.run, self.root))


if __name__ == '__main__':
    unittest.main()
