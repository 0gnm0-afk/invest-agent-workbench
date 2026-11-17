from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from accounts.store import AccountStore, hydrate
from accounts import account_view


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

    def test_saved_account_reference_follows_relocation_without_recreating_old_database(self):
        ref = self.store.save(self.snapshot)
        old = self.root / 'old'
        intermediate = self.root / 'usb'
        (self.root / 'config.json').write_text(json.dumps({'records': {
            'previous_root': str(old), 'previous_roots': [str(intermediate)]}}), encoding='utf-8')
        with patch('runtime_paths.ROOT', self.root):
            for owner in (old, intermediate, self.root):
                historical = {**ref, 'path': str(owner / 'runtime/accounts.sqlite3')}
                self.assertEqual(hydrate({'account_store_ref': historical}), self.snapshot)
        self.assertFalse(old.exists())
        self.assertFalse(intermediate.exists())

    def test_memory_views_and_repeated_refresh_request_do_not_create_receipt_files(self):
        from accounts import refresh_account_snapshot
        receipt = self.complete(self.store.begin('nh', 'one-observation', 'mcp'))
        calls = []
        def read(*args, **kwargs):
            calls.append(args)
            self.fail('Unexpected broker read')
        with closing(sqlite3.connect(self.store.path)) as db:
            counts = [db.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in ('account_objects','account_queries')]
            schema = db.execute('SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY name').fetchall()
        def file_contents():
            # SQLite mode=ro may create a shared-memory index and an empty WAL.
            # Existing WAL transactions, the DB and all exports remain protected.
            result = {}
            for path in self.root.rglob('*'):
                if not path.is_file() or path.name.endswith('-shm'):
                    continue
                content = path.read_bytes()
                if not content and path.name.endswith('-wal'):
                    continue
                result[str(path.relative_to(self.root))] = content
            return result
        files = file_contents()
        original = self.store.path.read_bytes()
        with patch.dict(sys.modules, {'nh_api':SimpleNamespace(read=read,NHError=RuntimeError)}), \
             patch.object(AccountStore, 'begin', side_effect=AssertionError('Unexpected begin')), \
             patch.object(AccountStore, 'save', side_effect=AssertionError('Unexpected save')):
            first = refresh_account_snapshot(self.root, request_id='one-observation')
            self.assertEqual(first['as_of'], self.snapshot['as_of'])
            self.assertEqual(first['attempted_at'], receipt['attempted_at'])
            for _ in range(20):
                again = refresh_account_snapshot(self.root, request_id='one-observation')
                saved = account_view({'mother':str(self.root/'mother')}, self.root/'runtime/dashboard/view',
                                     refresh=False, persist_receipt=False)
                self.assertEqual(saved['accounts'], first['accounts'])
                self.assertEqual(again['as_of'], first['as_of'])
            with closing(sqlite3.connect(self.store.path.as_uri() + '?mode=ro', uri=True)) as db:
                self.assertEqual([db.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in ('account_objects','account_queries')], counts)
                self.assertEqual(db.execute('SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY name').fetchall(), schema)
            self.assertEqual(file_contents(), files)
        self.assertEqual(self.store.path.read_bytes(), original)
        self.assertEqual(calls, [])
        self.assertFalse((self.root/'runtime/dashboard').exists())

    def test_legacy_views_keep_saved_observation_without_writing_references(self):
        receipt = self.complete(self.store.begin('nh', 'session-1', 'mcp'))
        def read(*args, **kwargs):
            self.fail('Unexpected broker read')
        modules={'nh_api':SimpleNamespace(read=read,NHError=RuntimeError)}
        outputs=[self.root/'runtime/dashboard'/name for name in ('one','two','three')]
        config={'mother':str(self.root/'mother')}
        with patch.dict(sys.modules,modules), \
             patch.object(AccountStore, 'begin', side_effect=AssertionError('Unexpected begin')), \
             patch.object(AccountStore, 'save', side_effect=AssertionError('Unexpected save')):
            first=account_view(config,outputs[0],request_id='daily-1')
            second=account_view(config,outputs[1],request_id='daily-1')
            saved=account_view(config,outputs[2],refresh=False)
        self.assertEqual(first['as_of'],second['as_of']);self.assertEqual(saved['as_of'],first['as_of'])
        self.assertEqual(saved['as_of'], self.snapshot['as_of'])
        self.assertEqual(saved['attempted_at'], receipt['attempted_at'])
        self.assertEqual([(a['alias'], a['positions']) for a in saved['accounts']],
                         [(a['alias'], a['positions']) for a in self.snapshot['accounts']])
        self.assertEqual(self.store.read(receipt['snapshot_ref']), self.snapshot)
        self.assertEqual(saved['source_mode'], 'shared_saved')
        self.assertFalse((self.root/'runtime/dashboard').exists())


if __name__ == '__main__':unittest.main()
