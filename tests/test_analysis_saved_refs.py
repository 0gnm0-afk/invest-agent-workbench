"""M05/M06 handoff uses only self-contained synthetic ledgers under OS temp."""
from contextlib import closing
from copy import deepcopy
from functools import partial
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from analysis import digest, ident, packed
from analysis.collaboration import Collaboration, VERSION
from analysis.saved_refs import adopted_plan_refs


class SavedRefsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = self.root / 'synthetic.sqlite3'
        with closing(self.connect()) as db, db:
            db.executescript('''
                CREATE TABLE analysis_cases (id TEXT PRIMARY KEY, stock_key TEXT, payload TEXT);
                CREATE TABLE analysis_snapshots (id TEXT PRIMARY KEY, case_id TEXT, payload TEXT, sha256 TEXT);
                CREATE TABLE analysis_items (id TEXT PRIMARY KEY, case_id TEXT, kind TEXT, payload TEXT);
                CREATE TABLE analysis_collaboration_heads (case_id TEXT PRIMARY KEY, revision INTEGER);
            ''')
        self.collab = Collaboration(connect=self.connect, root=self.root, case=self.case,
            snapshot=self.snapshot, item=self.item, rows=self.rows, snapshots=lambda cid: [],
            capture=lambda *a: self.fail('capture forbidden'), links=lambda *a: {})
        self.cid, self.sid = self.make_case()

    def tearDown(self):
        self.temp.cleanup()

    def connect(self):
        return sqlite3.connect(self.path)

    def readonly(self):
        return sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True)

    def decode(self, payload, expected):
        value = json.loads(payload)
        if digest(value) != expected:
            raise ValueError('synthetic_snapshot_integrity_failed')
        return value

    def case(self, cid):
        with closing(self.connect()) as db:
            return json.loads(db.execute('SELECT payload FROM analysis_cases WHERE id=?', (cid,)).fetchone()[0])

    def snapshot(self, cid, sid):
        with closing(self.connect()) as db:
            row = db.execute('SELECT payload,sha256 FROM analysis_snapshots WHERE case_id=? AND id=?', (cid, sid)).fetchone()
            return self.decode(*row)

    def rows(self, table, cid=None):
        with closing(self.connect()) as db:
            return [json.loads(r[0]) for r in db.execute('SELECT payload FROM ' + table + (' WHERE case_id=?' if cid else ''), (cid,) if cid else ())]

    def item(self, cid, rid):
        return next((r for r in self.rows('analysis_items', cid) if r['id'] == rid), None)

    def make_case(self, stock='US:SYNTH'):
        cid, sid = ident('an_'), ident('sn_')
        market, symbol = stock.split(':')
        bars = [dict(date=d, open=100, high=112, low=90, close=105, volume=1000)
                for d in ('2026-09-18', '2026-09-21', '2026-09-22')]
        drawing = dict(id='user_line', kind='trend', points=[dict(time='2026-09-18', price=90),
            dict(time='2026-09-22', price=95)], color='#218775', width=2, extend=True)
        chart = dict(market=market, symbol=symbol, name='Synthetic', currency='USD',
            isin_code='US0000000001', exchange='NASDAQ', price_basis='adjusted', timeframe='1d', bars=bars)
        snap = dict(id=sid, case_id=cid, stock_key=stock, captured_at='2026-09-23T00:00:00Z',
            chart=chart, context={'timeframe': '1d'},
            workspace={'revision': 7, 'updated_at': '2026-09-22T00:00:00Z', 'state': {'drawings': [drawing]}},
            accounts={'as_of': '2026-09-22', 'sensitive_fixture': 'DO-NOT-PROJECT-ACCOUNTS'}, market={'state': 'saved_only'})
        case = dict(id=cid, stock_key=stock, test_record=True)
        with closing(self.connect()) as db, db:
            db.execute('INSERT INTO analysis_cases VALUES (?,?,?)', (cid, stock, packed(case)))
            db.execute('INSERT INTO analysis_snapshots VALUES (?,?,?,?)', (sid, cid, packed(snap), digest(snap)))
        return cid, sid

    def plan(self, previous=None, cid=None, sid=None):
        cid, sid = cid or self.cid, sid or self.sid
        with closing(self.connect()) as db:
            rev = self.collab.revision(cid, db)
        p = dict(title='Synthetic plan', purpose='Review thesis', account_alias=None,
            allocation=dict(quantity=None, basis=None), scenarios=[], conditions=[dict(
                id='line', kind='price', statement='Review saved sloped line', action='review',
                evaluation='daily_close', operator='lt', price=None, currency='USD',
                quantity=dict(value=None, basis=None), priority=1, execution_price=None,
                drawing_ref=dict(drawing_id='user_line', workspace_revision=7, timeframe='1d',
                    price_basis='adjusted', geometry_version='drawing-tools-linear-v1', line='line'))])
        args = dict(analysis_key=cid, snapshot_id=sid, contract_version=VERSION,
            expected_revision=rev, references=[], plan_id=previous['plan_id'] if previous else None,
            expected_plan_version=previous['plan_version'] if previous else 0, plan=p)
        return self.collab.save('plan_save', args, self.case(cid), 'collaboration', '')['record']

    def decide(self, plan, action='adopt'):
        cid = plan['case_id']
        with closing(self.connect()) as db:
            rev = self.collab.revision(cid, db)
        review = self.collab.review(dict(analysis_key=cid, plan_record_id=plan['id'], expected_revision=rev),
            self.case(cid), 'browser', 'fixture-session')
        return self.collab.decide(dict(analysis_key=cid, contract_version=VERSION,
            expected_revision=rev, confirmation=review['confirmation'], action=action, reason='synthetic'),
            self.case(cid), 'browser', 'fixture-session', '')['record']

    def read(self, stock='US:SYNTH', **overrides):
        return adopted_plan_refs(stock, **{'root': self.root, 'connect': self.readonly,
            'snapshot_reader': self.decode, **overrides})

    def fingerprints(self):
        return {p.relative_to(self.root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in self.root.rglob('*') if p.is_file()}

    def rewrite_record(self, record):
        """Model a consistently rewritten but invalid semantic ledger for tests."""
        canonical = {k: v for k, v in record.items() if k not in ('markdown_path', 'markdown_sha256')}
        body = self.collab.markdown(canonical).encode('utf-8')
        (self.root / record['markdown_path']).write_bytes(body)
        record['markdown_sha256'] = hashlib.sha256(body).hexdigest()
        with closing(self.connect()) as db, db:
            db.execute('UPDATE analysis_items SET payload=? WHERE id=?', (packed(record), record['id']))

    def test_adopted_exact_version_draft_excluded_and_read_only(self):
        first = self.plan()
        adoption = self.decide(first)
        second = self.plan(previous=first)
        # A proposal in the same ledger never becomes a plan or adopted drawing.
        rid = ident('it_')
        with closing(self.connect()) as db, db:
            db.execute('INSERT INTO analysis_items VALUES (?,?,?,?)', (rid, self.cid, 'proposal',
                packed(dict(id=rid, case_id=self.cid, kind='proposal', drawings=[{'id': 'ai_unadopted'}]))))
            logical_before = '\n'.join(db.iterdump())
        before = self.fingerprints()
        with (patch('analysis.service.AnalysisService.__init__', side_effect=AssertionError('constructor')),
                patch('analysis.collaboration.Collaboration.migrate', side_effect=AssertionError('migration')),
                patch('accounts.store.AccountStore.save', side_effect=AssertionError('account write')),
                patch('drawings.store.WorkspaceStore.shared', side_effect=AssertionError('shared migration'))):
            packet = self.read()
        self.assertEqual(self.fingerprints(), before)
        with closing(self.connect()) as db:
            self.assertEqual('\n'.join(db.iterdump()), logical_before)
        self.assertEqual(len(packet['adopted_plans']), 1)
        result = packet['adopted_plans'][0]
        self.assertEqual(result['plan_record_id'], first['id'])
        self.assertNotEqual(result['plan_record_id'], second['id'])
        self.assertEqual(result['adoption_record_id'], adoption['id'])
        self.assertEqual(result['plan_sha256'], first['plan_sha256'])
        self.assertEqual(result['snapshot_sha256'], first['snapshot_sha256'])
        self.assertEqual(result['drawing_refs'], [first['plan']['conditions'][0]['drawing_ref']])
        self.assertEqual(result['frozen_drawings'][0]['drawing']['points'][0]['price'], 90)
        self.assertEqual(result['snapshot_basis']['workspace_saved_at'], '2026-09-22T00:00:00Z')
        self.assertNotIn('DO-NOT-PROJECT-ACCOUNTS', json.dumps(packet))
        self.assertNotIn('ai_unadopted', json.dumps(packet))

    def test_replacement_revoke_multiple_cases_and_exact_stock(self):
        first = self.plan(); self.decide(first)
        second = self.plan(previous=first); self.decide(second)
        cid, sid = self.make_case()
        other = self.plan(cid=cid, sid=sid); self.decide(other)
        foreign_cid, foreign_sid = self.make_case('US:OTHER')
        foreign = self.plan(cid=foreign_cid, sid=foreign_sid); self.decide(foreign)
        self.assertEqual({p['plan_record_id'] for p in self.read()['adopted_plans']}, {second['id'], other['id']})
        self.decide(second, 'revoke')
        self.assertEqual([p['plan_record_id'] for p in self.read()['adopted_plans']], [other['id']])
        self.decide(other, 'revoke')
        self.assertEqual(self.read()['adopted_plans'], [])
        self.assertEqual(self.read('US:OTHER')['adopted_plans'][0]['plan_record_id'], foreign['id'])

    def test_empty_missing_unsupported_and_invalid_stock_are_distinct(self):
        before = self.fingerprints()
        self.assertEqual(self.read()['adopted_plans'], [])
        self.assertEqual(self.read('US:ABSENT')['state'], 'available')
        for value in ('SYNTH', '', 'US:', 'KR/OTHER', None):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'exact_stock_key_required'):
                self.read(value)
        missing = self.root / 'missing.sqlite3'
        with self.assertRaises(sqlite3.OperationalError):
            self.read(connect=lambda: sqlite3.connect(missing.as_uri() + '?mode=ro', uri=True))
        self.assertEqual(self.fingerprints(), before)
        with closing(self.connect()) as db, db:
            db.execute('DROP TABLE analysis_collaboration_heads')
        with self.assertRaises(sqlite3.OperationalError):
            self.read()

    def test_snapshot_missing_or_hash_corrupt_fails_without_writes(self):
        plan = self.plan(); self.decide(plan)
        with closing(self.connect()) as db:
            original = db.execute('SELECT payload,sha256 FROM analysis_snapshots WHERE id=?', (self.sid,)).fetchone()
        with closing(self.connect()) as db, db:
            db.execute('UPDATE analysis_snapshots SET sha256=? WHERE id=?', ('bad', self.sid))
        before = self.fingerprints()
        with self.assertRaisesRegex(ValueError, 'snapshot_integrity_failed'):
            self.read()
        self.assertEqual(self.fingerprints(), before)
        with closing(self.connect()) as db, db:
            db.execute('DELETE FROM analysis_snapshots WHERE id=?', (self.sid,))
        with self.assertRaisesRegex(ValueError, 'snapshot_not_found_for_analysis'):
            self.read()

    def test_markdown_missing_and_record_or_adoption_integrity_fail(self):
        plan = self.plan(); adoption = self.decide(plan)
        path = self.root / plan['markdown_path']; original = path.read_bytes()
        path.unlink()
        before = self.fingerprints()
        with self.assertRaisesRegex(ValueError, 'record_markdown_missing'):
            self.read()
        self.assertEqual(self.fingerprints(), before)
        path.write_bytes(original)
        bad = deepcopy(plan); bad['plan_sha256'] = 'bad'; self.rewrite_record(bad)
        with self.assertRaisesRegex(ValueError, 'adopted_refs_plan_integrity_failed'):
            self.read()
        self.rewrite_record(plan)
        bad = deepcopy(adoption); bad['plan_version'] += 1; self.rewrite_record(bad)
        with self.assertRaisesRegex(ValueError, 'adopted_refs_adoption_basis_mismatch'):
            self.read()

    def test_identity_reader_integrity_and_foreign_markdown_fail(self):
        plan = self.plan(); adoption = self.decide(plan)
        bad = deepcopy(plan); bad['identity']['symbol'] = 'OTHER'; self.rewrite_record(bad)
        with self.assertRaisesRegex(ValueError, 'adopted_refs_plan_identity_mismatch'):
            self.read()
        self.rewrite_record(plan)
        def wrong_decoder(payload, expected):
            result = self.decode(payload, expected); result['stock_key'] = 'US:OTHER'; return result
        with self.assertRaisesRegex(ValueError, 'adopted_refs_snapshot_identity_mismatch'):
            self.read(snapshot_reader=wrong_decoder)
        def changed_decoder(payload, expected):
            result = self.decode(payload, expected); result['chart']['bars'][0]['close'] = 1; return result
        with self.assertRaisesRegex(ValueError, 'plan_snapshot_integrity_failed'):
            self.read(snapshot_reader=changed_decoder)
        bad = deepcopy(adoption); bad['markdown_path'] = '../outside.md'
        with closing(self.connect()) as db, db:
            db.execute('UPDATE analysis_items SET payload=? WHERE id=?', (packed(bad), bad['id']))
        with self.assertRaisesRegex(ValueError, 'adopted_refs_markdown_path_mismatch'):
            self.read()

    def test_single_read_transaction_and_query_only_defense(self):
        plan = self.plan(); self.decide(plan)
        connections, statements = [], []
        def supplied():
            connection = self.connect()
            connection.set_trace_callback(statements.append)
            connections.append(connection)
            return connection
        self.read(connect=supplied)
        self.assertEqual(len(connections), 1)
        self.assertEqual(statements.count('BEGIN'), 1)
        before = self.fingerprints()
        def cannot_write(payload, expected):
            with self.assertRaises(sqlite3.OperationalError):
                connections[-1].execute('DELETE FROM analysis_items')
            return self.decode(payload, expected)
        self.read(connect=supplied, snapshot_reader=cannot_write)
        self.assertEqual(self.fingerprints(), before)

    def test_revoked_history_missing_or_wrong_basis_cannot_be_empty_success(self):
        first = self.plan(); adoption = self.decide(first)
        second = self.plan(previous=first)
        revoke = self.decide(first, 'revoke')
        self.assertEqual(self.read()['adopted_plans'], [])
        bad = deepcopy(revoke); bad['plan_record_id'] = ident('it_'); self.rewrite_record(bad)
        before = self.fingerprints()
        with self.assertRaisesRegex(ValueError, 'adopted_refs_decision_plan_missing'):
            self.read()
        self.assertEqual(self.fingerprints(), before)
        bad = deepcopy(revoke)
        bad.update({field: second[field] for field in ('plan_id', 'plan_version', 'plan_sha256', 'snapshot_id', 'snapshot_sha256')})
        bad['plan_record_id'] = second['id']; self.rewrite_record(bad)
        with self.assertRaisesRegex(ValueError, 'adopted_refs_invalid_revocation_history'):
            self.read()
        self.rewrite_record(revoke)
        bad = deepcopy(adoption); bad['plan_sha256'] = 'bad'; self.rewrite_record(bad)
        with self.assertRaisesRegex(ValueError, 'adopted_refs_adoption_basis_mismatch'):
            self.read()
        self.rewrite_record(adoption)
        # Even a fully revoked version retains required immutable evidence.
        with closing(self.connect()) as db, db:
            db.execute('DELETE FROM analysis_snapshots WHERE id=?', (self.sid,))
        with self.assertRaisesRegex(ValueError, 'snapshot_not_found_for_analysis'):
            self.read()

    def test_reference_backed_snapshot_decoder_preserves_frozen_evidence_without_writes(self):
        from accounts.store import AccountStore
        from analysis.snapshots import decode_saved
        from market.prices import PriceStore
        snap = self.snapshot(self.cid, self.sid)
        prices = PriceStore(self.root / 'runtime/ohlcv.sqlite3', root=self.root)
        reference = prices.put(snap['chart'], source='synthetic-original')
        # Use the store's canonical numeric representation in the frozen input.
        snap['chart']['bars'] = prices.read(reference)
        snap['component_hashes'] = {'chart': digest(snap['chart']), 'accounts': digest(snap['accounts'])}
        with closing(self.connect()) as db, db:
            db.execute('UPDATE analysis_snapshots SET payload=?,sha256=? WHERE id=?',
                (packed(snap), digest(snap), self.sid))
        plan = self.plan(); self.decide(plan)
        account_reference = AccountStore(self.root / 'runtime/accounts.sqlite3').save(snap['accounts'])
        stored = deepcopy(snap)
        stored['chart'].pop('bars')
        stored['chart_price_ref'] = {'path': str(prices.path), 'reference': reference, 'frame': '1d'}
        stored['accounts'] = {'account_store_ref': account_reference}
        with closing(self.connect()) as db, db:
            db.execute('UPDATE analysis_snapshots SET payload=?,sha256=? WHERE id=?',
                (packed(stored), digest(stored), self.sid))
        # A later provider correction must not replace the adopted input.
        current = deepcopy(snap['chart']); current['bars'][0]['close'] = 101.0
        prices.put(current, source='synthetic-correction')
        # A completed synthetic fixture has no WAL coordination-file lifecycle.
        # Active-WAL snapshot consistency is exercised in test_analysis instead.
        for path in (prices.path, self.root / 'runtime/accounts.sqlite3'):
            with closing(sqlite3.connect(path)) as db:
                db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
                self.assertEqual(db.execute('PRAGMA journal_mode=DELETE').fetchone()[0], 'delete')
        before = self.fingerprints()
        with (patch.object(AccountStore, 'connect', side_effect=AssertionError('account schema write')),
                patch.object(AccountStore, 'save', side_effect=AssertionError('account save')),
                patch('analysis.service.AnalysisService.__init__', side_effect=AssertionError('service initialization'))):
            packet = self.read(snapshot_reader=partial(decode_saved, root=self.root))
        self.assertEqual(self.fingerprints(), before)
        result = packet['adopted_plans'][0]
        self.assertEqual(result['snapshot_sha256'], digest(snap))
        self.assertEqual(result['plan_sha256'], plan['plan_sha256'])
        self.assertEqual(result['snapshot_basis']['chart'], snap['chart'])
        self.assertEqual(result['snapshot_basis']['chart']['bars'][0]['close'], 105.0)
        self.assertEqual(result['frozen_drawings'][0]['drawing'], snap['workspace']['state']['drawings'][0])
        self.assertNotIn('DO-NOT-PROJECT-ACCOUNTS', json.dumps(packet))


if __name__ == '__main__':
    unittest.main()
