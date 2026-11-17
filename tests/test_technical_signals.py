"""Persistent watches, frozen indicator boundaries, and no-send delivery rehearsal."""
import copy
from datetime import date, timedelta
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import numpy as np
from signals.technical import evaluate, _cross_down, _wilder
from signals.store import SignalStore
from alerts.delivery import dispatch, DeliveryError


def fixture(n=180):
    start = date(2026, 1, 1)
    dates = []
    while len(dates) < n:
        if start.weekday() < 5:
            dates.append(start.isoformat())
        start += timedelta(days=1)
    values = 100 + 18 * np.sin(np.arange(n) / 8) + 5 * np.sin(np.arange(n) / 3)
    bars = [dict(date=d, open=float(c), high=float(c+1), low=float(c-1), close=float(c), volume=1000)
            for d, c in zip(dates, values)]
    return bars, dates


def run(bars, dates):
    return evaluate(bars, completed_asof=dates[-1], expected_sessions=dates, stock_key='US:TEST')


class Indicators(unittest.TestCase):
    def test_frozen_cross_tolerance_and_wilder_seed(self):
        self.assertEqual(_cross_down(np.array([60., 60.-1e-12, 59.]), 60)[1:].tolist(), [0., 1.])
        values = np.r_[np.nan, np.arange(1., 16.)]
        self.assertTrue(np.isnan(_wilder(values,14)[13]))
        self.assertEqual(_wilder(values,14)[14],7.5)
        self.assertAlmostEqual(_wilder(values,14)[15],(7.5*13+15)/14)

    def test_no_future_input_and_calendar_holds(self):
        bars, dates = fixture()
        today = run(bars[:120], dates[:120])
        self.assertEqual(today, evaluate(bars, completed_asof=dates[119], expected_sessions=dates, stock_key='US:TEST'))
        self.assertEqual(run(bars[:-1], dates)['reason'], 'latest_completed_bar_missing')
        self.assertEqual(run(bars[:90]+bars[91:], dates)['reason'], 'incomplete_history')
        self.assertEqual(run(bars[:67], dates[:67])['reason'], 'insufficient_history')
        self.assertEqual(evaluate(bars, completed_asof=dates[-1])['reason'], 'exchange_calendar_required')
        self.assertEqual(run(bars+bars[-1:], dates)['reason'], 'duplicate_or_unsorted_bars')
        bad = copy.deepcopy(bars); bad[-1]['close'] = float('nan')
        self.assertEqual(run(bad, dates)['reason'], 'invalid_price')

    def test_singles_and_exact_composite_are_separate(self):
        bars, dates = fixture(300)
        results = [run(bars[:i], dates[:i]) for i in range(68, 301)]
        kinds = {e['id'] for r in results for e in r['events']}
        self.assertTrue({'rsi30', 'cci-100', 'macd_signal_down', 'rsi60_down', 'macd_rsi60'} <= kinds)
        for i, result in enumerate(results):
            signals = {e['id'] for e in result['events']}
            if 'cci_recovery' in signals:
                self.assertIn('cci-100', signals)
            if 'macd_rsi60' in signals:
                self.assertIn('macd_signal_down', signals)
                self.assertTrue(any('rsi60_down' in {e['id'] for e in r['events']} for r in results[max(0,i-2):i+1]))
        # Golden event indices independently obtained from frozen final-research
        # old_signals.py (upward/downward), workbench Git f786c59. 300 bars,
        # 233 causal evaluation dates, all six definitions matched exactly.
        golden = {'rsi30': [93,147,190,244,297],
                  'cci-100': [78,91,132,146,187,229,242,283,296],
                  'cci_recovery': [78,91,132,146,187,229,242,283,296],
                  'macd_signal_down': [67,120,166,218,271],
                  'rsi60_down': [68,121,173,218,272], 'macd_rsi60': [218]}
        for key, expected in golden.items():
            self.assertEqual([67+i for i,r in enumerate(results) if key in {e['id'] for e in r['events']}], expected)
        self.assertNotIn('cci_rsi', kinds)
        self.assertTrue(all(not r['active'] for r in results))

    def test_flat_prices_do_not_invent_cci(self):
        bars, dates = fixture()
        for bar in bars:
            bar.update(open=100., high=100., low=100., close=100.)
        result = run(bars,dates)
        self.assertEqual(result['indicators']['rsi14'],50)
        self.assertIsNone(result['indicators']['cci20'])
        self.assertEqual(next(r for r in result['rules'] if r['id']=='cci-100')['state'], 'deferred')


class Persistence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = SignalStore(self.root)
        bars, dates = fixture(300)
        self.evaluation = next(run(bars[:i], dates[:i]) for i in range(68,301)
                               if any(e['id']=='rsi30' for e in run(bars[:i], dates[:i])['events']))
        self.targets = {'US': self.evaluation['as_of']}

    def tearDown(self):
        self.tmp.cleanup()

    def configure(self, enabled):
        return self.store.configure('US:TEST',['rsi30'],enabled=enabled,reason='Explicit fixture watch')

    def test_opt_in_persistence_dedup_and_disable(self):
        watch = self.configure(False)
        self.store.record(watch,self.evaluation,close=100)
        self.assertEqual(self.store.public()['events'],[])
        watch = self.configure(True)
        self.store.record(watch,self.evaluation,close=100)
        self.store.record(watch,self.evaluation,close=100)
        reopened = SignalStore(self.root)
        self.assertEqual(len(reopened.public()['events']),1)
        self.assertEqual(len(reopened.delivery_events(self.targets)),1)
        self.assertEqual(reopened.delivery_events({'US':'2099-01-01'}),[])
        self.configure(False)
        self.assertEqual(reopened.delivery_events(self.targets),[])

    def test_changed_watch_and_deferred_state_cannot_send_old_event(self):
        old = self.configure(True)
        self.store.record(old,self.evaluation,close=100)
        changed = self.store.configure('US:TEST',['rsi30','cci-100'],enabled=True,reason='Changed fixture')
        with self.assertRaisesRegex(ValueError,'revision_conflict'):
            self.store.record(old,self.evaluation,close=100)
        held = dict(self.evaluation,state='deferred',reason='incomplete_history',events=[])
        self.store.record(changed,held)
        self.assertEqual(self.store.delivery_events(self.targets),[])

    def test_real_delivery_ledger_with_fake_sender_no_duplicate(self):
        watch = self.configure(True)
        self.store.record(watch,self.evaluation,close=100)
        messages=[]
        options=dict(config={'enabled_at':'2000-01-01','chat':'fixture','token':'SYNTHETIC_PRIVATE_TOKEN'}, sender=lambda text: (messages.append(text) or {'message_id':1}),
                     include_trailing=False,include_scenarios=False)
        with patch('accounts.account_view', create=True, side_effect=AssertionError('account_query_forbidden')) as query, \
                patch('accounts.refresh_account_snapshot', create=True, side_effect=AssertionError('account_refresh_forbidden')) as refresh:
            self.assertEqual(dispatch(self.root,self.targets,**options)['counts'], {'sent': 1})
            self.assertEqual(dispatch(self.root,self.targets,**options)['counts'], {'sent': 1})
            query.assert_not_called()
            refresh.assert_not_called()
        self.assertEqual(len(messages),1)
        self.assertIn('개별 조건',messages[0])
        self.assertIn('저장 지식',messages[0])
        self.assertNotIn('SYNTHETIC_PRIVATE_TOKEN',messages[0])

    def test_account_independent_delivery_with_default_event_sources(self):
        watch = self.configure(True)
        self.store.record(watch, self.evaluation, close=100)
        messages = []
        with patch('accounts.account_view', create=True, side_effect=AssertionError('account_query_forbidden')) as query, \
                patch('accounts.refresh_account_snapshot', create=True, side_effect=AssertionError('account_refresh_forbidden')) as refresh:
            result = dispatch(self.root, self.targets,
                config={'enabled_at': '2000-01-01', 'chat': 'fixture'},
                sender=lambda text: messages.append(text) or {'message_id': 7})
        self.assertEqual(result['counts'], {'sent': 1})
        self.assertEqual(len(messages), 1)
        query.assert_not_called()
        refresh.assert_not_called()

    def test_saved_prices_and_watch_evaluate_without_network(self):
        from signals.store import evaluate_saved
        from market.prices import PriceStore
        import pandas as pd
        bars,dates=fixture(300)
        cutoff=self.evaluation['as_of'];bars=[b for b in bars if b['date']<=cutoff]
        dates=[d for d in dates if d<=cutoff]
        prices=PriceStore(self.root/'runtime/ohlcv.sqlite3')
        prices.put(dict(market='US',symbol='TEST',currency='USD',price_basis='adjusted',bars=bars),source='fixture')
        self.configure(True)
        with patch('exchange_calendars.get_calendar') as calendar:
            calendar.return_value.sessions_in_range.return_value=pd.to_datetime(dates)
            result=evaluate_saved(self.root,{'US':cutoff})
        self.assertEqual(result['state'],'evaluated')
        self.assertEqual(len(self.store.delivery_events({'US':cutoff})),1)
        state=result['states'][0]
        self.assertEqual(state['mode'],'saved_evaluation');self.assertTrue(state['active'])
        self.assertEqual(state['price_reference']['last'],cutoff)
    def test_corrected_price_basis_holds_old_unsent_event(self):
        watch=self.configure(True)
        self.store.record(watch,self.evaluation,close=100,price_reference={'revision':1})
        self.store.record(watch,self.evaluation,close=101,price_reference={'revision':2})
        self.assertEqual(len(self.store.public()['events']),1)
        self.assertEqual(self.store.delivery_events(self.targets),[])
    def test_unknown_delivery_is_not_resent(self):
        watch=self.configure(True);self.store.record(watch,self.evaluation,close=100)
        calls=[]
        def sender(text):
            calls.append(text)
            raise DeliveryError('unknown','fixture_timeout')
        options=dict(config={'enabled_at':'2000-01-01','chat':'fixture'},sender=sender,
                     include_trailing=False,include_scenarios=False)
        dispatch(self.root,self.targets,**options);dispatch(self.root,self.targets,**options)
        self.assertEqual(len(calls),1)


if __name__ == '__main__':
    unittest.main()
