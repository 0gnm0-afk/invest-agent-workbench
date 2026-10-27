import hashlib
import io
import urllib.error
import json
import sys
import tempfile
import unittest
from datetime import datetime,timezone,timedelta
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import atomic_io as artifact_io
import sectors.basis_refresh as br
import sectors.basis as sb
import sectors.observation as so
import market.daily as daily


class RefreshTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.evidence=self.root/'evidence';self.output=self.root/'new-run/basis'
        (self.evidence/'sources').mkdir(parents=True)
        self.raw=b'frozen official fixture'
        self.doc=self.evidence/'sources/faq.md';self.doc.write_bytes(self.raw)
        self.now=datetime.now(timezone.utc)
        self.meta=dict(url=sb.FAQ_URL,final_url=sb.FAQ_URL,status=200,sha256=artifact_io.sha(self.doc),observed_at=(self.now-timedelta(days=8)).isoformat())
        artifact_io.write(self.doc.with_suffix('.md.meta.json'),self.meta)
        artifact_io.write(self.evidence/'calculation-basis.json',dict(contract=sb.CONTRACT,kr_reference='saved_universe_equal_weight',documents=[dict(file='faq.md',url=sb.FAQ_URL,sha256=artifact_io.sha(self.doc))]))
    def prepare(self,raw=None):
        return br.prepare(self.evidence,self.output,self.now.isoformat(),fetcher=lambda url:self.raw if raw is None else raw)
    def load(self,path):
        inputs={}
        result=sb.load_basis(self.evidence,artifact_io.checked,artifact_io.load,inputs,datetime.now(timezone.utc).isoformat(),so.age_issue,rechecks=path)
        return result,inputs
    def test_stale_document_rechecked_same_bytes_and_offline_replayed(self):
        before={p:p.read_bytes() for p in self.evidence.rglob('*') if p.is_file()}
        path=self.prepare()
        with patch('socket.socket.connect',side_effect=AssertionError('offline')):
            result,inputs=self.load(path)
        self.assertEqual(result['evidence'][0]['sha256'],self.meta['sha256'])
        self.assertIn('recheck',result['evidence'][0]);self.assertIn(str(path.resolve()),inputs)
        self.assertEqual(before,{p:p.read_bytes() for p in before})
    def test_fresh_document_needs_no_network(self):
        self.meta['observed_at']=self.now.isoformat();artifact_io.write(self.doc.with_suffix('.md.meta.json'),self.meta)
        with patch.object(br,'fetch',side_effect=AssertionError('network')):
            result=br.prepare(self.evidence,self.output,self.now.isoformat(),fetcher=lambda url:(_ for _ in ()).throw(AssertionError('network')))
        self.assertIsNone(result);self.assertFalse(self.output.exists())
    def test_changed_official_bytes_preserved_but_blocked(self):
        with self.assertRaisesRegex(ValueError,'basis_document_changed_review_required'):self.prepare(b'changed official fixture')
        self.assertEqual((self.output/'document-0.md').read_bytes(),b'changed official fixture')
        self.assertEqual(self.doc.read_bytes(),self.raw)
    def test_explicitly_reviewed_revision_is_accepted_and_preserved(self):
        revised=b'reviewed auth wording; same candle semantics'
        revision=(sb.CONTRACT,sb.FAQ_URL,self.meta['sha256'],hashlib.sha256(revised).hexdigest())
        with patch.object(sb,'REVIEWED_REVISIONS',frozenset({revision})):
            path=self.prepare(revised);result,inputs=self.load(path)
        self.assertEqual(result['evidence'][0]['recheck']['sha256'],revision[-1])
        self.assertEqual(self.doc.read_bytes(),self.raw)

    def test_tampered_recheck_bytes_blocked(self):
        path=self.prepare();(path.parent/'document-0.md').write_bytes(b'tampered')
        with self.assertRaisesRegex(ValueError,'hash_mismatch'):self.load(path)
    def test_recheck_future_expired_identity_and_path_blocked(self):
        path=self.prepare();original=artifact_io.load(path)
        for field,value,expected in [('observed_at',(self.now+timedelta(days=1)).isoformat(),'not_current'),('observed_at',(self.now-timedelta(days=8)).isoformat(),'not_current'),('final_url','https://elsewhere.invalid','changed_review'),('file','../../outside.md','outside_root')]:
            receipt=json.loads(json.dumps(original));receipt['documents'][0][field]=value;artifact_io.write(path,receipt)
            with self.subTest(field=field,value=value),self.assertRaisesRegex(ValueError,expected):self.load(path)
    def test_original_hash_tamper_blocks_before_fetch(self):
        self.doc.write_bytes(b'tampered')
        with self.assertRaisesRegex(ValueError,'hash_mismatch'):self.prepare()
    def test_no_recheck_still_rejects_stale_document(self):
        with self.assertRaisesRegex(ValueError,'not_current'):self.load(None)
    def test_unauthorized_url_and_redirect_rejected(self):
        with self.assertRaisesRegex(ValueError,'url_not_allowed'):br.fetch('https://other.invalid')
        with self.assertRaisesRegex(ValueError,'redirect_refused'):br.NoRedirect().redirect_request(None,None,302,'',{},sb.FAQ_URL)
    def test_official_fetch_is_bounded_and_checks_response(self):
        for body,status,url,error in [(b'x'*(br.MAX_BYTES+1),200,sb.FAQ_URL,'size_invalid'),(b'valid',200,'https://other.invalid','identity_mismatch'),(b'valid',201,sb.FAQ_URL,'identity_mismatch')]:
            response=io.BytesIO(body);response.status=status;response.geturl=lambda:url
            with patch.object(br.urllib.request,'build_opener') as opener:
                opener.return_value.open.return_value=response
                with self.assertRaisesRegex(ValueError,error):br.fetch(sb.FAQ_URL)
        with patch.object(br.urllib.request,'build_opener') as opener:
            opener.return_value.open.side_effect=urllib.error.URLError('private detail')
            with self.assertRaisesRegex(ValueError,'^basis_document_refresh_unavailable$'):br.fetch(sb.FAQ_URL)

    def test_refresh_failure_stops_before_market_collection(self):
        seed={'roots':['market','classification',str(self.evidence),'nh']}
        with patch.object(daily,'ROOT',self.root),patch.object(daily,'code_hashes',return_value={}),patch.object(daily,'seed_check',return_value=(seed,{})),patch.object(br,'prepare',side_effect=ValueError('basis_document_refresh_unavailable')),patch.object(daily,'collect') as collection:
            result=daily.run({'engine':str(self.root)})
        self.assertEqual(result['state'],'failed');self.assertEqual(result['stages']['inputs'],'failed')
        collection.assert_not_called()

if __name__=='__main__':unittest.main()
