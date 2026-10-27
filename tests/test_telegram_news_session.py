import asyncio
import base64
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import URLError

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'scripts'),str(ROOT/'tests'),str(ROOT/'runtime/telegram-news-deps')]
from news.search import NewsService
from news.auth import ApiCredentials
from news.auth import EncryptedSession,string_session
from news.server import Server
from alerts.delivery import protect
from test_telegram_news import FakeClient
import news.bridge as bridge
from telethon.sessions import MemorySession
from telethon.crypto import AuthKey

API={'api_id':12345,'api_hash':'ab'*16}


class SessionClient(FakeClient):
    def __init__(self,api_id=None,api_hash=None,saved=None):
        super().__init__()
        self.session=string_session()(saved) if saved else MemorySession()
        self.session.set_dc(2,'149.154.167.51',443)
        self.session.auth_key=AuthKey(bytes(range(256)))
        self.authorized=True
        self.code_requests=0
    async def is_user_authorized(self):return self.authorized
    async def send_code_request(self,phone):
        self.code_requests+=1
        return await super().send_code_request(phone)


def service(folder,factory=SessionClient):
    credentials=ApiCredentials(folder)
    return NewsService(factory,credentials,EncryptedSession(folder))


async def login(s):
    s.credentials.save(**API)
    await s.request('login',{'phone':'+821011112222'})
    return await s.request('verify',{'code':'12345'})


class SessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_encrypted_auth_only_and_restart_without_code(self):
        with tempfile.TemporaryDirectory() as folder:
            first=service(folder)
            result=await login(first)
            self.assertTrue(result['session_saved'])
            session_string=string_session().save(first.client.session)
            encrypted=first.sessions.path.read_bytes()
            self.assertNotIn(session_string.encode(),encrypted)
            payload=json.loads(protect(base64.b64decode(json.loads(encrypted)['protected']),True))
            self.assertEqual(set(payload),{'session','api_fingerprint'})
            old_client=first.client
            await first.close()
            self.assertTrue(old_client.disconnected);self.assertFalse(old_client.logged_out)
            second=service(folder)
            restored=await second.request('restore',{})
            self.assertEqual(restored['state'],'connected');self.assertTrue(restored['session_saved'])
            self.assertEqual(second.client.code_requests,0)
            self.assertEqual(string_session().save(second.client.session),session_string)
            await second.request('dialogs',{})
            self.assertNotIn(b'rooms',second.sessions.path.read_bytes())
            self.assertEqual(len([x for x in Path(folder).rglob('*') if x.is_file()]),2)
            self.assertNotIn(session_string,json.dumps(restored))
            await second.close()
    async def test_explicit_logout_deletes_session_and_revokes(self):
        with tempfile.TemporaryDirectory() as folder:
            s=service(folder);await login(s);client=s.client
            r=await s.request('logout',{})
            self.assertTrue(client.logged_out);self.assertFalse(s.sessions.exists())
            self.assertFalse(r['session_saved']);self.assertTrue(s.credentials.exists())
            self.assertEqual((await service(folder).request('restore',{}))['state'],'not_connected')
    async def test_revoked_session_requires_auth_no_auto_code(self):
        with tempfile.TemporaryDirectory() as folder:
            s=service(folder);await login(s);await s.close()
            client=SessionClient();client.authorized=False
            second=service(folder,lambda *a:client)
            r=await second.request('restore',{})
            self.assertEqual(r['error'],'session_expired');self.assertFalse(second.sessions.exists())
            self.assertEqual(client.code_requests,0);self.assertTrue(client.disconnected)
    async def test_network_failure_preserves_session_for_retry(self):
        with tempfile.TemporaryDirectory() as folder:
            s=service(folder);await login(s);await s.close()
            before=s.sessions.path.read_bytes()
            client=SessionClient()
            async def fail():raise OSError('PRIVATE_NETWORK_DETAIL')
            client.connect=fail
            second=service(folder,lambda *a:client)
            r=await second.request('restore',{})
            self.assertEqual(r['state'],'not_connected');self.assertTrue(r['saved_session_available'])
            self.assertEqual(second.sessions.path.read_bytes(),before)
            self.assertNotIn('PRIVATE_NETWORK_DETAIL',json.dumps(r))
            async def succeed():pass
            client.connect=succeed
            self.assertEqual((await second.request('restore',{}))['state'],'connected')
            await second.close()
    async def test_corruption_and_changed_api_do_not_auto_login(self):
        with tempfile.TemporaryDirectory() as folder:
            s=service(folder);await login(s);await s.close()
            s.credentials.save(12346,'cd'*16)
            second=service(folder)
            self.assertEqual((await second.request('restore',{}))['error'],'session_unreadable')
            second.sessions.path.write_text('broken',encoding='utf-8')
            self.assertEqual((await second.request('restore',{}))['error'],'session_unreadable')
            self.assertIsNone(second.client)
    async def test_save_failure_does_not_claim_persistence_and_can_retry(self):
        with tempfile.TemporaryDirectory() as folder:
            s=service(folder)
            with patch.object(s.sessions,'save',side_effect=ValueError('session_save_failed')):
                r=await login(s)
            self.assertEqual(r['state'],'connected');self.assertFalse(r['session_saved'])
            self.assertEqual(r['error'],'session_save_failed')
            self.assertTrue((await s.request('persist_session',{}))['session_saved'])
            await s.close()
    async def test_logout_even_if_server_revoke_fails_cannot_restore(self):
        with tempfile.TemporaryDirectory() as folder:
            s=service(folder);await login(s)
            async def fail():raise OSError('private')
            s.client.log_out=fail
            r=await s.request('logout',{})
            self.assertFalse(r['saved_session_available']);self.assertIsNone(s.client)


class ProcessAndBridgeTests(unittest.TestCase):
    def test_server_shutdown_preserves_and_next_start_restores(self):
        with tempfile.TemporaryDirectory() as folder:
            first_service=service(folder);first_service.credentials.save(**API)
            first=Server(0,first_service)
            first.invoke('login',{'phone':'+821011112222'})
            self.assertTrue(first.invoke('verify',{'code':'12345'})['session_saved'])
            first.server_close()
            second_service=service(folder);second=Server(0,second_service)
            try:
                self.assertEqual(second.invoke('status',{})['state'],'connected')
                self.assertEqual(second_service.client.code_requests,0)
                with self.assertRaises(OSError):Server(second.server_port,service(folder))
            finally:second.server_close()
    def test_fresh_process_restores_dpapi_session_without_code(self):
        with tempfile.TemporaryDirectory() as folder:
            for op in ['login','restore']:
                result=subprocess.run([sys.executable,'-B','-X','utf8',__file__,'fixture',op,folder],capture_output=True,text=True,encoding='utf-8',check=True)
                data=json.loads(result.stdout)
                self.assertEqual(data['state'],'connected');self.assertTrue(data['session_saved'])
                self.assertEqual(data['code_requests'],1 if op=='login' else 0)
    def test_bridge_autostarts_only_refused_listener_with_saved_session(self):
        with tempfile.TemporaryDirectory() as folder,patch.object(bridge,'ROOT',Path(folder)):
            path=Path(folder)/'secrets/telegram-news-session.dpapi.json';path.parent.mkdir();path.write_text('fixture')
            with patch.object(bridge,'health',side_effect=[URLError(ConnectionRefusedError()),{'app':'telegram-news-1'}]),patch.object(bridge.subprocess,'Popen') as start:
                self.assertEqual(bridge.ensure_health(None)['app'],'telegram-news-1')
                start.assert_called_once()
                self.assertEqual(start.call_args.kwargs['stdout'],subprocess.DEVNULL)
            with patch.object(bridge,'health',side_effect=URLError(TimeoutError())),patch.object(bridge.subprocess,'Popen') as start:
                with self.assertRaises(URLError):bridge.ensure_health(None)
                start.assert_not_called()
            path.unlink()
            with patch.object(bridge,'health',side_effect=URLError(ConnectionRefusedError())),patch.object(bridge.subprocess,'Popen') as start:
                with self.assertRaises(URLError):bridge.ensure_health(None)
                start.assert_not_called()


async def fixture(op,folder):
    s=service(folder)
    result=await login(s) if op=='login' else await s.request('restore',{})
    result['code_requests']=s.client.code_requests
    await s.close()
    print(json.dumps(result))


if __name__=='__main__':
    if len(sys.argv)>1 and sys.argv[1]=='fixture':asyncio.run(fixture(sys.argv[2],sys.argv[3]))
    else:unittest.main()
