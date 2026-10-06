"""Synthetic approvals only: no production registry or remote data is touched."""
import copy
import http.client
import json
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch

import market_exclusion_review as m
import market_scope
from invest_agent.trading.store import instance_lock


def fixture():
    rows=[dict(market='US',symbol=s,name=s,isin_code='US'+s,listing_exchange_family='NASDAQ',
               provider_symbol=s,market_cap=None if s=='FAIL' else 100,market_cap_quality='verified') for s in ('FAIL','KEEP')]
    reports=[dict(market='US',symbol='FAIL',state='unavailable',nh_requested=True,
                  toss_error_code='unavailable_market_cap',reason='market_http_400')]
    snapshot=dict(universe=rows,series=[dict(r,bars=[dict(date='2026-10-02',open=1,high=2,low=1,close=2,volume=1)]) for r in rows],
        errors=[],sessions={'US':['2026-10-02']},nh_fallback={'results':reports},benchmarks={},profile={'US':{}},
        coverage={'listings':{'NASDAQ':{'state':'available','limit':None}},'scope_errors':[],'market_wide':True},collection_stats={})
    quality={'quality':[dict(market='US',symbol=s,last='2026-10-02',missing_sessions=[],invalid_sessions=[],non_sessions=[]) for s in ('FAIL','KEEP')]}
    return snapshot,quality


def saved(root,name,snapshot=None,quality=None,*,artifact=True):
    snapshot,quality=(snapshot,quality) if snapshot is not None else fixture()
    path=root/'runtime'/name;path.mkdir(parents=True)
    source=path/'source';source.mkdir()
    m.save(source/'snapshot.json',snapshot)
    from sector_observation import sha
    m.save(source/'manifest.json',{'snapshot.json':sha(source/'snapshot.json')})
    run={'market_source':str(source),'output':str(path),'state':'partial'}
    m.save(path/'run.json',run);m.save(path/'quality.json',quality)
    if artifact:m.save(path/'market-failures.json',m.inventory(snapshot,quality))
    hashes={p.name:sha(p) for p in path.iterdir() if p.is_file()}
    m.save(path/'manifest.json',{'output_hashes':hashes,'input_hashes':{str(p):sha(p) for p in source.iterdir()}})
    m.save(root/'runtime/latest.json',{'output':str(path),'state':'partial'})
    return path


class Inventory(unittest.TestCase):
    def test_only_attempted_unresolved_failures(self):
        snapshot,quality=fixture()
        issue=m.inventory(snapshot,quality)['issues'][0]
        self.assertTrue(issue['exclude_allowed']);self.assertEqual(issue['field'],'시가총액')
        self.assertIn('400',issue['nh_reason'])
        for changes in ({'nh_requested':False},{'state':'adopted_metadata'},{'scope_decision':'excluded'}):
            s=copy.deepcopy(snapshot);s['nh_fallback']['results'][0].update(changes)
            self.assertEqual(m.inventory(s,quality)['issues'],[])
        snapshot['universe'][0]['market_cap']=100
        self.assertEqual(m.inventory(snapshot,quality)['issues'],[])

    def test_price_gap_and_missing_identity(self):
        s,q=fixture();r=s['nh_fallback']['results'][0]
        r.update(toss_error_code='historical_price_gaps',reason='nh_identity_exchange_isin_mismatch',state='observed_not_adopted')
        self.assertEqual(m.inventory(s,q)['issues'],[])
        q['quality'][0]['missing_sessions']=['2026-10-01'];s['universe'][0]['isin_code']=None
        item=m.inventory(s,q)['issues'][0]
        self.assertFalse(item['exclude_allowed']);self.assertEqual(item['field'],'가격 이력')
        r['reason']='secret provider response <script>credentials</script>'
        self.assertNotIn('secret',m.inventory(s,q)['issues'][0]['nh_reason'])

    def test_same_ticker_different_identity_and_exchange(self):
        record=dict(market='US',symbol='FAIL',isin_code='USFAIL',exchange='NASDAQ',identity_bound=True,approval_id='id')
        p={'records':[dict(record,isin_code='OLD'),record],'lifecycle_records':[]}
        self.assertEqual(market_scope.match(record,p),record)
        self.assertIsNone(market_scope.match(dict(record,isin_code='NEW'),p))
        self.assertIsNone(market_scope.match(dict(record,exchange='NYSE'),p))
        self.assertIsNotNone(market_scope.match({'market':'US','symbol':'FAIL'},p))


class ReviewFixture(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.run=saved(self.root,'before')
        self.calls=[];self.views=[];self.services=[]
        self.path=self.root/'runtime/market-scope-user/records.json'
        self.addCleanup(self.join_all)

    def join_all(self):
        for s in self.services:
            if s.thread:s.thread.join(5)

    def worker(self,id):
        self.calls.append(id)
        s,q=fixture();s=market_scope.apply_snapshot(s,market_scope.load(user_registry=self.path))
        result=saved(self.root,'after-'+str(len(self.calls)),s,q)
        m.update_job(self.path,id,state='computed',result_run=str(result))

    def service(self,**kwargs):
        s=m.ExclusionReview(self.root,publish=kwargs.pop('publish',self.views.append),worker=kwargs.pop('worker',self.worker),**kwargs)
        self.services.append(s);return s

    def approve(self,s):
        value=s.status();return s.approve(value['review_id'],[value['issues'][0]['id']])

    def finish(self,s):
        s.thread.join(5);self.assertFalse(s.thread.is_alive());return s.status()


class Approval(ReviewFixture):
    def test_durable_consent_recalculation_and_next_collection(self):
        from sector_observation import sha
        original=sha(self.run/'source/snapshot.json')
        s=self.service();self.approve(s);done=self.finish(s)
        self.assertEqual(done['job']['state'],'completed');self.assertEqual(done['issues'],[])
        value,_=m.registry(self.path);self.assertEqual(len(value['records']),1)
        self.assertEqual(value['records'][0]['isin_code'],'USFAIL')
        self.assertEqual(sha(self.run/'source/snapshot.json'),original)
        policy=market_scope.load(user_registry=self.path)
        from test_hybrid_market import TossMarketPipelineTests
        f=TossMarketPipelineTests();f.setUp()
        try:
            f.config['universe']+=fixture()[0]['universe'][:1]
            with patch('market_scope.load',return_value=policy):result=f.run_collect()
            self.assertNotIn('FAIL',[c.args[1] for c in f.candles.call_args_list])
            self.assertEqual(len(result['universe']),2)
        finally:f.doCleanups()
        restarted=self.service();restarted.status();self.finish(restarted)
        self.assertEqual(len(self.calls),1)
        self.assertEqual(restarted.status()['issues'],[])

    def test_stale_unknown_duplicate_and_empty_requests_rejected(self):
        s=self.service();v=s.status();id=v['issues'][0]['id']
        for ids in ([],[id,id],['unknown'],[{}]):
            with self.assertRaises(ValueError):s.approve(v['review_id'],ids)
        saved(self.root,'newer')
        with self.assertRaises(m.ReviewConflict):s.approve(v['review_id'],[id])
        self.assertFalse(self.path.exists())

    def test_registry_change_invalidates_review_and_jobs_do_not_change_policy_hash(self):
        s=self.service();v=s.status();self.approve(s);self.finish(s)
        before=market_scope.load(user_registry=self.path)['sha256']
        m.update_job(self.path,m.registry(self.path)[0]['jobs'][-1]['id'],error='test')
        self.assertEqual(before,market_scope.load(user_registry=self.path)['sha256'])
        with self.assertRaises(m.ReviewConflict):s.approve(v['review_id'],[v['issues'][0]['id']])

    def test_runtime_busy_cannot_persist_consent(self):
        s=self.service();v=s.status()
        with instance_lock(self.root/'runtime'):
            with self.assertRaises(m.ReviewConflict):s.approve(v['review_id'],[v['issues'][0]['id']])
        self.assertFalse(self.path.exists())

    def test_duplicate_click_and_second_window_do_not_duplicate_approval(self):
        entered=threading.Event();release=threading.Event();self.addCleanup(release.set)
        def worker(id):
            entered.set();release.wait(5);self.worker(id)
        s=self.service(worker=worker);v=s.status();self.approve(s);self.assertTrue(entered.wait(1))
        for service in (s,self.service()):
            with self.assertRaises(m.ReviewConflict):service.approve(v['review_id'],[v['issues'][0]['id']])
        release.set();self.finish(s)
        self.assertEqual(len(m.registry(self.path)[0]['records']),1)

    def test_failure_retry_reuses_consent_and_publication_retry_reuses_result(self):
        def fail(id):raise RuntimeError('secret failure')
        s=self.service(worker=fail);self.approve(s);v=self.finish(s)
        self.assertTrue(v['job']['retry_available']);self.assertNotIn('secret',json.dumps(v))
        s.worker=self.worker;s.publish=lambda path:(_ for _ in ()).throw(ValueError('build failure'))
        s.retry(v['job']['id']);v=self.finish(s);self.assertEqual(v['job']['state'],'failed')
        s.publish=self.views.append;s.retry(v['job']['id']);v=self.finish(s)
        self.assertEqual(v['job']['state'],'completed',(self.path.parent/(v['job']['id']+'.log')).read_text(encoding='utf-8'));self.assertEqual(len(self.calls),1)
        self.assertEqual(len(m.registry(self.path)[0]['records']),1)

    def test_legacy_verified_snapshot_and_tamper_fail_closed(self):
        path=saved(self.root,'legacy',artifact=False);s=self.service()
        self.assertEqual(len(s.status()['issues']),1)
        (path/'source/snapshot.json').write_text('{}',encoding='utf-8')
        # A freshly submitted approval must re-verify the displayed evidence.
        v=s.status()
        with self.assertRaises(ValueError):s.approve(v['review_id'],[v['issues'][0]['id']])
        self.assertFalse(self.path.exists())

    def test_invalid_registry_rejected(self):
        m.save(self.path,{'schema':1,'records':[{'market':'US','symbol':'FAIL'}],'jobs':[]})
        with self.assertRaises(ValueError):self.service().status()

    def test_interrupted_approval_can_resume_after_restart(self):
        s=self.service()
        with patch.object(s,'_start'):self.approve(s)
        restarted=self.service();v=restarted.status()
        self.assertEqual(v['job']['state'],'interrupted');self.assertTrue(v['job']['retry_available'])
        restarted.retry(v['job']['id']);self.assertEqual(self.finish(restarted)['job']['state'],'completed')
        self.assertEqual(len(m.registry(self.path)[0]['records']),1)

    def test_remaining_universe_cannot_be_excluded_after_failed_apply(self):
        snap,q=fixture();snap['universe'][1]['market_cap']=None
        snap['nh_fallback']['results'].append(dict(snap['nh_fallback']['results'][0],symbol='KEEP'))
        saved(self.root,'two-failures',snap,q)
        s=self.service(worker=lambda id:(_ for _ in ()).throw(ValueError('test failed')))
        self.approve(s);v=self.finish(s)
        self.assertEqual(len(v['issues']),1)
        with self.assertRaisesRegex(ValueError,'empty_market_scope'):
            s.approve(v['review_id'],[v['issues'][0]['id']])
        self.assertEqual(len(m.registry(self.path)[0]['records']),1)

    def test_exchange_change_keeps_distinct_approved_identities(self):
        s=self.service();self.approve(s);self.finish(s)
        value,_=m.registry(self.path);old=value['records'][0]
        value['records'].append(dict(old,exchange='NYSE'))
        m.save(self.path,value);policy=market_scope.load(user_registry=self.path)
        for exchange in ('NASDAQ','NYSE'):
            self.assertEqual(market_scope.match(dict(old,exchange=exchange),policy)['exchange'],exchange)

    def test_child_accepts_completed_calculation_with_other_unapproved_gaps(self):
        s=self.service()
        with patch.object(s,'_start'):self.approve(s)
        job=m.registry(self.path)[0]['jobs'][-1]['id']
        import daily_market,repair_market_scope
        policy=market_scope.load(user_registry=self.path)
        def run(config,source):
            derived=m.load(source/'snapshot.json');self.assertEqual([r['symbol'] for r in derived['universe']],['KEEP'])
            result=saved(self.root,'partial-after',derived,fixture()[1])
            return {'state':'failed','stages':{'report':'completed'},'output':str(result)}
        m.save(self.root/'config.json',{})
        with patch.object(m,'ROOT',self.root),patch.object(m,'REGISTRY',self.path),patch.object(repair_market_scope,'ROOT',self.root),\
             patch('market_scope.load',return_value=policy),patch.object(repair_market_scope,'audit',return_value={'state':'partial'}),patch.object(daily_market,'run',side_effect=run):
            m.apply_job(job)
        self.assertEqual(m.registry(self.path)[0]['jobs'][-1]['state'],'computed')
        self.assertEqual(Path(m.load(self.root/'runtime/latest.json')['output']).name,'partial-after')

    def test_child_offline_derivation_and_failure_restores_latest(self):
        s=self.service()
        with patch.object(s,'_start'):self.approve(s)
        job=m.registry(self.path)[0]['jobs'][-1]['id'];before=m.load(self.root/'runtime/latest.json')
        import daily_market,repair_market_scope
        policy=market_scope.load(user_registry=self.path)
        def run(config,source):
            with socket.socket() as sock, self.assertRaisesRegex(AssertionError,'offline_network_forbidden'):
                sock.connect(('127.0.0.1',1))
            derived=m.load(source/'snapshot.json')
            self.assertEqual([r['symbol'] for r in derived['universe']],['KEEP'])
            m.save(self.root/'runtime/latest.json',{'output':'failed'})
            return {'state':'failed'}
        m.save(self.root/'config.json',{})
        with patch.object(m,'ROOT',self.root),patch.object(m,'REGISTRY',self.path),patch.object(repair_market_scope,'ROOT',self.root),\
             patch('market_scope.load',return_value=policy),patch.object(repair_market_scope,'audit',return_value={'state':'succeeded'}),patch.object(daily_market,'run',side_effect=run):
            with self.assertRaisesRegex(ValueError,'exclusion_calculation_failed'):m.apply_job(job)
        self.assertEqual(m.load(self.root/'runtime/latest.json'),before)
        self.assertEqual(m.registry(self.path)[0]['jobs'][-1]['state'],'failed')
        self.assertEqual(len(m.registry(self.path)[0]['records']),1)


class HTTP(ReviewFixture):
    def setUp(self):
        super().setUp()
        from dashboard import handler_for,ThreadingHTTPServer
        self.review=self.service()
        assets=Path(__file__).resolve().parents[1]/'dashboard'
        self.server=ThreadingHTTPServer(('127.0.0.1',0),handler_for({}, {}, assets,'token',self.root/'workspace.sqlite3',
             exclusion_review=self.review,analysis_capabilities={'collaboration':'mcp-token'},offline=True))
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.addCleanup(self.close)
        self.headers={'Content-Type':'application/json','Origin':'http://127.0.0.1:'+str(self.server.server_port),
                      'X-Workbench-Token':'token','Sec-Fetch-Site':'same-origin'}
        _,_,headers=self.request('GET','/health')
        self.headers['Cookie']=next(v.split(';')[0] for k,v in headers if k.lower()=='set-cookie' and v.startswith('workbench_scope_confirm='))

    def close(self):self.server.shutdown();self.server.server_close();self.thread.join(2)

    def request(self,method,path,body=None,headers=None):
        c=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=5)
        c.request(method,path,json.dumps(body) if body is not None else None,headers=self.headers if headers is None else headers)
        r=c.getresponse();raw=r.read();result=(r.status,raw,r.getheaders());c.close();return result

    def body(self):
        _,raw,_=self.request('GET','/api/market-exclusions');v=json.loads(raw)
        return dict(action='approve',review_id=v['review_id'],issue_ids=[v['issues'][0]['id']])

    def test_browser_consent_roundtrip_and_assets(self):
        body=self.body();code,raw,_=self.request('POST','/api/market-exclusions',body)
        self.assertEqual(code,202);self.finish(self.review)
        code,raw,_=self.request('GET','/api/market-exclusions');v=json.loads(raw)
        self.assertEqual(code,200);self.assertEqual(v['issues'],[]);self.assertEqual(v['job']['state'],'completed')
        self.assertNotIn(str(self.root),raw.decode());self.assertEqual(len(self.views),1)
        self.assertEqual(self.request('POST','/api/market-exclusions',body)[0],409)
        for asset in ('market-exclusions.js','market-exclusions.css'):
            self.assertEqual(self.request('GET','/'+asset)[0],200)

    def test_browser_cookie_origin_token_and_schema_required(self):
        body=self.body()
        for key in ('Origin','X-Workbench-Token','Cookie','Sec-Fetch-Site'):
            headers=dict(self.headers);headers.pop(key)
            self.assertEqual(self.request('POST','/api/market-exclusions',body,headers)[0],403,key)
        for changes in ({'X-Workbench-Token':'mcp-token'},{'Origin':'https://external.test'},{'Sec-Fetch-Site':'cross-site'}):
            self.assertEqual(self.request('POST','/api/market-exclusions',body,{**self.headers,**changes})[0],403)
        for bad in ({**body,'symbol':'injected'},{'action':'retry','job_id':[]},[]):
            self.assertEqual(self.request('POST','/api/market-exclusions',bad)[0],400)
        self.assertEqual(self.request('GET','/api/market-exclusions?path=private')[0],400)
        self.assertFalse(self.path.exists())


if __name__=='__main__':unittest.main()
