import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from contextlib import closing
from copy import deepcopy
from datetime import date, timedelta
from unittest.mock import patch

sys.path[:0]=[str(Path(__file__).resolve().parents[1]/'scripts'),str(Path(__file__).parent)]
from test_prices import series
from market.prices import PriceStore
from market.price_files import PriceFiles
from market.retire_history import HistoricalFiles, physical_sha


class HistoricalPriceTests(unittest.TestCase):
    def test_bulk_cached_reference_commits_pending_write_only_with_independent_verifier(self):
        for independent in (True,False):
            with self.subTest(independent_verifier=independent),tempfile.TemporaryDirectory() as tmp:
                store=PriceStore(Path(tmp)/'ohlcv.sqlite3')
                active=series();store.put(active,source='current')
                historical=deepcopy(active);historical['bars'][0]['close']=10.5
                with closing(store.connect(write=True)) as db:
                    files=HistoricalFiles(store,db,independent_verifier=independent)
                    started=files.last_commit
                    with patch('market.retire_history.time.monotonic',return_value=started):
                        first=files.reference(historical)
                    db.execute("INSERT INTO price_meta VALUES ('pending_cache_test','true')")
                    self.assertTrue(db.in_transaction)
                    with patch('market.retire_history.time.monotonic',return_value=started+1):
                        self.assertIs(files.reference(historical),first)
                    self.assertEqual(db.in_transaction,not independent)
                    with closing(store.connect()) as reader:
                        self.assertEqual(reader.execute("SELECT count(*) FROM price_meta WHERE key='pending_cache_test'").fetchone()[0],
                                         1 if independent else 0)
                        self.assertEqual(reader.execute('SELECT count(*) FROM price_variants').fetchone()[0],
                                         1 if independent else 0)
                    db.rollback()
                with closing(store.connect()) as reader:
                    self.assertEqual(reader.execute('SELECT count(*) FROM price_variants').fetchone()[0],
                                     1 if independent else 0)

    def test_bulk_new_reference_releases_write_transaction_before_return(self):
        for independent in (True,False):
            with self.subTest(independent_verifier=independent),tempfile.TemporaryDirectory() as tmp:
                store=PriceStore(Path(tmp)/'ohlcv.sqlite3')
                active=series();store.put(active,source='current')
                historical=deepcopy(active);historical['bars'][0]['close']=10.5
                with closing(store.connect(write=True)) as db:
                    files=HistoricalFiles(store,db,independent_verifier=independent)
                    with patch('market.retire_history.time.monotonic',return_value=files.last_commit):
                        ref=files.reference(historical)
                    self.assertEqual(db.in_transaction,not independent)
                    self.assertEqual(store.read(ref,connection=db),historical['bars'])
                    with closing(store.connect()) as reader:
                        self.assertEqual(reader.execute('SELECT count(*) FROM price_variants').fetchone()[0],
                                         1 if independent else 0)
                    db.rollback()
                with closing(store.connect()) as reader:
                    self.assertEqual(reader.execute('SELECT count(*) FROM price_variants').fetchone()[0],
                                     1 if independent else 0)

    def test_bulk_mmap_restores_original_bytes_and_pins_snapshot_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);store=PriceStore(root/'ohlcv.sqlite3')
            active=series()
            start=date(2016,1,1)
            active['bars']=[dict(active['bars'][0], date=(start+timedelta(days=i)).isoformat())
                            for i in range(1800)]
            active_ref=store.put(active,source='current')
            historical=deepcopy(active)
            isin=historical.pop('isin_code')
            historical['bars'][0]['close']=10.5
            historical['bars'][1]=dict(reversed(list(historical['bars'][1].items())))
            historical['bars'][-1]['volume']=None
            snapshot=dict(universe=[dict(market='US',symbol='AAA',isin_code=isin)],
                          note='한글 "bars": []',series=[historical]*8,
                          risk_series=[historical],benchmarks={'AAA':historical})
            path=root/'date'/'run'/'snapshot.json'
            path.parent.mkdir(parents=True)
            raw=b'\xef\xbb\xbf'+(json.dumps(snapshot,ensure_ascii=False,indent=2)+'\n').replace('\n','\r\n').encode()
            self.assertGreater(len(raw),3_000_000)
            expected=hashlib.sha256(raw).hexdigest()
            path.write_bytes(raw)
            (path.parent/'manifest.json').write_text(json.dumps({'snapshot.json':expected}))
            with closing(store.connect(write=True)) as db:
                archive=HistoricalFiles(store,db,independent_verifier=True)
                with patch.object(PriceFiles,'prepare',side_effect=AssertionError('expected mmap')):
                    archive.prepare_group([path],root/'ledger.jsonl',{})
                self.assertEqual(db.execute('SELECT count(*) FROM price_variants').fetchone()[0],2)
            files=PriceFiles(store)
            checked=files.market_snapshot(path,expected)
            for row in checked['series']+checked['risk_series']+list(checked['benchmarks'].values()):
                self.assertEqual(row['bars_ref']['series_id'],active_ref['series_id'])
                self.assertEqual(row['bars_ref']['identity']['isin'],isin)
            path.unlink()
            recovered=files.recover(path)
            self.assertEqual(hashlib.sha256(recovered).hexdigest(),expected)
            self.assertEqual(recovered,raw)
            self.assertEqual(store.read(active_ref),active['bars'])
            # Published metadata alone cannot validate a changed historical value.
            with closing(store.connect(write=True)) as db, db:
                db.execute('UPDATE price_variants SET close=10.25 WHERE day=?',(start.isoformat(),))
            with self.assertRaisesRegex(ValueError,'recovery_hash_mismatch'):
                files.recover(path)

    def test_small_snapshot_uses_its_own_historical_universe_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);store=PriceStore(root/'ohlcv.sqlite3')
            active=series();reference=store.put(active,source='current')
            old=deepcopy(active);isin=old.pop('isin_code')
            path=root/'date'/'run'/'snapshot.json';path.parent.mkdir(parents=True)
            path.write_text(json.dumps(dict(universe=[dict(market='US',symbol='AAA',isin_code=isin)],series=[old])))
            expected=physical_sha(path)
            (path.parent/'manifest.json').write_text(json.dumps({'snapshot.json':expected}))
            with closing(store.connect(write=True)) as db:
                HistoricalFiles(store,db,independent_verifier=True).prepare_group([path],root/'ledger.jsonl',{})
            recovered=PriceFiles(store).market_snapshot(path,expected)
            self.assertEqual(recovered['series'][0]['bars_ref']['series_id'],reference['series_id'])

    def test_composed_chart_pins_old_daily_variant_without_copying_bars(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=PriceStore(Path(tmp)/'ohlcv.sqlite3')
            archive=series()
            store.put(archive,source='history',kind='archive')
            daily=deepcopy(archive)
            daily['bars']=[dict(archive['bars'][1],close=11.25),dict(archive['bars'][1],date='2026-10-03')]
            with closing(store.connect(write=True)) as db,db:
                ref=HistoricalFiles(store,db).reference(daily)
            view={**archive,'bars':archive['bars'][:1]+daily['bars'],'daily_bars_ref':ref}
            pinned=store.pin(view)
            self.assertIn('segments',pinned)
            self.assertEqual(store.read(pinned),view['bars'])

    def test_sparse_observation_does_not_inherit_an_unreceived_bar(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=PriceStore(Path(tmp)/'ohlcv.sqlite3')
            full=series();full['bars'].append(dict(full['bars'][1],date='2026-10-03'))
            prior=store.put(full,source='first')
            sparse={**full,'bars':full['bars'][::2]}
            ref=store.put(sparse,source='partial')
            self.assertEqual(store.read(ref),sparse['bars'])
            self.assertEqual(store.read(prior),full['bars'])

    def test_archived_gap_and_variant_do_not_rollback_active_prices(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            store=PriceStore(root/'ohlcv.sqlite3')
            live=series()
            active=store.put(live,source='current')
            old=deepcopy(live)
            old['bars'][0]['close']-=1
            old['bars'].insert(0,dict(old['bars'][0],date='1999-01-04'))
            old['bars'].pop()
            paths=[]
            for n in ('one','two'):
                p=root/'2026-01-01'/'run'/n/'snapshot.json'
                p.parent.mkdir(parents=True)
                p.write_bytes(json.dumps({'series':[old],'phase':n},indent=2).replace('\n','\r\n').encode())
                (p.parent/'manifest.json').write_text(json.dumps({'snapshot.json':physical_sha(p)}))
                paths.append(p)
            with closing(store.connect(write=True)) as db:
                archive=HistoricalFiles(store,db)
                archive.prepare_group(paths,root/'ledger.jsonl',{})
                self.assertEqual(db.execute('SELECT count(*) FROM price_variants').fetchone()[0],2)
            self.assertEqual(store.read(active),live['bars'])
            for p in paths:
                raw=p.read_bytes();p.unlink()
                self.assertEqual(PriceFiles(store).recover(p),raw)
            newer=deepcopy(live);newer['bars'][0]['close']+=1
            store.put(newer,source='next')
            self.assertEqual(json.loads(PriceFiles(store).recover(paths[0]))['series'][0],old)
