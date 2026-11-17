import ctypes
import io
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import atomic_io
import market.adr as adr
import market.providers.nh_auth as nh_market_auth
import market.providers.nh_client as market
import nh_auth
import nh_api


class AtomicPublication(unittest.TestCase):
    def test_concurrent_writers_have_unique_temps_and_complete_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'progress.json'
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(lambda n:atomic_io.atomic_write(path,json.dumps({'n':n,'body':'x'*1000})),range(20)))
            self.assertEqual(len(json.loads(path.read_text(encoding='utf-8'))['body']),1000)
            self.assertEqual(list(Path(tmp).glob('*.tmp')),[])

    def test_persistent_lock_preserves_previous_and_cleans_own_temp(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'progress.json';path.write_text('previous')
            with patch.object(atomic_io.os,'replace',side_effect=PermissionError(13,'locked')) as replace, patch.object(atomic_io.time,'sleep'):
                with self.assertRaises(PermissionError):atomic_io.atomic_write(path,'new')
            self.assertEqual(replace.call_count,9)
            self.assertEqual(path.read_text(encoding='utf-8'),'previous')
            self.assertEqual(list(Path(tmp).glob('*.tmp')),[])

    @unittest.skipUnless(os.name=='nt','Windows sharing semantics')
    def test_real_windows_read_handle_reproduces_old_error_and_new_writer_recovers(self):
        from atomic_io import atomic_write as old_write
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.CreateFileW.argtypes=[ctypes.c_wchar_p,ctypes.c_uint32,ctypes.c_uint32,ctypes.c_void_p,ctypes.c_uint32,ctypes.c_uint32,ctypes.c_void_p]
        kernel.CreateFileW.restype=ctypes.c_void_p
        kernel.CloseHandle.argtypes=[ctypes.c_void_p]
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'progress.json';path.write_text('previous')
            handle=kernel.CreateFileW(str(path),0x80000000,3,None,3,0,None)
            self.assertNotEqual(handle,ctypes.c_void_p(-1).value)
            try:
                with self.assertRaises(PermissionError) as caught:old_write(path,'new')
                self.assertIn(caught.exception.winerror,(5,32))
                self.assertEqual(path.read_text(encoding='utf-8'),'previous')
            finally:kernel.CloseHandle(handle)
            handle=kernel.CreateFileW(str(path),0x80000000,3,None,3,0,None)
            timer=threading.Timer(.15,lambda:kernel.CloseHandle(handle));timer.start()
            try:atomic_io.atomic_write(path,'recovered')
            finally:timer.join()
            self.assertEqual(path.read_text(encoding='utf-8'),'recovered')

    def test_locked_progress_cannot_abort_successful_adr_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'source.html';source.write_text('saved fixture')
            snapshot={'sessions':{'US':['2026-10-06']},'universe':[{'market':'US','symbol':'ABC','isin_code':'US123','listing_exchange_family':'NYSE','security_type':'DEPOSITARY_RECEIPT'}]}
            original=adr.write
            def locked_progress(path,value):
                if path.name=='progress.json':raise PermissionError(13,'locked')
                original(path,value)
            with patch.object(adr,'cached_page',return_value={'path':str(source)}),patch.object(adr,'parse_pages',return_value={'market_cap':100}),patch.object(adr,'write',side_effect=locked_progress):
                result=adr.collect(snapshot,root/'evidence',cache=root/'cache')
            bundle=json.loads(Path(result['path']).read_text(encoding='utf-8'))
            self.assertEqual(bundle['records'][0]['state'],'available')
            self.assertEqual(bundle['progress_write_failures'],1)
            self.assertEqual(result['sha256'],adr.sha(result['path']))


class SharedNH(unittest.TestCase):
    def test_both_readers_use_one_cache_and_issuance(self):
        self.assertEqual(nh_api.ROOT,nh_market_auth.ROOT)
        self.assertIs(nh_market_auth.token,nh_auth.token)
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ,NH_APP_KEY='fake',NH_APP_SECRET='secret'),patch.object(nh_auth,'ROOT',Path(tmp)),patch.object(nh_api,'ROOT',Path(tmp)),patch.object(nh_auth,'protect',side_effect=lambda raw,**k:raw):
            with patch.object(nh_auth,'issue',return_value={'access_token':'shared','token_type':'Bearer','expires_in':86400}) as issue,patch.object(nh_api,'request') as account_issue:
                self.assertEqual(nh_market_auth.token(),'shared')
                self.assertEqual(nh_api.token(),'shared')
                self.assertEqual(issue.call_count,1)
                account_issue.assert_not_called()

    def test_market_400_invalid_token_retries_once_and_is_not_cached_as_missing(self):
        class Response(io.BytesIO):
            headers={}
        error=urllib.error.HTTPError('https://example.invalid',400,'bad',{},io.BytesIO(b'{"rsp_cd":"IGW40043"}'))
        with tempfile.TemporaryDirectory() as tmp,patch.object(market,'token',side_effect=['old','new']) as token,patch('urllib.request.OpenerDirector.open',side_effect=[error,Response(b'{"Output_1": []}')]) as request,patch.object(market.time,'sleep'):
            client=market.MarketClient(tmp)
            result,path=client.read(market.PERIOD,{'iem_cd':'ABC'})
            self.assertNotIn('http_status',json.loads(path.read_text(encoding='utf-8')))
            self.assertEqual(request.call_count,2)
            self.assertEqual(token.call_args_list[1].kwargs,{'rejected_token':'old'})
            events=[json.loads(line) for line in (Path(tmp)/'http.jsonl').read_text(encoding='utf-8').splitlines()]
            self.assertEqual(events[0]['provider_code'],'IGW40043')

    def test_repeated_auth_error_is_not_cached_or_looped(self):
        def failed(*a,**k):raise urllib.error.HTTPError('https://example.invalid',400,'bad',{},io.BytesIO(b'{"rsp_cd":"IGW40044"}'))
        with tempfile.TemporaryDirectory() as tmp,patch.object(market,'token',side_effect=['old','new']),patch('urllib.request.OpenerDirector.open',side_effect=failed) as request,patch.object(market.time,'sleep'):
            with self.assertRaisesRegex(nh_auth.NHError,'IGW40044'):
                market.MarketClient(tmp).read(market.PERIOD,{'iem_cd':'ABC'})
            self.assertEqual(request.call_count,2)
            self.assertEqual(list(Path(tmp).glob('responses/*.json')),[])


if __name__=='__main__':unittest.main()