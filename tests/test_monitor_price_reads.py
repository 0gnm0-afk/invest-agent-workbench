"""M07 C2 consumer equivalence against synthetic saved prices and snapshots."""
from contextlib import closing, contextmanager
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from alerts import holding_series, saved_inputs
from market.prices import PriceStore, hydrate_snapshot, saved_bars, saved_snapshot_series
from signals.store import SignalStore, evaluate_saved, packed


def series(**changes):
    return {**dict(market='US', symbol='SYNTH', currency='USD', provider='fixture',
                   price_basis='adjusted', bars=[
        dict(date='2026-10-01', open=10, high=12, low=9, close=11, volume=100),
        dict(date='2026-10-02', open=11, high=13, low=10, close=12, volume=None),
        dict(date='2026-10-05', open=12, high=14, low=11, close=13, volume=200)]), **changes}


class MonitorPriceReads(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.prices = PriceStore(self.root / 'runtime/ohlcv.sqlite3', root=self.root)
        self.signals = SignalStore(self.root)

    @contextmanager
    def public_reads_only(self, function_name, original):
        """Fail if the consumer opens prices outside the selected M03 API."""
        active = False
        connect = PriceStore.connect
        def public(*args, **kwargs):
            nonlocal active
            active = True
            try:
                return original(*args, **kwargs)
            finally:
                active = False
        def readonly(store, *, write=False):
            self.assertTrue(active, 'consumer bypassed the M03 read API')
            self.assertFalse(write, 'consumer attempted price writes')
            return connect(store, write=False)
        before = self.prices.path.read_bytes() if self.prices.path.exists() else None
        with patch('market.prices.' + function_name, side_effect=public) as call, \
                patch.object(PriceStore, 'connect', readonly):
            yield call
        after = self.prices.path.read_bytes() if self.prices.path.exists() else None
        self.assertEqual(after, before)

    def prior_sql(self, cutoff):
        sid = self.prices.find(dict(market='US', symbol='SYNTH'))
        with closing(self.prices.connect()) as db:
            rows = db.execute('SELECT day,open,high,low,close,volume,revision FROM prices '
                              'WHERE series_id=? AND day<=? ORDER BY day', (sid, cutoff)).fetchall()
        bars = [dict(zip(('date', 'open', 'high', 'low', 'close', 'volume'), r[:6])) for r in rows]
        reference = dict(series_id=sid, revision=max(r[6] for r in rows),
                         sha256=hashlib.sha256(packed(bars).encode()).hexdigest(),
                         first=bars[0]['date'], last=bars[-1]['date'])
        return bars, reference

    def evaluate_fixture(self, targets):
        import pandas as pd
        def evaluate(bars, *, completed_asof, expected_sessions, stock_key):
            return dict(state='evaluated', stock_key=stock_key, as_of=completed_asof,
                        rules=[], indicators={}, events=[dict(id='rsi30',
                            event_id='synthetic-event', date=completed_asof)])
        with patch('exchange_calendars.get_calendar') as calendar, \
                patch('signals.technical.evaluate', side_effect=evaluate) as calculate:
            calendar.return_value.sessions_in_range.return_value = pd.to_datetime(['2026-10-01', '2026-10-02'])
            result = evaluate_saved(self.root, targets)
        return result, calculate

    def test_signal_cutoff_row_revision_and_hash_match_prior_sql(self):
        self.prices.put(series(), source='initial')
        self.signals.configure('US:SYNTH', ['rsi30'], enabled=True, reason='fixture')
        self.signals.configure('US:IGNORED', ['rsi30'], enabled=False, reason='fixture')
        # A later correction outside the cutoff must not advance its revision.
        changed = series()
        changed['bars'][-1]['close'] = 13.5
        self.prices.put(changed, source='future-correction')
        targets = {'US': '2026-10-02'}
        prior_bars, prior_reference = self.prior_sql(targets['US'])
        with self.public_reads_only('saved_bars', saved_bars) as read:
            result, calculate = self.evaluate_fixture(targets)
        self.assertEqual(result['states'][0]['price_reference'], prior_reference)
        self.assertEqual(calculate.call_args.args[0], prior_bars)
        self.assertEqual(calculate.call_args.kwargs['completed_asof'], targets['US'])
        read.assert_called_once_with(dict(market='US', symbol='SYNTH'), root=self.root, cutoff=targets['US'])
        self.assertEqual(len(self.signals.delivery_events(targets)), 1)
        old_event = copy.deepcopy(self.signals.public()['events'][0])
        changed['bars'][0]['close'] = 11.5
        self.prices.put(changed, source='past-correction')
        prior_bars, prior_reference = self.prior_sql(targets['US'])
        with self.public_reads_only('saved_bars', saved_bars):
            result, calculate = self.evaluate_fixture(targets)
        self.assertEqual(result['states'][0]['price_reference'], prior_reference)
        self.assertEqual(calculate.call_args.args[0], prior_bars)
        self.assertEqual(self.signals.public()['events'], [old_event])
        self.assertEqual(self.signals.delivery_events(targets), [])

    def test_signal_absent_empty_ambiguous_and_missing_target_keep_deferred_shape(self):
        self.signals.configure('US:SYNTH', ['rsi30'], enabled=True, reason='fixture')
        def deferred(targets, error_type):
            with self.public_reads_only('saved_bars', saved_bars):
                result, calculate = self.evaluate_fixture(targets)
            state = result['states'][0]
            self.assertEqual(result['state'], 'deferred')
            self.assertEqual(state['reason'], 'saved_price_or_calendar_unavailable')
            self.assertEqual(state['error_type'], error_type)
            self.assertEqual(state['as_of'], targets.get('US'))
            self.assertIsNone(state['price_reference'])
            self.assertEqual(state['events'], [])
            calculate.assert_not_called()
        deferred({'US': '2026-10-02'}, 'OperationalError')
        self.assertFalse(self.prices.path.exists())
        self.prices.put(series(bars=[]), source='empty')
        deferred({'US': '2026-10-02'}, 'ValueError')
        self.prices.put(series(), source='full')
        deferred({}, 'ValueError')
        deferred({'US': '2026-09-30'}, 'ValueError')
        self.prices.put(series(provider='second-provider'), source='ambiguous')
        deferred({'US': '2026-10-02'}, 'ValueError')

    def snapshot(self, rows, *, name='snapshot.json', **changes):
        path = self.root / name
        value = dict(ohlcv_store={'path': str(self.prices.path)}, series=rows, **changes)
        raw = b'\xef\xbb\xbf' + json.dumps(value, indent=2).replace('\n', '\r\n').encode()
        path.write_bytes(raw)
        return path, hashlib.sha256(raw).hexdigest()

    def test_frozen_snapshot_hydration_preserves_selected_ref_and_original_hash(self):
        original = series()
        reference = self.prices.put(original, source='initial')
        row = {k: v for k, v in original.items() if k != 'bars'}
        row['bars_ref'] = reference
        # Invalid unselected refs and other series groups must never be read.
        path, expected = self.snapshot([row, {**row, 'symbol': 'OTHER', 'bars_ref': {}}],
                                       risk_series=[{'bars_ref': {}}], benchmarks={'bad': {'bars_ref': {}}})
        raw = path.read_bytes()
        changed = series()
        changed['bars'][0]['close'] = 11.5
        self.prices.put(changed, source='correction')
        with self.public_reads_only('hydrate_snapshot', hydrate_snapshot) as hydrate:
            result = holding_series(path, expected, {'US:SYNTH'}, root=self.root)
        self.assertEqual(result, {'US:SYNTH': {**row, 'bars': original['bars']}})
        hydrate.assert_called_once_with({'ohlcv_store': {'path': str(self.prices.path)},
                                        'series': [row]}, root=self.root)
        self.assertEqual(path.read_bytes(), raw)
        with patch('market.prices.hydrate_snapshot', side_effect=AssertionError('read before hash check')):
            with self.assertRaisesRegex(ValueError, 'saved_artifact_hash_mismatch'):
                holding_series(path, '0' * 64, {'US:SYNTH'}, root=self.root)
        corrupt = {**reference, 'numeric_sha256': '0' * 64, 'sha256': '0' * 64}
        path, expected = self.snapshot([{**row, 'bars_ref': corrupt}])
        with self.assertRaisesRegex(ValueError, 'ohlcv_reference_hash_mismatch'):
            holding_series(path, expected, {'US:SYNTH'}, root=self.root)

    def test_frozen_snapshot_empty_missing_ref_and_duplicates_keep_failures(self):
        row = {k: v for k, v in series().items() if k != 'bars'}
        path, expected = self.snapshot([row])
        with patch.object(PriceStore, 'connect', side_effect=AssertionError('empty or missing ref opened DB')):
            self.assertEqual(holding_series(path, expected, set(), root=self.root), {})
            with self.assertRaises(KeyError):
                holding_series(path, expected, {'US:SYNTH'}, root=self.root)
            raw = json.dumps({'ohlcv_store': {}, 'series': []}, indent=2).encode()
            # Keep the multi-line marker used by the existing frozen reader.
            raw = raw.replace(b'"ohlcv_store": {}', b'"ohlcv_store": {\n  }')
            path.write_bytes(raw)
            with self.assertRaises(KeyError):
                holding_series(path, hashlib.sha256(raw).hexdigest(), set(), root=self.root)
        row['bars_ref'] = self.prices.put(series(), source='initial')
        path, expected = self.snapshot([row, row])
        with self.assertRaisesRegex(ValueError, 'duplicate_market_series'):
            holding_series(path, expected, {'US:SYNTH'}, root=self.root)

    def test_saved_inputs_forwards_root_without_refresh_or_receipt(self):
        reference = self.prices.put(series(), source='initial')
        row = {k: v for k, v in series().items() if k != 'bars'}
        row['bars_ref'] = reference
        run = self.root / 'runtime/run'
        run.mkdir()
        path, expected = self.snapshot([row], name='runtime/snapshot.json')
        runfile = run / 'run.json'
        runfile.write_text(json.dumps({'market_source': str(path.parent)}))
        (run / 'manifest.json').write_text(json.dumps({'output_hashes': {
            'run.json': hashlib.sha256(runfile.read_bytes()).hexdigest()},
            'input_hashes': {str(path): expected}}))
        (self.root / 'config.json').write_text('{}')
        bundle = {'accounts': [{'positions': [{'market': 'US', 'symbol': 'SYNTH'}]}]}
        with patch('accounts.account_view', return_value=bundle) as accounts, \
                self.public_reads_only('hydrate_snapshot', hydrate_snapshot) as hydrate:
            actual, stocks = saved_inputs(self.root, run)
        self.assertIs(actual, bundle)
        self.assertEqual(stocks['US:SYNTH']['bars'], series()['bars'])
        self.assertEqual(hydrate.call_args.kwargs['root'], self.root)
        self.assertFalse(accounts.call_args.kwargs['refresh'])
        self.assertFalse(accounts.call_args.kwargs['persist_receipt'])

    def test_legacy_snapshot_contract_requires_unconverted_parser(self):
        # These pre-existing accepted/rejected inputs prevent replacing the old
        # embedded parser with saved_snapshot_series without a contract change.
        path = self.root / 'legacy.json'
        row = dict(market='US', symbol='SYNTH', bars=[dict(date='2026-10-01', close=11)])
        path.write_text(json.dumps({'series': [row]}, indent=2))
        expected = hashlib.sha256(path.read_bytes()).hexdigest()
        self.assertEqual(holding_series(path, expected, {'US:SYNTH'}), {'US:SYNTH': row})
        with self.assertRaises(KeyError):
            saved_snapshot_series(path, expected, {'US:SYNTH'}, root=self.root)
        path.write_text('{"series": []}')
        expected = hashlib.sha256(path.read_bytes()).hexdigest()
        with self.assertRaisesRegex(ValueError, 'snapshot_series_format_unconfirmed'):
            holding_series(path, expected, set())
        self.assertEqual(saved_snapshot_series(path, expected, set(), root=self.root), {})
