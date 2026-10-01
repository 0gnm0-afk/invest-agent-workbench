import copy
import hashlib
import http.client
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from saved_us_history import packed
from chart_history import HistoryStore
from chart_workspace import moving_averages
from dashboard import handler_for, ThreadingHTTPServer


def bar(day, price=10):
    return dict(date=day, open=price, high=price+2, low=price-1, close=price+1, volume=100)


class SavedHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.folder = self.root/'us-history/sample'
        self.folder.mkdir(parents=True)
        self.path = self.root/'history.sqlite3'
        self.stock = dict(market='US', symbol='AAA', currency='USD', bars=[bar('2026-09-24')])

    def tearDown(self):
        self.temp.cleanup()

    def fixture(self, rows=None, invalid=None, non_sessions=None, gaps=None, stale=False):
        rows = rows or [bar('1990-01-02'), bar('2016-01-04'), bar('2026-09-30')]
        listing = dict(symbol='AAA', market='US', name='Example')
        data = dict(symbol='AAA', market='US', currency='USD', listing=listing, state='received',
                    requested_end='2026-09-30', provider_end_reached=True, bars=rows, provider='Toss',
                    collected_at='2026-10-01', price_basis='adjusted')
        record = dict(symbol='AAA', state='received', provider_end_reached=True, first=rows[0]['date'],
                      last=rows[-1]['date'], bars=len(rows), invalid_ohlc=invalid or [],
                      non_sessions=non_sessions or [], missing_sessions=gaps or [], latest_session_missing=stale)
        self.series = 'series/'+hashlib.sha256(b'AAA').hexdigest()+'.json'
        files = {'collection.json':dict(end='2026-09-30'), 'universe.json':dict(rows=[listing]),
                 'coverage.json':[record], 'verification.json':dict(integrity='PASS', all_attempted=True, verified=1),
                 self.series:data}
        hashes = {}
        for name, value in files.items():
            if name in ('collection.json','universe.json',self.series):
                value = dict(data=value, sha256=hashlib.sha256(packed(value)).hexdigest())
            path = self.folder/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(packed(value))
            hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        manifest = packed(hashes);(self.folder/'manifest.json').write_bytes(manifest)
        (self.folder.parent/'active.json').write_bytes(packed(dict(collection='sample',manifest_sha256=hashlib.sha256(manifest).hexdigest())))

    def no_network(self):
        self.fail('Saved US history must not create a market client')

    def test_full_range_periods_sma_and_no_network(self):
        self.fixture(); before=copy.deepcopy(self.stock)
        store=HistoryStore(self.path,self.no_network)
        d=store.ensure(self.stock,retry=True)
        self.assertEqual(d['history']['first'],'1990-01-02')
        self.assertEqual(d['history']['last'],'2026-09-30')
        self.assertEqual(d['history']['requested_end'],'2026-09-30')
        self.assertEqual(d['history']['state'],'complete')
        for frame in ('1d','1w','1mo'):
            value=store.view(self.stock,frame)
            self.assertEqual(len(value['bars']),3)
            self.assertEqual(moving_averages(value,[2])['2'][-1]['value'],11)
        self.assertEqual(before,self.stock)
        self.assertFalse(self.path.exists())

    def test_quality_exclusion_is_view_only(self):
        rows=[bar('1990-01-02'),dict(bar('2021-05-05'),open=99),bar('2026-09-27')]
        self.fixture(rows,['2021-05-05'],['2026-09-27'],['2026-09-25'],True)
        original=(self.folder/self.series).read_bytes()
        value=HistoryStore(self.path,self.no_network).ensure(self.stock)
        self.assertEqual(value['history']['state'],'partial')
        self.assertEqual(value['history']['stored_count'],3)
        self.assertEqual(value['history']['daily_count'],1)
        self.assertEqual(value['history']['quality']['missing_sessions'],['2026-09-25'])
        self.assertEqual(original,(self.folder/self.series).read_bytes())

    def test_series_tampering_fails_without_refetch(self):
        self.fixture();store=HistoryStore(self.path,self.no_network)
        (self.folder/self.series).write_text('{}')
        value=store.ensure(self.stock,retry=True)
        self.assertEqual(value['history']['state'],'failed')
        self.assertEqual(value['bars'],self.stock['bars'])
        self.assertFalse(value['history']['can_retry'])

    def test_manifest_tampering_and_unknown_symbol(self):
        self.fixture();store=HistoryStore(self.path,self.no_network)
        value=store.ensure(dict(self.stock,symbol='UNKNOWN'))
        self.assertEqual(value['history']['state'],'unavailable')
        self.assertIsNone(store.saved.packet(dict(self.stock,market='KR')))
        (self.folder/'manifest.json').write_text('{}')
        self.assertEqual(HistoryStore(self.path,self.no_network).ensure(self.stock)['history']['state'],'failed')

    def test_newer_screen_is_explicit(self):
        self.fixture()
        value=HistoryStore(self.path,self.no_network).view(dict(self.stock,bars=[bar('2026-10-01')]))
        self.assertTrue(value['history']['screening_newer_than_history'])
        self.assertEqual(value['history']['state'],'partial')
        self.assertEqual(value['history']['last'],'2026-09-30')

    def test_http_new_symbol_available_without_screening_mutation(self):
        self.fixture();stocks={}
        server=ThreadingHTTPServer(('127.0.0.1',0),handler_for({},stocks,self.root,'token',self.root/'workspace.sqlite3'))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            def get(path):
                conn=http.client.HTTPConnection('127.0.0.1',server.server_port)
                conn.request('GET',path);response=conn.getresponse();value=json.loads(response.read());conn.close()
                self.assertEqual(response.status,200);return value
            self.assertEqual(get('/api/stock/US:AAA')['name'],'Example')
            self.assertEqual(get('/api/chart/US:AAA?timeframe=1mo')['history']['first'],'1990-01-02')
            self.assertEqual(get('/api/averages/US:AAA?periods=2')['2'][-1]['value'],11)
            self.assertEqual(stocks,{})
        finally:
            server.shutdown();server.server_close();thread.join()


if __name__=='__main__': unittest.main()
