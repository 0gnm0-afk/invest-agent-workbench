"""M01 synthetic market collection with no selection or taxonomy artifacts."""
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import atomic_io
from launch.daily import DailyLaunch
import market.daily as daily
from market.prices import PriceStore, freeze_snapshot


class CollectionWithoutScreening(unittest.TestCase):
    def test_fake_collection_publishes_prices_quality_and_c9_only(self):
        with tempfile.TemporaryDirectory() as temp, ExitStack() as stack:
            root = Path(temp).resolve()
            config = {'engine':str(root), 'ohlcv':{'provider':'toss', 'path':str(root/'runtime/ohlcv.sqlite3')}}
            (root/'config.json').write_text(json.dumps(config), encoding='utf-8')
            stack.enter_context(patch.object(daily, 'ROOT', root))
            stack.enter_context(patch.object(daily, 'code_hashes', return_value={}))
            original_connect = PriceStore.connect
            def guarded(store, **kwargs):
                self.assertTrue(store.path.resolve().is_relative_to(root))
                return original_connect(store, **kwargs)
            stack.enter_context(patch.object(PriceStore, 'connect', guarded))
            targets = {'KR':'2026-10-16', 'US':'2026-10-16'}
            bars = [{'date':targets['US'], 'open':10., 'high':12., 'low':9., 'close':11., 'volume':100.}]
            rows = [dict(market=m, symbol=s, provider='Toss OpenAPI candles', currency=c,
                         price_basis='toss_adjusted_ohlc_no_order_authority', listing_exchange_family=e,
                         bars=bars) for m,s,c,e in [('KR','000001','KRW','KOSPI'),('US','AAA','USD','NYSE')]]
            rule = dict(ma_period=1,slope_window=1,transition_window=1,rs_period=1,activity_window=1)
            snapshot = dict(universe=[{k:v for k,v in row.items() if k!='bars'} for row in rows],
                            series=rows, errors=[], benchmarks={}, sessions={m:[d] for m,d in targets.items()},
                            profile={m:rule for m in targets}, price_collection_provider='toss',
                            fetched_at='2026-10-17T00:00:00+00:00', collected_until='2026-10-17T00:01:00+00:00',
                            coverage={'listings':{e:{'state':'available','limit':None} for e in ('KOSPI','NYSE')}, 'scope_errors':[]},
                            collection_stats={'requested':2,'received':2,'failed':0})
            def collect(output, config_path, **kwargs):
                source = output/'attempt'; source.mkdir(parents=True)
                frozen = freeze_snapshot(snapshot, source/'snapshot.json', PriceStore(root/'runtime/ohlcv.sqlite3'))
                atomic_io.write_json(source/'snapshot.json', frozen)
                atomic_io.write_json(source/'manifest.json', {'snapshot.json':atomic_io.sha(source/'snapshot.json')})
                atomic_io.write_json(output/'latest.json', {'attempt':str(source)})
            collector = stack.enter_context(patch.object(daily, 'collect', side_effect=collect))
            result = daily.run(config, session_cutoff='2026-10-16',
                               evaluated_at='2026-10-17T00:01:00+00:00')
            self.assertEqual(result['state'], 'succeeded', result.get('error'))
            collector.assert_called_once()
            output = Path(result['output'])
            self.assertEqual(set(result['stages']), {'inputs','collection','nh_fallback','adr_metadata','quality','report'})
            self.assertEqual(result['data_times']['session_dates'], targets)
            self.assertEqual(result['quality']['received'], 2)
            self.assertTrue((root/'runtime/records.sqlite3').is_file())
            self.assertTrue((root/'runtime/ohlcv.sqlite3').is_file())
            for name in ('screen.json','priority-review.json','priority-charts.json','candidates.csv','sectors'):
                self.assertFalse((output/name).exists())
            receipt = DailyLaunch(root).collection_receipt(output, targets)
            self.assertEqual(receipt['status'], 'complete')
            self.assertEqual(receipt['completed']['KR'], {'requested':1,'completed':1,'failed':0})
            self.assertEqual(receipt['failed'], {'KR':0,'US':0})
            self.assertEqual(receipt['sha256'], hashlib.sha256(atomic_io.read_bytes(output/'run.json')).hexdigest())
            self.assertEqual(daily.verify(output)['state'], 'succeeded')
            source = Path(result['market_source'])
            self.assertEqual(atomic_io.load(source/'snapshot.json')['series'][0]['bars'], bars)

    def test_retained_helpers_keep_exact_identity_and_freshness(self):
        from market.classification_refresh import parse_alias_quote, classification_freshness
        page = b'<title>ABC-A - Example</title><a href="?f=sec_technology">Technology</a><a href="?f=ind_software">Software</a><a href="?f=exch_nyse">NYSE</a><a href="https://www.google.com/finance/quote/ABC.A:NYSE">identity</a>'
        result = parse_alias_quote(page, 'ABC-A', 'ABC.A', 'NYSE')
        self.assertEqual(result['sector'], 'Technology')
        with self.assertRaisesRegex(ValueError, 'identity'):
            parse_alias_quote(page, 'ABC-A', 'OTHER', 'NYSE')
        proof = [{'observed_at':'2026-10-01T00:00:00+00:00'}]
        self.assertEqual(classification_freshness(proof, '2026-10-02T00:00:00+00:00')['state'], 'recent')
        self.assertEqual(classification_freshness(proof, '2026-10-12T00:00:00+00:00')['state'], 'refresh_due')
        self.assertEqual(classification_freshness(proof, '2026-09-30T00:00:00+00:00')['state'], 'unverified')
