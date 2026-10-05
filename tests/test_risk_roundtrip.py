"""Real A DB -> fresh B reader -> calculation -> A save -> fresh screen query.

Only TemporaryDirectory and synthetic dependencies; provider calls fail tests.
"""
from copy import deepcopy
import http.client
import json
import threading
import unittest
from unittest.mock import patch

import test_collaboration as collaboration_fixture
from test_collaboration import FIXTURE, ROOT
from test_risk_adapter import integration_fixture
from analysis_service import AnalysisService, digest
from risk_service import RiskService, saved_account_input
from chart_workspace import Conflict, defaults
from analysis_collaboration import Forbidden
from risk_values import fingerprint


def account_fixture():
    return {'schema_version': 1, 'source': 'synthetic', 'as_of': '2026-10-05T06:00:00+09:00',
        'max_age_hours': '24', 'base_currency': 'KRW', 'accounts_complete': True,
        'fx_to_base': {'KRW': '1', 'USD': '1000'}, 'fx_observation': {'as_of': '2026-10-05T06:00:00+09:00'},
        'accounts': [{'alias': 'synthetic-us', 'holdings_complete': True, 'cash_complete': True,
            'liabilities_complete': True, 'cash_reconciliation': {'cash_excludes_cash_equivalents': True},
            'cash': {'KRW': '10000', 'USD': '100'}, 'liabilities': {}, 'positions': [
                {'symbol': 'KR-SYNTH', 'market': 'KR', 'currency': 'KRW', 'quantity': '10', 'price': '1000', 'price_basis': 'adjusted', 'isin_code': 'KR-SYNTHETIC'},
                {'symbol': 'SYNTH', 'market': 'US', 'currency': 'USD', 'quantity': '10', 'price': '100', 'price_basis': 'adjusted', 'isin_code': 'US0000000001'},
                {'symbol': 'CMA', 'market': 'KR', 'currency': 'KRW', 'quantity': '1', 'instrument_class_hint': 'CMA_note_from_broker_label', 'observed_evaluation_krw': '5000'}]}]}


class Roundtrip(unittest.TestCase):
    def setUp(self):
        self.f = collaboration_fixture.CollaborationTests(); self.f.setUp()
        self.current = self.f.discussion()
        self.p = self.f.plan(); self.f.adopt(self.p)
        self.f.data['accounts'].update(scenario_input=saved_account_input(account_fixture()), snapshot_sha256='synthetic-file-hash')
        # New service instance: only stored records, forbidden to capture a new snapshot.
        self.a = AnalysisService(self.f.store, self.f.hist, self.f.fin,
            lambda: self.fail('new snapshot/provider access forbidden'), self.f.root)
        self.clock = lambda: '2026-10-05T07:00:00+09:00'
        self.b = RiskService(self.a, self.f.view, clock=self.clock, allow_test_records=True)

    def tearDown(self): self.f.tearDown()

    def request(self, record=None):
        record = record or self.p
        assumptions = integration_fixture()[2]
        for observation in assumptions['path_observations'].values(): observation['price_basis'] = 'adjusted'
        assumptions['unplanned_prices'] = {'synthetic-us/KR/KR-SYNTH': '800'}
        return {'selections': [{'analysis_key': self.f.cid, 'record_id': record['id'],
            'plan_version': record['plan_version'], 'plan_sha256': record['plan_sha256'],
            'snapshot_sha256': record['snapshot_sha256'], 'mode': 'adopted', 'scenario_id': 's1'}],
            'assumptions': assumptions, 'account_alias': None}

    def calculate(self, args=None): return self.b.calculate(args or self.request(), self.f.origin, 'fixture-browser')
    def save(self, value):
        return self.b.save({'ticket': value['ticket'], 'analysis_key': self.f.cid,
            'title': '합성 시나리오 위험 검토', 'body': '완료봉 및 체결 시각은 합성 가정. 실증 아님.'}, self.f.origin, 'fixture-browser')

    def test_exact_version_hash_full_roundtrip_and_fresh_overview(self):
        p2 = self.f.plan(plan_id=self.p['plan_id'], expected_plan_version=1)
        result = self.calculate()
        self.assertEqual(result['result']['baseline']['total'], '1125000')
        self.assertEqual(result['result']['no_response']['total'], '1013000')
        self.assertEqual(result['result']['response']['total'], '1062500.0')
        self.assertEqual(result['result']['plan_refs'][0]['version'], 1)
        saved = self.save(result)
        self.assertTrue(saved['verified'])
        record = saved['record']
        self.assertEqual(record['principal'], 'risk')
        frozen = record['calculation']['assumptions']
        self.assertEqual(record['calculation']['result']['snapshot_hash'], fingerprint(frozen['account_snapshot']))
        self.assertEqual(record['calculation']['result']['scenario_hash'], fingerprint(frozen['scenario']))
        self.assertEqual(frozen['external_inputs'][0]['sha256'], digest(saved_account_input(account_fixture())))
        self.assertEqual(self.f.read()['current'], self.current)
        self.assertEqual(self.f.read()['active_plans'][0]['record']['id'], self.p['id'])
        fresh = RiskService(AnalysisService(self.f.store,self.f.hist,self.f.fin,self.a.view,self.f.root),
                            self.f.view,clock=self.clock,allow_test_records=True)
        screen = fresh.overview(self.f.origin)
        self.assertEqual(screen['reviews'][0]['record'], record)
        self.assertEqual({p['record']['id'] for p in screen['plans']}, {self.p['id'], p2['id']})
        self.assertTrue((self.f.root/record['markdown_path']).is_file())
        with self.assertRaises(Conflict): self.save(result)

    def test_hash_forgery_wrong_role_and_test_records_blocked(self):
        args = self.request(); args['selections'][0]['plan_sha256'] = 'forged'
        with self.assertRaises(Conflict): self.calculate(args)
        with self.assertRaises(Forbidden): self.b.dispatch('risk_overview', {}, self.f.origin, principal='collaboration')
        operational = RiskService(self.a,self.f.view,clock=self.clock)
        self.assertEqual(operational.overview(self.f.origin)['plans'], [])
        with self.assertRaisesRegex(ValueError,'test_record'): operational.calculate(self.request(),self.f.origin,'s')

    def test_changed_revision_or_account_requires_recalculation(self):
        result = self.calculate(); self.f.discussion(body='concurrent change')
        with self.assertRaises(Conflict): self.save(result)
        result = self.calculate()
        self.f.data['accounts']['scenario_input']['accounts'][0]['positions'][1]['quantity'] = '9'
        with self.assertRaises(Conflict): self.save(result)

    def test_replaced_adoption_never_saves_stale_result(self):
        result = self.calculate(); p2 = self.f.plan(plan_id=self.p['plan_id'],expected_plan_version=1); self.f.adopt(p2)
        with self.assertRaises(ValueError): self.save(result)
        with self.assertRaises(ValueError): self.calculate()

    def test_partial_missing_and_scope_remain_honest(self):
        args = self.request(); args['assumptions']['fills'] = {}
        result = self.calculate(args)['result']
        self.assertEqual(result['baseline']['total'], '1125000')
        self.assertEqual(result['response']['state'], 'deferred')
        self.f.data['accounts']['scenario_input']['accounts'][0]['cash_complete'] = False
        result = self.calculate()['result']
        self.assertIsNone(result['baseline']['total'])
        self.assertTrue(result['baseline']['missing'])
        del self.f.data['accounts']['scenario_input']['accounts'][0]['positions'][1]['isin_code']
        self.assertTrue(self.calculate()['projections'][0]['blockers'])

    def test_units_allocation_is_not_implicitly_initial(self):
        args = self.request(); del args['assumptions']['allocation_basis']
        result = self.calculate(args)
        self.assertIn('allocation_lifecycle_assumption_required',result['projections'][0]['response_blockers'])
        self.assertEqual(result['result']['response']['state'],'deferred')

    def test_frozen_drawing_flows_from_real_db_without_new_chart_query(self):
        state=defaults();state['drawings']=[dict(id='threshold',kind='horizontal',points=[dict(time='2026-09-18',price=90)],color='#218775',width=2,extend=True)]
        self.f.store.shared('US:SYNTH',revision=0,state=state)
        self.f.sid=self.f.s.capture(self.f.s.case(self.f.cid))['id']
        plan=deepcopy(FIXTURE['plan']);plan['conditions'][0].update(price=None,drawing_ref=dict(drawing_id='threshold',workspace_revision=1,timeframe='1d',price_basis='adjusted',geometry_version='drawing-tools-linear-v1',line='line'))
        p=self.f.plan(plan=plan);self.f.adopt(p)
        state['drawings'][0]['points'][0]['price']=10
        self.f.store.shared('US:SYNTH',revision=1,state=state)
        result=self.calculate(self.request(p))
        self.assertEqual(result['result']['response']['total'],'1062500.0')
        self.assertEqual(result['projections'][0]['conditions'][0]['drawing']['workspace']['state']['drawings'][0]['points'][0]['price'],90)

    def test_multiple_plans_save_all_exact_refs_and_versions_cannot_mix(self):
        plan = deepcopy(FIXTURE['plan']); plan['scenarios'][0]['response'] = 'none'
        first = self.f.plan(plan=plan); self.f.adopt(first)
        second = self.f.plan(plan=plan); self.f.adopt(second)
        args = self.request(first); args['selections'] += self.request(second)['selections']
        with self.assertRaisesRegex(ValueError,'select_one_scenario'): self.calculate(args)
        args['assumptions']['combine_allocations'] = True
        result = self.calculate(args)
        saved = self.save(result)['record']
        self.assertEqual(saved['plan_record_ids'],[first['id'],second['id']])
        next_draft = self.f.plan(plan_id=first['plan_id'],expected_plan_version=1)
        args = self.request(first); extra=self.request(next_draft)['selections'][0];extra['mode']='hypothetical'
        args['selections'].append(extra)
        with self.assertRaisesRegex(ValueError,'select_one_version'): self.calculate(args)

    def test_scoped_account_and_reusable_assumptions_remain_frozen(self):
        args=self.request();args['account_alias']='synthetic-us'
        result=self.calculate(args); saved=self.save(result)['record']
        self.assertEqual(self.b.overview(self.f.origin,'synthetic-us')['reviews'][0]['record'],saved)
        self.assertEqual(self.b.overview(self.f.origin)['reviews'],[])
        self.assertEqual(len(saved['calculation']['result']['code_sha256']),64)
        self.assertEqual(saved['calculation']['assumptions']['selected_inputs'],args['selections'])

    def test_total_scenario_cost_applied_once_and_remaining_not_initial(self):
        plan = deepcopy(FIXTURE['plan']); plan['scenarios'][0]['cost'] = '2'
        plan['allocation']['basis'] = 'initial'
        p = self.f.plan(plan=plan); self.f.adopt(p)
        args = self.request(p); args['assumptions']['cost_policy'] = 'record_total'
        result = self.calculate(args)['result']
        self.assertEqual(result['response']['total'], '1060300.0')
        self.assertEqual(len([x for x in result['response']['ledger'] if x['state']=='scenario_total_cost']), 1)
        plan['allocation']['basis'] = 'current_remaining'; p = self.f.plan(plan=plan); self.f.adopt(p)
        self.assertEqual(self.calculate(self.request(p))['result']['response']['state'], 'deferred')

    def test_dashboard_http_uses_same_store_and_blocks_result_injection(self):
        from dashboard import handler_for, ThreadingHTTPServer
        handler = handler_for(self.f.data, {'US:SYNTH':self.f.stock}, ROOT/'dashboard', 'browser-token', self.f.store.path,
            analysis_dependencies={'histories':self.f.hist,'financials':self.f.fin},
            risk_options={'clock':self.clock,'allow_test_records':True})
        server=ThreadingHTTPServer(('127.0.0.1',0),handler)
        worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
        conn=http.client.HTTPConnection('127.0.0.1',server.server_port)
        try:
            conn.request('GET','/health',headers={'Sec-Fetch-Site':'same-origin'})
            r=conn.getresponse();cookie=r.getheader('Set-Cookie').split(';')[0];r.read()
            headers={'Origin':'http://127.0.0.1:'+str(server.server_port),'X-Workbench-Token':'browser-token',
                     'Content-Type':'application/json','Sec-Fetch-Site':'same-origin','Cookie':cookie}
            def call(op,args):
                conn.request('POST','/api/analysis-rpc',json.dumps({'op':op,'arguments':args}),headers)
                r=conn.getresponse();return r.status,json.loads(r.read())
            status,value=call('risk_calculate',self.request());self.assertEqual(status,200,value)
            args={'ticket':value['ticket'],'analysis_key':self.f.cid,'title':'HTTP 검토','body':'fixture'}
            self.assertEqual(call('risk_save',{**args,'result':{'total':0}})[0],400)
            status,saved=call('risk_save',args);self.assertEqual(status,200,saved)
            status,screen=call('risk_overview',{});self.assertEqual(status,200,screen)
            self.assertEqual(screen['reviews'][0]['record']['id'],saved['record']['id'])
            for path in ('/','/risk-workbench.js','/risk-workbench.css','/api/home'):
                conn.request('GET',path);r=conn.getresponse();self.assertEqual(r.status,200,path);r.read()
        finally:conn.close();server.shutdown();server.server_close();worker.join()


if __name__ == '__main__': unittest.main()
