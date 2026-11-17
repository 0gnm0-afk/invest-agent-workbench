"""Isolated saved-record → drawing plan → confirmed watch integration."""
from contextlib import closing
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from analysis import Forbidden
from analysis.collaboration import VERSION
from analysis.service import AnalysisService
from drawings.store import WorkspaceStore, defaults, Conflict


class ScenarioFlowTests(unittest.TestCase):
    def setUp(self):
        # Keep the M07 fixture self-contained: C8 copies only registered test files.
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.store = WorkspaceStore(self.root / 'chart.sqlite3')
        self.bars = [dict(date=d, open=100, high=112, low=90, close=105, volume=1000)
                     for d in ('2026-09-18', '2026-09-21', '2026-09-22', '2026-09-23')]
        self.stock = dict(symbol='SYNTH', market='US', name='Synthetic only', currency='USD', bars=self.bars,
                          isin_code='US0000000001', exchange='NASDAQ', price_basis='adjusted')
        self.accounts = {'as_of':'2026-10-01', 'accounts':[{'alias':'synthetic-us','positions':[
            dict(symbol='SYNTH', market='US', quantity='10', currency='USD')]}], 'risk':{}}
        self.data = {'accounts':self.accounts, 'market':{'state':'partial'}}
        self.hist = SimpleNamespace(saved=SimpleNamespace(stocks={}, markets={}),
                                    view=lambda s,f:{**s, 'timeframe':f},
                                    ensure=lambda *a,**k: self.fail('provider access forbidden'))
        self.fin = SimpleNamespace(read=lambda k:{'state':'unavailable', 'result':None})
        self.view = lambda:(self.data, {'US:SYNTH':self.stock})
        self.s = AnalysisService(self.store, self.hist, self.fin, self.view, self.root)
        self.origin = 'http://127.0.0.1:1234'
        detail = self.s.dispatch('create', {'stock_key':'US:SYNTH','test_record':True}, self.origin)
        self.cid = detail['case']['id']; self.sid = detail['snapshots'][0]['id']

    def call(self, op, args=None, role='collaboration', session=None):
        return self.s.dispatch(op, {'analysis_key':self.cid, **(args or {})}, self.origin, principal=role, session=session)

    def read(self): return self.call('collaboration_read')

    def envelope(self):
        return dict(contract_version=VERSION, snapshot_id=self.sid, expected_revision=self.read()['revision'])

    def full_plan(self):
        plan = dict(title='합성 전체 계획', purpose='통합 검증', account_alias='synthetic-us',
                    allocation=dict(quantity='10', basis='units'), scenarios=[], conditions=[
                        dict(id='c1', kind='qualitative', statement='성장 논리 훼손 시 재검토', action='review',
                             evaluation=None, operator=None, price=None, currency=None,
                             quantity=dict(value=None, basis=None), priority=1, execution_price=None, drawing_ref=None)])
        return self.call('plan_save', {**self.envelope(), 'plan_id':None, 'expected_plan_version':0,
                                      'plan':plan, 'references':[]})['record']

    def adopt(self, plan, action='adopt'):
        rev = self.read()['revision']
        review = self.call('plan_review', dict(plan_record_id=plan['id'], expected_revision=rev),
                           role='browser', session='browser-fixture')
        return self.call('plan_decide', dict(contract_version=VERSION, expected_revision=rev,
                         confirmation=review['confirmation'], action=action, reason='synthetic confirmation'),
                         role='browser', session='browser-fixture')

    def drawing(self, kind='box'):
        prices = {'box':[95,110], 'channel':[95,100,110], 'riskreward':[100,95,110]}[kind]
        points = [dict(time=d,price=p) for d,p in zip(('2026-09-18','2026-09-23','2026-09-22'),prices)]
        state=defaults();state['drawings']=[dict(id='scenario',kind=kind,points=points,color='#218775',width=2,extend=True)]
        current=self.store.shared('US:SYNTH')
        return self.store.shared('US:SYNTH',revision=current['revision'],state=state)

    def draft(self, **extra):
        current=self.store.shared('US:SYNTH')
        return self.call('scenario_draft', dict(expected_revision=self.read()['revision'],drawing_id='scenario',
            workspace_revision=current['revision'], title='명확히 합성 시나리오',purpose='통합 검증',response=None, **extra),
            role='browser',session='fixture')['record']

    def register(self, plan, purpose='buy', **extra):
        return self.call('monitor_register',dict(plan_record_id=plan['id'],drawing_id='scenario',
            workspace_revision=self.store.shared('US:SYNTH')['revision'],purpose=purpose,boundary='both',tolerance_atr=.5,**extra),
            role='browser',session='fixture')

    def test_plan_input_and_monitor_share_adoption_and_integrity_checks(self):
        self.drawing(); plan = self.draft()
        readers = (
            lambda: self.call('plan_input', dict(plan_record_id=plan['id'], mode='adopted')),
            lambda: self.register(plan),
        )
        for read in readers:
            with self.assertRaisesRegex(ValueError, 'plan_version_not_adopted'): read()
        self.adopt(plan)
        frozen = self.s.snapshot(self.cid, plan['snapshot_id'])
        tampered = deepcopy(frozen); tampered['context']['tampered'] = True
        with patch.object(self.s.collaboration, 'snapshot', return_value=tampered):
            for read in readers:
                with self.assertRaisesRegex(ValueError, 'plan_snapshot_integrity_failed'): read()
        self.assertEqual(self.s.snapshot(self.cid, plan['snapshot_id']), frozen)
        self.adopt(plan, 'revoke')
        for read in readers:
            with self.assertRaisesRegex(ValueError, 'plan_version_not_adopted'): read()
        from scenarios.monitor import ScenarioStore
        self.assertEqual(ScenarioStore(self.store.path).read()['monitors'], [])
        preview = self.call('plan_input', dict(plan_record_id=plan['id'], mode='hypothetical'))
        self.assertIsNone(preview['adoption'])

    def test_box_channel_riskreward_frozen_plan_and_two_purposes(self):
        for kind in ('box','channel','riskreward'):
            with self.subTest(kind=kind):
                self.drawing(kind);plan=self.draft()
                with self.assertRaisesRegex(ValueError,'not_adopted'): self.register(plan)
                self.adopt(plan)
                buy=self.register(plan);held=self.register(plan,'holding')
                self.assertNotEqual(buy['id'],held['id'])
                self.assertEqual(buy['plan_record_id'],plan['id'])
                self.assertTrue(buy['test_record'])
                self.assertEqual(buy['confirmed_response'],'대응 기준 미정')
                self.assertTrue(all(c['price'] is None for c in plan['plan']['conditions']))
                self.assertIsNone(plan['plan']['allocation']['quantity'])
                self.assertEqual(plan['references'],[])
                before=deepcopy(self.s.snapshot(self.cid,plan['snapshot_id']))
                state=self.store.shared('US:SYNTH');state['state']['drawings'][0]['points'][0]['price']+=1
                self.store.shared('US:SYNTH',revision=state['revision'],state=state['state'])
                from scenarios.monitor import ScenarioStore
                monitor=ScenarioStore(self.store.path);monitor.sync_workspace('US:SYNTH')
                changed=next(m for m in monitor.read()['monitors'] if m['id']==buy['id'])
                self.assertGreater(changed['revision'],buy['revision'])
                self.assertEqual(self.s.snapshot(self.cid,plan['snapshot_id']),before)
                self.assertEqual(self.s.item(self.cid,plan['id']),plan)
                self.assertGreaterEqual(len(monitor.history(buy['id'])),2)

    def test_stale_saved_drawing_requires_new_draft(self):
        self.drawing();plan=self.draft();self.adopt(plan);self.drawing()
        with self.assertRaises(Conflict):self.register(plan)

    def test_simple_editor_preserves_full_plan_by_rejecting_replacement(self):
        self.drawing()
        original=self.full_plan()
        with self.assertRaisesRegex(ValueError,'full_plan_requires_full_editor'):
            self.draft(plan_id=original['plan_id'],expected_plan_version=original['plan_version'])
        self.assertEqual(self.s.item(self.cid,original['id']),original)

    def test_conversion_accept_rechecks_current_saved_account(self):
        from datetime import datetime, timezone
        from unittest.mock import patch
        from scenarios.monitor import ScenarioStore
        self.drawing();plan=self.draft();self.adopt(plan);watch=self.register(plan)
        args=dict(id=watch['id'],revision=watch['revision'],accept=True)
        # The downstream atomic one-time decision is tested in test_scenario_monitor.
        with patch.object(ScenarioStore,'resolve_conversion',return_value={'accepted':True}) as decide:
            with self.assertRaisesRegex(ValueError,'holding_confirmation_stale_or_missing'):
                self.s.dispatch('monitor_conversion',args,self.origin,principal='browser',session='fixture')
            decide.assert_not_called()
            self.accounts.update(as_of=datetime.now(timezone.utc).isoformat(),query_state='received')
            self.accounts['accounts'][0]['holdings_complete']=True
            result=self.s.dispatch('monitor_conversion',args,self.origin,principal='browser',session='fixture')
            self.assertTrue(result['accepted']);decide.assert_called_once()

    def test_monitor_and_scenario_writes_require_browser(self):
        self.drawing();plan=self.draft();self.adopt(plan)
        for role in ('legacy','collaboration','risk'):
            for op,args in [('scenario_draft',{}),('monitor_register',{}),('monitor_update',{}),('monitor_conversion',{})]:
                with self.subTest(role=role,op=op),self.assertRaises(Forbidden):self.call(op,args,role=role)

    def test_workspace_versions_and_resume_preserve_original_records(self):
        self.drawing();plan=self.draft();self.adopt(plan);watch=self.register(plan)
        self.drawing('channel')
        with closing(self.store.connect()) as db:
            rows=db.execute('SELECT revision,payload FROM chart_workspace_versions ORDER BY revision').fetchall()
        self.assertEqual([r[0] for r in rows],[0,1,2])
        from analysis.service import AnalysisService
        reopened=AnalysisService(self.store,self.hist,self.fin,self.view,self.root)
        listing=reopened.dispatch('list',{'stock_key':'US:SYNTH'},self.origin)
        self.assertEqual(listing['cases'][0]['latest_record_id'],self.read()['active_plans'][0]['adoption']['id'])
        value=reopened.dispatch('monitor_list',{'stock_key':'US:SYNTH'},self.origin)
        self.assertEqual(value['monitors'][0]['id'],watch['id'])
        self.assertIn('history',value['monitors'][0])
        self.assertIn('analysis='+self.cid,value['monitors'][0]['detail_url'])

    def test_provisional_weekly_research_does_not_change_pivot_status(self):
        from market.history.price_events import validate_research
        packet={'groups':[{'id':'g','event_ids':['p'],'confirmed_event_ids':[]}],
                'events':[{'id':'p','kind':'high','timeframe':'1w','status':'provisional'}]}
        before=deepcopy(packet)
        result=validate_research([dict(group_id='g',status='evidence_found',finding='합성 출처를 조사했으나 점은 잠정',
            searched_windows=[dict(start='2026-09-18',end='2026-09-23',query='synthetic')],
            sources=[dict(url='https://example.com/synthetic')])],packet)
        self.assertEqual(result[0]['status'],'evidence_found');self.assertEqual(packet,before)


if __name__=='__main__':unittest.main()
