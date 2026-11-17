"""M08 acceptance contracts: synthetic SQLite and broker response data only."""
import builtins
from contextlib import closing, contextmanager
from copy import deepcopy
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from accounts import account_view, refresh_account_snapshot, record
from accounts.brokers import meritz_next, normalize_meritz, normalize_nh, query_state
from accounts.saved import ReadOnlyAccountStore, saved_accounts, session_projection
from accounts.store import AccountStore, digest, packed


NH_STAMP = '2026-11-01T01:00:00+00:00'
MERITZ_STAMP = '2026-11-02T03:00:00+00:00'


def nh_observation():
    def page(rows):
        return {'rsp_msg': '조회가 완료되었습니다.', 'Output_0': {'dca': '1000.000'},
                'Output_1': rows, '_pagination': {'cts_flag': 'N', 'cts': None, 'request_cts': None}}
    return {'as_of': NH_STAMP, 'accounts_complete': True, 'accounts': [{
        'alias': 'nh-synthetic', 'markets': {
            'KR': {'state': 'received', 'pages': [page([{
                'iem_cd': '005930', 'iem_nm': '합성 국내', 'itg_bnc_qty': '2',
                'now_pr': '10000', 'phs_pr': '8000'}])]},
            'US': {'state': 'received', 'pages': [page([{
                'iem_cd': 'SYN', 'iem_nm': '합성 미국', 'cur_cd': 'USD',
                'fc_sec_trd_nat_cd': '200', 'cns_bse_bnc_qty': '0.1000000001',
                'fc_sec_end_pr': '100.25', 'fc_avg_phs_pr': '80.125',
                'tdt_sby_bse_xcg_rt': '1400.0000'}])]}}}]}


def meritz_observation():
    def page(data):
        return {'rsp_cd': '5766', 'tr_cont': '0', 'rsp_msg': '조회완료', 'data': data}
    return {'as_of': MERITZ_STAMP, 'accounts_complete': True, 'accounts': [{
        'alias': 'meritz-synthetic', 'responses': {
            'holdings': [page([{'pros_trgt_cls_code': '1', 'mksc_cls_name': '주식일반',
                'iscd': 'A005930', 'isnm': '합성 국내', 'rmnd_qty': '2',
                'psnt_prc': '10000', 'byng_unpr': '8000'}])],
            'ovs_holdings': [page([{'ovrs_stck_symb_iscd': 'SYN', 'iscd': 'SYN.OQ',
                'isnm': '합성 미국', 'ovrs_exch_vndr_idno': '0537', 'crcd': 'USD',
                'rmnd_clcd': '1', 'rmnd_dqty': '0.SYNTH_ACCOUNT_c775e7b757ed', 'psnt_prc': '100.25',
                'avrg_unpr': '80.125'}])],
            'deposit': [page({'tfam': '1000.000', 'd1_aft_tfam': '2000', 'd2_aft_tfam': '3000'})],
            'valuation': [page({'aset_vltn_amt': '999999', 'loan_amt': '100'})],
            'ovs_currencies': [page([{'crcd': 'USD', 'frcr_amt': '10.0100',
                'estm_frcr_amt': '11', 'vltn_exrt': '1500.0000', 'excl_wncr_vltn_amt': '16500'}])]}}]}


@contextmanager
def no_transport():
    original = builtins.__import__
    calls = []
    def checked(name, *args, **kwargs):
        if name.split('.')[0] in {'nh_api', 'nh_master', 'nh_auth', 'win32crypt'}:
            calls.append(name)
            raise AssertionError('saved account access imported a broker or credential adapter')
        return original(name, *args, **kwargs)
    with patch('builtins.__import__', side_effect=checked):
        yield calls


def files_under(root):
    # SQLite may create/adjust shared-memory locks during mode=ro WAL reads.
    # Ignore only that cache and an empty WAL; persistent WAL and DB bytes count.
    files = {}
    for path in root.rglob('*'):
        if not path.is_file() or path.name.endswith('-shm'):
            continue
        value = path.read_bytes()
        if path.name.endswith('-wal') and not value:
            continue
        files[path.relative_to(root).as_posix()] = value
    return files


def schema_at(path):
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
        return db.execute('SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY name').fetchall()


class AccountObservationContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = AccountStore(self.root / 'runtime/accounts.sqlite3')

    def complete(self, provider, snapshot, request='first', store=None):
        store = store or self.store
        claim = store.begin(provider, request, 'mcp')
        return store.complete(provider, request, claim['lease'],
            metadata={'query_state': query_state(snapshot)}, snapshot=snapshot)

    def test_same_request_and_provider_lock(self):
        """A08-01: pending ownership never expires merely because it is old."""
        reader = Mock()
        first = self.store.begin('nh', 'same', 'mcp')
        if first['execute']:
            reader('nh')
        with closing(self.store.connect()) as db, db:
            db.execute("UPDATE account_queries SET started_at='2000-01-01T00:00:00+00:00'")
        for request, origin in [('same', 'mcp'), ('other', 'api')]:
            claim = self.store.begin('nh', request, origin)
            self.assertFalse(claim['execute'])
            self.assertNotIn('lease', claim)
            self.assertEqual(claim['request_id'], 'same')
            if claim['execute']:
                reader('nh')
        other = self.store.begin('meritz', 'same', 'mcp')
        self.assertTrue(other['execute'])
        if other['execute']:
            reader('meritz')
        self.assertEqual(reader.call_count, 2)
        snapshot = normalize_nh(nh_observation())
        receipt = self.store.complete('nh', 'same', first['lease'],
            metadata={'query_state': 'received'}, snapshot=snapshot)
        repeated = self.store.begin('nh', 'same', 'api')
        self.assertFalse(repeated['execute'])
        self.assertEqual(repeated['result'], receipt)
        self.assertNotIn('lease', repeated)

    def test_failed_attempt_keeps_original_observation(self):
        """A08-02: last success and last completed attempt are separate facts."""
        snapshot = normalize_nh(nh_observation())
        receipt = self.complete('nh', snapshot)
        failed = self.store.begin('nh', 'failed', 'mcp')
        result = self.store.complete('nh', 'failed', failed['lease'], metadata={
            'query_state': 'failed', 'reason': 'transport_failure',
            'attempted_at': '2026-11-03T04:00:00+00:00'})
        self.assertEqual(self.store.latest('nh'), receipt)
        self.assertEqual(self.store.read(receipt['snapshot_ref']), snapshot)
        self.assertEqual(self.store.latest_attempt('nh'), result)
        self.assertIsNone(result['snapshot_ref'])
        self.assertIsNone(result['raw_ref'])
        self.assertFalse(self.store.begin('nh', 'failed', 'api')['execute'])
        view = saved_accounts(self.root)
        self.assertEqual(view['as_of'], NH_STAMP)
        self.assertEqual(view['attempted_at'], result['attempted_at'])
        self.assertEqual(view['query_state'], 'failed')
        self.assertEqual(view['reason'], 'transport_failure')
        self.assertEqual(view['accounts'][0]['positions'], snapshot['accounts'][0]['positions'])
        self.assertEqual(view['source_mode'], 'historical_saved')

    def test_partial_and_confirmed_empty_are_distinct(self):
        """A08-03: business success plus terminal page evidence confirms absence."""
        complete = normalize_nh(nh_observation())
        self.assertEqual(query_state(complete), 'received')
        source = nh_observation()
        source['accounts'][0]['markets']['US']['pages'][0].pop('_pagination')
        partial = normalize_nh(source)
        self.assertEqual(query_state(partial), 'partial')
        self.assertFalse(partial['accounts'][0]['holdings_complete'])
        self.assertTrue(partial['accounts'][0]['positions'])
        source = nh_observation()
        for section in source['accounts'][0]['markets'].values():
            section['pages'][0]['Output_1'] = []
        empty = normalize_nh(source)
        self.assertEqual(query_state(empty), 'received')
        self.assertEqual(empty['accounts'][0]['positions'], [])
        self.assertTrue(empty['accounts'][0]['holdings_complete'])
        self.assertFalse(empty['accounts'][0]['cash_complete'])
        source['accounts'][0]['markets']['US']['pages'][0]['rsp_msg'] = '권한 오류'
        self.assertEqual(query_state(normalize_nh(source)), 'partial')
        for confirmed in (True, False):
            empty_list = normalize_nh({'as_of': NH_STAMP, 'accounts': [], 'accounts_complete': confirmed})
            self.assertEqual(query_state(empty_list), 'received' if confirmed else 'partial')
        source = meritz_observation()
        page = source['accounts'][0]['responses']['ovs_holdings'][0]
        page.update(rsp_cd='5762', tr_cont='1', tr_cont_key='next-page')
        self.assertEqual(meritz_next('ovs_holdings', [page]), {'tr_cont': '1', 'tr_cont_key': 'next-page'})
        self.assertEqual(query_state(normalize_meritz(source)), 'partial')
        page.update(rsp_cd='error', tr_cont='0')
        with self.assertRaisesRegex(ValueError, 'business_response_unconfirmed'):
            meritz_next('ovs_holdings', [page])
        self.assertEqual(query_state(normalize_meritz(source)), 'partial')
        source['accounts'][0]['responses']['ovs_holdings'] = [{'rsp_cd': '5820', 'data': []}]
        self.assertEqual(query_state(normalize_meritz(source)), 'received')

    def test_saved_read_has_no_write_or_transport(self):
        """A08-04: saved reads preserve DB bytes/schema and never create a receipt."""
        for providers in [(), ('nh',), ('meritz',), ('nh', 'meritz'), ('nh', 'meritz', 'failed')]:
            with self.subTest(providers=providers):
                root = self.root / ('-'.join(providers) or 'missing')
                root.mkdir()
                store = AccountStore(root / 'runtime/accounts.sqlite3')
                for provider in providers:
                    if provider == 'failed':
                        claim = store.begin('nh', 'failed', 'mcp')
                        store.complete('nh', 'failed', claim['lease'],
                            metadata={'query_state': 'failed', 'reason': 'transport_failure'})
                    else:
                        snapshot = normalize_nh(nh_observation()) if provider == 'nh' else normalize_meritz(meritz_observation())
                        self.complete(provider, snapshot, store=store)
                schema = schema_at(store.path) if store.path.exists() else None
                before = files_under(root)
                with no_transport() as transports, \
                     patch.object(AccountStore, 'connect', side_effect=AssertionError('write connection')), \
                     patch.object(AccountStore, 'save', side_effect=AssertionError('save')), \
                     patch.object(AccountStore, 'begin', side_effect=AssertionError('begin')):
                    view = saved_accounts(root)
                    session_projection(view)
                self.assertEqual(transports, [])
                self.assertEqual(files_under(root), before)
                self.assertEqual(schema_at(store.path) if store.path.exists() else None, schema)
                self.assertFalse((root / 'runtime/dashboard').exists())
                if not providers:
                    self.assertEqual(view['source_mode'], 'missing')
                    self.assertEqual(view['query_state'], 'not_requested')
                    self.assertIsNone(view['as_of'])
                    self.assertFalse(store.path.exists())
                    self.assertEqual(session_projection(view), {
                        'accounts': [], 'as_of': None, 'attempted_at': None,
                        'query_state': 'not_requested', 'reason': 'saved_accounts_unavailable',
                        'source_mode': 'missing', 'stale': True, 'accounts_complete': False,
                        'limitations': []})
                else:
                    self.assertEqual(len(view['accounts']), len(set(providers) - {'failed'}))
        bad_root = self.root / 'malformed'
        (bad_root / 'runtime').mkdir(parents=True)
        (bad_root / 'runtime/accounts.sqlite3').write_bytes(b'not a sqlite database')
        with self.assertRaises(sqlite3.DatabaseError):
            saved_accounts(bad_root)
        with closing(ReadOnlyAccountStore(store.path).connect()) as db:
            self.assertEqual(db.execute('PRAGMA query_only').fetchone()[0], 1)
            with self.assertRaises(sqlite3.OperationalError):
                db.execute('CREATE TABLE forbidden_write(value TEXT)')

    def test_provider_times_precision_and_completeness(self):
        """A08-05: fractional lots and provider-specific rates keep exact text."""
        nh = nh_observation()
        del nh['accounts'][0]['markets']['KR']['pages'][0]['Output_1'][0]['phs_pr']
        meritz = meritz_observation()
        responses = meritz['accounts'][0]['responses']
        lot = responses['ovs_holdings'][0]['data'][0]
        responses['ovs_holdings'][0]['data'].append({**lot, 'rmnd_dqty': '0.0000000010', 'loan_date': '20261101'})
        responses['holdings'][0]['data'].append({'pros_trgt_cls_code': 'unknown', 'mksc_cls_name': '미지원'})
        self.complete('nh', normalize_nh(nh))
        self.complete('meritz', normalize_meritz(meritz))
        internal = saved_accounts(self.root)
        view = session_projection(internal)
        self.assertEqual(view['as_of'], NH_STAMP)
        self.assertEqual(view['providers']['nh']['as_of'], NH_STAMP)
        self.assertEqual(view['providers']['meritz']['as_of'], MERITZ_STAMP)
        self.assertEqual(view['query_state'], 'partial')
        self.assertEqual(set(view), {'accounts', 'as_of', 'attempted_at', 'query_state', 'reason',
            'source_mode', 'stale', 'accounts_complete', 'limitations', 'provider', 'providers', 'valuation'})
        provider_keys = {'as_of', 'attempted_at', 'query_state', 'reason', 'source_mode', 'stale',
                         'accounts_complete', 'limitations', 'valuation'}
        self.assertEqual(set(view['providers']['nh']), provider_keys)
        self.assertEqual(set(view['providers']['meritz']), (provider_keys - {'reason'}) | {'provider'})
        self.assertNotIn('USD', view['valuation']['fx_to_base'])
        accounts = {a['provider']: a for a in view['accounts']}
        self.assertEqual(accounts['nh']['fx_to_base']['USD'], '1400.0000')
        self.assertEqual(accounts['meritz']['fx_to_base']['USD'], '1500.0000')
        self.assertFalse(accounts['nh']['holdings_complete'])
        self.assertFalse(accounts['meritz']['holdings_complete'])
        self.assertFalse(accounts['nh']['cash_complete'])
        self.assertFalse(accounts['meritz']['cash_complete'])
        account_keys = {'alias', 'provider', 'as_of', 'attempted_at', 'query_state', 'stale',
                        'positions', 'cash', 'cash_complete', 'holdings_complete', 'fx_to_base',
                        'observed_deposits', 'observed_balances', 'query_markets'}
        self.assertEqual(set(accounts['nh']), account_keys)
        self.assertEqual(set(accounts['meritz']), account_keys | {'observed_foreign_deposits', 'balance_limitations'})
        self.assertEqual(accounts['meritz']['balance_limitations'], ['unsupported_rows_excluded'])
        nh_positions = {p['market']: p for p in accounts['nh']['positions']}
        meritz_positions = {p['market']: p for p in accounts['meritz']['positions']}
        self.assertEqual(nh_positions['US']['quantity'], '0.1000000001')
        self.assertEqual(meritz_positions['US']['quantity'], '0.1234567900')
        self.assertEqual(meritz_positions['US']['average_cost'], '80.125')
        self.assertEqual(meritz_positions['US']['price'], '100.25')
        self.assertEqual(meritz_positions['US'], {
            'market': 'US', 'symbol': 'SYN', 'quote_symbol': 'SYN', 'currency': 'USD',
            'quantity': '0.1234567900', 'price': '100.25', 'average_cost': '80.125',
            'asset_type': 'stock', 'price_basis': 'broker_last_price_observation',
            'benchmark': '^GSPC', 'observed_evaluation': None, 'observed_fx_rate': None})
        self.assertNotIn('average_cost', nh_positions['KR'])
        self.assertEqual(nh_positions['KR']['data_unavailable_reason'], 'broker_holding_value_or_instrument_unconfirmed')
        self.assertEqual(accounts['meritz']['observed_deposits']['US']['fc_dca'], '10.0100')
        self.assertEqual(accounts['meritz']['observed_deposits']['KR']['dca'], '1000.000')
        self.assertIn('working_orders_not_reconciled', view['limitations'])
        self.assertEqual(len(internal['accounts'][1]['positions'][1]['lots']), 2)
        self.assertIn('unsupported_rows', internal['accounts'][1])

    def test_saved_output_excludes_sensitive_evidence(self):
        """A08-06: legacy evidence remains internal, never crossing the allowlist."""
        clean = normalize_nh(nh_observation())
        receipt = self.complete('nh', clean)
        snapshot = deepcopy(clean)
        marker = 'SENSITIVE_SYNTHETIC_MARKER'
        secret = {'account_number': marker, 'access_token': marker, 'raw_json': marker, 'lease': marker}
        snapshot.update(secret)
        snapshot['raw_ref'] = {'path': marker, 'sha256': marker}
        snapshot['unexpected'] = secret
        account = snapshot['accounts'][0]
        account.update(secret)
        account['observed_balances']['KR']['unexpected'] = secret
        account['observed_deposits']['KR']['unexpected'] = marker
        account['query_markets']['KR']['reason'] = marker
        account['balance_limitations'] = [marker]
        position = account['positions'][0]
        position.update(secret)
        position['name'] = marker
        position['lots'][0].update(secret)
        position['broker_lot'] = secret
        position['unexpected'] = secret
        # Model a legacy DB directly: the live save path correctly forbids auth keys.
        ref = {**receipt['snapshot_ref'], 'sha256': digest(snapshot)}
        metadata = {**receipt, 'snapshot_ref': ref, 'raw_ref': {'path': marker, 'sha256': marker}}
        with closing(self.store.connect()) as db, db:
            db.execute('INSERT INTO account_objects VALUES (?,?)', (ref['sha256'], packed(snapshot)))
            db.execute('UPDATE account_queries SET snapshot_sha256=?,metadata=?', (ref['sha256'], packed(metadata)))
        before = files_under(self.root)
        original_read = ReadOnlyAccountStore.read
        reads = []
        def read(store, reference):
            reads.append(reference['sha256'])
            self.assertEqual(reference['sha256'], ref['sha256'])
            return original_read(store, reference)
        with patch.object(ReadOnlyAccountStore, 'read', read), no_transport():
            internal = saved_accounts(self.root)
            original_internal = deepcopy(internal)
            result = session_projection(internal)
        self.assertEqual(reads, [ref['sha256']])
        self.assertEqual(files_under(self.root), before)
        self.assertEqual(internal, original_internal)
        self.assertEqual(internal['accounts'][0]['positions'][0]['lots'][0]['access_token'], marker)
        serialized = json.dumps(result, sort_keys=True)
        self.assertNotIn(marker, serialized)
        forbidden = {'lots', 'broker_lot', 'raw', 'raw_ref', 'raw_json', 'lease', 'account_number',
                     'acct_no', 'account_no', 'access_token', 'token', 'authorization', 'snapshot_ref'}
        def assert_keys(value):
            if isinstance(value, dict):
                self.assertTrue(forbidden.isdisjoint(value))
                for item in value.values():
                    assert_keys(item)
            elif isinstance(value, list):
                for item in value:
                    assert_keys(item)
        assert_keys(result)
        safe = result['accounts'][0]
        self.assertEqual(safe['alias'], 'nh-synthetic')
        self.assertEqual(safe['positions'][0]['quantity'], '2')
        self.assertEqual(safe['positions'][1]['quantity'], '0.1000000001')
        self.assertEqual(safe['observed_deposits']['KR']['dca'], '1000.000')
        self.assertEqual(set(result), {'accounts', 'as_of', 'attempted_at', 'query_state', 'reason',
            'source_mode', 'stale', 'accounts_complete', 'limitations', 'valuation'})
        self.assertEqual(set(safe['positions'][0]), {'market', 'symbol', 'currency', 'quantity',
            'price', 'average_cost', 'asset_type', 'price_basis'})
        self.assertEqual(safe['query_markets']['KR']['reason'], 'unavailable_reason_redacted')
        for code in ('transport_failure', 'http_503'):
            self.assertEqual(session_projection({'reason': code})['reason'], code)
        # Free text or nested objects under an otherwise allowed key must not pass through.
        poisoned = deepcopy(internal)
        poisoned['reason'] = marker
        poisoned['limitations'] = [marker, {'reason': marker}]
        poisoned['accounts'][0]['alias'] = 'nh-synthetic'
        poisoned['accounts'][0]['positions'][0].update(
            quantity={'raw_json': marker}, currency=marker, symbol=marker + ' ' + marker,
            data_unavailable_reason=marker, price=marker)
        poisoned['valuation']['fx_to_base']['USD'] = {'raw_json': marker}
        projected = json.dumps(session_projection(poisoned), sort_keys=True)
        self.assertNotIn(marker, projected)
        for alias in ('nh-SYNTH_ACCOUNT_254aa248acb4', 'nh-123456-78901'):
            alias_view = deepcopy(internal)
            alias_view['accounts'][0]['alias'] = alias
            result_alias = session_projection(alias_view)['accounts'][0]['alias']
            self.assertIsNone(result_alias)
        # A fresh output must never alias and mutate the saved normalized observation.
        result['accounts'][0]['positions'][0]['quantity'] = '999'
        self.assertEqual(internal, original_internal)

    def test_lease_integrity_and_failed_normalization(self):
        """A08-07: invalid completion and tampered references cannot replace success."""
        snapshot = normalize_nh(nh_observation())
        claim = self.store.begin('nh', 'first', 'mcp')
        for lease, payload, expected in [
            ('wrong', snapshot, 'lease_invalid'),
            (claim['lease'], normalize_meritz(meritz_observation()), 'provider_mismatch')]:
            with self.assertRaisesRegex(ValueError, expected):
                self.store.complete('nh', 'first', lease, metadata={'query_state': 'received'}, snapshot=payload)
        receipt = self.store.complete('nh', 'first', claim['lease'], metadata={'query_state': 'received'}, snapshot=snapshot)
        with self.assertRaisesRegex(ValueError, 'lease_invalid'):
            self.store.complete('nh', 'first', claim['lease'], metadata={'query_state': 'failed'})
        self.assertEqual(self.store.latest('nh'), receipt)
        self.assertEqual(self.store.read(receipt['snapshot_ref']), snapshot)
        corrupt = self.store.save({'fixture': 'before'})
        with closing(self.store.connect()) as db, db:
            db.execute('UPDATE account_objects SET payload=? WHERE sha256=?', (packed({'fixture': 'after'}), corrupt['sha256']))
        with self.assertRaisesRegex(ValueError, 'integrity_failed'):
            ReadOnlyAccountStore(self.store.path).read(corrupt)
        bad = self.store.begin('nh', 'bad-normalization', 'mcp')
        output = io.StringIO()
        with patch.object(record, 'AccountStore', return_value=self.store), \
             patch.object(sys, 'argv', ['record.py', 'complete', '--provider', 'nh', '--request-id', 'bad-normalization']), \
             patch.object(sys, 'stdin', io.StringIO(json.dumps({'lease': bad['lease'], 'observation': {'as_of': NH_STAMP}}))), \
             patch.object(sys, 'stdout', output):
            record.main()
        result = json.loads(output.getvalue())
        self.assertEqual(result['query_state'], 'failed')
        self.assertEqual(result['reason'], 'broker_normalization_failed')
        self.assertIsNone(result['snapshot_ref'])
        self.assertFalse(self.store.begin('nh', 'bad-normalization', 'mcp')['execute'])
        self.assertEqual(self.store.latest('nh'), receipt)

    def test_legacy_entries_never_refresh_broker(self):
        """A08-08: retained entry points ignore refresh and only read stored facts."""
        snapshot = normalize_nh(nh_observation())
        self.complete('nh', snapshot)
        failed = self.store.begin('nh', 'failed', 'mcp')
        self.store.complete('nh', 'failed', failed['lease'], metadata={
            'query_state': 'failed', 'reason': 'transport_failure', 'attempted_at': MERITZ_STAMP})
        empty_root = self.root / 'missing'
        empty_root.mkdir()
        for root, expected_state, stamp in [(self.root, 'failed', NH_STAMP), (empty_root, 'not_requested', None)]:
            before = files_under(root)
            output = root / 'runtime/dashboard/view'
            with no_transport() as imports, \
                 patch.object(AccountStore, 'begin', side_effect=AssertionError('begin')), \
                 patch.object(AccountStore, 'save', side_effect=AssertionError('save')), \
                 patch.object(AccountStore, 'connect', side_effect=AssertionError('write connection')):
                for refresh in (True, False):
                    for persist in (True, False):
                        view = account_view({'mother': str(root / 'never-read')}, output, refresh=refresh,
                                            request_id='ignored', persist_receipt=persist)
                        self.assertEqual(view['query_state'], expected_state)
                        self.assertEqual(view['as_of'], stamp)
                view = refresh_account_snapshot(root, request_id='ignored')
                self.assertEqual(view['query_state'], expected_state)
                self.assertEqual(view['as_of'], stamp)
            self.assertEqual(imports, [])
            self.assertEqual(files_under(root), before)
            self.assertFalse(output.exists())


if __name__ == '__main__':
    unittest.main()
