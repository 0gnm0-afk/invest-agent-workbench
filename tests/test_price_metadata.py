from contextlib import closing
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path[:0]=[str(Path(__file__).resolve().parents[1]/'scripts'), str(Path(__file__).parent)]
from market.prices import PriceStore,freeze_snapshot,hydrate_snapshot
from market.history.chart import HistoryStore
from test_prices import series


class PriceMetadataTests(unittest.TestCase):
    def test_run_keeps_nonprice_fields_while_db_seed_only_keeps_price_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            store=PriceStore(root/'ohlcv.sqlite3')
            stock={**series(),'market_cap':123456,'sector':'sample','shares_outstanding':42,'provider_symbol':'AAA',
                   'benchmark':'^GSPC','collected_at':'2026-10-02T20:30:00+00:00','listing_exchange_family':'NYSE',
                   'collection':{'mode':'incremental','full_history_requested_from':'2025-01-01','pages':[{'before':None,'returned_count':2}],
                                 'prior_source':{'series_id':1,'revision':1}}}
            thin=freeze_snapshot({'series':[stock],'universe':[{'symbol':'AAA','market_cap':123456}]},root/'snapshot.json',store)
            restored=hydrate_snapshot(thin)
            self.assertEqual(restored['series'][0]['market_cap'],123456)
            self.assertEqual(restored['series'][0]['sector'],'sample')
            self.assertEqual(restored['series'][0]['bars'],stock['bars'])
            seed=store.daily_seed(stock,stock['bars'][-1]['date'])
            for key in ('market','symbol','provider','provider_symbol','currency','price_basis','isin_code','collected_at',
                        'listing_exchange_family','collection','benchmark','bars'):
                self.assertEqual(seed[key],stock[key])
            for key in ('market_cap','sector','shares_outstanding'):
                self.assertNotIn(key,seed)

    def test_selected_history_metadata_is_readable_without_stock_screening_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            store=PriceStore(root/'ohlcv.sqlite3')
            stock={**series(),'market_cap':99,'listing_exchange_family':'NYSE'}
            history={'state':'complete','reason':'requested_history_received','screening_price_difference':False}
            ref=store.put({**stock,'selected_history':history},source='selected source',kind='selected',
                          metadata={'research_note':'not a price observation'})
            cached=HistoryStore(root/'chart-history.sqlite3').cached(stock)
            self.assertEqual(cached['series']['bars'],stock['bars'])
            self.assertEqual(cached['history']['state'],'complete')
            self.assertEqual(cached['history']['reason'],history['reason'])
            with closing(store.connect()) as db:
                raw=db.execute('SELECT metadata FROM price_observations WHERE revision=?',(ref['revision'],)).fetchone()[0]
            self.assertEqual(json.loads(raw)['selected_history'],history)
            self.assertNotIn('market_cap',json.loads(raw))
            self.assertNotIn('research_note',json.loads(raw))
