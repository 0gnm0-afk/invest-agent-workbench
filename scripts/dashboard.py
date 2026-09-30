"""Private loopback dashboard; serves only curated views and bundled assets."""
import argparse
import json
import mimetypes
import secrets
import threading
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from dashboard_data import ROOT, build


def handler_for(data, stocks, assets, token):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def send(self, body, kind='application/json; charset=utf-8', status=200):
            self.send_response(status)
            self.send_header('Content-Type', kind)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            expected = '127.0.0.1:' + str(self.server.server_port)
            if self.headers.get('Host') != expected or self.headers.get('Origin', 'http://' + expected) != 'http://' + expected:
                return self.send(b'{"error":"local_origin_required"}', status=403)
            path = urllib.parse.urlsplit(self.path).path
            if path == '/health':
                return self.send(json.dumps({'app': 'invest-workbench', 'token': token}).encode())
            if path == '/api/home':
                return self.send(json.dumps(data, ensure_ascii=False, allow_nan=False).encode('utf-8'))
            if path.startswith('/api/stock/'):
                key = urllib.parse.unquote(path[len('/api/stock/'):])
                value = stocks.get(key)
                if value is None:
                    value = {'state': 'unavailable', 'reason': 'saved_price_series_missing', 'bars': []}
                return self.send(json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8'))
            names = {'/': 'index.html', '/app.js': 'app.js', '/style.css': 'style.css',
                     '/format.js': 'format.js', '/charts.js': 'charts.js', '/workbench.png': 'workbench.png',
                     '/vendor/lightweight-charts.js': 'vendor/lightweight-charts.js',
                     '/vendor/NOTICE': 'vendor/NOTICE', '/vendor/LICENSE': 'vendor/LICENSE'}
            if path not in names:
                return self.send(b'{"error":"not_found"}', status=404)
            file = assets / names[path]
            if not file.is_file():
                return self.send(b'{"error":"asset_missing"}', status=404)
            kind = mimetypes.guess_type(file.name)[0] or 'text/plain'
            if file.suffix == '.js':
                kind = 'text/javascript'
            return self.send(file.read_bytes(), kind + ('; charset=utf-8' if kind.startswith('text/') else ''))

        def do_POST(self):
            self.send(b'{"error":"read_only"}', status=405)
    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--open-browser', action='store_true')
    parser.add_argument('--saved-accounts', action='store_true')
    parser.add_argument('--build-only', action='store_true')
    parser.add_argument('--ready-file', type=Path)
    parser.add_argument('--port', type=int, default=0)
    args = parser.parse_args()
    output, data, stocks = build(refresh=not args.saved_accounts)
    if args.build_only:
        print(json.dumps({'output': str(output), 'market_state': data['market']['state'],
                          'account_query_state': data['accounts']['query_state']}))
        return
    token = secrets.token_urlsafe(18)
    server = ThreadingHTTPServer(('127.0.0.1', args.port), handler_for(data, stocks, ROOT / 'dashboard', token))
    server.daemon_threads = True
    ready = {'url': 'http://127.0.0.1:' + str(server.server_port), 'output': str(output), 'token': token}
    if args.ready_file:
        args.ready_file.write_text(json.dumps(ready), encoding='utf-8')
    print(json.dumps(ready), flush=True)
    if args.open_browser:
        webbrowser.open(ready['url'])
    server.serve_forever()


if __name__ == '__main__':
    main()
