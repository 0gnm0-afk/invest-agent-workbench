"""Show the local dashboard shell before importing or building its data."""
from web import ASSETS, STARTUP_ASSETS
import argparse
import json
import os
import queue
import subprocess
import time
from launch.instance import acquire, release, existing, publish, reuse
from launch.journal import error_frames
import re
import sys
import threading
import urllib.parse
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from workbench_paths import ROOT
from atomic_io import atomic_write


def loading_page(assets):
    page = (assets / ASSETS['/']).read_text(encoding='utf-8')
    page = re.sub(r'<script\b[^>]*>.*?</script>', '', page, flags=re.S)
    # M10 owns the application shell; the loading shell exposes only retained screens.
    page = re.sub(r'<a\b[^>]*data-nav="(?:screening|screener|sectors|sector|cycles|cycle|accounts)"[^>]*>.*?</a>',
                  '', page, flags=re.S)
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
            for label in ('시장 자료', '종목 차트', '재무 자료', '분석 기록')) + '</div></section>')


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
        diagnostic = write_error(ready_file.with_suffix('.err.log'))
        if sys.stderr is not None:
            sys.stderr.write(json.dumps(diagnostic) + '\n')
            sys.stderr.flush()


def write_error(path):
    # Store locations, never exception messages, request values or source lines.
    exc = sys.exc_info()[1]
    diagnostic = {'error_type': type(exc).__name__, 'error_frames': error_frames(exc)[-32:]}
    atomic_write(path, json.dumps(diagnostic, ensure_ascii=True))
    return diagnostic


def announce(args, value):
    if args.ready_file:
        atomic_write(args.ready_file, json.dumps(value))
    if sys.stdout is not None:
        try:
            print(json.dumps(value), flush=True)
        except (BrokenPipeError, OSError):
            pass  # A desktop caller closing its pipe must not kill the server.
    if getattr(args, 'detached', False):
        # The desktop process reads one handshake. Workers never retain its pipe.
        stream = sys.stdout
        sys.stdout = open(os.devnull, 'w', encoding='utf-8')
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass


def desktop(remaining, open_browser=False):
    """Short-lived client; detached server readiness travels over a pipe, not files."""
    python = Path(sys.executable)
    if python.name.lower() == 'pythonw.exe':
        python = python.with_name('python.exe')
    command = [str(python), '-B', '-u', str(ROOT/'scripts/dashboard_startup.py'), '--detached', *remaining]
    if open_browser:
        command.append('--open-browser')
    process = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    result = queue.Queue()
    def receive():
        try:
            result.put(process.stdout.readline())
        except OSError:
            result.put(b'')
        finally:
            process.stdout.close()
    threading.Thread(target=receive, daemon=True).start()
    try:
        line = result.get(timeout=245)
    except queue.Empty:
        raise ValueError('dashboard_start_timeout: inspect runtime/dashboard/server.json') from None
    if not line:
        raise ValueError('dashboard_start_failed: inspect runtime/dashboard/launch-error.log')
    value = json.loads(line)
    if value.get('error'):
        raise ValueError(value['error'])
    print(json.dumps(value), flush=True)
    return value


def serve(args, remaining):
    folder = ROOT / 'runtime/dashboard'
    folder.mkdir(parents=True, exist_ok=True)
    inner_ready = folder / 'data-ready.json'
    # Only the owner of the OS lease can replace the previous server receipt.
    inner_ready.unlink(missing_ok=True)
    state = {'instance_id': uuid.uuid4().hex}
    server = ThreadingHTTPServer(('127.0.0.1', 0), handler_for(ROOT / 'dashboard', state, inner_ready))
    server.daemon_threads = True
    ready = {'url': 'http://127.0.0.1:' + str(server.server_port), 'output': None, 'state': 'loading', 'pid': os.getpid()}
    publish(ROOT, {**ready, 'instance_id': state['instance_id'], 'offline': '--offline' in remaining})
    announce(args, ready)
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
    parser.add_argument('--desktop', action='store_true')
    parser.add_argument('--detached', action='store_true', help=argparse.SUPPRESS)
    args, remaining = parser.parse_known_args()
    if args.desktop:
        if args.ready_file or args.detached:
            parser.error('--desktop cannot be combined with --ready-file or --detached')
        return desktop(remaining, args.open_browser)
    try:
        return launch(parser, args, remaining)
    except Exception:
        write_error(ROOT / 'runtime/dashboard/launch-error.log')
        if args.detached:
            announce(args, {'error': 'dashboard_start_failed: inspect runtime/dashboard/launch-error.log'})
        if sys.stderr is not None and not args.detached:
            raise
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
        announce(args, result)
        if args.open_browser:
            webbrowser.open(result['url'])
        return
    raise ValueError('dashboard_instance_unresponsive: inspect the running server log')


if __name__ == '__main__':
    main()
