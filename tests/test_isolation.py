import json
from pathlib import Path
import socket
import tempfile
import threading
import unittest

import isolation
from run_isolated import selection


class IsolationTests(unittest.TestCase):
    def denied(self, name, *, language='python'):
        result = isolation.probe(name, language=language)
        self.assertEqual(result['status'], 'blocked', result)
        self.assertNotEqual(result['exit_code'], 0, result)
        self.assertTrue(result['guard_events'], result)
        saved = json.loads(Path(result['receipt']).read_text(encoding='utf-8'))
        self.assertEqual(saved['guard_events'], result['guard_events'])
        self.assertEqual(result['live_children'], 0)

    def test_protected_paths(self):
        for language in ('python', 'node'):
            for action in ('read', 'write', 'truncate', 'enumerate', 'rename', 'delete'):
                with self.subTest(language=language, action=action):
                    self.denied(action, language=language)
        for action in ('recursive', 'recursive_read', 'glob', 'mkdtemp'):
            self.denied(action, language='node')

    def test_sqlite_uri(self):
        self.denied('sqlite')
        self.denied('sqlite_attach')

    def test_path_escape(self):
        for language in ('python', 'node'):
            for action in ('escape', 'link', 'drive'):
                with self.subTest(language=language, action=action):
                    self.denied(action, language=language)

    def test_swallowed_violation_fails_run(self):
        for language in ('python', 'node'):
            self.denied('swallow', language=language)

    def test_network_and_credentials_denied(self):
        for language in ('python', 'node'):
            for action in ('dns', 'reverse_dns', 'connect', 'unowned', 'bind', 'credentials', 'registry', 'process'):
                with self.subTest(language=language, action=action):
                    self.denied(action, language=language)
        self.denied('native_import')
        self.denied('resolver', language='node')
        self.denied('esm_native', language='node')

    def test_loopback_and_socketpair_allowed(self):
        with socket.socket() as server:
            server.bind(('127.0.0.1', 0))
            server.listen()
            def echo():
                connection, _ = server.accept()
                with connection:
                    connection.sendall(connection.recv(16))
            thread = threading.Thread(target=echo)
            thread.start()
            with socket.create_connection(server.getsockname(), timeout=2) as client:
                client.sendall(b'fixture')
                self.assertEqual(client.recv(16), b'fixture')
            thread.join(2)
            self.assertFalse(thread.is_alive())
        a, b = socket.socketpair()
        try:
            a.sendall(b'pair')
            self.assertEqual(b.recv(4), b'pair')
        finally:
            a.close(); b.close()
        self.assertEqual(isolation.ACTIVE.events, [])

    def test_child_guard_handshake(self):
        for language in ('python', 'node'):
            good = isolation.probe('ready', language=language)
            self.assertEqual(good['status'], 'passed', good)
            self.assertTrue(good['handshake'])
        bad = isolation.probe('not_ready')
        self.assertEqual(bad['status'], 'unverified', bad)
        self.assertFalse(bad['handshake'])
        self.assertFalse(bad['executed'])
        with self.assertRaisesRegex(ValueError, 'unregistered_probe'):
            isolation.probe('powershell -Command malicious')
        for field, value in (('python_executable', 'powershell.exe'), ('read_roots', ['C:/']),
                             ('protected', []), ('code', 'C:/Windows')):
            with self.assertRaisesRegex(ValueError, 'child_configuration_changed'):
                isolation.launch(dict(isolation.ACTIVE.config, **{field: value}))
        self.denied('process')

    def test_child_timeout_cleanup(self):
        result = isolation.probe('sleep', timeout=.5)
        self.assertEqual(result['status'], 'failed', result)
        self.assertEqual(result['reason'], 'timeout', result)
        self.assertTrue(result['cleaned'])
        self.assertEqual(result['live_children'], 0)
        self.assertNotEqual(result['exit_code'], 0)
        tree = isolation.probe('sleep_tree', timeout=3)
        self.assertEqual(tree['reason'], 'timeout', tree)
        self.assertGreaterEqual(tree['active_before_cleanup'], 2, tree)
        self.assertGreaterEqual(tree['job_processes'], 2, tree)
        self.assertEqual(tree['live_children'], 0, tree)
        self.assertTrue(tree['cleaned'], tree)

    def test_manifest_selection(self):
        actual = Path(isolation.ACTIVE.config['code'])
        registered, full, excluded = selection(actual, 'M00')
        self.assertEqual(registered, full)
        self.assertTrue(excluded)
        self.assertIn('tests/test_isolation_node.cjs', full)
        self.assertEqual(selection(actual, 'M00', [full[0]])[1], [full[0]])
        for module, nodes in [('M99', None), ('../M00', None), ('M00', ['missing']), ('M00', [full[0], full[0]])]:
            with self.assertRaises(ValueError):
                selection(actual, module, nodes)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            folder = root / 'tests/suites'; folder.mkdir(parents=True)
            (root / 'tests/test_fake.py').write_text('')
            manifest = folder / 'M00.txt'
            for text in ('', '../escape.cjs', 'test_missing.X.test_a',
                         'test_fake.X.test_a\ntest_fake.X.test_a'):
                manifest.write_text(text)
                with self.assertRaises(ValueError):
                    selection(root, 'M00')

    def test_result_accounting(self):
        results = [isolation.probe(name) for name in ('ready', 'failure', 'skip', 'not_ready', 'swallow')]
        counts = isolation.count_results(results)
        self.assertEqual(counts, dict(selected=5, executed=4, passed=1, failed=1, skipped=1, unverified=1, blocked=1))
        self.assertEqual(len({r['receipt'] for r in results}), 5)

    def test_interrupted_run(self):
        result = isolation.probe('interrupt')
        self.assertEqual(result['status'], 'failed', result)
        self.assertEqual(result['reason'], 'interrupted', result)
        self.assertTrue(result['cleaned'])
        self.assertEqual(result['live_children'], 0)
