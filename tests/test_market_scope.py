import copy,hashlib,json,sys,tempfile,unittest
from pathlib import Path
from datetime import datetime,timezone
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import market_scope as m
from toss_universe import discover
from test_toss_universe import FakeToss
import classification_refresh as refresh
from sector_observation import checked,classification_age_issue,SECTORS

class ScopeTests(unittest.TestCase):
    def policy(self,symbol='LOW'):
        return {'schema':1,'scope':'market_symbol_until_user_restores','path':'test','sha256':'a'*64,'records':[{'market':'US','symbol':symbol,'reason':'user_excluded','approved_on':'2026-10-02','approval':'test approval','evidence_run':'test'}]}

    def test_approved_scope_blocks_detail_and_price_requests(self):
        fake=FakeToss()
        rows,meta=discover(fake,['US'],now=datetime(2026,10,2,tzinfo=timezone.utc),exclusions=self.policy())
        self.assertEqual({r['symbol'] for r in rows},{'HIGH'})
        for path,params in fake.calls:
            if path!='/api/v1/stocks/all':self.assertNotIn('LOW',params['symbols'].split(','))
        self.assertEqual(meta['NASDAQ']['approved_exclusions'][0]['symbol'],'LOW')
        self.assertEqual(meta['NASDAQ']['state'],'available')

    def test_daily_and_raw_risk_never_fetch_excluded_candles(self):
        import hybrid_market as h
        from test_hybrid_market import TossMarketPipelineTests
        fixture=TossMarketPipelineTests();fixture.setUp()
        try:
            fixture.config['universe'][1]['symbol']='LOW'
            fixture.config['risk_universe']=[{'market':'US','symbol':'LOW'}]
            with patch('market_scope.load',return_value=self.policy()):
                result=fixture.run_collect()
            self.assertNotIn('LOW',[c.args[1] for c in fixture.candles.call_args_list])
            self.assertEqual([r['symbol'] for r in result['universe']],['005930.KS'])
            self.assertEqual(result['scope_exclusions'][0]['symbol'],'LOW')
        finally:fixture.doCleanups()

    def test_scope_change_does_not_reuse_old_unfiltered_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake=FakeToss();discover(fake,['US'],cache_root=tmp,now=datetime(2026,10,2,tzinfo=timezone.utc))
            fake.calls=[]
            rows,_=discover(fake,['US'],cache_root=tmp,now=datetime(2026,10,2,tzinfo=timezone.utc),exclusions=self.policy())
            self.assertEqual({r['symbol'] for r in rows},{'HIGH'})
            self.assertTrue(fake.calls)

    def test_unapproved_error_stays_and_source_is_unchanged(self):
        snap={'universe':[{'market':'US','symbol':s,'listing_exchange_family':'NASDAQ'} for s in ('LOW','HIGH')],
              'series':[],'errors':[{'market':'US','symbol':s} for s in ('LOW','HIGH')],
              'coverage':{'scope_errors':[],'listings':{'NASDAQ':{'state':'available','limit':None}},'market_wide':False},'collection_stats':{}}
        before=copy.deepcopy(snap);got=m.apply_snapshot(snap,self.policy())
        self.assertEqual(snap,before);self.assertEqual([r['symbol'] for r in got['errors']],['HIGH'])
        self.assertFalse(got['coverage']['active_scope_complete'])
        self.assertEqual(got['scope_exclusions'][0]['symbol'],'LOW')

    def test_excluded_failure_can_leave_complete_active_scope(self):
        snap={'universe':[{'market':'US','symbol':s,'listing_exchange_family':'NASDAQ'} for s in ('LOW','HIGH')],
              'series':[{'market':'US','symbol':'HIGH'}],'errors':[{'market':'US','symbol':'LOW'}],
              'coverage':{'scope_errors':[],'listings':{'NASDAQ':{'state':'available','limit':None}},'market_wide':False},'collection_stats':{}}
        got=m.apply_snapshot(snap,self.policy());self.assertTrue(got['coverage']['active_scope_complete'])
        self.assertFalse(got['coverage']['market_wide']);self.assertEqual(got['coverage']['requested'],1)
        self.assertEqual(got['coverage']['listings']['NASDAQ']['approved_excluded'],1)
        snap['coverage']['scope_errors']=[{'stage':'listing'}]
        self.assertFalse(m.apply_snapshot(snap,self.policy())['coverage']['active_scope_complete'])

    def test_qualified_nh_recovery_recomputes_active_completeness(self):
        snap={'universe':[{'market':'US','symbol':s,'listing_exchange_family':'NASDAQ'} for s in ('LOW','HIGH')],
              'series':[],'errors':[{'market':'US','symbol':s} for s in ('LOW','HIGH')],
              'coverage':{'scope_errors':[],'listings':{'NASDAQ':{'state':'available','limit':None}},'market_wide':False},'collection_stats':{}}
        got=m.apply_snapshot(snap,self.policy())
        self.assertFalse(got['coverage']['active_scope_complete'])
        got['series']=[{'market':'US','symbol':'HIGH'}];got['errors']=[]
        got['coverage']['market_wide']=True
        m.refresh_coverage(got)
        self.assertTrue(got['coverage']['active_scope_complete'])
        self.assertFalse(got['coverage']['market_wide'])
        got['coverage']['listings']['NASDAQ']['state']='partial'
        m.refresh_coverage(got)
        self.assertFalse(got['coverage']['active_scope_complete'])

    def test_saved_nh_recovery_removes_excluded_before_target_requests(self):
        import nh_fallback as nh
        snap={'universe':[{'market':'US','symbol':s,'listing_exchange_family':'NASDAQ'} for s in ('LOW','HIGH')],
              'series':[{'market':'US','symbol':'HIGH'}],
              'errors':[{'market':'US','symbol':'LOW','error_code':'market_transport_or_payload_unavailable'}],
              'coverage':{'scope_errors':[],'listings':{'NASDAQ':{'state':'available','limit':None}},'market_wide':False},'collection_stats':{}}
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'source';source.mkdir();(source/'snapshot.json').write_text(json.dumps(snap))
            config={'nh_us_master':'us','nh_us_master_sha256':'u','nh_kr_master':'kr','nh_kr_master_sha256':'k'}
            with patch('market_scope.load',return_value=self.policy()),patch.object(nh,'load_checked',return_value=[]),patch.object(nh,'_reference_rows',return_value={}),patch.object(nh,'MarketClient') as client,patch.object(nh,'audit',return_value={}):
                _,summary,_=nh.recover(source,root/'output',config)
                self.assertEqual(summary['attempted'],0)
                client.return_value.read.assert_not_called()
                saved=json.loads((root/'output/snapshot.json').read_text(encoding='utf-8'))
                self.assertEqual([r['symbol'] for r in saved['universe']],['HIGH'])
                self.assertTrue(saved['coverage']['active_scope_complete'])
                self.assertFalse(saved['coverage']['market_wide'])

    def test_policy_requires_approval_and_unique_market_symbol(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'policy.json';data=self.policy();data['records'][0]['approval']='';p.write_text(json.dumps(data))
            with self.assertRaises(ValueError):m.load(p)
            data=self.policy();data['records']*=2;p.write_text(json.dumps(data))
            with self.assertRaises(ValueError):m.load(p)

    def test_new_listing_waiting_does_not_hide_gaps_or_other_failures(self):
        screen={'rows':[{'market':'US','symbol':'NEW','state':'unavailable','universe':{'universe_rejection_reasons':['unavailable_insufficient_liquidity_bars']}}]}
        snapshot={'series':[{'market':'US','symbol':'NEW','bars':[{'date':'2026-09-30'},{'date':'2026-10-01'}]}], 'sessions':{'US':['2026-09-29','2026-09-30','2026-10-01']}}
        self.assertEqual(len(m.analysis_pending(screen,snapshot)),1)
        screen['rows'][0]['universe']['universe_rejection_reasons'].insert(0,'market_cap_below_minimum')
        self.assertEqual(m.analysis_pending(screen,snapshot)[0]['known_rejections'],['market_cap_below_minimum'])
        snapshot['series'][0]['bars'][0]['date']='2026-09-29'
        self.assertFalse(m.analysis_pending(screen,snapshot))
        snapshot['series'][0]['bars'][0]['date']='2026-09-30'
        screen['rows'][0]['universe']['universe_rejection_reasons'].append('unavailable_market_cap')
        self.assertFalse(m.analysis_pending(screen,snapshot))

    def test_selected_chart_cannot_refetch_approved_exclusion(self):
        from chart_history import HistoryStore
        with tempfile.TemporaryDirectory() as tmp,patch('market_scope.load',return_value=self.policy()),patch.object(HistoryStore,'view',return_value={'bars':[]}):
            factory=lambda: self.fail('must not create network client')
            result=HistoryStore(Path(tmp)/'cache.db',factory).ensure({'market':'US','symbol':'LOW'},retry=True)
            self.assertEqual(result['history']['state'],'excluded')

    def test_bulk_history_cannot_refetch_approved_exclusion(self):
        from us_toss_history import Collector
        with tempfile.TemporaryDirectory() as tmp,patch('market_scope.load',return_value=self.policy()):
            collector=Collector(Path(tmp),client=lambda *args:self.fail('must not query excluded symbol'))
            self.assertEqual(collector.collect_one({'symbol':'LOW'},'2026-10-01',True)['state'],'excluded')

class RefreshTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.html=b'<title>NEW - New Inc Stock Price and Quote</title><a href="/screener.ashx?f=sec_healthcare">Healthcare</a><a href="/screener.ashx?f=ind_biotechnology">Biotechnology</a><a href="/screener.ashx?f=exch_nasd">NASD</a><a href="https://www.google.com/finance/quote/NEW:NASDAQ">NEW</a>'
        self.row={'market':'US','symbol':'NEW','isin_code':'US0001','exchange':'NASDAQ','group_state':'unmatched','reason':'new_listing_classification_not_acquired'}
        (self.root/'quote.html').write_bytes(self.html)
        self.write('meta.json',{'url':'https://finviz.com/stock?t=NEW','final_url':'https://finviz.com/stock?t=NEW','status':200,'observed_at':'2026-10-02T05:00:00+00:00','sha256':self.sha('quote.html')})
        self.write('snapshot.json',{'universe':[{'market':'US','symbol':'NEW','isin_code':'US0001','listing_exchange_family':'NASDAQ'}]})
        self.manifest={'schema':1,'identity_snapshot':{'path':str(self.root/'snapshot.json'),'sha256':self.sha('snapshot.json')},'records':[{'market':'US','symbol':'NEW','isin_code':'US0001','exchange':'NASDAQ','finviz_symbol':'NEW','document':'quote.html','meta':'meta.json','meta_sha256':self.sha('meta.json')}]}
        self.sync()
    def sha(self,name):return hashlib.sha256((self.root/name).read_bytes()).hexdigest()
    def write(self,name,obj):(self.root/name).write_text(json.dumps(obj),encoding='utf-8')
    def sync(self):self.write('manifest.json',self.manifest);self.snapshot={'classification_refresh':{'path':str(self.root/'manifest.json'),'sha256':self.sha('manifest.json')}}
    def run_refresh(self,row=None):return refresh.apply([row or self.row],self.snapshot,checked,{},'2026-10-02T06:00:00+00:00',classification_age_issue,SECTORS)
    def test_exact_identity_and_direct_evidence_refresh(self):
        self.assertEqual(self.run_refresh()[0]['group_name'],'Healthcare');self.assertEqual(self.row['group_state'],'unmatched')
    def test_changed_isin_is_not_inherited(self):
        self.assertEqual(self.run_refresh({**self.row,'isin_code':'US0002'})[0]['group_state'],'unmatched')
    def test_tampered_html_is_rejected(self):
        (self.root/'quote.html').write_bytes(self.html+b'changed')
        with self.assertRaisesRegex(ValueError,'input_hash_mismatch'):self.run_refresh()
    def test_unverified_source_identity_is_rejected(self):
        self.manifest['records'][0]['isin_code']='OTHER';self.sync()
        with self.assertRaisesRegex(ValueError,'identity_unverified'):self.run_refresh()
    def test_future_evidence_is_rejected(self):
        meta=json.loads((self.root/'meta.json').read_text());meta['observed_at']='2099-01-01T00:00:00+00:00';self.write('meta.json',meta);self.manifest['records'][0]['meta_sha256']=self.sha('meta.json');self.sync()
        with self.assertRaisesRegex(ValueError,'timestamp_invalid'):self.run_refresh()
    def test_verified_operating_unit_can_be_repaired_without_excluding_it(self):
        self.manifest['records'][0]['allow_operating_unit']=True;self.sync()
        original={k:self.row[k] for k in ('market','symbol','isin_code','exchange')}
        original.update(identity_matched=True,source_security_type='유닛')
        row={**self.row,'reason':'original_conflict:security_type_conflict','original_classification':original}
        self.assertEqual(self.run_refresh(row)[0]['group_state'],'matched')
        for typ in ('우선주','폐쇄형펀드'):
            row['original_classification']['source_security_type']=typ
            self.assertEqual(self.run_refresh(row)[0]['group_state'],'unmatched')
        row['original_classification'].update(source_security_type='유닛',isin_code='OTHER')
        self.assertEqual(self.run_refresh(row)[0]['group_state'],'unmatched')

    def test_type_conflict_not_overridden(self):
        row={**self.row,'reason':'original_conflict:security_type_conflict'}
        self.assertEqual(self.run_refresh(row)[0],row)

if __name__=='__main__':unittest.main()
