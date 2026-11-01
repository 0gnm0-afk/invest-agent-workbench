import json
import hashlib
from contextlib import closing
from copy import deepcopy
import tempfile
import unittest
from pathlib import Path
import sys
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from market.history.collect import Collector, read_checked, parse_page, save_checked
from market.providers.toss_client import TossMarketClient


def candle(day):
    return dict(timestamp=day+'T14:00:00+09:00',currency='USD',openPrice='10',highPrice='12',lowPrice='9',closePrice='11',volume='100')


class HistoryTests(unittest.TestCase):
    def test_provider_end_and_both_resume_levels(self):
        pages=[{'result':{'candles':[candle('2026-09-30'),candle('2016-01-04')],'nextBefore':'2015-12-31T14:00:00+09:00'}},{'result':{'candles':[candle('2015-12-31')],'nextBefore':None}}]
        calls=[]
        def fake(path,params):calls.append(params);return pages[len(calls)-1]
        with tempfile.TemporaryDirectory() as td:
            c=Collector(td,fake);r=c.collect_one({'symbol':'AAPL'},'2026-09-30')
            self.assertEqual((r['state'],r['first'],len(r['bars'])),('received','2015-12-31',3))
            self.assertTrue(r['provider_end_reached'])
            self.assertEqual(c.collect_one({'symbol':'AAPL'},'2026-09-30'),r)
            for p in (Path(td)/'series').glob('*.json'):p.unlink()
            self.assertEqual(c.collect_one({'symbol':'AAPL'},'2026-09-30')['bars'],r['bars'])
            self.assertEqual(len(calls),2)
            p=next((Path(td)/'series').glob('*.json'));v=json.loads(p.read_bytes())
            self.assertNotIn('bars',v['data']);self.assertIn('bars_ref',v['data'])
            self.assertEqual(Path(v['data']['ohlcv_path']),Path(td)/'ohlcv.sqlite3')
            v['data']['first']='2000-01-01';p.write_text(json.dumps(v))
            with self.assertRaisesRegex(ValueError,'saved_hash_mismatch'):read_checked(p)

    def test_backfill_marker_preserves_current_prices_and_replays_verified_raw(self):
        from market.prices import PriceStore
        from market.history.saved import SavedHistory
        from market.history.verify import verify
        from test_prices import series
        with tempfile.TemporaryDirectory() as td:
            workbench=Path(td);runtime=workbench/'runtime';root=runtime/'us-history'/'new'
            root.mkdir(parents=True)
            (workbench/'config.json').write_text(json.dumps({'ohlcv':{'path':'runtime/prices.sqlite3'}}))
            store=PriceStore(runtime/'prices.sqlite3')
            current=series();current.update(price_basis='toss_adjusted_ohlc_no_order_authority')
            current['bars']=[dict(current['bars'][0],date='2026-09-30',close=11.5)]
            pinned=store.put(current,source='daily')
            item=dict(symbol='AAA',isinCode=current['isin_code'],market='US')
            calls=[]
            def get(path,params):
                calls.append(params)
                return {'result':{'candles':[candle('2026-09-30'),candle('2016-01-04')],'nextBefore':None}}
            collector=Collector(root,get)
            result=collector.collect_one(item,'2026-09-30')
            self.assertEqual(store.read(pinned),current['bars'])
            marker=root/'series'/(hashlib.sha256(b'AAA').hexdigest()+'.json')
            receipt=json.loads(marker.read_bytes())
            self.assertEqual(receipt['data']['ohlcv_path'],str(store.path))
            self.assertNotIn('bars',receipt['data'])
            self.assertEqual(read_checked(marker),result)
            with closing(store.connect()) as db:
                self.assertEqual(db.execute('SELECT count(*) FROM prices').fetchone()[0],1)
                self.assertEqual(db.execute('SELECT count(*) FROM price_variants').fetchone()[0],2)
            save_checked(root/'collection.json',{'markets':['US'],'end':'2026-09-30','scope':'synthetic'})
            save_checked(root/'universe.json',{'rows':[item]})
            verify(root,require_finished=True)
            pointer=root.parent/'active.json'
            pointer.write_text(json.dumps({'collection':root.name,
                'manifest_sha256':hashlib.sha256((root/'manifest.json').read_bytes()).hexdigest()}))
            replay=SavedHistory(pointer,'US')
            self.assertFalse(replay.error)
            self.assertEqual(replay.read('series/'+marker.name,wrapped=True),result)
            self.assertEqual(Collector(root,lambda *_:self.fail('network')).collect_one(item,'2026-09-30'),result)
            self.assertEqual(len(calls),1)
            # Full marker and source responses remain valid after a later live correction.
            newer=deepcopy(current);newer['bars'][0]['close']=11.75
            store.put(newer,source='later')
            self.assertEqual(read_checked(marker),result)
            self.assertEqual(verify(root,require_finished=True)['counts'],{'received':1})

    def test_standalone_backfill_never_selects_the_operating_database(self):
        with tempfile.TemporaryDirectory() as td,patch('market.prices.default_path',side_effect=AssertionError('operating DB')):
            value=Collector(td,lambda *_:{'result':{'candles':[candle('2026-09-30')],'nextBefore':None}}).collect_one({'symbol':'AAA'},'2026-09-30')
            marker=next((Path(td)/'series').glob('*.json'))
            self.assertEqual(read_checked(marker),value)
            self.assertTrue((Path(td)/'ohlcv.sqlite3').exists())

    def test_original_legacy_series_resume_preserves_sha_and_format(self):
        from market.history.collect import packed
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);saved=root/'series'/(hashlib.sha256(b'AAA').hexdigest()+'.json')
            saved.parent.mkdir()
            value=dict(symbol='AAA',market='US',listing={'symbol':'AAA'},requested_end='2026-09-30',
                       state='received',bars=parse_page({'result':{'candles':[candle('2026-09-30')]}},'2026-09-30')[0])
            raw=(json.dumps({'data':value,'sha256':hashlib.sha256(packed(value)).hexdigest()},indent=4)+'\n').encode()
            saved.write_bytes(raw)
            with patch('market.prices.default_path',side_effect=AssertionError('operating DB')):
                self.assertEqual(Collector(root,lambda *_:self.fail('network')).collect_one({'symbol':'AAA'},'2026-09-30'),value)
            self.assertEqual(saved.read_bytes(),raw)
            self.assertFalse((root/'ohlcv.sqlite3').exists())

    def test_parallel_backfill_workers_share_one_isolated_price_store(self):
        from concurrent.futures import ThreadPoolExecutor
        with tempfile.TemporaryDirectory() as td:
            collector=Collector(td,lambda *_:{'result':{
                'candles':[candle('2026-09-30')],'nextBefore':None}})
            with ThreadPoolExecutor(max_workers=6) as pool:
                results=list(pool.map(
                    lambda i:collector.collect_one({'symbol':'TEST'+str(i)},'2026-09-30'),range(18)))
            markers=list((Path(td)/'series').glob('*.json'))
            self.assertEqual(len(markers),18)
            self.assertEqual(sorted(read_checked(p)['symbol'] for p in markers),
                             sorted(row['symbol'] for row in results))

    def test_resumed_workers_share_one_client_and_rate_limiter(self):
        from concurrent.futures import ThreadPoolExecutor
        import time
        def factory(**kwargs):
            time.sleep(.01)
            return lambda path,params: {'result':params}
        with tempfile.TemporaryDirectory() as td,patch('market.history.collect.TossMarketClient',side_effect=factory) as create:
            c=Collector(td)
            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(lambda i:c.get('/api/v1/candles',{'symbol':str(i)}),range(8)))
            create.assert_called_once()

    def test_runner_does_not_retain_completed_full_histories(self):
        import tracemalloc
        from market.history.collect import run
        class FakeCollector(Collector):
            def universe(self):return {'rows':[{'symbol':str(i)} for i in range(40)]}
            def collect_one(self,item,end,retry=False):
                return {'state':'received','bars':[bytearray(1024*1024)]}
        with tempfile.TemporaryDirectory() as td:
            save_checked(Path(td)/'collection.json',{'end':'2026-09-30'})
            tracemalloc.start()
            try:
                with patch('market.history.collect.Collector',FakeCollector),patch('builtins.print'):
                    run(td,workers=4)
                _,peak=tracemalloc.get_traced_memory()
            finally:tracemalloc.stop()
            self.assertLess(peak,16*1024*1024)
            progress=json.loads((Path(td)/'progress.json').read_bytes())
            self.assertEqual((progress['processed'],progress['bars']),(40,40))

    def test_future_duplicate_and_currency_rejected(self):
        bad=[{'result':{'candles':[candle('2026-10-01')]}},{'result':{'candles':[candle('2016-01-04'),candle('2016-01-04')]}},{'result':{'candles':[{**candle('2016-01-04'),'currency':'KRW'}]}}]
        for page in bad:
            with self.subTest(page=page),self.assertRaises(ValueError):parse_page(page,'2026-09-30')

    def test_cycle_is_saved_as_failure_with_partial_data(self):
        def fake(path,params):return {'result':{'candles':[candle('2016-01-04')],'nextBefore':'2016-01-04T14:00:00+09:00'}}
        with tempfile.TemporaryDirectory() as td:
            r=Collector(td,fake).collect_one({'symbol':'AAPL'},'2026-09-30')
            self.assertEqual(r['state'],'failed');self.assertEqual(len(r['bars']),1)
            self.assertIn('cursor_not_descending',r['error'])
            save_checked(Path(td)/'collection.json',{'end':'2026-09-30','scope':'synthetic'})
            save_checked(Path(td)/'universe.json',{'rows':[{'symbol':'AAPL'}]})
            from market.history.verify import verify
            result=verify(td,require_finished=True)
            self.assertEqual(result['counts'],{'failed':1})
            self.assertTrue(result['all_attempted'])

    def test_opt_in_rate_preserves_default_and_provider_limit(self):
        import io
        with patch('market.providers.toss_client.get_token',return_value='fake'):
            self.assertEqual(TossMarketClient()._min_interval,.25)
            c=TossMarketClient(min_interval=.1)
            for limit,expected in [('20',.1),('2',.5)]:
                response=io.BytesIO(b'{"result":{}}');response.headers={'X-RateLimit-Limit':limit}
                with patch('market.providers.toss_client.urllib.request.urlopen',return_value=response),patch.object(c,'_wait'):
                    c('/api/v1/candles',{})
                self.assertEqual(c._interval,expected)
            for value in [0,True,float('nan'),2]:
                with self.assertRaises(ValueError):TossMarketClient(min_interval=value)


if __name__=='__main__':unittest.main()
