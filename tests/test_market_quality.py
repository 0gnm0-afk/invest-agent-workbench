import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from market_quality import complete_cap, error_disposition, history_usability, quote_time_state, LIFECYCLE


class MarketQualityTests(unittest.TestCase):
    def setUp(self):
        self.row = dict(market='US', symbol='EXAMPLE', provider_symbol='EXAMPLE',
                        shares_outstanding=100, quote_time_state='not_provided', tradable=True)
        self.bar = dict(date='2026-09-23', open=19, low=18, high=21, close=20, volume=10)

    def test_raw_completed_close_is_required_and_undated_quote_not_relabelled(self):
        with patch('market_quality.daily_series', return_value={
                'bars': [self.bar], 'currency': 'USD', 'source_url': 'source',
                'price_basis': 'toss_raw_ohlc_no_order_authority'}) as call:
            result = complete_cap(None, self.row, ['2026-09-22', '2026-09-23'])
        self.assertEqual(result['market_cap'], 2000)
        self.assertEqual(result['market_cap_as_of'], '2026-09-23')
        self.assertFalse(call.call_args.kwargs['adjusted'])
        self.assertEqual(call.call_args.args[3], ['2026-09-23'])
        self.assertNotIn('quote_timestamp', result)
        self.assertNotIn('last_price', result)

    def test_stale_raw_close_is_not_used(self):
        self.bar['date'] = '2026-09-22'
        with patch('market_quality.daily_series', return_value={'bars': [self.bar]}):
            with self.assertRaises(ValueError):
                complete_cap(None, self.row, ['2026-09-23'])

    def test_suspension_and_unknown_shares_do_not_trigger_fallback_calls(self):
        with patch('market_quality.daily_series') as call:
            self.row['tradable'] = False
            self.assertIsNone(complete_cap(None, self.row, ['2026-09-23'])['market_cap'])
            self.row.update(tradable=True, shares_outstanding=None)
            self.assertIsNone(complete_cap(None, self.row, ['2026-09-23'])['market_cap'])
            call.assert_not_called()

    def test_null_time_different_from_invalid_time(self):
        self.assertEqual(quote_time_state(None), 'not_provided')
        for value in ('broken', '', '2026-09-23T00:00:00'):
            self.assertEqual(quote_time_state(value), 'invalid')

    def test_verified_lifecycle_requires_identity_date_and_expected_error(self):
        row = dict(market='US', symbol='AFBI', isin_code='US00832E1038')
        err = dict(error_code='stale_or_invalid_last_session')
        self.assertEqual(error_disposition(err, row, '2026-09-23')['state'], 'confirmed_trading_ended')
        self.assertEqual(error_disposition(err, row, '2027-01-01')['state'], 'confirmed_trading_ended')
        for date in ('2026-07-01',):
            self.assertTrue(error_disposition(err, row, date)['state'].startswith('quarantined'))
        row['isin_code'] = 'different'
        self.assertTrue(error_disposition(err, row, '2026-09-23')['state'].startswith('quarantined'))
        self.assertEqual(error_disposition({'error_code': 'market_http_500'}, row, '2026-09-23')['state'], 'collection_failure')

    def test_gap_does_not_compress_calendar_or_claim_whole_history(self):
        bars = [{**self.bar, 'date': date} for date in ('2026-09-18', '2026-09-22', '2026-09-23')]
        rule = dict(ma_period=2, slope_window=1, transition_window=1, rs_period=1, activity_window=1)
        quality = history_usability(bars, ['2026-09-18', '2026-09-21', '2026-09-22', '2026-09-23'], rule)
        self.assertEqual(quality['continuous_bars'], 2)
        self.assertFalse(quality['profile_history_available'])
        self.assertFalse(quality['sperandeo_history_available']['63'])

    def test_all_reviewed_corporate_actions_match_only_the_recorded_security(self):
        self.assertEqual(len({(r['market'],r['symbol'],r['isin_code']) for r in LIFECYCLE}), len(LIFECYCLE))
        for row in LIFECYCLE:
            if row.get('last_trading_session'):
                self.assertLess(row['last_trading_session'], row['effective_session'])
            self.assertLessEqual(row['effective_session'], row['verified_on'])
            result = error_disposition({'error_code':'stale_or_invalid_last_session'}, row, row['verified_on'])
            self.assertEqual(result['state'], 'confirmed_trading_ended')
            self.assertTrue(result['source_url'].startswith('https://'))


if __name__ == '__main__':
    unittest.main()
