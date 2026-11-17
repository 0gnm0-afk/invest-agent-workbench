"""C4 official-source boundary: synthetic bytes, unchanged actuals and offline replay."""
import copy
from datetime import date
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from financials.collect import Collector, SourceError
from financials.normalize import dart_normalize, sec_normalize
from financials.providers.official import FinancialPeriod, OfficialFinancialAdapter
from financials.store import FinancialStore, lock_file, packed
from market.financial_price_receipts import pack_price_input, price_input_bytes, read_price_input


YEAR = date.today().year - 1
STAMP = '2026-11-01T00:00:00Z'


def sec_source():
    annual = [dict(start=f'{y}-01-01', end=f'{y}-12-31', val=100 + y - YEAR,
                   filed=f'{y + 1}-02-01', form='10-K', accn=f'fixture-{y}')
              for y in range(YEAR - 5, YEAR + 1)]
    quarters = [dict(start=f'{YEAR}-{(q - 1) * 3 + 1:02d}-01',
                     end=f'{YEAR}-{q * 3:02d}-' + ('31' if q in (1, 4) else '30'),
                     val=20 + q, filed=f'{YEAR + 1}-02-01', form='10-Q', accn=f'fixture-q{q}')
                for q in range(1, 5)]
    rows = annual + quarters
    facts = {'Revenues': {'units': {'USD': rows}},
             'NetIncomeLoss': {'units': {'USD': [dict(r, val=r['val'] / 10) for r in rows]}},
             'EarningsPerShareDiluted': {'units': {'USD/shares': [dict(r, val=4) for r in rows]}}}
    return dict(provider='SEC', identity=dict(cik='1045810', name='Synthetic Issuer', tickers=['ABC'],
                filings=dict(recent={k: [] for k in ('form', 'reportDate', 'filingDate', 'accessionNumber', 'primaryDocument', 'items')}, files=[])),
                facts=dict(cik='1045810', facts={'us-gaap': facts}))


def dart_source():
    reports = []
    for q, sales in ((3, 30), (4, 150)):
        end = f'{YEAR}-{q * 3:02d}-' + ('31' if q == 4 else '30')
        def row(tag, amount, cumulative=None):
            return dict(corp_code='001', sj_div='IS', account_id=tag, account_nm=tag,
                        currency='KRW', rcept_no=f'{YEAR + 1}0301000001',
                        thstrm_amount=str(amount), thstrm_add_amount=str(cumulative or ''))
        rows = [row('ifrs-full_Revenue', sales, sales * q),
                row('ifrs-full_DilutedEarningsLossPerShare', q, q * q)]
        shares = dict(corp_code='001', se='보통주', stlm_dt=end, distb_stock_co='90',
                      istc_totqy='100', tesstk_co='10', rcept_no=f'{YEAR + 1}0301000001')
        reports.append(dict(year=YEAR, quarter=q, end=end, prefix=f'{YEAR}-Q{q}',
                            fs=dict(status='000', list=rows), shares=dict(status='000', list=[shares])))
    return dict(provider='DART', identity=dict(corp_code='001', stock_code='000001',
                corp_name='합성 발행회사', acc_mt='12'), statement_scope='CFS', reports=reports)


def bodies_for(source):
    if source['provider'] == 'SEC':
        return {'companyfacts.json': packed(source['facts'])}
    return {r['prefix'] + suffix: packed(r[field]) for r in source['reports']
            for field, suffix in (('fs', '-statements.json'), ('shares', '-shares.json'))}


def write_sources(folder, bodies):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    sources = []
    for name, raw in bodies.items():
        (folder / name).write_bytes(raw)
        sources.append(dict(id=name, url='https://issuer.invalid/' + name, fetched_at=STAMP,
                            sha256=hashlib.sha256(raw).hexdigest(), bytes=len(raw)))
    return sources


class ReplaySECCollector(Collector):
    """Run the existing SEC/issuer IR chain; replace only transport and credentials."""
    def __init__(self, folder, progress=lambda stage: None):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.sources = []
        self.progress = progress
        self.config = {}

    def fetch(self, url, name, params=None, sec=False, binary=False):
        source = sec_source()
        payloads = {'tickers.json': {'0': dict(ticker='ABC', cik_str=1045810)},
                    'submissions.json': source['identity'], 'companyfacts.json': source['facts']}
        if name in payloads:
            raw = packed(payloads[name])
        elif name.startswith('split-') and name.endswith('.pdf'):
            raw = b'%PDF-1.4 synthetic issuer split evidence\n' + name.encode('ascii')
        else:
            raise AssertionError('unexpected synthetic request: ' + name)
        item = write_sources(self.folder, {name: raw})[0]
        item['url'] = url
        self.sources.append(item)
        self.progress(name)
        return raw if binary else json.loads(raw)


class OfficialAdapter(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.adapter = OfficialFinancialAdapter()
        self.period = FinancialPeriod.current()

    def assert_provenance(self, bundle, folder):
        ids = {s['id'] for s in bundle['sources']}
        for source in bundle['sources']:
            raw = (folder / source['id']).read_bytes()
            self.assertEqual(source['sha256'], hashlib.sha256(raw).hexdigest())
            self.assertEqual(source['bytes'], len(raw))
            self.assertTrue(source['fetched_at'])
            self.assertNotIn('synthetic-secret', source['url'])
            self.assertNotIn('crtfc_key', source['url'])
        for frame in ('annual', 'quarterly'):
            for row in bundle['normalized'][frame]:
                self.assertTrue({'date', 'label', 'fy', 'fq', 'values'} <= row.keys())
                for cell in row['values'].values():
                    self.assertTrue({'value', 'unit', 'start', 'end', 'filed', 'sources', 'formula', 'reason'} <= cell.keys())
                    self.assertLessEqual(set(cell['sources']), ids)

    def test_sec_and_dart_saved_actuals_match_existing_normalizers(self):
        for key, source, normalizer in (('US:ABC', sec_source(), sec_normalize),
                                        ('KR:000001.KS', dart_source(), dart_normalize)):
            with self.subTest(key=key):
                raw = self.root / key.replace(':', '-')
                sources = write_sources(raw, bodies_for(source))
                before = copy.deepcopy(source)
                expected = normalizer(dict(source, symbol=key.split(':', 1)[1]))
                with patch('financials.providers.official.Collector') as collector, \
                        patch('financials.collect.settings') as credentials:
                    bundle = self.adapter.normalize_saved(key, self.period, source, sources, raw, lambda stage: None)
                    collector.assert_not_called()
                    credentials.assert_not_called()
                self.assertEqual(bundle['normalized'], expected)
                self.assertEqual(source, before)
                self.assert_provenance(bundle, raw)
                if key.startswith('US:'):
                    self.assertEqual(len(bundle['normalized']['annual']), 6)
                    self.assertIsNone(bundle['normalized']['annual'][-1]['values']['operating']['value'])
                    self.assertTrue(bundle['normalized']['annual'][-1]['values']['operating']['reason'])
                else:
                    self.assertEqual(bundle['normalized']['standard'], 'K-IFRS · 연결')
                    self.assertIsNone(bundle['normalized']['quarterly'][-1]['values']['eps']['value'])

    def test_collection_retains_existing_sec_and_issuer_pdf_chain(self):
        old = ReplaySECCollector(self.root / 'old')
        expected_source = old.usa('ABC')
        expected_source['symbol'] = 'ABC'
        with patch('financials.providers.official.Collector', ReplaySECCollector):
            bundle = self.adapter.collect('US:ABC', self.period, self.root / 'adapter', lambda stage: None)
        self.assertEqual(bundle['source'], expected_source)
        self.assertEqual(bundle['normalized'], sec_normalize(expected_source))
        self.assertEqual(bundle['sources'], old.sources)
        pdfs = [s for s in bundle['sources'] if s['id'].endswith('.pdf')]
        self.assertEqual(len(pdfs), 2)
        for source in pdfs:
            self.assertEqual((self.root / 'adapter' / source['id']).read_bytes(),
                             (self.root / 'old' / source['id']).read_bytes())
        self.assert_provenance(bundle, self.root / 'adapter')

    def test_collector_receipt_removes_synthetic_auth_query(self):
        source = sec_source()
        class RequestCollector(Collector):
            def __init__(self, folder, progress):
                self.folder = Path(folder)
                self.folder.mkdir(parents=True, exist_ok=True)
                self.progress, self.config, self.sources = progress, {}, []

            def usa(self, symbol):
                payload = self.fetch('https://issuer.invalid/facts', 'companyfacts.json',
                                     params={'crtfc_key': 'synthetic-secret', 'company': symbol})
                return dict(source, facts=payload)

        with patch('financials.providers.official.Collector', RequestCollector), \
                patch('financials.collect.urllib.request.urlopen') as transport, \
                patch('financials.collect.time.sleep'):
            transport.return_value.__enter__.return_value.read.return_value = packed(source['facts'])
            bundle = self.adapter.collect('US:ABC', self.period, self.root, lambda stage: None)
        self.assertIn('synthetic-secret', transport.call_args.args[0].full_url)
        self.assertEqual(bundle['sources'][0]['url'], 'https://issuer.invalid/facts?company=ABC')
        self.assert_provenance(bundle, self.root)
    def test_sec_explicit_supplement_is_only_saved_normalization(self):
        source = sec_source()
        sources = write_sources(self.root, bodies_for(source))
        with patch('financials.providers.official.Collector') as collector, \
                patch('financials.collect.settings') as credentials:
            bundle = self.adapter.supplement_saved('US:ABC', self.period, source, sources,
                                                  self.root, lambda stage: None)
        collector.assert_not_called()
        credentials.assert_not_called()
        self.assertNotIn('supplemented_at', bundle['source'])
        self.assertEqual(bundle['normalized'], sec_normalize(dict(source, symbol='ABC')))

    def test_unknown_provider_cannot_fall_through_to_sec(self):
        with patch('financials.providers.official.sec_normalize') as sec, \
                patch('financials.providers.official.Collector') as collector:
            with self.assertRaisesRegex(SourceError, 'UNSUPPORTED_FINANCIAL_PROVIDER'):
                self.adapter.normalize_saved('US:ABC', self.period, {'provider': 'UNKNOWN'}, [],
                                             self.root, lambda stage: None)
        sec.assert_not_called()
        collector.assert_not_called()

    def test_collection_preserves_auth_identity_and_transport_errors(self):
        for reason in ('SEC_USER_AGENT_REQUIRED', 'SEC_IDENTITY_MISMATCH', 'HTTP_403:companyfacts.json'):
            with self.subTest(reason=reason), patch('financials.providers.official.Collector') as collector:
                collector.return_value.usa.side_effect = SourceError(reason)
                with self.assertRaisesRegex(SourceError, reason):
                    self.adapter.collect('US:ABC', self.period, self.root, lambda stage: None)
                collector.return_value.usa.assert_called_once_with('ABC')
                collector.return_value.korea.assert_not_called()

    def test_dart_identity_failure_is_not_unsupported_format(self):
        source = dart_source()
        source['reports'][0]['fs']['list'][0]['corp_code'] = 'wrong'
        sources = write_sources(self.root, bodies_for(source))
        with self.assertRaisesRegex(ValueError, 'IDENTITY'):
            self.adapter.normalize_saved('KR:000001.KS', self.period, source, sources,
                                         self.root, lambda stage: None)

    def test_unsupported_business_stays_explicit_and_does_not_normalize(self):
        source = dict(provider='SEC', identity={'name': 'Synthetic Fund'},
                      eligibility=dict(status='unsupported', reason='Synthetic business scope', kind='fund'))
        with patch('financials.providers.official.sec_normalize') as normalize:
            bundle = self.adapter.normalize_saved('US:ABC', self.period, source, [], self.root, lambda stage: None)
        normalize.assert_not_called()
        self.assertEqual(bundle['normalized']['eligibility'], source['eligibility'])
        self.assertEqual(bundle['normalized']['annual'], [])
        self.assertEqual(bundle['normalized']['quarterly'], [])


class SyntheticAdapter:
    def __init__(self):
        self.collected = []
        self.replayed = []
        self.corrupt = None

    def collect(self, key, period, raw_folder, progress):
        self.collected.append((key, period))
        source = sec_source()
        sources = write_sources(raw_folder, bodies_for(source))
        bundle = OfficialFinancialAdapter().normalize_saved(key, period, source, sources, raw_folder, progress)
        if self.corrupt == 'hash':
            bundle['sources'][0]['sha256'] = '0' * 64
        elif self.corrupt == 'duplicate':
            bundle['sources'].append(dict(bundle['sources'][0], sha256='0' * 64, bytes=1))
        return bundle

    def normalize_saved(self, key, period, source, sources, raw_folder, progress):
        self.replayed.append(key)
        return OfficialFinancialAdapter().normalize_saved(key, period, source, sources, raw_folder, progress)

    def supplement_saved(self, *args):
        raise AssertionError('unexpected supplement')


class ImmediateThread:
    def __init__(self, *, target, args, daemon):
        self.target, self.args = target, args

    def start(self):
        self.target(*self.args)


class StoreAdapterBoundary(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.adapter = SyntheticAdapter()
        self.store = FinancialStore(self.root / 'runtime' / 'financials', adapter=self.adapter)
        self.key = 'US:ABC'
        self.stock = dict(market='US', symbol='ABC', currency='USD', bars=[])
        for target, value in [('financials.store.settings', {}), ('financials.collect.settings', {})]:
            mock = patch(target, return_value=value)
            mock.start()
            self.addCleanup(mock.stop)

    def create(self):
        with patch('financials.store.threading.Thread', ImmediateThread):
            return self.store.start(self.key, self.stock)

    def test_injection_preserves_public_result_and_offline_recalculation(self):
        with patch('financials.providers.official.Collector') as collector, \
                patch('financials.store.pack_price_input', wraps=pack_price_input) as pack:
            response = self.create()
        collector.assert_not_called()
        self.assertEqual(response['state'], 'complete')
        snapshot = self.store.folder(self.key) / 'snapshots' / response['result']['snapshot_id']
        manifest = json.loads((snapshot / 'manifest.json').read_bytes())
        adapter_path = Path(__file__).resolve().parents[1] / 'scripts/financials/providers/official.py'
        self.assertEqual(manifest['code']['financial_official.py'], hashlib.sha256(adapter_path.read_bytes()).hexdigest())
        self.assertEqual(len(self.adapter.collected), 1)
        self.assertEqual(self.adapter.collected[0][0], self.key)
        self.assertEqual(self.adapter.collected[0][1], FinancialPeriod.current())
        pack.assert_called_once()
        self.assertIs(pack.call_args.args[2], self.store.price_store)
        self.assertTrue({'key', 'state', 'attempt', 'result', 'config', 'reused',
                         'calculation_version', 'result_stale'} <= response.keys())
        self.assertEqual(response['result']['schema'], 2)
        self.assertEqual(response['result']['display_range'], {'annual': 5, 'quarterly': 12})
        self.assertEqual(response['result']['annual'][-1]['values']['sales']['value'], 100)
        before = {p.relative_to(self.root).as_posix(): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        with patch.object(self.store.price_store, 'connect') as connect:
            self.assertEqual(self.store.read(self.key), response)
            connect.assert_not_called()
        after = {p.relative_to(self.root).as_posix(): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual(after, before)
        with patch('financials.providers.official.Collector') as collector, \
                patch('financials.store.price_input_bytes', wraps=price_input_bytes) as raw_price, \
                patch('financials.store.read_price_input', wraps=read_price_input) as read_price:
            replay = self.store.recalculate_saved(self.key)
        collector.assert_not_called()
        self.assertEqual(len(self.adapter.collected), 1)
        self.assertEqual(self.adapter.replayed, [self.key])
        self.assertEqual(raw_price.call_count, 1)
        self.assertEqual(read_price.call_count, 1)
        self.assertEqual(replay['state'], 'complete')
        self.assertEqual(replay['result']['annual'], response['result']['annual'])
        self.assertEqual(replay['result']['quarterly'], response['result']['quarterly'])
        self.assertEqual(replay['result']['collected_at'], response['result']['collected_at'])
        self.assertEqual(replay['result']['source_snapshot_id'], response['result']['snapshot_id'])
        self.assertNotEqual(replay['result']['snapshot_id'], response['result']['snapshot_id'])

    def test_bad_hash_or_conflicting_source_id_cannot_publish_or_replace_success(self):
        successful = self.create()
        self.assertEqual(successful['state'], 'complete')
        folder = self.store.folder(self.key)
        pointer = (folder / 'latest.json').read_bytes()
        for corruption in ('hash', 'duplicate'):
            with self.subTest(corruption=corruption):
                self.adapter.corrupt = corruption
                lease = lock_file(folder / 'request.lock')
                with patch('financials.store.publish_snapshot') as publish:
                    self.store._work(self.key, self.stock, lease)
                publish.assert_not_called()
                response = self.store.read(self.key)
                self.assertEqual(response['state'], 'failed')
                self.assertEqual((folder / 'latest.json').read_bytes(), pointer)
                self.assertEqual(response['result']['snapshot_id'], successful['result']['snapshot_id'])

    def test_unsupported_format_retains_raw_source_and_previous_success(self):
        successful = self.create()
        folder = self.store.folder(self.key)
        pointer = (folder / 'latest.json').read_bytes()
        source = sec_source()
        source['facts']['facts'] = {'unknown-taxonomy': {}}
        def collector_factory(raw_folder, progress):
            collector = Mock()
            collector.sources = write_sources(raw_folder, bodies_for(source))
            collector.usa.return_value = source
            return collector
        self.store.adapter = OfficialFinancialAdapter()
        lease = lock_file(folder / 'request.lock')
        with patch('financials.providers.official.Collector', side_effect=collector_factory), \
                patch('financials.store.publish_snapshot') as publish:
            self.store._work(self.key, self.stock, lease)
        publish.assert_not_called()
        response = self.store.read(self.key)
        self.assertEqual(response['attempt']['error'], 'UNSUPPORTED_FINANCIAL_FORMAT')
        self.assertEqual((folder / 'latest.json').read_bytes(), pointer)
        self.assertEqual(response['result']['snapshot_id'], successful['result']['snapshot_id'])
        failed = [p.parent for p in (folder / 'snapshots').glob('*/failure.json')]
        self.assertEqual(len(failed), 1)
        self.assertEqual(json.loads((failed[0] / 'source.json').read_bytes())['facts'], source['facts'])
        sources = json.loads((failed[0] / 'sources.json').read_bytes())
        self.assertEqual(sources[0]['sha256'], hashlib.sha256((failed[0] / 'raw/companyfacts.json').read_bytes()).hexdigest())


if __name__ == '__main__':
    unittest.main()
