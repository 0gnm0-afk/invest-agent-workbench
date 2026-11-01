import hashlib
from pathlib import Path
import sys
import unittest

sys.path[:0]=[str(Path(__file__).resolve().parents[1]/'scripts'),str(Path(__file__).parent)]
import test_saved_us_history as fixture_module
from test_saved_us_history import bar
from market.history.saved import SavedHistory, DBHistory
from market.prices import PriceStore


class DBHistoryTests(unittest.TestCase):
    def test_archive_view_quality_and_daily_composition_equal_original(self):
        fixture=fixture_module.SavedHistoryTests();fixture.setUp()
        self.addCleanup(fixture.doCleanups);self.addCleanup(fixture.tearDown)
        fixture.fixture(rows=[bar('1990-01-02'),dict(bar('2020-01-02'),open=99),bar('2026-09-30')],
                        invalid=['2020-01-02'],listing_values={'isinCode':'USAAA'})
        old=SavedHistory(fixture.root/'us-history/active.json','US')
        store=PriceStore(fixture.root/'ohlcv.sqlite3')
        name='series/'+hashlib.sha256(b'AAA').hexdigest()+'.json'
        raw=old.read(name,wrapped=True)
        ref=store.put(raw,source='archive',kind='archive')
        store.set_meta('archive_US',{'source':str(old.root),'config':old.config,'listings':old.listings,
                       'coverage':old.coverage,'references':{name:[ref,{k:v for k,v in raw.items() if k!='bars'}]}})
        current=dict(market='US',symbol='AAA',currency='USD',price_basis='adjusted',provider='Toss',
                     isin_code='USAAA',bars=[bar('2026-09-30'),bar('2026-10-01')])
        db=DBHistory(store,'US')
        self.assertEqual(db.packet(current),old.packet(current))
        self.assertEqual(db.packet(current)['history']['daily_update_mode'],'merged')
        store.put(current,source='daily-1')
        combined=db.packet(current)
        pin=store.pin({**combined['series'],'history':combined['history']})
        self.assertEqual(store.read(pin),combined['series']['bars'])
        changed={**current,'bars':[dict(bar('2026-09-30'),close=11.5),bar('2026-10-01')]}
        corrected=store.put(changed,source='daily')
        self.assertEqual(store.read(store.pin({**combined['series'],'history':combined['history']})),combined['series']['bars'])
        self.assertEqual(db.packet(changed),old.packet(changed))
        self.assertEqual(db.packet(changed)['history']['daily_update_mode'],'daily_only')
        verified=db.packet({**changed,'bars_ref':corrected})
        self.assertEqual(verified['history']['daily_update_mode'],'merged_with_corrections')
        self.assertEqual(verified['series']['bars'][0]['date'],'1990-01-02')
        self.assertEqual(verified['history']['verified_correction_dates'],['2026-09-30'])
        invalid={**corrected,'sha256':'tampered','numeric_sha256':'tampered'}
        self.assertEqual(db.packet({**changed,'bars_ref':invalid})['history']['daily_update_mode'],'daily_only')
        # Reading the DB no longer depends on opening a legacy price file.
        (old.root/name).rename(old.root/'series-not-used.json')
        self.assertEqual(db.packet(current)['history']['daily_update_mode'],'merged')


if __name__=='__main__':unittest.main()
