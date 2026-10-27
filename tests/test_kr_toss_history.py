import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from market.history.collect import Collector, parse_page, save_checked, run
from market.history.verify import verify

class KoreanHistoryTests(unittest.TestCase):
    def test_both_exchanges_identity_currency_pagination_and_raw_audit(self):
        calls=[]
        def fake(path,params):
            calls.append((path,params))
            if path.endswith('/stocks/all'):
                return {'result':[dict(symbol='005930' if params['market']=='KOSPI' else '0001A0',securityType='STOCK',isCommonShare=True)]}
            assert params['symbol'] in ('005930','0001A0')
            assert params['before']=='2026-09-30T23:59:59+09:00'
            return {'result':{'candles':[dict(timestamp='2026-09-30T00:00:00+09:00',currency='KRW',openPrice='1000',highPrice='1200',lowPrice='900',closePrice='1100',volume='100')],'nextBefore':None}}
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);c=Collector(root,fake,market='KR');u=c.universe()
            self.assertEqual({r['symbol'] for r in u['rows']},{'005930.KS','0001A0.KQ'})
            save_checked(root/'collection.json',{'markets':['KR'],'end':'2026-09-30','scope':'synthetic'})
            for item in u['rows']:
                r=c.collect_one(item,'2026-09-30');self.assertEqual(r['currency'],'KRW');self.assertEqual(r['state'],'received')
                self.assertEqual(c.collect_one(item,'2026-09-30'),r)
            self.assertEqual(len(calls),4)
            result=verify(root,True)
            self.assertEqual(result['counts'],{'received':2});self.assertIn('XKRX',result['calendar'])
            u['rows'].pop();save_checked(root/'universe.json',u)
            with self.assertRaises((KeyError,AssertionError)):verify(root,True)
    def test_korean_historical_dst_uses_local_trading_date(self):
        timestamps=['1988-10-08T23:00:00+09:00','1988-05-08T23:00:00+09:00','1987-05-10T23:00:00+09:00','1987-05-09T00:00:00+09:00']
        candles=[dict(timestamp=t,currency='KRW',openPrice=10,highPrice=12,lowPrice=9,closePrice=11,volume=100) for t in timestamps]
        bars,_=parse_page({'result':{'candles':candles}},'2026-09-30','KRW')
        self.assertEqual([b['date'] for b in bars],['1988-10-09','1988-05-09','1987-05-11','1987-05-09'])

    def test_existing_engine_extra_closure_is_not_missing_data(self):
        def fake(path,params):
            return {'result':{'candles':[dict(timestamp=day+'T00:00:00+09:00',currency='KRW',openPrice=10,highPrice=12,lowPrice=9,closePrice=11,volume=100) for day in ['2026-06-04','2026-06-02']],'nextBefore':None}}
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);item={'symbol':'005930.KS','provider_symbol':'005930'}
            Collector(root,fake,market='KR').collect_one(item,'2026-06-04')
            save_checked(root/'collection.json',{'markets':['KR'],'end':'2026-06-04','scope':'synthetic'})
            save_checked(root/'universe.json',{'rows':[item]})
            result=verify(root,True)
            self.assertEqual(result['with_internal_gaps'],0)
            self.assertIn('2026-06-03',result['extra_closures'])

    def test_wrong_currency_and_cross_market_resume_rejected(self):
        with self.assertRaises(ValueError):parse_page({'result':{'candles':[{'timestamp':'2026-09-30T00:00:00+09:00','currency':'USD'}]}},'2026-09-30','KRW')
        with tempfile.TemporaryDirectory() as td:
            save_checked(Path(td)/'collection.json',{'markets':['US'],'end':'2026-09-30'})
            with self.assertRaisesRegex(ValueError,'collection_market_mismatch'):run(td,market='KR')
    def test_cross_exchange_identity_collision_rejected(self):
        def fake(path,params):return {'result':[{'symbol':'005930'}]}
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaisesRegex(ValueError,'cross_exchange_symbol_collision'):Collector(td,fake,market='KR').universe()

if __name__=='__main__':unittest.main()
