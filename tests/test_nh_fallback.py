import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from market.nh_fallback import _compare, _nh_bars
from market.providers.nh_auth import NHMarketError, token
from market.providers.nh_client import MarketClient


class NHFallbackTest(unittest.TestCase):
    def fixture(self):
        sessions = [f'2026-09-{i:02d}' for i in range(1, 30)]
        sessions += [f'2026-10-{i:02d}' for i in range(1, 31)]
        sessions += [f'2026-11-{i:02d}' for i in range(1, 31)]
        sessions += [f'2026-12-{i:02d}' for i in range(1, 31)]
        sessions += [f'2027-01-{i:02d}' for i in range(1, 31)]
        bars = [{'date':d,'open':10.,'high':11.,'low':9.,'close':10.,'volume':1000.}
                for d in sessions]
        prior = {'provider':'Toss OpenAPI candles',
                 'bars':[{**b,'volume':1500.} for b in bars[:-1]]}
        return sessions,bars,prior

    def test_historical_price_and_volume_window_required(self):
        sessions,bars,prior = self.fixture()
        result = _compare(bars,prior,sessions)
        self.assertEqual(result['overlap_sessions'],len(sessions)-1)
        prior['bars'][2]['high'] = 12.
        with self.assertRaisesRegex(ValueError,'historical_ohlc_mismatch'):
            _compare(bars,prior,sessions)
        prior['bars'][2]['high'] = 11.
        prior['bars'][2]['volume'] = 2000.
        with self.assertRaisesRegex(ValueError,'volume_relation_unverified'):
            _compare(bars,prior,sessions)

    def test_nonrecent_reference_rejected(self):
        sessions,bars,prior = self.fixture()
        prior['bars'] = prior['bars'][:-22]
        with self.assertRaisesRegex(ValueError,'recent_toss_overlap_insufficient'):
            _compare(bars,prior,sessions)

    def test_korean_volume_requires_exact_match(self):
        sessions,bars,prior = self.fixture()
        for bar in prior['bars']:
            bar['volume'] = 1000.
        self.assertTrue(_compare(bars,prior,sessions,'KR')['ohlc_exact'])
        prior['bars'][2]['volume'] = 1001.
        with self.assertRaisesRegex(ValueError,'volume_relation_unverified'):
            _compare(bars,prior,sessions,'KR')

    def test_period_requires_completed_session(self):
        class FakeClient:
            def read(self,path,inputs):
                rows = [{'trade_date':date.replace('-',''),'open_prc':10,'high':11,
                         'low':9,'close_prc':10,'movolume':1} for date in
                        ('2026-09-22','2026-09-24')]
                return {'payload':{'rsp_cd':'00000','Output_1':rows}},Path('evidence')
        with self.assertRaisesRegex(ValueError,'history_incomplete'):
            _nh_bars(FakeClient(),'TEST',['2026-09-22','2026-09-23','2026-09-24'])

    def test_korean_integrated_market_parser(self):
        class FakeClient:
            def read(self,path,inputs):
                self_test.assertEqual(path,'/krstock/quote/v1/period')
                self_test.assertEqual(inputs['market_cd'],'UNT')
                self_test.assertEqual(inputs['view_main_yn'],'N')
                rows=[{'bsop_date':date.replace('-',''),'stck_oprc':'10','stck_hgpr':'11',
                       'stck_lwpr':'9','stck_prpr':'10','vol':'1234'} for date in
                      ('2026-09-22','2026-09-23','2026-09-24')]
                return {'payload':{'rsp_cd':'00000','Output_1':rows}},Path('evidence')
        self_test=self
        bars,_ = _nh_bars(FakeClient(),'005930',['2026-09-22','2026-09-23','2026-09-24'],'KR')
        self.assertEqual(bars[-1]['volume'],1234)

    def test_account_path_rejected_before_auth(self):
        with tempfile.TemporaryDirectory() as tmp, patch('market.providers.nh_client.token') as auth:
            client = MarketClient(Path(tmp))
            with self.assertRaisesRegex(NHMarketError,'market_only_period_required'):
                client.read('/gbstock/inquiry/v1/balance',{})
            auth.assert_not_called()

    def test_missing_auth_never_queries_market(self):
        with patch.dict(os.environ,{'NH_APP_KEY':'','NH_APP_SECRET':''}):
            with self.assertRaisesRegex(NHMarketError,'credentials_missing'):
                token()


if __name__ == '__main__':
    unittest.main()
