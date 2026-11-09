"""Real OS leases and launcher reuse, with synthetic local servers only."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import launch.instance as instance
import launch.startup as startup


class Instance(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.value = dict(url='http://127.0.0.1:12345', dashboard_url='http://127.0.0.1:12346',
                          pid=123, state='ready', offline=False)

    def tearDown(self):
        self.temp.cleanup()

    def test_lease_blocks_second_owner_and_releases_without_deleting_lock(self):
        first = instance.acquire(self.root)
        try:
            self.assertIsNotNone(first)
            self.assertIsNone(instance.acquire(self.root))
        finally:
            instance.release(first)
        second = instance.acquire(self.root)
        self.assertIsNotNone(second)
        instance.release(second)

    def test_loading_launch_reuses_without_loading_builder(self):
        receipt = self.root/'ready.json'
        value = dict(self.value, state='loading', dashboard_url=None)
        with patch.object(startup, 'ROOT', self.root), patch.object(startup, 'acquire', return_value=None), patch.object(startup, 'existing', return_value=value), patch.object(startup, 'serve') as build, patch.object(sys, 'argv', ['startup','--saved-accounts','--ready-file',str(receipt)]):
            startup.main()
        build.assert_not_called()
        self.assertTrue(json.loads(receipt.read_text())['reused'])
        self.assertEqual(json.loads(receipt.read_text())['url'], value['url'])

    def test_repeated_reopen_needs_no_ready_files_or_logs(self):
        with patch.object(startup, 'ROOT', self.root), patch.object(startup, 'acquire', return_value=None), patch.object(startup, 'existing', return_value=self.value), patch.object(startup, 'serve') as build, patch.object(sys, 'argv', ['startup', '--saved-accounts']), patch.object(sys, 'stdout', None):
            for _ in range(30):
                startup.main()
        build.assert_not_called()
        self.assertFalse(list(self.root.rglob('*')))

    def test_repeated_failure_overwrites_bounded_diagnostic(self):
        with patch.object(startup, 'ROOT', self.root), patch.object(startup, 'acquire', side_effect=OSError('x'*100000)), patch.object(sys, 'argv', ['startup']), patch.object(sys, 'stdout', None), patch.object(sys, 'stderr', None):
            for _ in range(10):
                with self.assertRaises(SystemExit):
                    startup.main()
        files = [p for p in self.root.rglob('*') if p.is_file()]
        self.assertEqual(len(files), 1)
        self.assertLessEqual(files[0].stat().st_size, 65536)

    def test_desktop_pipe_reuses_real_detached_server_and_restart_keeps_file_count(self):
        import subprocess
        from contextlib import redirect_stdout
        import io
        scripts = Path(__file__).resolve().parents[1]/'scripts'
        (self.root/'scripts').mkdir()
        code = ("import sys, json\nfrom pathlib import Path\n"
                + "sys.path.insert(0, " + repr(str(scripts)) + ")\n"
                + "import launch.startup as s\ns.ROOT = Path(" + repr(str(self.root)) + ")\n"
                + "def builder(arguments, ready, state):\n"
                + "    value=json.loads((s.ROOT/'runtime/dashboard/server.json').read_text())\n"
                + "    s.atomic_write(ready, json.dumps({'url': value['url']}))\n"
                + "s.run_dashboard=builder\ns.main()\n")
        (self.root/'scripts/dashboard_startup.py').write_text(code, encoding='utf-8')
        processes = []
        real_popen = subprocess.Popen
        def popen(*args, **kwargs):
            child = real_popen(*args, **kwargs)
            processes.append(child)
            return child
        def files():
            return sorted(str(p.relative_to(self.root)) for p in (self.root/'runtime').rglob('*') if p.is_file())
        try:
            with patch.object(startup, 'ROOT', self.root), patch.object(subprocess, 'Popen', side_effect=popen), redirect_stdout(io.StringIO()):
                first = startup.desktop(['--offline', '--saved-accounts'])
                for _ in range(8):
                    result = startup.desktop(['--offline', '--saved-accounts'])
                    self.assertTrue(result['reused'])
                    self.assertEqual(result['pid'], first['pid'])
                    processes[-1].wait(timeout=5)
                before = files()
                self.assertEqual(len(before), 3)  # lease, current shell, current data readiness
                processes[0].terminate()
                processes[0].wait(timeout=5)
                second = startup.desktop(['--offline', '--saved-accounts'])
                self.assertNotEqual(second['pid'], first['pid'])
                # Ensure the second builder has published before comparing files.
                for _ in range(100):
                    if (self.root/'runtime/dashboard/data-ready.json').exists():
                        break
                    import time
                    time.sleep(.01)
                self.assertEqual(files(), before)
        finally:
            for child in processes:
                if child.poll() is None:
                    child.terminate()
                child.wait(timeout=5)

    def test_daily_flags_reach_existing_server_but_saved_reopen_is_read_only(self):
        with patch.object(instance, 'request', return_value={'workspace':str(self.root),'token':'local'}) as request:
            instance.reuse(self.root, self.value, ['--saved-accounts'])
            request.assert_not_called()
            instance.reuse(self.root, self.value, ['--daily-refresh','--retry-market','--saved-accounts'])
            self.assertEqual(request.call_args.args[1:3], ('/api/launch',dict(daily_refresh=True,retry_market=True,saved_accounts=True)))

    def test_pythonw_reuse_failure_is_logged_without_starting_another_server(self):
        with patch.object(startup, 'ROOT', self.root), patch.object(startup, 'acquire', return_value=None), patch.object(startup, 'existing', return_value=self.value), patch.object(startup, 'reuse', side_effect=ValueError('reuse_fixture_failure')), patch.object(startup, 'serve') as build, patch.object(sys, 'argv', ['startup', '--daily-refresh', '--open-browser']), patch.object(sys, 'stdout', None), patch.object(sys, 'stderr', None):
            try:
                with self.assertRaises(SystemExit) as error:
                    startup.main()
                self.assertEqual(error.exception.code, 1)
            finally:
                for stream in (sys.stdout, sys.stderr):
                    if stream is not None:
                        stream.close()
        build.assert_not_called()
        logs = list((self.root/'runtime/dashboard').glob('launch-error.log'))
        self.assertEqual(len(logs), 1)
        diagnostic = logs[0].read_text(encoding='utf-8')
        self.assertIn('Traceback (most recent call last)', diagnostic)
        self.assertIn('ValueError: reuse_fixture_failure', diagnostic)
        self.assertFalse(list((self.root/'runtime/dashboard').glob('*.json')))

    def test_pythonw_lease_failure_uses_explicit_receipt_log_before_serve(self):
        receipt = self.root/'diagnostics'/'ready.json'
        with patch.object(startup, 'ROOT', self.root), patch.object(startup, 'acquire', side_effect=OSError('lease_fixture_failure')), patch.object(startup, 'serve') as build, patch.object(sys, 'argv', ['startup', '--ready-file', str(receipt)]), patch.object(sys, 'stdout', None), patch.object(sys, 'stderr', None):
            try:
                with self.assertRaises(SystemExit) as error:
                    startup.main()
                self.assertEqual(error.exception.code, 1)
            finally:
                for stream in (sys.stdout, sys.stderr):
                    if stream is not None:
                        stream.close()
        build.assert_not_called()
        self.assertFalse(receipt.exists())
        self.assertIn('OSError: lease_fixture_failure', (self.root/'runtime/dashboard/launch-error.log').read_text(encoding='utf-8'))

    def test_slow_loading_probe_waits_for_existing_owner(self):
        loading = dict(self.value,state='loading',dashboard_url=None)
        with patch.object(instance, 'existing', side_effect=[TimeoutError(),self.value]), patch.object(instance.time,'sleep'), patch.object(instance,'request',return_value={'workspace':str(self.root),'token':'local'}):
            result = instance.reuse(self.root,loading,['--daily-refresh','--saved-accounts'])
        self.assertTrue(result['reused'])
        self.assertEqual(result['url'],self.value['dashboard_url'])

    def test_offline_mismatch_and_failed_builder_do_not_start_another_server(self):
        with self.assertRaisesRegex(ValueError, 'mode_conflict'):
            instance.reuse(self.root, self.value, ['--offline'])
        with self.assertRaisesRegex(ValueError, 'startup_failed'):
            instance.reuse(self.root, dict(self.value,state='failed'), [])

    def test_stale_or_other_workspace_receipt_is_not_reused(self):
        instance.publish(self.root, {**self.value,'instance_id':'current'})
        with patch.object(instance,'request',return_value={'workspace':str(self.root),'instance_id':'old','protocol':1}):
            with self.assertRaisesRegex(ValueError, 'incompatible'):
                instance.existing(self.root)


if __name__ == '__main__':
    unittest.main()
