import copy
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from market.quality import adr_cap_unavailable, complete_cap
from market.nh_fallback import completed_cap, fallback_targets


class ADRCapTests(unittest.TestCase):
    def setUp(self):
        self.bar = dict(date='2026-10-02', open=49, high=51, low=48, close=50, volume=1000)
        self.row = dict(market='US', symbol='NVO', security_type='DEPOSITARY_RECEIPT',
                        isin_code='US6701002056', listing_exchange_family='NYSE',
                        currency='USD', shares_outstanding=100, last_price=50,
                        quote_time_state='not_provided', tradable=True,
                        bars=[self.bar])
        self.quote = {'rsp_cd': '00000', 'Output_0': {
            'iem_cd': 'NVO', 'exch_id': 'NYY', 'currency_unit': 'USD',
            'trade_date': '20261002', 'normal_trdprc': 50, 'list_num': 100}}

    def test_cap_guard_clears_estimate_without_changing_price_or_identity(self):
        for field in ('security_type', 'securityType'):
            with self.subTest(field=field):
                row = {k: copy.deepcopy(v) for k, v in self.row.items() if k != 'security_type'}
                row[field] = 'DEPOSITARY_RECEIPT'
                row.update(market_cap=5000, undated_quote_market_cap=5000,
                           market_cap_as_of='2026-10-02', market_cap_currency='USD')
                before = copy.deepcopy(row)
                result = {**row, **adr_cap_unavailable(row)}
                self.assertEqual(row, before)
                for key in ('market_cap', 'undated_quote_market_cap', 'market_cap_as_of',
                            'market_cap_currency', 'market_cap_error'):
                    self.assertIsNone(result[key])
                self.assertEqual(result['market_cap_quality'], 'unavailable_adr_share_basis')
                self.assertEqual(result['market_cap_reason'], 'adr_ratio_or_share_units_unverified')
                for key in ('bars', 'currency', 'shares_outstanding', 'last_price', 'isin_code'):
                    self.assertEqual(result[key], before[key])

    def test_completed_toss_quote_does_not_resolve_adr_share_units(self):
        with patch('market.quality.daily_series') as request:
            for state in ('not_provided', 'provided'):
                with self.subTest(state=state):
                    row = {**self.row, 'quote_time_state': state}
                    result = complete_cap(None, row, ['2026-10-02'])
                    self.assertIsNone(result['market_cap'])
                    self.assertEqual(result['market_cap_quality'], 'unavailable_adr_share_basis')
            request.assert_not_called()

    def test_nh_dated_price_and_listing_shares_do_not_resolve_adr_share_units(self):
        before = copy.deepcopy(self.quote)
        for shares in (100, None):
            with self.subTest(shares=shares):
                result = completed_cap({**self.row, 'shares_outstanding': shares},
                                       self.bar, self.quote, 'NYY')
                self.assertIsNone(result['market_cap'])
                self.assertEqual(result['market_cap_quality'], 'unavailable_adr_share_basis')
        self.assertEqual(self.quote, before)

    def test_only_adr_share_basis_metadata_is_skipped(self):
        adr = {**self.row, **adr_cap_unavailable(self.row)}
        regular = {**adr, 'symbol': 'REG', 'security_type': 'STOCK'}
        other_missing = {**adr, 'symbol': 'OTHER', 'market_cap_quality': 'unavailable_price'}
        snapshot = {'errors': [], 'series': [adr, regular, other_missing],
                    'sessions': {'US': ['2026-10-02']}}
        targets = fallback_targets(snapshot)
        self.assertEqual({row['symbol'] for row in targets}, {'REG', 'OTHER'})
        self.assertTrue(all(row['error_code'] == 'unavailable_market_cap' for row in targets))

    def test_adr_price_error_and_history_gap_still_target_nh(self):
        adr = {**self.row, **adr_cap_unavailable(self.row)}
        snapshot = {'errors': [{'market': 'US', 'symbol': 'NVO', 'error_code': 'market_http_503'}],
                    'series': [adr], 'sessions': {'US': ['2026-10-02']}}
        self.assertEqual(fallback_targets(snapshot)[0]['error_code'], 'market_http_503')
        snapshot['errors'] = []
        snapshot['sessions']['US'] = ['2026-09-30', '2026-10-01', '2026-10-02']
        snapshot['series'][0]['bars'] = [{**self.bar, 'date': '2026-09-30'}, self.bar]
        self.assertEqual(fallback_targets(snapshot)[0]['error_code'], 'historical_price_gaps')

    def test_regular_stock_keeps_existing_nh_cap_contract(self):
        row = {**self.row, 'security_type': 'STOCK'}
        self.assertEqual(adr_cap_unavailable(row), {})
        result = completed_cap(row, self.bar, self.quote, 'NYY')
        self.assertEqual(result['market_cap'], 5000)
        self.assertEqual(result['market_cap_as_of'], '2026-10-02')
        self.assertEqual(result['market_cap_quality'], 'completed_close_estimate')
        with self.assertRaisesRegex(ValueError, 'nh_dated_market_cap_unavailable'):
            completed_cap(row, self.bar, self.quote, 'NQQ')


if __name__ == '__main__':
    unittest.main()
