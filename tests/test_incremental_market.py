import sys
import json
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import market.toss_collection as market
from market.providers.toss_candles import daily_series


class IncrementalMarketTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.days = ['2026-09-08', '2026-09-09', '2026-09-10', '2026-09-11']
        rule = dict(ma_period=2, slope_window=1, transition_window=1, rs_period=1,
                    activity_window=1, min_rs=0, min_activity_ratio=1, min_average_value=0)
        self.config = {'profile': {'status': 'review_only', 'KR': rule, 'US': rule},
                       'universe': [{'market': 'KR', 'symbol': '005930.KS',
                                     'provider_symbol': '005930', 'benchmark': '^KS11'}]}
        self.calls = []
        self.changed = False

    def bars(self, days):
        return [dict(date=day, open=99., high=101., low=98., close=100., volume=1000.)
                for day in days]

    def prior(self, provider='Toss OpenAPI candles'):
        return {'market': 'KR', 'symbol': '005930.KS', 'provider_symbol': '005930',
                'benchmark': '^KS11', 'currency': 'KRW', 'provider': provider,
                'price_basis': 'toss_adjusted_ohlc_no_order_authority',
                'collected_at': '2026-09-10T00:00:00+00:00', 'bars': self.bars(self.days[:3])}

    def fetch(self, get, symbol, region, sessions, **options):
        self.calls.append((symbol, options))
        bars = self.bars(self.days)
        if symbol == '005930.KS' and self.changed:
            bars[2]['close'] = 100.5
        if options.get('stop_on_or_before'):
            bars = bars[-options['page_size']:]
        return {'market': region, 'symbol': symbol,
                'provider_symbol': 'KOSPI' if symbol == '^KS11' else '005930',
                'benchmark': options.get('benchmark'), 'currency': 'KRW',
                'provider': 'Toss OpenAPI candles',
                'instrument_type': 'INDEX' if symbol == '^KS11' else 'EQUITY',
                'price_basis': 'toss_index_points_observation_only' if symbol == '^KS11'
                    else 'toss_adjusted_ohlc_no_order_authority',
                'bars': bars, 'collection': {}}

    def collect(self, previous):
        with patch.object(market, 'completed_sessions', return_value=self.days), \
             patch.object(market, 'daily_series', side_effect=self.fetch):
            return market.collect(self.config, {}, Path(self.temp.name), lambda *a: None,
                now=datetime(2026, 9, 11, tzinfo=timezone.utc),
                prior_series={('KR', '005930.KS'): previous},
                prior_source={'path': 'saved', 'sha256': 'abc'})

    def test_recent_request_merges_verified_history(self):
        result = self.collect(self.prior())
        row = result['series'][0]
        self.assertEqual([b['date'] for b in row['bars']], self.days)
        self.assertEqual(row['collection']['mode'], 'incremental')
        self.assertEqual(row['collection']['new_bars'], 1)
        self.assertEqual(result['collection_stats']['incremental_updates'], 1)
        self.assertEqual([options['stop_on_or_before'] for symbol, options in self.calls
                          if symbol == '005930.KS'], [self.days[2]])
        self.assertEqual(self.calls[-1][1]['page_size'], 2)

    def test_changed_overlap_refreshes_only_that_symbol(self):
        self.changed = True
        result = self.collect(self.prior())
        self.assertEqual(result['series'][0]['collection']['mode'], 'overlap_refresh')
        self.assertEqual(result['collection_stats']['overlap_refreshes'], 1)
        self.assertEqual(sum(symbol == '005930.KS' for symbol, _ in self.calls), 2)

    def test_database_seed_without_previous_run_and_two_missing_sessions(self):
        from market.prices import PriceStore
        seed = self.prior()
        seed['bars'] = seed['bars'][:2]
        store = PriceStore(Path(self.temp.name)/'ohlcv.sqlite3')
        ref = store.put(seed,source='previous-day',kind='series')
        with patch.object(market,'completed_sessions',return_value=self.days), patch.object(market,'daily_series',side_effect=self.fetch):
            result = market.collect(self.config,{},Path(self.temp.name),lambda *a:None,now=datetime(2026,9,11,tzinfo=timezone.utc))
        row = result['series'][0]
        self.assertEqual([b['date'] for b in row['bars']], self.days)
        self.assertEqual(self.calls[-1][1]['page_size'],3)
        self.assertEqual(row['collection']['prior_source']['revision'],ref['revision'])

    def test_nh_history_is_not_a_toss_seed(self):
        result = self.collect(self.prior(provider='NH market candles'))
        self.assertEqual(result['series'][0]['collection']['mode'], 'full')
        self.assertEqual(result['collection_stats']['incremental_updates'], 0)
        self.assertNotIn('stop_on_or_before', self.calls[-1][1])

    def test_old_full_history_with_contiguous_seed_uses_boundary(self):
        previous = self.prior()
        previous['collected_at'] = '2026-07-01T00:00:00+00:00'
        result = self.collect(previous)
        self.assertEqual(result['series'][0]['collection']['mode'], 'incremental')
        self.assertEqual(self.calls[-1][1]['page_size'], 2)

    def test_same_session_rechecks_one_bar_then_reuses_checkpoint(self):
        seed = self.prior()
        seed['bars'] = self.bars(self.days)
        result = self.collect(seed)
        self.assertEqual(self.calls[-1][1]['page_size'], 1)
        self.assertEqual(result['series'][0]['collection']['new_bars'], 0)
        calls = len(self.calls)
        self.collect(seed)
        self.assertEqual(calls, len(self.calls))

    def test_relisted_ticker_gets_its_own_full_seed(self):
        listing = self.config['universe'][0]
        listing.update(isin_code='OLD', listing_exchange_family='KOSPI')
        self.collect(None)
        listing['isin_code'] = 'NEW'
        result = self.collect(None)
        self.assertEqual(result['series'][0]['isin_code'], 'NEW')
        self.assertEqual(result['series'][0]['collection']['mode'], 'full')
        self.assertNotIn('page_size', self.calls[-1][1])

    def test_seed_with_internal_gap_is_refetched(self):
        seed = self.prior()
        del seed['bars'][1]
        row = self.collect(seed)['series'][0]
        self.assertEqual(row['collection']['mode'], 'full')
        self.assertEqual([b['date'] for b in row['bars']], self.days)

    def test_previous_request_did_not_cover_required_prefix(self):
        seed = self.prior()
        del seed['bars'][0]
        seed['collection'] = {'first_requested_session':self.days[1]}
        row = self.collect(seed)['series'][0]
        self.assertEqual(row['collection']['mode'], 'full')
        self.assertEqual(row['collection']['full_history_requested_from'],self.days[0])

    def test_missing_completed_bar_has_explicit_failure(self):
        original = self.fetch
        def missing(get,symbol,region,sessions,**options):
            result = original(get,symbol,region,sessions,**options)
            if symbol == '005930.KS' and options.get('stop_on_or_before'):
                result['bars'] = result['bars'][:-1]
            return result
        self.fetch = missing
        result = self.collect(self.prior())
        self.assertEqual(result['errors'][0]['error_code'], 'market_completed_session_missing')
        self.assertFalse(result['series'])

    def test_partial_correction_cannot_replace_existing_db_values(self):
        from market.prices import PriceStore
        store = PriceStore(Path(self.temp.name)/'ohlcv.sqlite3')
        seed = self.prior()
        ref = store.put(seed, source='source', kind='series')
        original = self.fetch
        self.changed = True
        def truncated(get,symbol,region,sessions,**options):
            result = original(get,symbol,region,sessions,**options)
            if symbol == '005930.KS' and not options.get('stop_on_or_before'):
                result['bars'] = result['bars'][-2:]
            return result
        self.fetch = truncated
        result = self.collect(None)
        self.assertEqual(result['errors'][0]['error_code'], 'market_corrected_history_incomplete')
        self.assertEqual(store.read(ref), seed['bars'])

    def test_correction_must_match_the_second_provider_response(self):
        original = self.fetch
        self.changed = True
        def conflicting(get,symbol,region,sessions,**options):
            result = original(get,symbol,region,sessions,**options)
            if symbol == '005930.KS' and not options.get('stop_on_or_before'):
                result['bars'][2]['close'] = 100
            return result
        self.fetch = conflicting
        result = self.collect(self.prior())
        self.assertFalse(result['series'])
        self.assertEqual(result['errors'][0]['error_code'], 'market_correction_responses_disagree')

    def test_first_listing_short_history_stays_short(self):
        original = self.fetch
        def new_listing(get,symbol,region,sessions,**options):
            result = original(get,symbol,region,sessions,**options)
            if symbol == '005930.KS':
                result['bars'] = result['bars'][-1:]
            return result
        self.fetch = new_listing
        row = self.collect(None)['series'][0]
        self.assertEqual(len(row['bars']), 1)
        self.assertEqual(row['collection']['mode'], 'full')

    def test_toss_pagination_stops_at_verified_overlap(self):
        requests = []
        def get(path, params):
            requests.append((path, params))
            candles = [dict(timestamp=day+'T00:00:00+09:00', currency='KRW',
                            openPrice=99, highPrice=101, lowPrice=98,
                            closePrice=100, volume=1000) for day in reversed(self.days[-2:])]
            return {'result': {'candles': candles, 'nextBefore': '2026-09-07T00:00:00+09:00'}}
        row = daily_series(get, '005930.KS', 'KR', self.days,
                           stop_on_or_before=self.days[2], page_size=2)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0][1]['count'], 2)
        self.assertEqual(requests[0][1]['before'], self.days[-1]+'T00:00:00+09:00')
        self.assertEqual(len(row['bars']), 2)

    def test_us_inclusive_upper_bound_observes_daylight_saving_time(self):
        for day,offset in [('2026-10-30','-04:00'),('2026-11-02','-05:00')]:
            with self.subTest(day=day):
                def get(path,params):
                    self.assertEqual(params['before'], day+'T00:00:00'+offset)
                    self.assertEqual(params['count'], 1)
                    return {'result':{'candles':[],'nextBefore':None}}
                daily_series(get,'AAPL','US',[day],stop_on_or_before=day,page_size=1)

    def test_single_day_raw_cap_query_has_one_slot(self):
        day = self.days[-1]
        def get(path,params):
            self.assertEqual(params['count'],1)
            self.assertEqual(params['before'],day+'T00:00:00+09:00')
            self.assertEqual(params['adjusted'],'false')
            return {'result':{'candles':[],'nextBefore':None}}
        daily_series(get,'005930.KS','KR',[day],adjusted=False,max_pages=1)

    def test_frozen_collection_uses_pinned_db_without_prior_snapshot(self):
        import market.collect as runner
        from market.prices import PriceStore
        root = Path(self.temp.name)
        db_path = root/'runtime'/'ohlcv.sqlite3'
        store = PriceStore(db_path)
        instrument = dict(listing_exchange_family='KOSPI',isin_code='KR7005930003')
        seed = {**self.prior(), **instrument}
        ref = store.put(seed,source='saved',kind='series')
        store.set_meta('archive_import_complete',True)
        cfg = root/'config.json'
        cfg.write_text(json.dumps(self.config),encoding='utf-8')
        requests = []
        def get(path,params):
            requests.append((path,dict(params)))
            count = params['count']
            candles = [dict(timestamp=day+'T00:00:00+09:00',currency='KRW',openPrice=99,highPrice=101,
                            lowPrice=98,closePrice=100,volume=1000) for day in reversed(self.days[-count:])]
            return {'result':{'candles':candles,'nextBefore':None}}
        rows = [{**self.config['universe'][0], **instrument}]
        meta = {e:{'state':'available','listed':int(e=='KOSPI')} for e in ('KOSPI','NASDAQ','NYSE')}
        instance = root/'runtime'/'test-run'/'collection'
        with patch.object(runner,'datetime',wraps=datetime) as clock, \
             patch.object(runner,'ROOT',root), patch.object(runner,'default_path',return_value=db_path), \
             patch.object(runner,'TossMarketClient',return_value=get), patch.object(market,'discover',return_value=(rows,meta)), \
             patch.object(runner,'completed_sessions',return_value=self.days), patch.object(market,'completed_sessions',return_value=self.days):
            clock.now.return_value = datetime(2026,9,11,16,tzinfo=timezone.utc)
            runner.run(instance,cfg,prior_source=root/'runtime'/'deleted-old-snapshot')
            initial_requests=list(requests)
            first_attempt=Path(json.loads((instance/'latest.json').read_bytes())['attempt'])
            first_ref=json.loads((first_attempt/'snapshot.json').read_bytes())['series'][0]['bars_ref']
            corrected={**seed,'bars':self.bars(self.days)}
            corrected['bars'][-1]['close']=100.25
            live_ref=store.put(corrected,source='separate-verified-correction',kind='series')
            requests.clear()
            clock.now.return_value = datetime(2026,9,11,16,1,tzinfo=timezone.utc)
            runner.run(instance)
            self.assertEqual(requests,[])
            self.assertEqual(store.observation(live_ref['series_id'],'series')[0],live_ref)
            self.assertEqual(store.read(live_ref),corrected['bars'])
            with closing(store.connect()) as db:
                self.assertEqual(db.execute('SELECT close FROM prices WHERE series_id=? AND day=?',
                    (live_ref['series_id'],self.days[-1])).fetchone()[0],100.25)
        frozen = json.loads((instance/'frozen.json').read_bytes())
        self.assertIsNone(frozen['prior_source'])
        self.assertEqual(frozen['config']['seed_revision'],ref['revision'])
        actual = [params for _,params in initial_requests if params.get('symbol')=='005930']
        self.assertEqual(len(actual),1)
        self.assertEqual((actual[0]['count'],actual[0]['before']),(2,self.days[-1]+'T00:00:00+09:00'))
        attempt = Path(json.loads((instance/'latest.json').read_bytes())['attempt'])
        row = json.loads((attempt/'snapshot.json').read_bytes())['series'][0]
        self.assertNotIn('bars',row)
        self.assertEqual(row['bars_ref'],first_ref)
        self.assertEqual([b['date'] for b in store.read(row['bars_ref'])],self.days)


if __name__ == '__main__':
    unittest.main()
