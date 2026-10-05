"""Isolated synthetic preview. Never imports dashboard_data, a broker, or a bridge.

No arbitrary root/config/DB path switches: serves only a fixed UI allowlist and
calculates against the bundled synthetic account. Not an operational server.
"""
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import argparse
import json
import secrets
import sys
import tempfile

from portfolio_scenarios import compare
from risk_adapter import project_plan, assemble
from risk_events import EventStore, FakeSender

ROOT = Path(__file__).resolve().parents[1]


def demo():
    sys.path.insert(0, str(ROOT/'tests'))
    from test_risk_adapter import integration_fixture
    envelope, snapshot, assumptions = integration_fixture()
    projection = project_plan(envelope, snapshot, scenario_id='s1', assumptions=assumptions)
    scenario = assemble([projection], assumptions)
    scenario['prices']['demo/KR'] = {'price': '800', 'horizon': assumptions['horizon'], 'source_ref': 'explicit-synthetic-KR-assumption'}
    return {'snapshot': snapshot, 'scenario': scenario, 'shared': envelope, 'projection': projection,
            'result': compare(snapshot, scenario), 'connection': 'collaboration.v1 합성 계약 연결 · 운영 DB 미연결'}


def event_demo():
    data = demo()
    condition = data['projection']['conditions'][0]
    observation = deepcopy(data['scenario']['response']['path'][0]['observations']['demo/US'])
    observation['evaluated_at'] = observation['as_of']
    plan = {'id': data['shared']['plan_id'], 'version': 1, 'symbol': 'ALPHA', 'source_ref': data['shared']['plan_record']['id']}
    active = [(plan['id'], 1)]
    with tempfile.TemporaryDirectory(prefix='risk-synthetic-') as temp:
        path = Path(temp)/'events.sqlite3'
        store = EventStore(path)
        transitions = []
        try:
            for label, price in [('조건 충족', '89'), ('반복 관측', '88'), ('자료 소실', None), ('유효 자료로 해소', '91'), ('다시 충족', '87')]:
                observation.update(snapshot_id=label, price=price)
                result = store.observe(plan, condition, observation, active_refs=active)
                transitions.append({'label': label, **result})
            sender = FakeSender(['failed', 'unknown'])
            deliveries = [store.deliver(sender, active_refs=active, now=0), store.deliver(sender, active_refs=active, now=30)]
        finally:
            store.close()
        reopened = EventStore(path)
        try:
            repeated = reopened.deliver(sender, active_refs=active, now=100)
            return {'transitions': transitions, 'deliveries': deliveries, 'after_restart': repeated,
                    'events': reopened.events(), 'fake_calls': len(sender.calls)}
        finally:
            reopened.close()


def handler_for(data, token):
    assets = {'/': ('risk-preview.html','text/html'), '/risk.js': ('risk.js','text/javascript'),
              '/risk.css': ('risk.css','text/css'), '/format.js': ('format.js','text/javascript')}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def send(self, status, value, mime='application/json'):
            raw = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', mime+'; charset=utf-8')
            self.send_header('Content-Length', str(len(raw)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(raw)

        def host_ok(self):
            return self.headers.get('Host') == '127.0.0.1:'+str(self.server.server_port)

        def do_GET(self):
            if not self.host_ok():
                return self.send(403, {'error': 'loopback_host_required'})
            if self.path in assets:
                file, mime = assets[self.path]
                return self.send(200, (ROOT/'dashboard'/file).read_bytes(), mime)
            if self.path == '/api/risk-demo':
                return self.send(200, {**data, 'token': token})
            return self.send(404, {'error': 'not_found'})

        def do_POST(self):
            origin = 'http://127.0.0.1:'+str(self.server.server_port)
            if not self.host_ok() or self.headers.get('Origin') != origin or not secrets.compare_digest(self.headers.get('X-Risk-Preview-Token', ''), token):
                return self.send(403, {'error': 'origin_and_session_required'})
            if self.path not in ('/api/risk-calculate', '/api/risk-events-demo'):
                return self.send(404, {'error': 'not_found'})
            try:
                length = int(self.headers.get('Content-Length', '-1'))
                if not 0 <= length <= 131072:
                    return self.send(413, {'error': 'invalid_body_length'})
                payload = json.loads(self.rfile.read(length))
                if self.path == '/api/risk-events-demo':
                    return self.send(200, event_demo())
                if not isinstance(payload, dict) or set(payload) != {'scenario', 'example'}:
                    raise ValueError('scenario_and_example_required')
                snapshot = deepcopy(data['snapshot'])
                if payload['example'] == 'partial':
                    snapshot.update(complete=False, issues=['CMA·부채 대조 미완료'])
                elif payload['example'] == 'missing_fx':
                    snapshot['fx'].pop('USD', None)
                elif payload['example'] != 'complete':
                    raise ValueError('unknown_example')
                return self.send(200, compare(snapshot, payload['scenario']))
            except (ValueError, KeyError, TypeError, ArithmeticError):
                return self.send(400, {'error': 'calculation_input_invalid'})

    return Handler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=0)
    args = parser.parse_args()
    server = ThreadingHTTPServer(('127.0.0.1', args.port), handler_for(demo(), secrets.token_urlsafe(32)))
    print('Synthetic risk preview: http://127.0.0.1:'+str(server.server_port), flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
