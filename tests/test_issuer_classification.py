import copy,hashlib,json,sys,tempfile,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from classification_refresh import apply_issuer_links
from sector_observation import checked,classification_age_issue

class IssuerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.data={'fields':['cik','name','ticker','exchange'],'data':[[99,'Same issuer','DST-A','NYSE'],[99,'Same issuer','SRC','NYSE']]}
        self.source={'market':'US','symbol':'SRC','isin_code':'US2','exchange':'NYSE','group_state':'matched','group_code':'Technology','group_name':'Technology','evidence':[{'source':'verified Finviz'}]}
        self.dest={'market':'US','symbol':'DST.A','isin_code':'US1','exchange':'NYSE','group_state':'unmatched','reason':'finviz_public_evidence_absent'}
        self.identities={tuple(r[k] for k in ('market','symbol','isin_code','exchange')) for r in (self.source,self.dest)}
        for row in (self.source,self.dest):
            row['original_classification']={k:row[k] for k in ('market','symbol','isin_code','exchange')}
            row['original_classification'].update(identity_matched=True,source_security_type='보통주')
        self.link={k:self.dest[k] for k in ('market','symbol','isin_code','exchange')};self.link.update(cik=99,source={k:self.source[k] for k in ('market','symbol','isin_code','exchange')})
        self.sync()
    def sync(self,url='https://www.sec.gov/files/company_tickers_exchange.json',time='2026-10-02T11:00:00+00:00'):
        self.path=self.root/'sec.json';self.path.write_text(json.dumps(self.data))
        self.meta=self.root/'sources.json';self.meta.write_text(json.dumps([{'url':url,'fetched_at':time,'sha256':hashlib.sha256(self.path.read_bytes()).hexdigest()}]))
        self.manifest={'issuer_reference':{'path':str(self.path),'meta_path':str(self.meta),'meta_sha256':hashlib.sha256(self.meta.read_bytes()).hexdigest()},'issuer_links':[self.link]}
    def run_it(self,groups=None):
        return apply_issuer_links(groups or [self.dest,self.source],self.manifest,self.identities,checked,{},'2026-10-02T12:00:00+00:00',classification_age_issue)
    def test_same_issuer_classification_preserves_each_stock(self):
        before=copy.deepcopy(self.dest);out=self.run_it()
        self.assertEqual(out[0]['group_code'],'Technology');self.assertEqual(out[0]['symbol'],'DST.A')
        self.assertEqual(out[0]['isin_code'],'US1');self.assertEqual(self.dest,before)
        self.assertEqual(out[1],self.source);self.assertEqual(out[0]['evidence'][-1]['cik'],99)
    def test_different_issuer_not_merged(self):
        self.data['data'][0][0]=100;self.sync()
        with self.assertRaisesRegex(ValueError,'cik_mismatch'):self.run_it()
    def test_isin_change_not_inherited(self):
        self.link['isin_code']='CHANGED'
        with self.assertRaisesRegex(ValueError,'identity_unverified'):self.run_it()
    def test_preferred_or_unverified_identity_not_inherited(self):
        self.dest['original_classification']['source_security_type']='우선주'
        self.assertEqual(self.run_it()[0]['group_state'],'unmatched')
        self.dest['original_classification'].update(source_security_type='보통주',identity_matched=False)
        self.assertEqual(self.run_it()[0]['group_state'],'unmatched')
    def test_existing_normal_classification_is_preserved(self):
        self.dest.update(group_state='matched',group_code='Energy')
        self.assertEqual(self.run_it()[0],self.dest)
    def test_source_future_or_wrong_domain_rejected(self):
        self.sync(time='2099-01-01T00:00:00+00:00')
        with self.assertRaisesRegex(ValueError,'source_or_time_invalid'):self.run_it()
        self.sync(url='https://example.com/sec')
        with self.assertRaisesRegex(ValueError,'source_or_time_invalid'):self.run_it()
    def test_tampered_source_rejected(self):
        self.path.write_text('{}')
        with self.assertRaisesRegex(ValueError,'input_hash_mismatch'):self.run_it()
    def test_conflicting_sibling_sectors_not_inherited(self):
        sibling=copy.deepcopy(self.source);sibling.update(symbol='OTHER',isin_code='US3',group_code='Energy')
        sibling['original_classification'].update(symbol='OTHER',isin_code='US3')
        self.data['data'].append([99,'Same issuer','OTHER','NYSE']);self.sync()
        self.assertEqual(self.run_it([self.dest,self.source,sibling])[0]['group_state'],'unmatched')

if __name__=='__main__':unittest.main()
