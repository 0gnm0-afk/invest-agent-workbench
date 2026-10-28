"""Show the local dashboard shell before importing or building its data."""
from web import ASSETS, STARTUP_ASSETS
import argparse
import json
import os
import time
from launch.instance import acquire, release, existing, publish, reuse
import re
import sys
import threading
import traceback
import urllib.parse
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from workbench_paths import ROOT


def loading_page(assets):
    page = (assets / ASSETS['/']).read_text(encoding='utf-8')
    page = re.sub(r'<script\b[^>]*>.*?</script>', '', page, flags=re.S)
    page = re.sub(r'<link\b[^>]*href="([^"]+)"[^>]*>',
                  lambda match: match[0] if match[1] in STARTUP_ASSETS else '', page)
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
            if path == '/api/instance':
                status = public_status(state, ready_file)
                return self.send(json.dumps(dict(protocol=1, workspace=str(ROOT), instance_id=state.get('instance_id'),
                    state=status['state'], dashboard_url=status.get('url'))).encode(), 'application/json')
            if path == '/api/startup':
                return self.send(json.dumps(public_status(state, ready_file)).encode(), 'application/json')
            if path == '/':
                return self.send(loading_page(assets).encode('utf-8'), 'text/html; charset=utf-8')
            files = {url: (name, 'text/css' if url.endswith('.css') else 'text/javascript' if url.endswith('.js') else 'text/plain')
                     for url,name in STARTUP_ASSETS.items()}
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
        from web.server import main
        main()
        state['failed'] = True
    except BaseException:
        state['failed'] = True
        # Persist even when pythonw inherited unusable standard handles.
        with ready_file.with_suffix('.err.log').open('a', encoding='utf-8', buffering=1) as log:
            traceback.print_exc(file=log)
        traceback.print_exc()
        if sys.stderr is not None:
            sys.stderr.flush()


def serve(args, remaining):
    if args.ready_file is None:
        args.ready_file = ROOT / 'runtime/dashboard' / ('ready-' + uuid.uuid4().hex + '.json')
    args.ready_file.parent.mkdir(parents=True, exist_ok=True)
    inner_ready = args.ready_file.with_name(args.ready_file.stem + '-data.json')
    # Reject a reused receipt instead of ever redirecting to an old server.
    if args.ready_file.exists() or inner_ready.exists():
        raise ValueError('ready-file must be new for each launch')
    state = {'instance_id': uuid.uuid4().hex}
    server = ThreadingHTTPServer(('127.0.0.1', 0), handler_for(ROOT / 'dashboard', state, inner_ready))
    server.daemon_threads = True
    ready = {'url': 'http://127.0.0.1:' + str(server.server_port), 'output': None, 'state': 'loading', 'pid': os.getpid()}
    publish(ROOT, {**ready, 'instance_id': state['instance_id'], 'offline': '--offline' in remaining})
    args.ready_file.write_text(json.dumps(ready), encoding='utf-8')
    print(json.dumps(ready), flush=True)
    threading.Thread(target=run_dashboard, args=(remaining, inner_ready, state), daemon=True).start()
    if args.open_browser:
        threading.Thread(target=webbrowser.open, args=(ready['url'],), daemon=True).start()
    serve_requests(server, state)


def serve_requests(server, state):
    # Failed builders must not retain their imported data/process indefinitely.
    server.timeout = .5
    failed_at = None
    try:
        while True:
            server.handle_request()
            if state.get('failed'):
                failed_at = failed_at or time.monotonic()
                if time.monotonic() - failed_at >= 20:
                    break
    finally:
        server.server_close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ready-file', type=Path)
    parser.add_argument('--open-browser', action='store_true')
    args, remaining = parser.parse_known_args()
    # Reuse and lease errors happen before serve(); pythonw needs diagnostics here too.
    detached_errors = sys.stderr is None
    if sys.stdout is None or detached_errors:
        diagnostic = args.ready_file or ROOT / 'runtime/dashboard' / ('launch-' + uuid.uuid4().hex + '.json')
        diagnostic.parent.mkdir(parents=True, exist_ok=True)
        if sys.stdout is None:
            sys.stdout = diagnostic.with_suffix('.log').open('a', encoding='utf-8', buffering=1)
        if detached_errors:
            sys.stderr = diagnostic.with_suffix('.err.log').open('a', encoding='utf-8', buffering=1)
    try:
        return launch(parser, args, remaining)
    except Exception:
        if not detached_errors:
            raise
        traceback.print_exc()
        sys.stderr.flush()
        raise SystemExit(1) from None


def launch(parser, args, remaining):
    if '--retry-market' in remaining and '--daily-refresh' not in remaining:
        parser.error('--retry-market requires --daily-refresh')
    if '--offline' in remaining and any(flag in remaining for flag in ('--daily-refresh', '--retry-market')):
        parser.error('--offline cannot be combined with market refresh')
    if args.ready_file and args.ready_file.exists():
        parser.error('ready-file must be new for each launch')
    # Lease is held through serving, including initial preparation. Stale files
    # cannot hold a lease after a process exits.
    for _ in range(40):
        lease = acquire(ROOT)
        if lease is not None:
            try:
                return serve(args, remaining)
            finally:
                release(lease)
        try:
            value = existing(ROOT)
        except (OSError, ValueError, KeyError):
            time.sleep(.25)
            continue
        result = reuse(ROOT, value, remaining)
        if args.ready_file:
            args.ready_file.parent.mkdir(parents=True, exist_ok=True)
            args.ready_file.write_text(json.dumps(result), encoding='utf-8')
        if sys.stdout is not None:
            print(json.dumps(result), flush=True)
        if args.open_browser:
            webbrowser.open(result['url'])
        return
    raise ValueError('dashboard_instance_unresponsive: inspect the running server log')


if __name__ == '__main__':
    main()
