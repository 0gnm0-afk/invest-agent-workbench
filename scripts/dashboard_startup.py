"""Show the local dashboard shell before importing or building its data."""
import argparse
import json
import re
import sys
import threading
import traceback
import urllib.parse
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def loading_page(assets):
    page = (assets / 'index.html').read_text(encoding='utf-8')
    page = re.sub(r'<script\b[^>]*>.*?</script>', '', page, flags=re.S)
    page = re.sub(r'<link\b[^>]*href="/(?:chart-tools|financials|risk-workbench|market-exclusions)\.css"[^>]*>', '', page)
    return page.replace('</head>', '<script defer src="/startup.js"></script></head>').replace(
        '<div class="loading">저장 자료를 불러오고 있습니다…</div>',
        '<section aria-live="polite"><p class="eyebrow">PRIVATE WORKBENCH</p>'
        '<h1>투자 작업대</h1><div class="notice" id="startup-message">저장 자료를 불러오고 있습니다…</div>'
        '<p class="sub">준비가 끝나면 선택한 화면으로 자동 연결합니다.</p>'
        '<div class="stats" aria-hidden="true">' + ''.join(
            '<div class="stat"><span class="label">' + label + '</span>'
            '<div class="value">…</div><span class="detail">불러오는 중</span></div>'
            for label in ('시장 자료', '섹터 관찰', '스크리닝', '계좌 자료')) + '</div></section>')


def public_status(state, ready_file):
    if state.get('failed'):
        return {'state': 'failed', 'error': 'dashboard_startup_failed'}
    try:
        result = json.loads(ready_file.read_text(encoding='utf-8'))
        url = urllib.parse.urlsplit(result['url'])
        if url.scheme != 'http' or url.hostname != '127.0.0.1' or not url.port or url.path or url.query or url.fragment or url.username or url.password:
            raise ValueError('invalid_dashboard_url')
        return {'state': 'ready', 'url': result['url']}
    except (OSError, ValueError, KeyError, TypeError):
        return {'state': 'loading'}


def handler_for(assets, state, ready_file):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            expected = '127.0.0.1:' + str(self.server.server_port)
            if self.headers.get('Host') != expected or self.headers.get('Origin', 'http://' + expected) != 'http://' + expected:
                return self.send(b'{"error":"local_origin_required"}', 'application/json', 403)
            path = urllib.parse.urlsplit(self.path).path
            if path == '/api/startup':
                return self.send(json.dumps(public_status(state, ready_file)).encode(), 'application/json')
            if path == '/':
                return self.send(loading_page(assets).encode('utf-8'), 'text/html; charset=utf-8')
            files = {'/style.css': ('style.css', 'text/css'), '/startup.js': ('startup.js', 'text/javascript'),
                     '/vendor/NOTICE': ('vendor/NOTICE', 'text/plain')}
            if path not in files:
                return self.send(b'{"error":"not_found"}', 'application/json', 404)
            file, kind = files[path]
            return self.send((assets / file).read_bytes(), kind + '; charset=utf-8')

        def send(self, body, kind, status=200):
            self.send_response(status)
            self.send_header('Content-Type', kind)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
            self.end_headers()
            self.wfile.write(body)
    return Handler


def run_dashboard(arguments, ready_file, state):
    try:
        # Imports and the unchanged snapshot builder run after shell readiness.
        sys.argv = [str(ROOT / 'scripts/dashboard.py'), '--ready-file', str(ready_file), *arguments]
        from dashboard import main
        main()
        state['failed'] = True
    except BaseException:
        state['failed'] = True
        traceback.print_exc()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ready-file', type=Path)
    parser.add_argument('--open-browser', action='store_true')
    args, remaining = parser.parse_known_args()
    if args.ready_file is None:
        args.ready_file = ROOT / 'runtime/dashboard' / ('ready-' + uuid.uuid4().hex + '.json')
    args.ready_file.parent.mkdir(parents=True, exist_ok=True)
    inner_ready = args.ready_file.with_name(args.ready_file.stem + '-data.json')
    # Reject a reused receipt instead of ever redirecting to an old server.
    if args.ready_file.exists() or inner_ready.exists():
        parser.error('ready-file must be new for each launch')
    # pythonw desktop launches have no console; keep diagnostics in private runtime.
    if sys.stdout is None:
        sys.stdout = args.ready_file.with_suffix('.log').open('a', encoding='utf-8')
    if sys.stderr is None:
        sys.stderr = args.ready_file.with_suffix('.err.log').open('a', encoding='utf-8')
    state = {}
    server = ThreadingHTTPServer(('127.0.0.1', 0), handler_for(ROOT / 'dashboard', state, inner_ready))
    server.daemon_threads = True
    ready = {'url': 'http://127.0.0.1:' + str(server.server_port), 'output': None, 'state': 'loading'}
    args.ready_file.write_text(json.dumps(ready), encoding='utf-8')
    print(json.dumps(ready), flush=True)
    threading.Thread(target=run_dashboard, args=(remaining, inner_ready, state), daemon=True).start()
    if args.open_browser:
        threading.Thread(target=webbrowser.open, args=(ready['url'],), daemon=True).start()
    server.serve_forever()


if __name__ == '__main__':
    main()
