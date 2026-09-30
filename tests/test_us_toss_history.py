import json
import tempfile
import unittest
from pathlib import Path
import sys
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from us_toss_history import Collector, read_checked, parse_page
from toss_market_client import TossMarketClient


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
            p=next((Path(td)/'series').glob('*.json'));v=json.loads(p.read_bytes());v['data']['bars'][0]['close']=22;p.write_text(json.dumps(v))
            with self.assertRaisesRegex(ValueError,'saved_hash_mismatch'):read_checked(p)

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

    def test_opt_in_rate_preserves_default_and_provider_limit(self):
        import io
        with patch('toss_market_client.get_token',return_value='fake'):
            self.assertEqual(TossMarketClient()._min_interval,.25)
            c=TossMarketClient(min_interval=.1)
            for limit,expected in [('20',.1),('2',.5)]:
                response=io.BytesIO(b'{"result":{}}');response.headers={'X-RateLimit-Limit':limit}
                with patch('toss_market_client.urllib.request.urlopen',return_value=response),patch.object(c,'_wait'):
                    c('/api/v1/candles',{})
                self.assertEqual(c._interval,expected)
            for value in [0,True,float('nan'),2]:
                with self.assertRaises(ValueError):TossMarketClient(min_interval=value)


if __name__=='__main__':unittest.main()
