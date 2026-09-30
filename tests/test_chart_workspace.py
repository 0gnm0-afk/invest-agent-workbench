import copy
import http.client
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from chart_workspace import WorkspaceStore, defaults, validate, Conflict, moving_averages
from dashboard import handler_for, ThreadingHTTPServer


def drawing(kind='trend', ident='test'):
    points=[{'time':'2026-09-21','price':1000.125},{'time':'2026-09-22','price':1100.25},{'time':'2026-09-23','price':1050.5}]
    n={'trend':2,'channel':3,'horizontal':1,'box':2,'measure':2}[kind]
    return {'id':ident,'kind':kind,'points':points[:n],'color':'#218775','width':2,'extend':False}


class Workspaces(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'workspace.sqlite3';self.store=WorkspaceStore(self.path)
    def tearDown(self):self.temp.cleanup()
    def test_every_tool_and_average_persists_across_store_reopen(self):
        state=defaults();state['drawings']=[drawing(k,k) for k in ['trend','channel','horizontal','box','measure']]
        original=copy.deepcopy(state);self.store.save('KR:AAA',0,state)
        loaded=WorkspaceStore(self.path).read('KR:AAA')
        self.assertEqual(loaded['state'],original);self.assertEqual(loaded['revision'],1);self.assertEqual(state,original)
    def test_symbol_isolation(self):
        state=defaults();state['drawings']=[drawing()];self.store.save('KR:AAA',0,state)
        self.assertEqual(self.store.read('US:AAA')['revision'],0)
    def test_conflict_does_not_overwrite(self):
        a,b=WorkspaceStore(self.path),WorkspaceStore(self.path)
        a.save('KR:AAA',0,defaults())
        with self.assertRaises(Conflict):b.save('KR:AAA',0,defaults())
        self.assertEqual(b.read('KR:AAA')['revision'],1)
    def test_delete_and_restore_revision(self):
        state=defaults();state['drawings']=[drawing()];self.store.save('KR:A',0,state)
        self.store.save('KR:A',1,defaults());self.assertEqual(self.store.read('KR:A')['state']['drawings'],[])
        self.store.save('KR:A',2,state);self.assertEqual(self.store.read('KR:A')['state'],state)
    def test_invalid_shapes_rejected(self):
        for change in [{'color':'url(secret)'},{'points':[]},{'width':10},{'kind':'order'},{'id':'../private'}]:
            state=defaults();state['drawings']=[{**drawing(),**change}]
            with self.assertRaises(ValueError):validate(state)
    def test_nan_bad_dates_and_same_date_rejected(self):
        for point in [{'time':'2026-09-31','price':10},{'time':'2026-09-22','price':float('nan')},{'time':'2026-09-22','price':-1},{'time':'2026-09-22','price':True}]:
            state=defaults();state['drawings']=[drawing()];state['drawings'][0]['points'][0]=point
            with self.assertRaises(ValueError):validate(state)
        state=defaults();state['drawings']=[drawing()];state['drawings'][0]['points'][1]['time']='2026-09-21'
        with self.assertRaises(ValueError):validate(state)
    def test_limits_and_unknown_fields(self):
        for patch in [{'averages':[{'period':0,'color':'#ffffff','width':2,'visible':True}]},{'drawings':[drawing(ident=str(i)) for i in range(101)]},{'order':'buy'}]:
            with self.assertRaises(ValueError):validate({**defaults(),**patch})
    def test_sma_full_precision_and_warmup(self):
        bars=[{'date':f'2026-09-{i+1:02}','close':v} for i,v in enumerate([1000.1,1000.2,1000.3,1000.4])]
        original=copy.deepcopy(bars);values=moving_averages({'bars':bars},[3])['3']
        self.assertNotIn('value',values[1]);self.assertAlmostEqual(values[2]['value'],1000.2);self.assertAlmostEqual(values[3]['value'],1000.3);self.assertEqual(bars,original)
    def test_sma_missing_and_insufficient_not_filled(self):
        bars=[{'date':f'2026-09-{i+1:02}','close':v} for i,v in enumerate([1,None,3,4])]
        values=moving_averages({'bars':bars},[2,500]);self.assertNotIn('value',values['2'][2]);self.assertEqual(values['2'][3]['value'],3.5);self.assertTrue(all('value' not in p for p in values['500']))


class WorkspaceHTTP(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory();cls.root=Path(cls.temp.name)
        stocks={'KR:A':{'bars':[{'date':'2026-09-21','close':100.1},{'date':'2026-09-22','close':100.2}]}}
        cls.server=ThreadingHTTPServer(('127.0.0.1',0),handler_for({},stocks,cls.root,'test-token',cls.root/'state.sqlite3'))
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()
    @classmethod
    def tearDownClass(cls):cls.server.shutdown();cls.server.server_close();cls.thread.join();cls.temp.cleanup()
    def call(self,path,method='GET',body=None,headers=None):
        c=http.client.HTTPConnection('127.0.0.1',self.server.server_port)
        c.request(method,path,body=body,headers=headers or {});r=c.getresponse();status=r.status;data=r.read();c.close();return status,json.loads(data)
    def headers(self):return {'Origin':'http://127.0.0.1:'+str(self.server.server_port),'X-Workbench-Token':'test-token','Content-Type':'application/json'}
    def test_save_requires_origin_and_token(self):
        body=json.dumps({'revision':0,'state':defaults()})
        for headers in [{},{**self.headers(),'Origin':'https://evil.test'},{**self.headers(),'X-Workbench-Token':'wrong'},{**self.headers(),'Host':'evil.test'}]:
            self.assertEqual(self.call('/api/chart-state/KR%3AA','POST',body,headers)[0],403)
    def test_workspace_roundtrip_and_conflict(self):
        code,current=self.call('/api/chart-state/KR%3AA');self.assertEqual(code,200)
        body=json.dumps({'revision':current['revision'],'state':defaults()})
        self.assertEqual(self.call('/api/chart-state/KR%3AA','POST',body,self.headers())[0],200)
        self.assertEqual(self.call('/api/chart-state/KR%3AA','POST',body,self.headers())[0],409)
    def test_path_and_order_boundaries(self):
        self.assertEqual(self.call('/api/chart-state/..%2Fsecret')[0],404)
        self.assertEqual(self.call('/api/home','POST','{}',self.headers())[0],405)
        self.assertEqual(self.call('/api/chart-state/US%3Ano','POST','{}',self.headers())[0],404)
    def test_invalid_payload_and_size(self):
        self.assertEqual(self.call('/api/chart-state/KR%3AA','POST','{}',self.headers())[0],400)
        self.assertEqual(self.call('/api/chart-state/KR%3AA','POST',' '*131073,self.headers())[0],413)
    def test_sma_endpoint(self):
        status,data=self.call('/api/averages/KR%3AA?periods=2');self.assertEqual(status,200);self.assertAlmostEqual(data['2'][1]['value'],100.15)
        self.assertEqual(self.call('/api/averages/KR%3AA?periods=0')[0],400)


if __name__=='__main__':unittest.main()
