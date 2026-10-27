"""Provider retry contracts with synthetic data only; never query real providers."""
import copy,json,sys,tempfile,threading,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import market.exclusions as m
import market.retry as retry
import market.nh_fallback as nh_fallback
from test_market_exclusion_review import fixture,saved
from invest_agent.trading.store import instance_lock


class RetryFlow(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        saved(self.root,'before');self.path=self.root/'runtime/market-scope-user/records.json';self.services=[];self.calls=[]
        self.addCleanup(self.join)
    def join(self):
        for s in self.services:
            if s.thread:s.thread.join(5)
    def worker(self,id):
        job=m.registry(self.path)[0]['jobs'][-1];self.calls.append(job.get('provider'))
        m.update_job(self.path,id,collection_started_at=m.now())
        result=saved(self.root,'after-'+str(len(self.calls)))
        m.update_job(self.path,id,state='computed',result_run=str(result))
    def service(self,worker=None,publish=None):
        s=m.ExclusionReview(self.root,worker=worker or self.worker,publish=publish);self.services.append(s);return s
    def collect(self,s,provider):
        v=s.status()
        if s.thread and s.thread.is_alive():s.thread.join(5);v=s.status()
        s.recollect(v['review_id'],[v['issues'][0]['id']],provider)
        s.thread.join(5);self.assertFalse(s.thread.is_alive());return s.status()
    def test_toss_then_nh_once_then_exclude_persists_across_restart(self):
        s=self.service();v=s.status();ids=[v['issues'][0]['id']]
        self.assertFalse(v['issues'][0]['exclude_allowed']);self.assertTrue(v['issues'][0]['retry']['toss'])
        for action in (lambda:s.approve(v['review_id'],ids),lambda:s.recollect(v['review_id'],ids,'nh')):
            with self.assertRaises(m.ReviewConflict):action()
        v=self.collect(s,'toss');self.assertTrue(v['issues'][0]['retry']['nh']);self.assertFalse(v['issues'][0]['retry']['toss'])
        s=self.service();v=self.collect(s,'nh');self.assertTrue(v['issues'][0]['exclude_allowed'])
        for provider in ('toss','nh'):
            with self.assertRaises(m.ReviewConflict):s.recollect(v['review_id'],[v['issues'][0]['id']],provider)
        with patch.object(s,'_start'):s.approve(v['review_id'],[v['issues'][0]['id']])
        self.assertEqual(len(m.registry(self.path)[0]['records']),1)
        self.assertEqual(self.calls,['toss','nh'])
    def test_new_identity_or_completed_session_does_not_inherit_retry(self):
        s=self.service();self.collect(s,'toss');self.collect(s,'nh')
        snap,q=fixture();snap['universe'][0]['isin_code']='NEW';saved(self.root,'new-identity',snap,q)
        self.assertEqual(s.status()['issues'][0]['retry']['attempted'],[])
        snap,q=fixture();snap['sessions']['US']=['2026-10-05'];saved(self.root,'new-session',snap,q)
        self.assertEqual(s.status()['issues'][0]['retry']['attempted'],[])
    def test_stale_unknown_busy_and_duplicates_are_rejected(self):
        entered=threading.Event();release=threading.Event();self.addCleanup(release.set)
        def work(id):entered.set();release.wait(3);self.worker(id)
        s=self.service(work);v=s.status();ids=[v['issues'][0]['id']]
        for values in ([],ids+ids,['unknown']):
            with self.assertRaises(ValueError):s.recollect(v['review_id'],values,'toss')
        with instance_lock(self.root/'runtime'):
            with self.assertRaises(m.ReviewConflict):s.recollect(v['review_id'],ids,'toss')
        s.recollect(v['review_id'],ids,'toss');self.assertTrue(entered.wait(1))
        for service in (s,self.service()):
            with self.assertRaises(m.ReviewConflict):service.recollect(v['review_id'],ids,'toss')
        release.set();s.thread.join(5)
        with self.assertRaises(m.ReviewConflict):s.recollect(v['review_id'],ids,'nh')
    def test_failed_provider_still_unlocks_next_but_publication_failure_reuses_result(self):
        def fail(id):m.update_job(self.path,id,collection_started_at=m.now());raise ValueError('private auth failure')
        s=self.service(fail);v=self.collect(s,'toss')
        self.assertTrue(v['issues'][0]['retry']['nh']);self.assertFalse(v['job']['retry_available'])
        self.assertNotIn('private auth failure',json.dumps(v))
        s.worker=self.worker;s.publish=lambda p:(_ for _ in ()).throw(ValueError('publish'))
        v=self.collect(s,'nh')
        # A completed source can be re-published without another remote request.
        job=m.registry(self.path)[0]['jobs'][-1];m.update_job(self.path,job['id'],result_source=job['source_run'])
        s.publish=lambda p:None;s.retry(job['id']);s.thread.join(5)
        self.assertEqual(s.status()['job']['state'],'completed');self.assertEqual(self.calls,['nh'])
    def test_newer_daily_result_supersedes_failed_cached_retry(self):
        s=self.service();v=self.collect(s,'toss');job=m.registry(self.path)[0]['jobs'][-1]
        m.update_job(self.path,job['id'],state='failed',result_source='cached')
        snap,q=fixture();snap['sessions']['US']=['2026-10-05'];saved(self.root,'new-day',snap,q)
        v=s.status();self.assertEqual(v['job']['state'],'superseded');self.assertFalse(v['job']['retry_available'])
        with patch.object(s,'_start'):s.recollect(v['review_id'],[v['issues'][0]['id']],'toss')
        self.assertEqual(m.registry(self.path)[0]['jobs'][-1]['state'],'approved')

    def test_success_removes_issues_and_preserves_original(self):
        original=(self.root/'runtime/before/source/snapshot.json').read_bytes()
        def succeed(id):
            m.update_job(self.path,id,collection_started_at=m.now());snap,q=fixture()
            for field in ('universe','series'):snap[field][0]['market_cap']=100
            result=saved(self.root,'recovered',snap,q);m.update_job(self.path,id,state='computed',result_run=str(result))
        s=self.service(succeed);v=self.collect(s,'toss')
        self.assertEqual(v['issues'],[]);self.assertEqual(v['job']['state'],'completed')
        self.assertEqual((self.root/'runtime/before/source/snapshot.json').read_bytes(),original)


class TossCollection(unittest.TestCase):
    def get(self,path,params):
        self.calls.append((path,params))
        if path=='/api/v1/stocks':return {'result':[dict(symbol='FAIL',isinCode='USFAIL',market='NASDAQ',currency='USD',sharesOutstanding='100',status='ACTIVE')]}
        self.assertEqual(params['symbol'],'FAIL')
        return {'result':{'candles':[dict(timestamp='2026-10-02T00:00:00-04:00',openPrice=1,highPrice=2,lowPrice=1,closePrice=2,volume=1,currency='USD')]}}
    def setUp(self):self.calls=[];self.snap,self.q=fixture();self.issue=m.inventory(self.snap,self.q)['issues'][0]
    def test_cap_only_retains_prices_other_symbols_and_original(self):
        before=copy.deepcopy(self.snap);after,outcomes=retry.toss_retry(self.snap,[self.issue],self.get)
        self.assertEqual(self.snap,before);self.assertEqual(after['series'][1],before['series'][1])
        self.assertEqual(after['series'][0]['bars'],before['series'][0]['bars']);self.assertEqual(after['universe'][0]['market_cap'],200)
        self.assertEqual([c[0] for c in self.calls],['/api/v1/stocks','/api/v1/candles']);self.assertEqual(self.calls[1][1]['adjusted'],'false')
        self.assertEqual(outcomes[0]['state'],'adopted')
    def test_identity_mismatch_makes_no_price_request_and_keeps_original(self):
        def get(path,params):v=self.get(path,params);v['result'][0]['isinCode']='DIFFERENT';return v
        after,outcomes=retry.toss_retry(self.snap,[self.issue],get)
        self.assertEqual(len(self.calls),1);self.assertEqual(after['series'],self.snap['series']);self.assertEqual(outcomes[0]['state'],'unavailable')
    def test_price_retry_validates_full_series_and_does_not_copy_cap(self):
        self.issue['field']='가격 이력';self.snap['series']=self.snap['series'][1:]
        self.snap['errors']=[{'market':'US','symbol':'FAIL','error_code':'market_http_404'}]
        after,outcomes=retry.toss_retry(self.snap,[self.issue],self.get)
        self.assertEqual(after['errors'],[]);self.assertEqual(outcomes[0]['state'],'adopted')
        self.assertEqual(self.calls[-1][1]['adjusted'],'true')
        badget=lambda p,a: self.get(p,a) if p=='/api/v1/stocks' else {'result':{'candles':[]}}
        failed,outcomes=retry.toss_retry(self.snap,[self.issue],badget)
        self.assertEqual(failed['errors'],self.snap['errors']);self.assertEqual(outcomes[0]['state'],'unavailable')


class NHSelection(unittest.TestCase):
    def test_only_selected_symbol_requested_no_automatic_exclusion_and_other_reports_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp);source=folder/'source';source.mkdir();snap,q=fixture()
            snap['errors']=[{'market':'US','symbol':'FAIL','error_code':'market_http_404'},{'market':'US','symbol':'KEEP','error_code':'market_http_404'}];snap['series']=[]
            snap['nh_fallback']['results'].append({**snap['nh_fallback']['results'][0],'symbol':'KEEP'})
            m.save(source/'snapshot.json',snap)
            policy={'dual_provider':{'enabled':True},'records':[]}
            calls=[]
            def missing(client,symbol,sessions,market):calls.append(symbol);raise ValueError('nh_period_empty')
            masters=[{'sNationCode':'USA','sStockCode':'NQQ','sSymbol':'FAIL','sStandardCode':'USFAIL'}]
            config=dict(nh_us_master='us',nh_us_master_sha256='x',nh_kr_master='kr',nh_kr_master_sha256='x')
            with patch('market.scope.load',return_value=policy),patch('market.scope.apply_snapshot',side_effect=lambda s,p:s),patch.object(nh_fallback,'load_checked',return_value=masters),patch.object(nh_fallback,'_reference_rows',return_value={}),patch.object(nh_fallback,'MarketClient'),patch.object(nh_fallback,'_nh_bars',side_effect=missing),patch.object(nh_fallback,'audit',return_value={}):
                output,summary,_=nh_fallback.recover(source,folder/'new',config,only_keys={('US','FAIL')},allow_auto_exclusions=False)
            result=m.load(output/'snapshot.json');self.assertEqual(calls,['FAIL']);self.assertEqual(summary['automatic_exclusions'],[])
            self.assertEqual(len(result['universe']),2);self.assertEqual(len(result['nh_fallback']['results']),2)
            self.assertEqual(next(r for r in result['nh_fallback']['results'] if r['symbol']=='KEEP'),snap['nh_fallback']['results'][1])




class RetryHTTP(unittest.TestCase):
    def test_browser_schema_csrf_and_real_service_sequence(self):
        import http.client
        from web.server import handler_for,ThreadingHTTPServer
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);saved(root,'before');registry=root/'runtime/market-scope-user/records.json'
            calls=[]
            def worker(id):
                calls.append(id);m.update_job(registry,id,collection_started_at=m.now())
                m.update_job(registry,id,state='computed',result_run=str(root/'runtime/before'))
            service=m.ExclusionReview(root,worker=worker)
            server=ThreadingHTTPServer(('127.0.0.1',0),handler_for({}, {}, Path(__file__).resolve().parents[1]/'dashboard','token',root/'workspace.sqlite3',exclusion_review=service))
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            headers={'Origin':'http://127.0.0.1:'+str(server.server_port),'X-Workbench-Token':'token','Sec-Fetch-Site':'same-origin','Content-Type':'application/json'}
            def request(method,path,body=None,headers_override=None):
                c=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=5);c.request(method,path,json.dumps(body) if body else None,headers=headers if headers_override is None else headers_override)
                response=c.getresponse();value=(response.status,json.loads(response.read()),response.getheaders());c.close();return value
            try:
                _,_,hs=request('GET','/health');headers['Cookie']=next(v.split(';')[0] for k,v in hs if k.lower()=='set-cookie' and v.startswith('workbench_scope_confirm='))
                _,v,_=request('GET','/api/market-exclusions');body={'action':'recollect','provider':'toss','review_id':v['review_id'],'issue_ids':[v['issues'][0]['id']]}
                for field in ['Cookie','Origin','X-Workbench-Token','Sec-Fetch-Site']:
                    bad=dict(headers);bad.pop(field);self.assertEqual(request('POST','/api/market-exclusions',body,bad)[0],403)
                self.assertEqual(request('POST','/api/market-exclusions',{**body,'provider':'all'})[0],400)
                self.assertEqual(request('POST','/api/market-exclusions',{**body,'provider':'nh'})[0],409)
                self.assertEqual(request('POST','/api/market-exclusions',body)[0],202);service.thread.join(5)
                self.assertEqual(request('POST','/api/market-exclusions',body)[0],409)
                self.assertEqual(len(calls),1)
            finally:
                if service.thread:service.thread.join(5)
                server.shutdown();server.server_close();thread.join(5)

class RetryWorker(RetryFlow):
    def test_replay_failure_restores_pointer_and_reuses_collected_source(self):
        import market.daily as daily_market, market.repair as repair_market_scope
        s=self.service();v=s.status()
        with patch.object(s,'_start'):s.recollect(v['review_id'],[v['issues'][0]['id']],'toss')
        job=m.registry(self.path)[0]['jobs'][-1];m.save(self.root/'config.json',{})
        before=m.load(self.root/'runtime/latest.json');fixture_get=TossCollection();fixture_get.setUp();calls=[]
        def run(config,source):
            with retry.socket.socket() as sock,self.assertRaisesRegex(AssertionError,'retry_replay_network_forbidden'):
                sock.connect(('127.0.0.1',1))
            calls.append(str(source));m.save(self.root/'runtime/latest.json',{'output':'invalid'})
            return {'error':'synthetic failure'}
        with patch.object(repair_market_scope,'ROOT',self.root),patch.object(repair_market_scope,'audit',return_value={'state':'partial'}),patch.object(retry,'audit',return_value={}),patch.object(retry,'TossMarketClient',return_value=fixture_get.get) as factory,patch.object(daily_market,'run',side_effect=run):
            with self.assertRaisesRegex(ValueError,'retry_calculation_failed'):retry.apply_retry(self.root,self.path,job['id'])
            self.assertEqual(m.load(self.root/'runtime/latest.json'),before)
            collected=m.registry(self.path)[0]['jobs'][-1]['result_source']
            def success(config,source):
                self.assertEqual(str(source),collected)
                return {'output':str(self.root/'runtime/recovered'),'stages':{'report':'completed'}}
            with patch.object(daily_market,'run',side_effect=success):retry.apply_retry(self.root,self.path,job['id'])
            self.assertEqual(factory.call_count,1)
        self.assertEqual(m.registry(self.path)[0]['jobs'][-1]['state'],'computed')
    def test_interrupted_collection_is_not_silently_repeated(self):
        s=self.service();v=s.status()
        with patch.object(s,'_start'):s.recollect(v['review_id'],[v['issues'][0]['id']],'toss')
        job=m.registry(self.path)[0]['jobs'][-1];m.save(self.root/'config.json',{})
        m.update_job(self.path,job['id'],collection_started_at=m.now())
        with patch.object(retry,'TossMarketClient') as factory:
            with self.assertRaisesRegex(ValueError,'retry_collection_interrupted'):retry.apply_retry(self.root,self.path,job['id'])
            factory.assert_not_called()


class ReadRecovery(unittest.TestCase):
    def test_windows_transient_read_lock_recovers_but_persistent_failure_surfaces(self):
        with patch.object(Path,'read_bytes',side_effect=[PermissionError(),b'{"ok":true}']),patch.object(m.time,'sleep'):
            self.assertEqual(m.load('synthetic.json'),{'ok':True})
        with patch.object(Path,'read_bytes',side_effect=PermissionError()),patch.object(m.time,'sleep'):
            with self.assertRaises(PermissionError):m.load('synthetic.json')

if __name__=='__main__':unittest.main()
