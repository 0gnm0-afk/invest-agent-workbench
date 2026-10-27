import io
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import launch.startup as startup


class Startup(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / 'shell').mkdir()
        self.ready = self.root / 'data-ready.json'
        self.state = {}
        (self.root / 'shell/index.html').write_text('<head><link rel="stylesheet" href="/style.css"><link rel="stylesheet" href="/financial-focus.css"><script defer src="/app.js"></script></head><div class="loading">저장 자료를 불러오고 있습니다…</div>', encoding='utf-8')
        (self.root / 'shell/style.css').write_text('body{}')
        (self.root / 'shell/startup.js').write_text('void 0;')
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), startup.handler_for(self.root, self.state, self.ready))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = 'http://127.0.0.1:' + str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.temp.cleanup()

    def get(self, path):
        with urllib.request.urlopen(self.url + path, timeout=2) as response:
            return response.read().decode('utf-8')

    def test_shell_and_status_respond_while_builder_is_blocked(self):
        blocked = threading.Event()
        entered = threading.Event()
        def slow_main():
            entered.set()
            blocked.wait(5)
        with patch.dict(sys.modules, {'web.server': type('FakeDashboard', (), {'main': staticmethod(slow_main)})}), patch.object(sys, 'argv', []):
            worker = threading.Thread(target=startup.run_dashboard, args=([], self.ready, self.state), daemon=True)
            worker.start()
            try:
                self.assertTrue(entered.wait(2))
                page = self.get('/')
                self.assertIn('스크리닝', page)
                self.assertIn('/startup.js', page)
                self.assertNotIn('/financial-focus.css', page)
                self.assertNotIn('/app.js', page)
                self.assertEqual(json.loads(self.get('/api/startup')), {'state': 'loading'})
                self.assertEqual(self.get('/style.css'), 'body{}')
                self.assertTrue(worker.is_alive())
            finally:
                blocked.set()
                worker.join(2)

    def test_ready_redirect_is_local_and_never_exposes_token_or_path(self):
        self.ready.write_text(json.dumps({'url': self.url, 'token': 'SECRET', 'output': 'PRIVATE'}))
        self.assertEqual(json.loads(self.get('/api/startup')), {'state': 'ready', 'url': self.url})
        self.ready.write_text(json.dumps({'url': 'http://example.com:80'}))
        self.assertEqual(json.loads(self.get('/api/startup')), {'state': 'loading'})

    def test_partial_receipt_and_failure_are_distinct(self):
        self.ready.write_text('{')
        self.assertEqual(json.loads(self.get('/api/startup'))['state'], 'loading')
        self.state['failed'] = True
        self.assertEqual(json.loads(self.get('/api/startup'))['state'], 'failed')

    def test_worker_failure_persists_traceback_without_console_output(self):
        def failed_main():
            raise RuntimeError('preparation_fixture_failure')
        with patch.dict(sys.modules, {'web.server': type('FailedDashboard', (), {'main': staticmethod(failed_main)})}), patch.object(sys, 'argv', []), patch.object(sys, 'stderr', io.StringIO()):
            startup.run_dashboard([], self.ready, self.state)
        self.assertTrue(self.state['failed'])
        diagnostic = self.ready.with_suffix('.err.log').read_text(encoding='utf-8')
        self.assertIn('RuntimeError: preparation_fixture_failure', diagnostic)
        self.assertEqual(json.loads(self.get('/api/startup'))['state'], 'failed')

    def test_foreign_host_and_unlisted_paths_are_blocked(self):
        request = urllib.request.Request(self.url + '/', headers={'Host': 'example.com'})
        with self.assertRaises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(request, timeout=2)
        self.assertEqual(exc.exception.code, 403)
        with self.assertRaises(urllib.error.HTTPError) as exc:
            self.get('/config.json')
        self.assertEqual(exc.exception.code, 404)

    def test_cli_publishes_shell_before_worker_and_preserves_flags(self):
        receipt = self.root / 'shell-ready.json'
        launched = []
        class FakeThread:
            def __init__(self, *, target, args, daemon):
                self.target, self.args = target, args
            def start(self):
                self_assert = json.loads(receipt.read_text())
                assert self_assert['state'] == 'loading'
                launched.append((self.target, self.args))
        fake_server = type('Server', (), {'server_port': 12345, 'serve_forever': lambda self: None})()
        with patch.object(startup, 'ROOT', self.root), patch.object(startup, 'serve_requests'), patch.object(sys, 'argv', ['startup', '--ready-file', str(receipt), '--daily-refresh', '--saved-accounts', '--open-browser']), patch.object(startup, 'ThreadingHTTPServer', return_value=fake_server), patch.object(startup.threading, 'Thread', FakeThread):
            startup.main()
        self.assertEqual(launched[0][0], startup.run_dashboard)
        self.assertEqual(launched[0][1][0], ['--daily-refresh', '--saved-accounts'])
        self.assertEqual(launched[1][0], startup.webbrowser.open)
        self.assertEqual(launched[1][1], ('http://127.0.0.1:12345',))


if __name__ == '__main__':
    unittest.main()
