"""Saved-input -> Agent draft -> 108 results -> browser adoption/fills, fully isolated."""
from copy import deepcopy
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import test_collaboration as fixtures
from test_trade_cycle import plan, COMBO, fill
from analysis import Forbidden
from drawings.store import Conflict
from cycles.ledger import TradeCycleStore
from cycles.agent import DraftJobs, cli_command, generate, SCHEMA
from cycles import agent as trade_cycle_agent


class CycleApiTests(unittest.TestCase):
    setUp=fixtures.CollaborationTests.setUp
    tearDown=fixtures.CollaborationTests.tearDown

    def rpc(self,op,args,role='browser'):
        return self.s.dispatch('cycle_'+op,args,self.origin,principal=role,session='test-browser' if role=='browser' else None)

    def prepare(self):
        self.accounts['accounts'][0].update(alias='synthetic',cash={'USD':'1000'},holdings_complete=True)
        self.accounts['accounts'][0]['positions']=[]
        args={'stock_key':'US:SYNTH','budget':'1000','account_alias':'synthetic'}
        from cycles import context,source_for,empty_plan
        ctx=context(self.s,args['stock_key'],args['account_alias'])
        source,_=source_for(self.s,ctx,self.origin)
        return TradeCycleStore(self.store.path).save_draft(args['stock_key'],empty_plan(ctx,'1000'),source=source)

    def test_cash_currency_fallback_no_duplicate_cma_or_other_account(self):
        a=self.accounts['accounts'][0]
        a.update(cash={'USD':'1000'},observed_deposits={'US':{'fc_dca':'1000','krw_dca':'1400000'}})
        a['positions'][0]['average_cost']='90'
        c=self.rpc('context',{'stock_key':'US:SYNTH'})
        self.assertEqual(c['budget'],'1000')
        self.assertEqual(c['initial_holding'],{'quantity':10,'average_price':'90'})
        a['cash']={}
        self.assertEqual(self.rpc('context',{'stock_key':'US:SYNTH'})['budget'],'1000')
        del a['observed_deposits']['US']['fc_dca']
        self.assertIsNone(self.rpc('context',{'stock_key':'US:SYNTH'})['budget'])

    def test_evidence_draft_calculate_adopt_fill_resume_preserves_original(self):
        c=self.prepare(); cid=c['cycle_id']
        packet=self.rpc('agent_input',{'cycle_id':cid},role='collaboration')
        self.assertEqual(packet['workspace']['state']['drawings'],[])
        p=plan()
        c=self.rpc('save_draft',{'cycle_id':cid,'expected_revision':c['revision'],'plan':p},role='collaboration')['cycle']
        self.assertIsNone(c['adopted'])
        self.assertEqual(self.rpc('calculate',{'plan':p})['evaluated_result_count'],108)
        c=self.rpc('approve',{'cycle_id':cid,'draft_version':c['draft_version'],'combination_id':COMBO,'expected_revision':c['revision']})['cycle']
        original=deepcopy(c['original_plan'])
        c=self.rpc('fill',{'cycle_id':cid,'expected_revision':c['revision'],'fill':fill('buy',20,10,1)})['cycle']
        self.assertEqual(c['state']['quantity'],20)
        self.assertEqual(self.rpc('read',{'cycle_id':cid})['cycle']['original_plan'],original)
        self.assertEqual(len(self.rpc('list',{})['cycles']),1)

    def test_mcp_cannot_adopt_record_or_run_and_cannot_change_account_inputs(self):
        c=self.prepare()
        for op in ('approve','fill','request_draft'):
            with self.assertRaises(Forbidden):self.rpc(op,{},role='collaboration')
        p=plan();p['budget']='2000'
        with self.assertRaisesRegex(ValueError,'agent_cannot_change_account_inputs'):
            self.rpc('save_draft',{'cycle_id':c['cycle_id'],'expected_revision':c['revision'],'plan':p},role='collaboration')

    def test_quote_symbol_and_unknown_holding_are_not_zero(self):
        a=self.accounts['accounts'][0]
        a.update(cash={'USD':'1000'},holdings_complete=True)
        p=a['positions'][0]
        p.update(symbol='BROKER-SYNTH',quote_symbol='SYNTH',average_cost='90')
        self.assertEqual(self.rpc('context',{'stock_key':'US:SYNTH'})['initial_holding']['quantity'],10)
        p['data_unavailable_reason']='unverified_identity'
        self.assertIsNone(self.rpc('context',{'stock_key':'US:SYNTH'})['initial_holding']['quantity'])
        a.update(positions=[],holdings_complete=False)
        self.assertIsNone(self.rpc('context',{'stock_key':'US:SYNTH'})['initial_holding']['quantity'])

    def test_missing_account_list_preserves_unknown_until_explicit_user_input(self):
        self.accounts.update(accounts=[],query_state='failed',stale=True)
        ctx=self.rpc('context',{'stock_key':'US:SYNTH'})
        self.assertEqual(ctx['initial_holding'],{'quantity':None,'average_price':None})
        self.assertEqual(ctx['query_state'],'failed');self.assertTrue(ctx['stale'])
        from cycles.agent import manager_for
        with patch.object(manager_for(self.store.path,self.root),'start',return_value={'state':'queued'}):
            c=self.rpc('request_draft',{'stock_key':'US:SYNTH','budget':'1000'})['cycle']
        p=plan();p.update(account_alias=None,initial_holding=ctx['initial_holding'])
        c=self.rpc('save_draft',{'cycle_id':c['cycle_id'],'expected_revision':c['revision'],'plan':p},role='collaboration')['cycle']
        result=self.rpc('calculate',{'cycle_id':c['cycle_id'],'plan':p})
        self.assertIn('initial_holding.quantity',result['needs_input'])
        self.assertEqual(result['evaluated_result_count'],0)
        p['initial_holding']={'quantity':0,'average_price':'0'}
        c=self.rpc('save_draft',{'cycle_id':c['cycle_id'],'expected_revision':c['revision'],'plan':p})['cycle']
        self.assertEqual(c['draft']['plan']['initial_holding'],p['initial_holding'])
        self.assertEqual(self.rpc('calculate',{'cycle_id':c['cycle_id'],'plan':p})['evaluated_result_count'],108)

    def test_complete_empty_account_is_still_confirmed_zero(self):
        self.prepare()
        ctx=self.rpc('context',{'stock_key':'US:SYNTH','account_alias':'synthetic'})
        self.assertEqual(ctx['initial_holding'],{'quantity':0,'average_price':'0'})

    def test_general_creation_skips_test_cases_and_explicit_source_remains_test(self):
        # The fixture initially has only an explicitly marked test analysis.
        self.assertIsNone(self.rpc('context',{'stock_key':'US:SYNTH'})['analysis_key'])
        production=self.prepare()['draft']['source']
        self.assertFalse(production['test_record'])
        test=self.s.dispatch('create',{'stock_key':'US:SYNTH','test_record':True},self.origin)['case']
        ctx=self.rpc('context',{'stock_key':'US:SYNTH','account_alias':'synthetic'})
        self.assertEqual(ctx['analysis_key'],production['analysis_key'])
        from cycles import source_for
        explicit,_=source_for(self.s,{**ctx,'analysis_key':test['id']},self.origin)
        self.assertTrue(explicit['test_record'])
        from cycles.agent import manager_for
        with patch.object(manager_for(self.store.path,self.root),'start',return_value={'state':'queued'}):
            c=self.rpc('request_draft',{'stock_key':'US:SYNTH','account_alias':'synthetic','budget':'1000'})['cycle']
        self.assertEqual(c['draft']['source']['analysis_key'],production['analysis_key'])
        self.assertFalse(c['draft']['source']['test_record'])

    def test_missing_stable_identity_can_be_drafted_but_cannot_alert(self):
        self.stock.pop('isin_code')
        c=self.prepare();p=plan()
        c=self.rpc('save_draft',{'cycle_id':c['cycle_id'],'expected_revision':c['revision'],'plan':p},role='collaboration')['cycle']
        c=self.rpc('approve',{'cycle_id':c['cycle_id'],'draft_version':c['draft_version'],
                             'combination_id':COMBO,'expected_revision':c['revision']})['cycle']
        self.assertIsNone(c['adopted']['source']['identity']['stable_id'])
        now=datetime.now(timezone.utc)
        self.accounts.update(as_of=now.isoformat(),query_state='received')
        from cycles.alerts import TradeCycleAlertStore
        result=TradeCycleAlertStore(self.store.path).update([c],self.accounts,
            {'US:SYNTH':{**self.stock,'isin_code':'DIFFERENT_INSTRUMENT'}},
            {'US':self.bars[-1]['date']},now,{'US':[b['date'] for b in self.bars]})
        self.assertEqual(result['states'][0]['status'],'deferred')
        self.assertEqual(result['states'][0]['issue'],'cycle_identity_unconfirmed')
        self.assertEqual(result['events'],[])
        self.assertEqual(self.rpc('read',{'cycle_id':c['cycle_id']})['cycle'],c)

    def test_existing_test_cycle_refresh_preserves_explicit_test_source(self):
        self.prepare()
        test=self.s.dispatch('create',{'stock_key':'US:SYNTH','test_record':True},self.origin)['case']
        from cycles import source_for
        ctx=self.rpc('context',{'stock_key':'US:SYNTH','account_alias':'synthetic'})
        source,_=source_for(self.s,{**ctx,'analysis_key':test['id']},self.origin)
        c=TradeCycleStore(self.store.path).save_draft('US:SYNTH',plan(),source=source)
        from cycles.agent import manager_for
        with patch.object(manager_for(self.store.path,self.root),'start',return_value={'state':'queued'}):
            refreshed=self.rpc('request_draft',{'stock_key':'US:SYNTH','account_alias':'synthetic',
                'cycle_id':c['cycle_id'],'expected_revision':c['revision']})['cycle']
        self.assertTrue(refreshed['draft']['source']['test_record'])
        self.assertEqual(refreshed['draft']['source']['analysis_key'],test['id'])

    def test_auto_job_saves_draft_and_stale_result_cannot_overwrite_user_edit(self):
        c=self.prepare(); store=TradeCycleStore(self.store.path)
        packet=self.rpc('agent_input',{'cycle_id':c['cycle_id']},role='collaboration')
        jobs=DraftJobs(self.store.path,self.root,runner=lambda packet,path:plan())
        jobs.start(c,packet)
        thread=jobs.threads.get(c['cycle_id'])
        if thread:thread.join(10)
        self.assertEqual(jobs.status(c['cycle_id'])['state'],'completed')
        self.assertIsNone(store.read(c['cycle_id'])['adopted'])
        c=store.read(c['cycle_id']); entered=threading.Event(); proceed=threading.Event()
        def slow(packet,path):entered.set();proceed.wait(10);return plan()
        jobs.runner=slow;jobs.start(c,packet);self.assertTrue(entered.wait(5))
        changed=plan();changed['title']='사용자 새 초안'
        store.save_draft(c['stock_key'],changed,c['cycle_id'],c['revision'],c['draft']['source'])
        thread=jobs.threads[c['cycle_id']];proceed.set();thread.join(10)
        self.assertEqual(jobs.status(c['cycle_id'])['error'],'draft_changed_during_generation')
        self.assertEqual(store.read(c['cycle_id'])['draft']['plan']['title'],'사용자 새 초안')

    def test_structured_generator_keeps_account_values_and_removes_null_step(self):
        c=self.prepare();packet=self.rpc('agent_input',{'cycle_id':c['cycle_id']})
        raw={k:deepcopy(plan().get(k)) for k in SCHEMA['properties']}
        raw.update(rationale='가상 자료',unresolved=[],sources=[])
        for scenario in raw['scenarios'].values():
            for e in scenario['events']:
                e.setdefault('step',None);e.setdefault('price',None)
        def fake(command,**kwargs):
            out=Path(command[command.index('--output-last-message')+1]);out.write_text(__import__('json').dumps(raw),encoding='utf-8')
            self.assertIn('--sandbox',command);self.assertEqual(command[command.index('--sandbox')+1],'read-only')
            self.assertIn('features.shell_tool=false',command)
            self.assertNotIn('--dangerously-bypass-approvals-and-sandbox',command)
            return type('R',(),{'returncode':0,'stderr':''})()
        with patch('cycles.agent.subprocess.run',side_effect=fake),patch('cycles.agent.shutil.which',return_value='codex.exe'):
            p=generate(packet,self.root)
        self.assertEqual(p['budget'],'1000')
        self.assertNotIn('step',p['scenarios']['bearish']['events'][-1])
        self.assertNotIn('price',p['scenarios']['bullish']['events'][0])

    def test_reentry_is_a_new_cycle_with_previous_record_preserved(self):
        c=self.prepare();store=TradeCycleStore(self.store.path)
        c=store.save_draft(c['stock_key'],plan(),c['cycle_id'],c['revision'],c['draft']['source'])
        c=store.adopt(c['cycle_id'],c['draft_version'],COMBO,c['revision'],principal='browser')
        c=store.record_fill(c['cycle_id'],fill('buy',20,10,1),c['revision'],principal='browser')
        stop=fill('stop',20,9,None);stop.pop('step',None)
        c=store.record_fill(c['cycle_id'],stop,c['revision'],principal='browser')
        from cycles.agent import manager_for
        with patch.object(manager_for(self.s.store.path, self.s.root),'start',return_value={'state':'queued'}):
            new=self.rpc('request_draft',{'stock_key':'US:SYNTH','account_alias':'synthetic',
                'budget':'500','reentry_from':c['cycle_id']})['cycle']
        self.assertNotEqual(new['cycle_id'],c['cycle_id'])
        self.assertEqual(new['draft']['plan']['initial_holding'],{'quantity':0,'average_price':'0'})
        self.assertEqual(new['draft']['source']['reentry_from'],c['cycle_id'])
        self.assertEqual(store.read(c['cycle_id']),c)

    def test_two_local_servers_keep_separate_confirmation_cookies(self):
        import http.client,json
        from web.server import handler_for,ThreadingHTTPServer
        c=self.prepare();store=TradeCycleStore(self.store.path)
        c=store.save_draft(c['stock_key'],plan(),c['cycle_id'],c['revision'],c['draft']['source'])
        servers=[];cookies=[]
        try:
            for _ in range(2):
                h=handler_for(self.data,{'US:SYNTH':self.stock},ROOT/'dashboard','fixture-token',self.store.path,
                              analysis_dependencies={'histories':self.hist,'financials':self.fin})
                server=ThreadingHTTPServer(('127.0.0.1',0),h)
                thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
                servers.append((server,thread))
                conn=http.client.HTTPConnection('127.0.0.1',server.server_port)
                conn.request('GET','/health',headers={'Sec-Fetch-Site':'same-origin'})
                response=conn.getresponse()
                cookies.append(next(v.split(';')[0] for k,v in response.getheaders()
                                    if k.lower()=='set-cookie' and v.startswith('workbench_confirm_')))
                response.read();conn.close()
            self.assertNotEqual(cookies[0].split('=')[0],cookies[1].split('=')[0])
            server=servers[0][0];origin='http://127.0.0.1:'+str(server.server_port)
            def approve(cookie):
                conn=http.client.HTTPConnection('127.0.0.1',server.server_port)
                conn.request('POST','/api/analysis-rpc',json.dumps({'op':'cycle_approve','arguments':{
                    'cycle_id':c['cycle_id'],'draft_version':c['draft_version'],
                    'combination_id':COMBO,'expected_revision':c['revision']}}),headers={
                    'Content-Type':'application/json','Origin':origin,'Sec-Fetch-Site':'same-origin',
                    'X-Workbench-Token':'fixture-token','Cookie':cookie})
                response=conn.getresponse();response.read();status=response.status;conn.close();return status
            self.assertEqual(approve(cookies[1]+'; workbench_confirm=other-server'),403)
            self.assertEqual(approve('; '.join(cookies)+'; workbench_confirm=other-server'),200)
        finally:
            for server,thread in servers:
                server.shutdown();server.server_close();thread.join()


if __name__=='__main__':unittest.main()
