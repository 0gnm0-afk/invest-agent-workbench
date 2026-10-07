"""Real subprocess interruption tests; temporary fixtures only, no API calls."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock

SCRIPTS = Path(__file__).resolve().parents[1]/'scripts'
sys.path.insert(0, str(SCRIPTS))
from daily_launch import DailyLaunch
from run_journal import RunJournal

NOW = datetime(2026, 10, 7, 0, tzinfo=timezone.utc)
PLAN = dict(day='2026-10-07', cutoff='2026-10-06', evaluated_at=NOW.isoformat(), targets={})
FLAGS = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


def until(predicate, seconds=15):
    deadline = time.monotonic()+seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.05)
    raise AssertionError('Timed out waiting for fixture')


class WorkerLifetime(unittest.TestCase):
    def test_parent_killed_worker_finishes_and_blocks_duplicate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'scripts').mkdir()
            (root/'scripts/daily_market.py').write_text("""import json,time
from pathlib import Path
root=Path.cwd()
print(json.dumps({'stage':'collection','output':str(root/'runtime/fixture')}),flush=True)
(root/'started').write_text('ready')
limit=time.monotonic()+15
while not (root/'release').exists() and time.monotonic()<limit: time.sleep(.05)
print(json.dumps({'state':'failed','output':str(root/'runtime/fixture'),'stages':{'collection':'completed'}}),flush=True)
(root/'done').write_text('complete')
""", encoding='utf-8')
            program = ('import sys;sys.path.insert(0,'+repr(str(SCRIPTS))+');'
                       'from daily_launch import DailyLaunch;from datetime import datetime;'
                       'DailyLaunch('+repr(str(root))+',planner=lambda n:'+repr(PLAN)+',accounts=False).once('
                       'datetime.fromisoformat('+repr(NOW.isoformat())+'),scheduled=True)')
            parent = subprocess.Popen([sys.executable, '-c', program], stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL, creationflags=FLAGS)
            try:
                until(lambda:(root/'started').exists())
                parent.terminate(); parent.wait(timeout=5)
                receipt = root/'runtime/dashboard-daily/2026-10-07.json'
                before = receipt.read_bytes()
                runner = Mock()
                gate = DailyLaunch(root, runner=runner, planner=lambda n:PLAN, accounts=False)
                self.assertTrue(gate.once(NOW, scheduled=True)['following'])
                self.assertTrue(gate.once(NOW, retry=True)['following'])
                self.assertEqual(before, receipt.read_bytes())
                runner.assert_not_called()
                (root/'release').touch()
                until(lambda:(root/'done').exists())
                life = next((root/'runtime/dashboard-daily/workers').glob('*.json'))
                until(lambda:json.loads(life.read_text())['finished_at'] is not None)
                body = ''.join(p.read_text() for p in (root/'runtime/dashboard-daily').glob('*.log'))
                self.assertIn('"collection": "completed"', body)
                self.assertNotIn('OSError', body)
                self.assertEqual(json.loads(life.read_text())['exit_code'], 0)
                # Market finishing alone must not pretend accounts/risk also completed.
                self.assertEqual(gate.once(NOW, scheduled=True)['state'], 'interrupted')
                runner.assert_not_called()
            finally:
                (root/'release').touch()
                if parent.poll() is None:
                    parent.terminate(); parent.wait(timeout=5)
                time.sleep(.4)

    def test_coordinator_failure_keeps_private_stack_location(self):
        with tempfile.TemporaryDirectory() as tmp:
            def fail(plan, progress):
                raise RuntimeError('PRIVATE_VALUE')
            gate=DailyLaunch(tmp,runner=fail,planner=lambda n:PLAN,accounts=False)
            record=gate.once(NOW)
            self.assertEqual(record['error_type'],'RuntimeError')
            self.assertTrue(record['error_frames'])
            self.assertNotIn('PRIVATE_VALUE',json.dumps(record))
            self.assertNotIn('error_frames',gate.public(record))

    def test_partial_lines_are_read_after_flush_and_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'scripts').mkdir()
            output = dict(state='failed', output=str(root/'runtime/fixture'), stages={})
            payload=json.dumps(output)
            (root/'scripts/daily_market.py').write_text(
                'import sys,time\nsys.stdout.write('+repr(payload[:15])+');sys.stdout.flush();time.sleep(.2)\n'
                'sys.stdout.write('+repr(payload[15:]+'\n')+');sys.stdout.flush()\n')
            gate=DailyLaunch(root,accounts=False);gate.folder.mkdir(parents=True)
            self.assertEqual(gate.execute(PLAN,lambda u:None),output)

    def test_journal_records_heartbeat_and_sanitized_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                with RunJournal(tmp,'fixture',interval=.02) as journal:
                    time.sleep(.06)
                    running=json.loads(journal.path.read_text())
                    self.assertGreater(running['heartbeat_at'],running['started_at'])
                    raise ValueError('PRIVATE_TOKEN_MUST_NOT_APPEAR')
            body=journal.path.read_text();value=json.loads(body)
            self.assertEqual(value['state'],'failed')
            self.assertEqual(value['error_type'],'ValueError')
            self.assertTrue(value['error_frames'])
            self.assertIsNotNone(value['finished_at'])
            self.assertNotIn('PRIVATE_TOKEN',body)


@unittest.skipUnless(sys.platform == 'win32','Windows launcher contract')
class ScheduledLauncher(unittest.TestCase):
    def fixture(self, root, delay, exit_code=0):
        source=SCRIPTS.parent
        shutil.copyfile(source/'scheduled-market.ps1',root/'scheduled-market.ps1')
        (root/'config.json').write_text((source/'config.json').read_text(encoding='utf-8'),encoding='utf-8')
        (root/'scripts').mkdir()
        (root/'scripts/scheduled_market.py').write_text(
            'import time,sys\nfrom pathlib import Path\nroot=Path.cwd()\n'
            'print("started",flush=True)\n(root/"started").touch()\n'
            +('limit=time.monotonic()+15\nwhile not (root/"release").exists() and time.monotonic()<limit:time.sleep(.05)\n' if delay else '')
            +'print("completed",flush=True)\n(root/"done").touch()\n'+f'sys.exit({exit_code})\n')
        return subprocess.Popen(['powershell.exe','-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass',
                                 '-File',str(root/'scheduled-market.ps1'),'-Check'],
                                stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=FLAGS)

    def test_console_wrapper_termination_does_not_kill_python(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);parent=self.fixture(root,True)
            try:
                until(lambda:(root/'started').exists())
                parent.terminate();parent.wait(timeout=5)
                (root/'release').touch()
                until(lambda:(root/'done').exists())
                log=next((root/'runtime/morning-schedule').glob('*.log'))
                self.assertIn('completed',log.read_text())
            finally:
                (root/'release').touch()
                if parent.poll() is None:parent.terminate();parent.wait(timeout=5)
                time.sleep(.4)

    def test_wrapper_preserves_failure_exit_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);parent=self.fixture(root,False,exit_code=2)
            self.assertEqual(parent.wait(timeout=15),2)
            receipt=next((root/'runtime/morning-schedule').glob('*-launcher.json'))
            self.assertEqual(json.loads(receipt.read_text(encoding='utf-8-sig'))['exit_code'],2)

    def test_wrapper_records_real_exit_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);parent=self.fixture(root,False)
            self.assertEqual(parent.wait(timeout=15),0)
            receipt=next((root/'runtime/morning-schedule').glob('*-launcher.json'))
            result=json.loads(receipt.read_text(encoding='utf-8-sig'))
            self.assertEqual(result['state'],'finished')
            self.assertEqual(result['exit_code'],0)
            self.assertIsNotNone(result['worker_pid'])


if __name__=='__main__':
    unittest.main()
