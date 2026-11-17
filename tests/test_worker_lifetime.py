"""C8 lifetime evidence: guarded fixed children and synthetic launch boundaries.

M00 deliberately permits only fixed probe subprocesses. DailyLaunch parsing and
lock behavior use temporary files plus a fake Popen; these are not claims that an
operating market worker or PowerShell wrapper survived its coordinator.
"""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch

import isolation

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
from invest_agent.trading.store import instance_lock
from launch.daily import DailyLaunch
from launch.journal import RunJournal

NOW = datetime(2026, 10, 7, 0, tzinfo=timezone.utc)
PLAN = dict(day='2026-10-07', cutoff='2026-10-06', evaluated_at=NOW.isoformat(), targets={})


def until(predicate, seconds=3):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.01)
    raise AssertionError('Timed out waiting for fixture')


class FixtureProcess:
    """Write controlled fragments to the real temporary log, without spawning."""
    def __init__(self, stream, fragments, exit_code=0):
        self.stream = stream
        self.fragments = iter(fragments)
        self.exit_code = exit_code
        self.returncode = None
        self.waited = False
        self.closed = False

    def __enter__(self):
        return self

    def poll(self):
        try:
            self.stream.write(next(self.fragments))
            self.stream.flush()
        except StopIteration:
            self.returncode = self.exit_code
        return self.returncode

    def wait(self):
        self.waited = True
        return self.returncode

    def __exit__(self, *exc):
        self.closed = True


class WorkerLifetime(unittest.TestCase):
    def assert_cleaned(self, result):
        self.assertTrue(result['handshake'], result)
        self.assertTrue(result['cleaned'], result)
        self.assertEqual(result['live_children'], 0, result)
        self.assertTrue(all(result['cleanup'].values()), result)
        self.assertEqual(result['guard_events'], [], result)
        saved = json.loads(Path(result['receipt']).read_text(encoding='utf-8'))
        self.assertEqual(saved['cleanup'], result['cleanup'])

    def test_guarded_fixture_child_finishes_and_closes_handles(self):
        result = isolation.probe('ready')
        self.assertEqual(result['status'], 'passed', result)
        self.assertEqual(result['process_exit_code'], 0, result)
        self.assert_cleaned(result)

    def test_guarded_fixture_timeout_closes_process_tree(self):
        result = isolation.probe('sleep_tree', timeout=3)
        self.assertEqual(result['status'], 'failed', result)
        self.assertEqual(result['reason'], 'timeout', result)
        self.assertGreaterEqual(result['active_before_cleanup'], 2, result)
        self.assertGreaterEqual(result['job_processes'], 2, result)
        self.assert_cleaned(result)

    def test_guarded_fixture_interruption_is_not_success(self):
        result = isolation.probe('interrupt')
        self.assertEqual(result['status'], 'failed', result)
        self.assertEqual(result['reason'], 'interrupted', result)
        self.assertNotEqual(result['exit_code'], 0, result)
        self.assert_cleaned(result)

    def test_worker_lock_blocks_duplicate_then_preserves_interruption(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = Mock()
            gate = DailyLaunch(root, runner=runner, planner=lambda n: PLAN, accounts=False)
            record = dict(PLAN, state='running', scheduled_day=PLAN['day'],
                          attempt_id='synthetic-interrupted-attempt')
            gate.save(record)
            receipt = gate.folder / (PLAN['day'] + '.json')
            before = receipt.read_bytes()
            with instance_lock(gate.folder / 'worker-lock'):
                self.assertTrue(gate.once(NOW, scheduled=True)['following'])
                self.assertTrue(gate.once(NOW, retry=True)['following'])
                self.assertEqual(before, receipt.read_bytes())
                runner.assert_not_called()
            result = gate.once(NOW, scheduled=True)
            self.assertEqual(result['state'], 'interrupted')
            self.assertEqual(result['attempt_id'], record['attempt_id'])
            self.assertEqual(result['error'], 'previous_launch_interrupted')
            runner.assert_not_called()
            with instance_lock(gate.folder / 'worker-lock'):
                pass

    def test_coordinator_failure_keeps_private_stack_location(self):
        with tempfile.TemporaryDirectory() as tmp:
            def fail(plan, progress):
                raise RuntimeError('PRIVATE_VALUE')
            gate = DailyLaunch(tmp, runner=fail, planner=lambda n: PLAN, accounts=False)
            record = gate.once(NOW)
            self.assertEqual(record['error_type'], 'RuntimeError')
            self.assertTrue(record['error_frames'])
            self.assertNotIn('PRIVATE_VALUE', json.dumps(record))
            self.assertNotIn('error_frames', gate.public(record))

    def test_partial_lines_are_read_after_flush_and_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = dict(state='failed', output=str(root / 'runtime/fixture'), stages={})
            payload = json.dumps(output) + '\n'
            gate = DailyLaunch(root, accounts=False)
            gate.folder.mkdir(parents=True)
            children = []
            def spawn(command, **kwargs):
                self.assertEqual(kwargs['cwd'], root)
                self.assertIs(kwargs['stdout'], kwargs['stderr'])
                self.assertEqual(kwargs['stdin'], subprocess.DEVNULL)
                self.assertEqual(kwargs['creationflags'], getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                self.assertEqual(command[command.index('--root') + 1], str(root))
                self.assertEqual(command[command.index('--session-cutoff') + 1], PLAN['cutoff'])
                self.assertEqual(command[command.index('--evaluated-at') + 1], PLAN['evaluated_at'])
                self.assertEqual(command[command.index('--prior-run') + 1], str(root / 'runtime/prior'))
                child = FixtureProcess(kwargs['stdout'], [payload[:15], payload[15:]])
                children.append(child)
                return child
            with patch('launch.daily.subprocess.Popen', side_effect=spawn) as popen:
                result = gate.execute(dict(PLAN, prior_run=str(root / 'runtime/prior')), lambda u: None)
            popen.assert_called_once()
            self.assertEqual({key: result[key] for key in output}, output)
            self.assertTrue(children[0].waited)
            self.assertTrue(children[0].closed)
            self.assertTrue(children[0].stream.closed)

    def test_worker_failure_closes_temporary_log_and_process_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            gate = DailyLaunch(tmp, accounts=False)
            gate.folder.mkdir(parents=True)
            children = []
            def spawn(command, **kwargs):
                child = FixtureProcess(kwargs['stdout'], ['PRIVATE_WORKER_DIAGNOSTIC\n'], exit_code=3)
                children.append(child)
                return child
            with patch('launch.daily.subprocess.Popen', side_effect=spawn):
                with self.assertRaisesRegex(RuntimeError, '^market_worker_failed$'):
                    gate.execute(PLAN, lambda u: None)
            self.assertTrue(children[0].waited)
            self.assertTrue(children[0].closed)
            self.assertTrue(children[0].stream.closed)

    def test_journal_records_heartbeat_and_sanitized_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                with RunJournal(tmp, 'fixture', interval=.02) as journal:
                    initial = json.loads(journal.path.read_text())['heartbeat_at']
                    until(lambda: json.loads(journal.path.read_text())['heartbeat_at'] > initial)
                    raise ValueError('PRIVATE_TOKEN_MUST_NOT_APPEAR')
            body = journal.path.read_text()
            value = json.loads(body)
            self.assertEqual(value['state'], 'failed')
            self.assertEqual(value['error_type'], 'ValueError')
            self.assertTrue(value['error_frames'])
            self.assertIsNotNone(value['finished_at'])
            self.assertNotIn('PRIVATE_TOKEN', body)
            self.assertFalse(journal.thread.is_alive())

    def test_journal_distinguishes_exit_codes_and_interruption(self):
        with tempfile.TemporaryDirectory() as tmp:
            for code, state in ((0, 'succeeded'), (2, 'partial'), (1, 'failed')):
                with self.subTest(code=code):
                    with self.assertRaises(SystemExit):
                        with RunJournal(tmp, 'fixture') as journal:
                            raise SystemExit(code)
                    value = json.loads(journal.path.read_text())
                    self.assertEqual(value['state'], state)
                    self.assertEqual(value['exit_code'], code)
                    self.assertIsNotNone(value['finished_at'])
                    self.assertFalse(journal.thread.is_alive())
            with self.assertRaises(KeyboardInterrupt):
                with RunJournal(tmp, 'fixture') as journal:
                    raise KeyboardInterrupt('PRIVATE_TOKEN_MUST_NOT_APPEAR')
            body = journal.path.read_text()
            value = json.loads(body)
            self.assertEqual(value['state'], 'interrupted')
            self.assertEqual(value['error_type'], 'KeyboardInterrupt')
            self.assertNotIn('PRIVATE_TOKEN', body)
            self.assertIsNotNone(value['finished_at'])
            self.assertFalse(journal.thread.is_alive())


if __name__ == '__main__':
    unittest.main()
