"""M06 L1 contracts: archived scalar goldens, geometry and saved-only inputs."""
import copy
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import numpy as np

from drawings.l1_trendlines import (SOLVER_SOURCE_SHA256, build_trendlines,
                                    completed_daily_bars, direction_segments,
                                    l1_trendlines, l1trend, pentasolve, period_last_session)
from market.history.chart import aggregate, HistoryStore
from market.prices import PriceStore


# Captured by executing the SHA-verified archived l1trend(..., scalar=True)
# inside the M00 guard. Original source remains in OS temp, never in this repo.
# All fitted log-price values and objectives use absolute tolerance 1e-10.
GOLDEN = json.loads(r'''{"source_sha256":"b8f351a849736d8c16696e374e587949b5c854bcd480a7c8acdff73cd95969e6","tolerance":1E-10,"vectors":[{"input":[4.605170185988092,4.605170185988092,4.605170185988092,4.605170185988092,4.605170185988092,4.605170185988092,4.605170185988092,4.605170185988092,4.605170185988092,4.605170185988092,4.605170185988092,4.605170185988092,4.605170185988092,4.605170185988092,4.605170185988092,4.605170185988092,4.605170185988092,4.605170185988092,4.605170185988092,4.605170185988092,4.605170185988092,4.605170185988092,4.605170185988092,4.605170185988092],"expected":[4.605170185988092,4.605170185988092,4.605170185988092,4.605170185988092,4.605170185988092,4.605170185988092,4.605170185988092,4.605170185988092,4.605170185988092,4.605170185988092,4.605170185988092,4.605170185988092,4.605170185988092,4.605170185988092,4.605170185988092,4.605170185988092,4.605170185988092,4.605170185988092,4.605170185988092,4.605170185988092,4.605170185988092,4.605170185988092,4.605170185988092,4.605170185988092],"diagnostics":{"primal_dual_gap":0.0,"iterations":0,"objective":0.0,"max_dual_abs":0.0},"max_abs_error":0.0},{"input":[4.605170185988092,4.741169963242388,4.608505253744872,4.545072424108764,4.732463788124725,4.785988639502911,4.623185223476867,4.649993732519648,4.844269603770871,4.805779054763766,4.659802486962545,4.774290302890388,4.929145334005212,4.813133794537203,4.726554372498828,4.903257188470752,4.982682953386179,4.823893705191176,4.8237668938483,5.020969532736038,5.008660108322495,4.853010185368069,4.94380959380806,5.1144422218429675,5.018053376870076,4.91063980561374,5.073088657930678,5.177021661541732,5.02617113144908,4.9994913611720735,5.195597687089297,5.210123729390436],"expected":[4.606774674415702,4.623936521655634,4.641098368772202,4.658260224771222,4.675422096533702,4.692583981496967,4.709745881942196,4.726907809180169,4.744069766842132,4.761231750323583,4.77839376579686,4.795555826617251,4.812717930273752,4.829880071554144,4.847042261765555,4.864204512081278,4.881366811703305,4.898529157446534,4.915691563730166,4.932854031936363,4.95001654666855,4.967179108367277,4.984341727738804,5.0015043954697544,5.018667098478851,5.035829839252284,5.052992620326621,5.07015542902259,5.087318256934237,5.1044811059908195,5.121643972519731,5.138806844559996],"diagnostics":{"primal_dual_gap":5.839108140711694E-07,"iterations":19,"objective":0.11258157568776475,"max_dual_abs":0.5562139094080596},"max_abs_error":0.0},{"input":[4.4520266346738815,4.455291524980504,4.428583767139889,4.404014040828163,4.412365301441213,4.463976468317091,4.542321923423912,4.614273971950535,4.650675244609676,4.6444455153473205,4.614978543912375,4.596098490270688,4.615055005258067,4.675601112008217,4.755662566824364,4.821046033697771,4.846673253134366,4.832154719898259,4.8020315669839695,4.7905354451125515,4.820330560348525,4.823439131101995,4.838090599828636,4.830346233166227,4.780175181719857,4.693893700314777,4.600242733954172,4.532539700895283,4.50798827149166,4.516994834985642,4.529126161856166,4.5120085347320265,4.451431928341751,4.360174479742771,4.27006722516084,4.212226926732314,4.1977324656945205,4.210744822415111,4.218343157224756,4.190968755028891,4.120781080021307,4.0845267729928,4.057897928646306,4.068609199192104,4.121186908464836,4.194158190636254,4.253385707192607,4.273267041141383,4.2526338782041115,4.215477533644897,4.1960502703003195,4.217594536008017,4.277909330655439,4.350717034147885,4.401981890843915,4.411046320480024,4.383459340257126,4.347530607767638,4.336750034905383,4.3689904476189145],"expected":[4.400658508545239,4.424208236980619,4.447757966009996,4.4713076966108565,4.494857429576708,4.518407164855586,4.541956901377677,4.5655066375029385,4.5890563715468975,4.612606102278771,4.636155829254924,4.659705552500245,4.683255271715435,4.706804985477591,4.730354690167897,4.753904376686023,4.777454014285766,4.8010033784752215,4.805264438457199,4.809524940573536,4.813785112917369,4.818044219107613,4.785464818430722,4.747883563970159,4.710302202899599,4.672720814291866,4.6351394145867815,4.597558009738117,4.559976602616805,4.522395194757545,4.484813787019464,4.4472323802021805,4.409650975690201,4.372069576129351,4.33448818701018,4.296906823217864,4.259325554682002,4.228976073358199,4.198628585355974,4.168281334627311,4.13793430789417,4.107588674618785,4.123805400507465,4.140022428068542,4.156239508157432,4.172456607108899,4.1886737151135085,4.204890828223469,4.221107944752483,4.237325064186476,4.253542186457397,4.269759311207174,4.285976437379215,4.302193563378296,4.31841068748916,4.334627808446245,4.350844926072461,4.367062041242378,4.383279155157515,4.399496268720502],"diagnostics":{"primal_dual_gap":1.9683803913463828E-07,"iterations":75,"objective":0.18618894126734767,"max_dual_abs":0.9999998761616422},"max_abs_error":0.0}]}''')


def bars(count=40, start='2026-01-05', slope=.012):
    result = []
    day = date.fromisoformat(start)
    while len(result) < count:
        if day.weekday() < 5:
            close = float(100*np.exp(slope*len(result)))
            result.append(dict(date=day.isoformat(), open=close, high=close+3,
                               low=close-2, close=close, volume=100))
        day += timedelta(days=1)
    return result


class L1Trendlines(unittest.TestCase):
    def test_original_solver_golden(self):
        self.assertEqual(GOLDEN['source_sha256'], SOLVER_SOURCE_SHA256)
        for vector in GOLDEN['vectors']:
            fitted, info = l1trend(vector['input'])
            np.testing.assert_allclose(fitted, vector['expected'], rtol=0, atol=1e-10)
            self.assertAlmostEqual(info['objective'], vector['diagnostics']['objective'], delta=1e-10)
            self.assertLess(info['primal_dual_gap'], 1e-6)
            y = np.array(vector['input'])
            objective = .5*np.sum((y-fitted)**2)+np.sum(np.abs(np.diff(fitted, n=2)))
            self.assertAlmostEqual(objective, info['objective'], delta=1e-12)
        diagonal = np.arange(12, dtype=float)/10+7
        rhs = np.cos(np.arange(12))
        matrix = np.diag(diagonal)+np.diag(np.full(11, -4.), 1)+np.diag(np.full(11, -4.), -1)
        matrix += np.diag(np.ones(10), 2)+np.diag(np.ones(10), -2)
        np.testing.assert_allclose(pentasolve(diagonal, rhs), np.linalg.solve(matrix, rhs), rtol=0, atol=1e-12)
        for bad in ([], [1, 2], [1, float('nan'), 3], [[1, 2, 3]]):
            with self.assertRaises(ValueError):
                l1trend(bad)

    def test_direction_threshold_and_minimum(self):
        for slope, direction in ((-1.00001e-6, 'down'), (-1e-6, 'flat'),
                                 (1e-6, 'flat'), (1.00001e-6, 'up')):
            self.assertEqual(direction_segments([0, slope], [1, 1], min_bars=2)[0]['direction'], direction)
        self.assertEqual(direction_segments(np.arange(9)*.01, np.ones(9)), [])
        self.assertEqual(direction_segments(np.arange(10)*.01, np.ones(10)),
                         [dict(start=0, end=9, direction='up')])
        values = np.r_[np.arange(10)*.01, np.full(9, .09), .09-np.arange(1, 11)*.01]
        runs = direction_segments(values, np.ones(len(values)))
        self.assertEqual([r['direction'] for r in runs], ['up', 'flat', 'down'])
        self.assertEqual([r['end']-r['start']+1 for r in runs], [10, 10, 11])
        flat = bars(slope=0)
        result = build_trendlines(flat, completed_through=flat[-1]['date'], as_of_date='2026-03-01')
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['lines'], [])

    def test_line_envelopes_and_progress(self):
        for slope, direction, bound in ((.012, 'up', 'low'), (-.012, 'down', 'high')):
            rows = bars(slope=slope)
            result = build_trendlines(rows, completed_through=rows[-1]['date'], as_of_date='2026-03-01')
            self.assertEqual(result['status'], 'ok')
            self.assertEqual(len(result['lines']), 1)
            line = result['lines'][0]
            self.assertEqual(line['direction'], direction)
            fitted, _ = l1trend(np.log([b['close'] for b in rows]))
            expected_slope = np.polyfit(np.arange(len(rows)), np.exp(fitted), 1)[0]
            self.assertAlmostEqual(line['slope'], expected_slope, delta=1e-10)
            residual = [b[bound]-(line['intercept']+line['slope']*i) for i, b in enumerate(rows)]
            self.assertTrue(all(v >= -1e-10 if direction == 'up' else v <= 1e-10 for v in residual))
            self.assertAlmostEqual(min(abs(v) for v in residual), 0, delta=1e-10)
            self.assertTrue(line['in_progress'])
            self.assertEqual((line['label'], line['line_style']), ('진행 중', 'dashed'))
            self.assertEqual(line['points'][0]['time'], rows[0]['date'])
            historical = build_trendlines(rows, completed_through=rows[-1]['date'],
                                          as_of_date='2026-03-01', start=rows[3]['date'], end=rows[-2]['date'])
            old = historical['lines'][0]
            self.assertFalse(old['in_progress'])
            self.assertEqual(old['line_style'], 'solid')
            self.assertEqual(old['points'][0]['logical'], 3)
        # A final flat run must not mark the preceding up run as in progress.
        rows = bars(count=30)
        with patch('drawings.l1_trendlines.l1trend', return_value=(
                np.r_[np.arange(15)*.01+4, np.full(15, 4.14)], {'objective': 0})):
            result = build_trendlines(rows, completed_through=rows[-1]['date'], as_of_date='2026-03-01')
        self.assertFalse(result['lines'][0]['in_progress'])

    def test_week_month_completed_and_daily_slope(self):
        rows = bars(count=360, start='2024-11-04', slope=.001)
        cutoff = rows[-1]['date']
        for frame in ('1w', '1mo'):
            result = build_trendlines(rows, completed_through=cutoff, as_of_date=cutoff, timeframe=frame)
            self.assertEqual(result['status'], 'ok')
            current = date.fromisoformat(cutoff)
            boundary = (current-timedelta(days=current.weekday())).isoformat() if frame == '1w' else current.replace(day=1).isoformat()
            self.assertLess(result['calculated_window']['end'], boundary)
            self.assertEqual(result['window']['timeframe'], frame)
            completed = [b for b in aggregate(rows, frame) if b['date'] < boundary]
            self.assertEqual(result['calculated_window']['bar_count'], len(completed))
            self.assertTrue(result['lines'][-1]['in_progress'])
        for sessions, step, expected in ((5, 4e-6, 'flat'), (5, 6e-6, 'up'),
                                         (20, -19e-6, 'flat'), (20, -21e-6, 'down')):
            run = direction_segments(np.arange(12)*step, np.full(12, sessions))[0]
            self.assertEqual(run['direction'], expected)
        # Ten is a count of displayed bars, not the daily rows used to aggregate.
        short = bars(count=45, start='2026-01-05')
        result = build_trendlines(short, completed_through=short[-1]['date'],
                                  as_of_date='2026-04-01', timeframe='1w')
        self.assertEqual(result['status'], 'insufficient_data')

    def test_window_default_and_errors(self):
        rows = bars(count=190, start='2025-12-01', slope=.001)
        original = copy.deepcopy(rows)
        cutoff = rows[-2]['date']
        result = build_trendlines(rows, completed_through=cutoff, as_of_date='2026-10-01', end='2030-01-01')
        self.assertEqual(result['latest_completed_bar'], cutoff)
        self.assertEqual(result['window']['end'], cutoff)
        self.assertEqual(result['window']['start'], '2026-02-20')
        self.assertEqual(rows, original)
        self.assertFalse(result['stored'])
        self.assertFalse(result['signal'])
        self.assertEqual(build_trendlines([], completed_through=cutoff, as_of_date='2026-10-01')['status'], 'insufficient_data')
        bad = copy.deepcopy(rows)
        bad[3]['close'] = 0
        self.assertEqual(build_trendlines(bad, completed_through=cutoff, as_of_date='2026-10-01')['status'], 'invalid_data')
        for kwargs in ({'start': '2026-09-01', 'end': '2026-01-01'}, {'timeframe': '4h'}, {'start': 'bad'}):
            self.assertEqual(build_trendlines(rows, completed_through=cutoff, as_of_date='2026-10-01', **kwargs)['status'], 'invalid_data')
        with patch('drawings.l1_trendlines.l1trend', side_effect=ArithmeticError('synthetic failure')):
            failed = build_trendlines(rows, completed_through=cutoff, as_of_date='2026-10-01')
        self.assertEqual(failed['status'], 'solver_failed')
        self.assertEqual(failed['lines'], [])
        slots = [dict(rows[0], completed=False), rows[1], {'date': '2030-01-01', 'projected': True}]
        self.assertEqual(completed_daily_bars(slots, cutoff), [rows[1]])

    def test_current_period_last_session_before_and_after_close(self):
        # Friday and month end can be complete before the calendar period ends.
        rows = bars(count=460, start='2024-09-02', slope=.001)
        rows = [b for b in rows if b['date'] <= '2026-05-29']
        source = dict(identity={'market': 'US', 'symbol': 'L1TEST'}, bars=rows)
        for frame, expected_date in (('1w', '2026-05-25'), ('1mo', '2026-05-01')):
            for cutoff, complete in (('2026-05-28', False), ('2026-05-29', True)):
                period = Mock(return_value='2026-05-29')
                result = l1_trendlines('US:L1TEST', root=Path.cwd(), timeframe=frame,
                                       now=datetime(2026, 5, 29, 22, tzinfo=timezone.utc),
                                       bars_reader=Mock(return_value=source),
                                       sessions_reader=Mock(return_value=[cutoff]), period_reader=period)
                self.assertEqual(result['status'], 'ok')
                self.assertEqual(result['calculated_window']['end'] == expected_date, complete)
                period.assert_called_once_with('US', timeframe=frame, as_of_date='2026-05-29')
                self.assertEqual(result['current_period_last_session'], '2026-05-29')
        self.assertEqual(period_last_session('US', timeframe='1w', as_of_date='2026-05-29'), '2026-05-29')
        self.assertEqual(period_last_session('US', timeframe='1mo', as_of_date='2026-05-29'), '2026-05-29')
        # Explicit Korean closure is absent from the packaged calendar.
        self.assertEqual(period_last_session('KR', timeframe='1w', as_of_date='2026-07-16'), '2026-07-16')

    def test_saved_reader_read_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = PriceStore(root/'runtime/ohlcv.sqlite3', root=root)
            rows = bars()
            series = dict(market='US', symbol='L1TEST', currency='USD', provider='Toss OpenAPI candles',
                          price_basis='adjusted', bars=rows)
            store.put(series, source='synthetic')
            def files():
                return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*')
                        if p.is_file() and not p.name.endswith(('-shm', '-wal'))}
            before = files()
            real_connect = PriceStore.connect
            def ro_connect(obj, *, write=False):
                self.assertFalse(write)
                return real_connect(obj, write=False)
            sessions = Mock(return_value=[rows[-2]['date']])
            with patch.object(PriceStore, 'connect', ro_connect), \
                    patch.object(HistoryStore, 'ensure', side_effect=AssertionError('refresh')), \
                    patch('market.history.chart.TossMarketClient', side_effect=AssertionError('network')):
                result = l1_trendlines('US:L1TEST', root=root, now=datetime(2026, 3, 1, tzinfo=timezone.utc),
                                       sessions_reader=sessions)
            self.assertEqual(result['status'], 'ok')
            self.assertEqual(result['latest_completed_bar'], rows[-2]['date'])
            self.assertEqual(result['bars_ref']['last'], rows[-2]['date'])
            self.assertEqual(before, files())
            sessions.assert_called_once()
            fake = Mock(return_value={'identity': {'market': 'KR', 'symbol': 'L1TEST'}, 'bars': rows})
            result = l1_trendlines('US:L1TEST', root=root, now=datetime(2026, 3, 1, tzinfo=timezone.utc),
                                   sessions_reader=sessions, bars_reader=fake)
            self.assertEqual(result['reason'], 'l1_price_identity_mismatch')
