from contextlib import closing
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from accounts.store import AccountStore
from accounts import account_view, publish_receipt
from atomic_io import load_bom


class AccountStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.store=AccountStore(self.root/'runtime/accounts.sqlite3')
        self.snapshot={'provider':'NH PLUG','as_of':'2026-11-01T01:00:00+00:00',
                       'accounts':[{'alias':'nh-example','positions':[],'holdings_complete':True}],
                       'accounts_complete':True}

    def complete(self, claim, snapshot=None):
        return self.store.complete('nh',claim['request_id'],claim['lease'],
                 metadata={'query_state':'received'},snapshot=snapshot or self.snapshot)

    def test_api_and_mcp_share_one_pending_query_and_idempotent_receipt(self):
        api=self.store.begin('nh','daily-1','api');self.assertTrue(api['execute'])
        mcp=self.store.begin('nh','manual-2','mcp');self.assertFalse(mcp['execute'])
        self.assertEqual(mcp['request_id'],'daily-1')
        receipt=self.complete(api)
        again=self.store.begin('nh','daily-1','mcp')
        self.assertFalse(again['execute']);self.assertEqual(again['result'],receipt)
        self.assertEqual(self.store.read(receipt['snapshot_ref']),self.snapshot)
        self.assertTrue(self.store.begin('meritz','other-provider','mcp')['execute'])

    def test_failed_attempt_does_not_make_past_snapshot_fresh(self):
        receipt=self.complete(self.store.begin('nh','good','api'))
        bad=self.store.begin('nh','bad','mcp')
        self.store.complete('nh','bad',bad['lease'],metadata={'query_state':'failed','reason':'transport_failure'})
        self.assertEqual(self.store.latest('nh'),receipt)
        self.assertFalse(self.store.begin('nh','bad','api')['execute'])

    def test_reject_credentials_invalid_lease_and_missing_data(self):
        claim=self.store.begin('nh','one','mcp')
        with self.assertRaisesRegex(ValueError,'lease_invalid'):
            self.store.complete('nh','one','wrong',metadata={'query_state':'received'},snapshot=self.snapshot)
        with self.assertRaisesRegex(ValueError,'required'):
            self.store.complete('nh','one',claim['lease'],metadata={'query_state':'received'})
        with self.assertRaisesRegex(ValueError,'credentials_forbidden'):
            self.store.save({'data':[{'access_token':'do-not-store'}]})
        with self.assertRaisesRegex(ValueError,'provider_mismatch'):
            self.store.complete('nh','one',claim['lease'],metadata={'query_state':'received'},
                                snapshot={**self.snapshot,'provider':'Meritz Open API'})

    def test_object_dedup_and_integrity(self):
        ref=self.store.save(self.snapshot);self.assertEqual(ref,self.store.save(self.snapshot))
        with closing(self.store.connect()) as db,db:
            self.assertEqual(db.execute('SELECT count(*) FROM account_objects').fetchone()[0],1)
            db.execute("UPDATE account_objects SET payload='{}'")
        with self.assertRaisesRegex(ValueError,'integrity_failed'):self.store.read(ref)

    def test_api_adapter_and_saved_dashboard_write_references(self):
        calls=[]
        def read(path, inputs, continuation=None):
            calls.append(path)
            return ({'Output_0':[{'acct_type':'01','acct_no':'SYNTH_ACCOUNT_254aa248acb4'}]} if path=='/n2/acctinfo'
                    else {'Output_0':{},'Output_1':[],'rsp_msg':'OK'}),{}
        def normalize(raw, listings):
            return {**self.snapshot,'as_of':raw['as_of'],
                    'accounts':[{'alias':raw['accounts'][0]['alias'],'positions':[],'holdings_complete':True}]}
        modules={'nh_snapshot':SimpleNamespace(normalize=normalize),
                 'nh_collect':SimpleNamespace(pages=lambda p,i,r:[r(p,i)[0]]),
                 'nh_api':SimpleNamespace(read=read,NHError=RuntimeError),
                 'nh_diagnostics':SimpleNamespace(successful=lambda v:True)}
        outputs=[self.root/'runtime/dashboard'/name for name in ('one','two','three')]
        for out in outputs:out.mkdir(parents=True)
        config={'mother':str(self.root/'mother')}
        with patch.dict(sys.modules,modules):
            first=account_view(config,outputs[0],request_id='daily-1');publish_receipt(outputs[0],first)
            second=account_view(config,outputs[1],request_id='daily-1');publish_receipt(outputs[1],second)
            saved=account_view(config,outputs[2],refresh=False);publish_receipt(outputs[2],saved)
        self.assertEqual(len(calls),3)  # acctinfo + KR/US balances, not three refreshes.
        self.assertEqual(first['as_of'],second['as_of']);self.assertEqual(saved['as_of'],first['as_of'])
        self.assertEqual(load_bom(outputs[2]/'account-view.json')['accounts'],first['accounts'])
        self.assertNotIn('positions',(outputs[2]/'account-snapshot.private.json').read_text(encoding='utf-8'))


if __name__ == '__main__':unittest.main()
