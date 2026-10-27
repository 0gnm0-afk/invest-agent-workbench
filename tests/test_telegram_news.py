import asyncio
from datetime import datetime, timedelta
import http.client
import json
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch, AsyncMock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'scripts'),str(ROOT/'runtime/telegram-news-deps')]
from news.search import NewsService, KST, client_factory
from news.server import Server
from news.auth import ApiCredentials

SessionPasswordNeededError=type('SessionPasswordNeededError',(Exception,),{})
FloodWaitError=type('FloodWaitError',(Exception,),{})
PasswordHashInvalidError=type('PasswordHashInvalidError',(Exception,),{})


def message(i, text, date=None):
    return NS(id=i,message=text,date=date or datetime(2026,10,7,12,tzinfo=KST),entities=[])


class FakeClient:
    def __init__(self):
        self.two_factor=False; self.disconnected=False; self.logged_out=False
        self.request=None; self.error=None; self.dialog_calls=0
        self.messages=[message(20,'NVDA 실적 발표 https://example.com/news'),message(19,'NVDA 안녕')]
    async def connect(self): pass
    async def disconnect(self): self.disconnected=True
    async def log_out(self): self.logged_out=True
    async def send_code_request(self,phone): return NS(phone_code_hash='SENSITIVE_HASH')
    async def sign_in(self,**kwargs):
        if self.two_factor and 'password' not in kwargs: raise SessionPasswordNeededError('SENSITIVE_CODE')
        if kwargs.get('password')=='wrong': raise PasswordHashInvalidError('SENSITIVE_PASSWORD')
    async def get_dialogs(self,**kwargs):
        self.dialog_calls+=1
        return [NS(id=-100123,title='합성 뉴스방',is_group=False,is_channel=True,
                   entity=NS(id=123,broadcast=True,username='fixture_channel'),input_entity='PEER'),
                NS(id=55,title='개인 대화',is_group=False,is_channel=False)]
    async def __call__(self,request):
        if self.error: raise self.error
        self.request=request
        return NS(messages=self.messages,count=20)


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client=FakeClient();self.s=NewsService(lambda *args:self.client)
    async def login(self):
        await self.s.request('login',dict(api_id=12345,api_hash='a'*32,phone='+821011112222'))
        return await self.s.request('verify',{'code':'12345'})
    async def search(self,**extra):
        return await self.s.request('search',dict(room_id='-100123',query='NVDA',start='2026-10-07',end='2026-10-07',**extra))
    async def test_authentication_and_2fa_redaction(self):
        self.client.two_factor=True
        result=await self.login();self.assertEqual(result['state'],'password_required')
        result=await self.s.request('verify',{'password':'wrong'})
        self.assertEqual(result['error'],'invalid_password');self.assertNotIn('SENSITIVE',json.dumps(result))
        self.assertEqual((await self.s.request('verify',{'password':'correct'}))['state'],'connected')
        self.assertIsNone(self.s.phone);self.assertIsNone(self.s.code_hash)
    async def test_unauthorized_and_invalid_inputs(self):
        self.assertEqual((await self.search())['error'],'authentication_required')
        self.assertEqual((await self.s.request('send',{}))['error'],'unsupported_operation')
        await self.login()
        self.assertEqual((await self.search())['error'],'load_rooms_first')
        await self.s.request('dialogs',{})
        for args in [dict(room_id='55',query='x',start='2026-10-07',end='2026-10-07'),
                     dict(room_id='-100123',query='',start='2026-10-07',end='2026-10-07'),
                     dict(room_id='-100123',query='x',start='2026-10-07',end='2024-10-07')]:
            self.assertIn('error',await self.s.request('search',args))
        self.assertIsNone(self.client.request)
    async def test_dialogs_exclude_dms_and_do_not_keep_messages(self):
        await self.login();r=await self.s.request('dialogs',{'limit':1})
        self.assertEqual(len(r['rooms']),1);self.assertEqual(r['rooms'][0]['kind'],'channel')
        self.assertTrue(r['private_dialogs_excluded']);self.assertIsNone(r['next_offset'])
        await self.s.request('dialogs',{});self.assertEqual(self.client.dialog_calls,1)
        self.assertEqual(set(self.s.rooms),{'-100123'})
    async def test_server_search_dates_news_filter_and_pagination(self):
        await self.login();await self.s.request('dialogs',{})
        r=await self.search(limit=2,offset_id=80)
        self.assertEqual(r['scanned'],2);self.assertEqual(r['news_candidates'],1);self.assertEqual(r['next_offset_id'],19)
        self.assertEqual(r['items'][0]['message_url'],'https://t.me/fixture_channel/20')
        req=self.client.request
        self.assertEqual(req.q,'NVDA');self.assertEqual(req.peer,'PEER');self.assertEqual(req.offset_id,80)
        self.assertEqual(req.min_date,datetime(2026,10,7,tzinfo=KST)-timedelta(seconds=1))
        self.assertEqual(req.max_date,datetime(2026,10,8,tzinfo=KST))
        self.assertNotIn('items',vars(self.s))
    async def test_date_edges_and_empty_filtered_page_still_advance(self):
        await self.login();await self.s.request('dialogs',{})
        self.client.messages=[message(40,'실적',datetime(2026,10,8,tzinfo=KST)),message(39,'잡담')]
        r=await self.search(limit=2)
        self.assertEqual(r['items'],[]);self.assertEqual(r['next_offset_id'],39)
        self.client.messages=[message(38,'실적',datetime(2026,10,7,tzinfo=KST))]
        self.assertEqual((await self.search())['news_candidates'],1)
    async def test_floodwait_no_retry_logout_clears_even_on_disconnect_failure(self):
        await self.login();await self.s.request('dialogs',{})
        err=FloodWaitError('SENSITIVE');err.seconds=30;self.client.error=err
        r=await self.search();self.assertEqual(r['error'],'telegram_rate_limited')
        self.assertGreater(r['retry_after_seconds'],0)
        self.client.error=None
        self.assertEqual((await self.search())['error'],'telegram_rate_limited')
        async def broken(): raise RuntimeError('SENSITIVE')
        self.client.disconnect=broken
        r=await self.s.request('logout',{})
        self.assertEqual(r['state'],'not_connected');self.assertIsNone(self.s.client);self.assertFalse(self.s.rooms)
        self.assertNotIn('SENSITIVE',json.dumps(r))
    async def test_real_library_memory_session_writes_no_session_file(self):
        from telethon.sessions import MemorySession
        with tempfile.TemporaryDirectory() as folder, patch('news.search.ROOT',ROOT):
            with patch('os.getcwd',return_value=folder):
                client=client_factory(12345,'a'*32)
                self.assertIs(type(client.session),MemorySession)
                self.assertTrue(client._no_updates)
                await client.disconnect()
            self.assertEqual(list(Path(folder).iterdir()),[])

    async def test_real_telethon_dc_migration_requires_retry_budget(self):
        from telethon.errors import PhoneMigrateError
        from telethon.tl.functions.auth import SendCodeRequest
        from telethon.tl.types import CodeSettings
        client=client_factory(12345,'a'*32)
        class Sender:
            def __init__(self):self.calls=0
            def send(self,request,ordered=False):
                self.calls+=1;f=asyncio.get_running_loop().create_future()
                if self.calls==1:f.set_exception(PhoneMigrateError(request=request,capture=5))
                else:f.set_result(True)
                return f
        request=SendCodeRequest('+821011112222',12345,'a'*32,CodeSettings())
        with patch.object(client,'is_user_authorized',AsyncMock(return_value=False)),patch.object(client,'_switch_dc',AsyncMock()) as switch:
            original=client._request_retries
            client._request_retries=0;client._raise_last_call_error=False
            with self.assertRaisesRegex(ValueError,'Request was unsuccessful'):
                await client._call(Sender(),request)
            client._request_retries=original;client._raise_last_call_error=True
            sender=Sender()
            self.assertTrue(await client._call(sender,request))
            self.assertEqual(sender.calls,2);switch.assert_called_with(5)
        await client.disconnect()

    async def test_environment_import_keeps_values_out_of_result(self):
        from telegram_news_import import import_credentials
        with tempfile.TemporaryDirectory() as folder,patch('news.auth.winreg.OpenKey'),patch('news.auth.winreg.QueryValueEx',side_effect=[('12345',1),('ab'*16,1)]):
            result=import_credentials(folder)
            self.assertEqual(result['state'],'api_credentials_saved')
            self.assertNotIn('12345',json.dumps(result));self.assertNotIn('ab'*16,json.dumps(result))
            self.assertEqual(ApiCredentials(folder).load()['api_id'],12345)

    async def test_dpapi_credentials_reused_without_exposure_or_news_writes(self):
        with tempfile.TemporaryDirectory() as folder:
            store=ApiCredentials(folder)
            self.s.credentials=store
            r=await self.s.request('save_credentials',{'api_id':12345,'api_hash':'ab'*16})
            self.assertTrue(r['api_credentials_saved']);self.assertNotIn('api_hash',r)
            encrypted=store.path.read_bytes()
            self.assertNotIn(b'ab'*16,encrypted);self.assertNotIn(b'12345',encrypted)
            self.assertEqual(store.load(),{'api_id':12345,'api_hash':'ab'*16})
            r=await self.s.request('login',{'phone':'+821011112222'})
            self.assertEqual(r['state'],'code_required')
            await self.s.request('verify',{'code':'12345'})
            await self.s.request('dialogs',{});await self.search()
            self.assertEqual(store.path.read_bytes(),encrypted)
            self.assertEqual([p for p in Path(folder).rglob('*') if p.is_file()],[store.path])
            await self.s.request('logout',{})
            self.assertTrue(store.exists())
            await self.s.request('clear_credentials',{})
            self.assertFalse(store.exists())
    async def test_corrupt_saved_credentials_safe_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            store=ApiCredentials(folder);self.s.credentials=store
            store.path.parent.mkdir();store.path.write_text('{"protected":"SECRET"}',encoding='utf-8')
            r=await self.s.request('login',{'phone':'+821011112222'})
            self.assertEqual(r['error'],'api_credentials_unreadable');self.assertNotIn('SECRET',json.dumps(r))
            self.assertIsNone(self.s.client)


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.client=FakeClient();self.s=NewsService(lambda *args:self.client)
        self.server=Server(0,self.s)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join()
    def req(self,path,body=None,headers=None):
        conn=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=5)
        conn.request('POST' if body is not None else 'GET',path,body=json.dumps(body) if body is not None else None,headers=headers or {})
        response=conn.getresponse();data=response.read();status=response.status;hs=dict(response.getheaders());conn.close()
        return status,hs,json.loads(data) if hs.get('Content-Type','').startswith('application/json') else data
    def test_boundary_and_native_token(self):
        status,hs,health=self.req('/health');self.assertEqual(status,200);self.assertIn('read_token',health)
        self.assertEqual(hs['Cache-Control'],'no-store')
        self.assertNotIn('read_token',self.req('/health',headers={'Sec-Fetch-Site':'same-origin'})[2])
        self.assertEqual(self.req('/health',headers={'Origin':'https://evil.example'})[0],403)
        self.assertEqual(self.req('/health',headers={'Host':'evil.example'})[0],403)
        self.assertEqual(self.req('/',headers={'Sec-Fetch-Site':'cross-site'})[0],403)
    def test_mcp_read_only_and_ui_csrf_separation(self):
        headers={'Content-Type':'application/json','Authorization':'Bearer '+self.server.read_token}
        self.assertEqual(self.req('/mcp',{'op':'status','args':{}},headers)[2]['state'],'not_connected')
        self.assertEqual(self.req('/mcp',{'op':'login','args':{}},headers)[0],403)
        self.assertEqual(self.req('/mcp',{'op':'save_credentials','args':{}},headers)[0],403)
        self.assertEqual(self.req('/mcp',{'op':'clear_credentials','args':{}},headers)[0],403)
        self.assertEqual(self.req('/ui',{'op':'logout','args':{}},headers)[0],403)
        _,hs,_=self.req('/')
        for path in ('/app.js','/style.css'):
            self.assertEqual(self.req(path)[0],200)
        cookie=hs['Set-Cookie'].split(';')[0]
        self.assertEqual(self.req('/ui-state')[0],403)
        token=self.req('/ui-state',headers={'Cookie':cookie})[2]['token']
        auth={'Content-Type':'application/json','Origin':self.server.origin,'Cookie':cookie,'X-CSRF-Token':token}
        result=self.req('/ui',{'op':'login','args':{'api_id':12345,'api_hash':'a'*32,'phone':'+821011112222'}},auth)
        self.assertEqual(result[2]['state'],'code_required')
        self.assertNotIn('phone',result[2]);self.assertNotIn('code_hash',result[2])
        self.assertEqual(self.req('/ui',{'op':'verify','args':{'code':'12345'}},auth)[2]['state'],'connected')
        self.assertEqual(self.req('/mcp',{'op':'dialogs','args':{}},headers)[2]['total'],1)
        r=self.req('/mcp',{'op':'search','args':{'room_id':'-100123','query':'NVDA','start':'2026-10-07','end':'2026-10-07'}},headers)[2]
        self.assertEqual(r['news_candidates'],1);self.assertEqual(r['storage'],'not_saved')
        self.assertEqual(self.req('/ui',{'op':'logout','args':{}},auth)[2]['state'],'not_connected')
    def test_invalid_payload_and_no_cors(self):
        headers={'Content-Type':'application/json','Authorization':'Bearer '+self.server.read_token}
        status,hs,_=self.req('/mcp',{'op':[],'args':{}},headers)
        self.assertEqual(status,400);self.assertNotIn('Access-Control-Allow-Origin',hs)
        self.assertEqual(self.req('/mcp',{'op':'status','args':{}},{**headers,'Sec-Fetch-Site':'same-origin'})[0],403)


if __name__=='__main__': unittest.main()
