import copy,json,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import nh_fallback as n
import market_scope as m
from nh_market_auth import NHMarketError
from nh_market_client import MarketClient,PERIOD,us_period_inputs,digest

class ExpandedFallbackTests(unittest.TestCase):
    def policy(self):
        return {'schema':1,'scope':'market_symbol_until_user_restores','path':'test','sha256':'a'*64,'records':[],
          'dual_provider':{'enabled':True,'rule_id':'dual','approved_on':'2026-10-02','approval':'user'}}

    def run_recovery(self, reason, healthy=True, toss='stale_or_invalid_last_session'):
        row={'market':'US','symbol':'TEST','isin_code':'US_TEST','listing_exchange_family':'NASDAQ'}
        snap={'universe':[row,dict(row,symbol='GOOD')],'series':[dict(row,symbol='GOOD')],
              'errors':[{'market':'US','symbol':'TEST','error_code':toss}],
              'sessions':{'US':['2026-10-01']},'providers':{},
              'coverage':{'scope_errors':[],'listings':{'NASDAQ':{'state':'available','limit':None}},'market_wide':False},'collection_stats':{}}
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);src=root/'source';src.mkdir();(src/'snapshot.json').write_text(json.dumps(snap))
            cfg={'nh_us_master':'us','nh_us_master_sha256':'u','nh_kr_master':'kr','nh_kr_master_sha256':'k'}
            with patch('market_scope.load',return_value=self.policy()),patch.object(n,'load_checked',return_value=[]),patch.object(n,'_reference_rows',return_value={}),patch.object(n,'MarketClient'),patch.object(n,'audit',return_value={}),patch.object(n,'_nh_bars',side_effect=reason) as call,patch.object(n,'healthy_control',return_value=healthy):
                out,summary,_=n.recover(src,root/'out',cfg)
                self.assertEqual(call.call_count,1)
                return summary,json.loads((out/'snapshot.json').read_text(encoding='utf8'))

    def test_stale_missing_and_transport_all_targeted(self):
        codes=['stale_or_invalid_last_session','no_completed_session_bars','market_transport_or_payload_unavailable','market_http_401','completed_sessions_unavailable','KeyError']
        rows=[{'market':'US','symbol':str(i),'error_code':c} for i,c in enumerate(codes)]
        self.assertEqual(len(n.fallback_targets({'errors':rows})),4)

    def test_historical_gap_and_unavailable_cap_targeted_without_touching_normal(self):
        rows=[{'market':'US','symbol':'GAP','bars':[{'date':'2026-09-28'},{'date':'2026-10-01'}]},
              {'market':'US','symbol':'CAP','market_cap_quality':'unavailable_quote_time'},
              {'market':'US','symbol':'NORMAL'}]
        got=n.fallback_targets({'errors':[],'series':rows,'sessions':{'US':['2026-09-28','2026-09-29','2026-10-01']}})
        self.assertEqual([r['symbol'] for r in got],['GAP','CAP'])

    def test_per_security_no_data_excluded_only_with_healthy_control(self):
        for exc in (NHMarketError('market_http_400'),ValueError('nh_period_empty'),ValueError('nh_period_completed_session_missing')):
            got,snap=self.run_recovery(exc)
            self.assertEqual(len(got['automatic_exclusions']),1)
            self.assertEqual(got['unrecovered'],0)
            self.assertEqual([r['symbol'] for r in snap['universe']],['GOOD'])
            self.assertEqual(snap['errors'],[])

    def test_auth_rate_limit_server_network_and_parity_failure_never_excluded(self):
        for reason in ('market_http_401','market_http_403','market_http_429','market_http_500','market_transport_failure','nh_toss_historical_ohlc_mismatch'):
            got,snap=self.run_recovery(NHMarketError(reason))
            self.assertEqual(got['automatic_exclusions'],[])
            self.assertEqual(len(snap['errors']),1)

    def test_bad_control_and_toss_systemic_failure_never_excluded(self):
        for healthy,toss in ((False,'stale_or_invalid_last_session'),(True,'market_transport_or_payload_unavailable')):
            got,_=self.run_recovery(NHMarketError('market_http_400'),healthy,toss)
            self.assertEqual(got['automatic_exclusions'],[])

    def test_dual_exclusion_requires_same_nonempty_isin_and_effective_date(self):
        policy=self.policy();policy['records']=[{'market':'US','symbol':'TEST','isin_code':'OLD','dual_provider_rule':'dual','effective_session':'2026-10-01'}]
        for isin in (None,'NEW'):
            self.assertIsNone(m.match({'market':'US','symbol':'TEST','isin_code':isin},policy))
        row={'market':'US','symbol':'TEST','isin_code':'OLD'}
        self.assertIsNone(m.match(row,policy,completed_session='2026-09-30'))
        self.assertIsNotNone(m.match(row,policy,completed_session='2027-01-01'))

    def test_lifecycle_never_expires_but_retains_identity_and_historical_guard(self):
        policy=self.policy();policy['lifecycle_records']=[{'market':'US','symbol':'TEST','isin_code':'OLD','state':'confirmed_trading_ended','effective_session':'2026-09-01','verified_on':'2026-09-24','source_url':'https://official.test'}]
        row={'market':'US','symbol':'TEST','isin_code':'OLD'}
        self.assertIsNotNone(m.match(row,policy,completed_session='2027-01-01'))
        self.assertIsNone(m.match(row,policy,completed_session='2026-08-31'))
        self.assertIsNone(m.match(dict(row,isin_code='NEW'),policy,completed_session='2027-01-01'))

    def test_cached_400_replays_without_auth_or_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);args=us_period_inputs('TEST','20261001');p=root/'responses'/(digest(PERIOD,args)+'.json');p.parent.mkdir()
            p.write_text(json.dumps({'path':PERIOD,'inputs':args,'http_status':400}))
            with patch('nh_market_client.token') as token:
                with self.assertRaisesRegex(NHMarketError,'market_http_400'):MarketClient(root).read(PERIOD,args)
                token.assert_not_called()

    def test_dated_nh_close_cap_and_identity_currency_date_guards(self):
        row={'symbol':'TEST','shares_outstanding':100};bar={'date':'2026-10-01','close':10}
        payload={'rsp_cd':'00000','Output_0':{'iem_cd':'TEST','exch_id':'NQQ','currency_unit':'USD','trade_date':'20261001','normal_trdprc':10}}
        self.assertEqual(n.completed_cap(row,bar,payload,'NQQ')['market_cap'],1000)
        for key,value in [('iem_cd','OTHER'),('trade_date','20260930'),('normal_trdprc',9),('currency_unit','KRW')]:
            bad=copy.deepcopy(payload);bad['Output_0'][key]=value
            with self.assertRaisesRegex(ValueError,'dated_market_cap'):n.completed_cap(row,bar,bad,'NQQ')

if __name__=='__main__':unittest.main()
