import io
import json
import sys
import subprocess
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import toss_api as api


class TokenTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'auth/token_cache.json'
        p=patch.object(api,'TOKEN_CACHE',self.path);p.start();self.addCleanup(p.stop)
        p=patch.object(api,'load_env',return_value={'TOSS_CLIENT_ID':'fixture','TOSS_CLIENT_SECRET':'fixture'});p.start();self.addCleanup(p.stop)
        self.calls=0
    def saved(self,token='old',seconds=3600):
        self.path.parent.mkdir(exist_ok=True)
        self.path.write_text(json.dumps({'access_token':token,'expires_at':time.time()+seconds}))
    def issue(self,*args,**kwargs):
        self.calls+=1
        return io.BytesIO(json.dumps({'access_token':'new','expires_in':86400,'token_type':'Bearer'}).encode())
    def test_valid_cache_no_issuance_and_no_rewrite(self):
        self.saved();before=self.path.read_bytes()
        with patch.object(api.urllib.request,'urlopen',side_effect=AssertionError('network')):
            self.assertEqual(api.get_token(),'old');self.assertEqual(api.get_token(),'old')
        self.assertEqual(self.path.read_bytes(),before)
    def test_near_expiry_refreshes_then_reuses(self):
        self.saved(seconds=30)
        with patch.object(api.urllib.request,'urlopen',side_effect=self.issue):
            self.assertEqual(api.get_token(),'new');self.assertEqual(api.get_token(),'new')
        self.assertEqual(self.calls,1)
    def test_401_forces_refresh_but_late_rejection_reuses_newer_cache(self):
        self.saved()
        with patch.object(api.urllib.request,'urlopen',side_effect=self.issue):
            self.assertEqual(api.get_token(rejected_token='old'),'new')
            self.assertEqual(api.get_token(rejected_token='old'),'new')
        self.assertEqual(self.calls,1)
    def test_failed_refresh_preserves_existing_cache(self):
        self.saved(seconds=-1);before=self.path.read_bytes()
        with patch.object(api.urllib.request,'urlopen',side_effect=OSError('unavailable')):
            with self.assertRaises(OSError):api.get_token()
        self.assertEqual(self.path.read_bytes(),before)
    def test_invalid_metadata_never_published(self):
        self.saved(seconds=-1);before=self.path.read_bytes()
        for lifetime in (None,True,0,60,float('inf')):
            with self.subTest(lifetime=lifetime), patch.object(api.urllib.request,'urlopen',return_value=io.BytesIO(json.dumps({'access_token':'new','expires_in':lifetime,'token_type':'Bearer'}).encode())):
                with self.assertRaisesRegex(ValueError,'token_metadata_invalid'):api.get_token()
            self.assertEqual(self.path.read_bytes(),before)
    def test_concurrent_rejected_token_issues_only_once(self):
        self.saved();barrier=threading.Barrier(8)
        def call(_):barrier.wait(5);return api.get_token(rejected_token='old')
        with patch.object(api.urllib.request,'urlopen',side_effect=self.issue),ThreadPoolExecutor(max_workers=8) as pool:
            self.assertEqual(list(pool.map(call,range(8))),['new']*8)
        self.assertEqual(self.calls,1)
        self.assertFalse(list(self.path.parent.glob('*.tmp')))
    def test_independent_processes_share_one_refresh(self):
        self.saved(seconds=-1)
        program = """
import io,json,sys,time
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,sys.argv[1])
import toss_api as api
api.TOKEN_CACHE=Path(sys.argv[2])
start=Path(sys.argv[3]);issued=Path(sys.argv[4])
limit=time.monotonic()+10
while not start.exists():
    if time.monotonic()>limit:raise RuntimeError('fixture_timeout')
    time.sleep(.02)
def issue(*args,**kwargs):
    with issued.open('a') as f:f.write('issued\\n')
    time.sleep(.1)
    return io.BytesIO(b'{"access_token":"fixture-shared","expires_in":86400,"token_type":"Bearer"}')
with patch.object(api,'load_env',return_value={'TOSS_CLIENT_ID':'fixture','TOSS_CLIENT_SECRET':'fixture'}),patch.object(api.urllib.request,'urlopen',side_effect=issue):
    assert api.get_token()=='fixture-shared'
"""
        start=Path(self.temp.name)/'start';issued=Path(self.temp.name)/'issued'
        processes=[subprocess.Popen([sys.executable,'-B','-c',program,str(Path(api.__file__).parent),str(self.path),str(start),str(issued)],stdout=subprocess.PIPE,stderr=subprocess.PIPE) for _ in range(4)]
        try:
            start.touch()
            for process in processes:
                _,error=process.communicate(timeout=20)
                self.assertEqual(process.returncode,0,error.decode(errors='replace'))
        finally:
            for process in processes:
                if process.poll() is None:process.kill();process.wait()
        self.assertEqual(issued.read_text().count('issued'),1)

    def test_market_client_refreshes_during_same_collection_before_expiry(self):
        from toss_market_client import TossMarketClient
        clock=[1000.0];used=[]
        self.path.parent.mkdir()
        self.path.write_text(json.dumps({'access_token':'old','expires_at':1200.0}))
        def opening(request,**kwargs):
            if request.full_url.endswith('/oauth2/token'):return self.issue(request,**kwargs)
            used.append(request.get_header('Authorization'))
            response=io.BytesIO(b'{"result":{"candles":[]}}');response.headers={}
            return response
        with patch.object(api.time,'time',side_effect=lambda:clock[0]),patch.object(api.urllib.request,'urlopen',side_effect=opening):
            client=TossMarketClient()
            with patch.object(client,'_wait'):
                client('/api/v1/candles',{'symbol':'FIRST'})
                clock[0]=1190.0
                client('/api/v1/candles',{'symbol':'LATER'})
        self.assertEqual(used,['Bearer old','Bearer new']);self.assertEqual(self.calls,1)

    def test_malformed_cache_recovers(self):
        self.path.parent.mkdir();self.path.write_text('invalid')
        with patch.object(api.urllib.request,'urlopen',side_effect=self.issue):
            self.assertEqual(api.get_token(),'new')

if __name__=='__main__':unittest.main()
