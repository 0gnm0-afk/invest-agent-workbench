import http.client
import json
import sys
import threading
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from risk_preview import demo, event_demo, handler_for, ThreadingHTTPServer


class Preview(unittest.TestCase):
    def setUp(self):
        self.data = demo()
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler_for(self.data, 'synthetic-token'))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def request(self, path, body=None, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port)
        try:
            conn.request('GET' if body is None else 'POST', path, body, headers or {})
            response = conn.getresponse()
            return response.status, response.read()
        finally:
            conn.close()

    def test_only_known_synthetic_assets_and_endpoints(self):
        for path in ('/', '/risk.js', '/risk.css', '/format.js', '/api/risk-demo'):
            self.assertEqual(self.request(path)[0], 200)
        for path in ('/config.json', '/plan.md', '/api/accounts', '/api/analysis-rpc', '/../STATUS.md'):
            self.assertEqual(self.request(path)[0], 404)
        self.assertEqual(self.request('/', headers={'Host': 'attacker.example'})[0], 403)

    def test_post_boundary_and_calculation(self):
        body = json.dumps({'scenario': self.data['scenario'], 'example': 'complete'})
        headers = {'Content-Type': 'application/json', 'Origin': 'http://127.0.0.1:'+str(self.server.server_port), 'X-Risk-Preview-Token': 'synthetic-token'}
        self.assertEqual(self.request('/api/risk-calculate', body)[0], 403)
        bad = {**headers, 'Origin': 'http://attacker.example'}
        self.assertEqual(self.request('/api/risk-calculate', body, bad)[0], 403)
        status, result = self.request('/api/risk-calculate', body, headers)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(result)['response']['total'], '1062500.0')
        self.assertEqual(self.request('/api/risk-calculate', '{}', headers)[0], 400)

    def test_fake_events_survive_restart_without_unknown_retry(self):
        result = event_demo()
        self.assertEqual(result['fake_calls'], 2)
        self.assertIsNone(result['after_restart'])
        self.assertTrue(result['transitions'][2]['latched'])
        self.assertEqual(result['events'][-1]['status'], 'unknown')


if __name__ == '__main__': unittest.main()
