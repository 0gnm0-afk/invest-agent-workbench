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

    def test_loopback_server_reuse_checks_identity_without_launching(self):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        requests = []
        identity = dict(workspace=str(self.root), instance_id='fixture', protocol=1,
                        state='ready', offline=False)
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_GET(self):
                requests.append(self.path)
                payload = json.dumps(identity).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            def do_POST(self):
                raise AssertionError('saved_reopen_must_not_launch')
        with patch('socket.getfqdn', return_value='127.0.0.1'), patch('urllib.request.getproxies', return_value={}):
            server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            url = 'http://127.0.0.1:' + str(server.server_port)
            instance.publish(self.root, dict(self.value, url=url, dashboard_url=url, instance_id='fixture'))
            try:
                for arguments in ([], ['--saved-accounts']):
                    result = instance.reuse(self.root, instance.existing(self.root), arguments)
                    self.assertTrue(result['reused'])
                    self.assertEqual(result['url'], url)
                self.assertEqual(requests, ['/api/instance', '/api/instance'])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(2)
            self.assertFalse(thread.is_alive())

    def test_desktop_fixture_command_closes_handshake_pipe(self):
        import io
        import subprocess
        from contextlib import redirect_stdout
        from types import SimpleNamespace
        value = dict(url=self.value['url'], reused=True)
        stream = io.BytesIO((json.dumps(value) + '\n').encode())
        child = SimpleNamespace(stdout=stream)
        with patch.object(startup, 'ROOT', self.root), patch.object(subprocess, 'Popen', return_value=child) as popen, redirect_stdout(io.StringIO()):
            result = startup.desktop(['--offline', '--saved-accounts'])
        self.assertEqual(result, value)
        self.assertTrue(stream.closed)
        command = popen.call_args.args[0]
        self.assertEqual(command[1:], ['-B', '-u', str(self.root/'scripts/dashboard_startup.py'),
                                      '--detached', '--offline', '--saved-accounts'])
        self.assertEqual(popen.call_args.kwargs['cwd'], self.root)
        self.assertEqual(popen.call_args.kwargs['creationflags'], getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.assertEqual(popen.call_args.kwargs['stdin'], subprocess.DEVNULL)

    def test_daily_flags_reach_existing_server_but_saved_reopen_is_read_only(self):
        with patch.object(instance, 'request', return_value={'workspace':str(self.root),'token':'local'}) as request:
            for arguments in ([], ['--saved-accounts']):
                instance.reuse(self.root, self.value, arguments)
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
        self.assertEqual(json.loads(diagnostic)['error_type'], 'ValueError')
        self.assertTrue(json.loads(diagnostic)['error_frames'])
        self.assertNotIn('reuse_fixture_failure', diagnostic)
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
        diagnostic = (self.root/'runtime/dashboard/launch-error.log').read_text(encoding='utf-8')
        self.assertEqual(json.loads(diagnostic)['error_type'], 'OSError')
        self.assertNotIn('lease_fixture_failure', diagnostic)

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
        for status in ({'workspace':str(self.root),'instance_id':'old','protocol':1},
                       {'workspace':str(self.root/'other'),'instance_id':'current','protocol':1},
                       {'workspace':str(self.root),'instance_id':'current','protocol':2}):
            with self.subTest(status=status), patch.object(instance,'request',return_value=status):
                with self.assertRaisesRegex(ValueError, 'incompatible'):
                    instance.existing(self.root)

    def test_offline_refresh_and_retry_without_refresh_are_rejected(self):
        with patch.object(instance, 'request') as request:
            with self.assertRaisesRegex(ValueError, 'offline_cannot_refresh'):
                instance.reuse(self.root, dict(self.value, offline=True), ['--offline', '--daily-refresh'])
            with self.assertRaisesRegex(ValueError, 'retry_market_requires'):
                instance.reuse(self.root, self.value, ['--retry-market'])
        request.assert_not_called()

    def test_daily_without_saved_accounts_still_requests_saved_observations(self):
        with patch.object(instance, 'request', return_value={'workspace':str(self.root),'token':'local'}) as request:
            instance.reuse(self.root, self.value, ['--daily-refresh'])
        self.assertEqual(request.call_args.args[1:3], ('/api/launch',dict(daily_refresh=True,retry_market=False,saved_accounts=True)))



if __name__ == '__main__':
    unittest.main()
