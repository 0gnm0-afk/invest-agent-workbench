"""Loopback Telegram UI/bridge with encrypted authorization restoration."""
import asyncio
from concurrent.futures import TimeoutError as FutureTimeout
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import socket
import threading
from telegram_news import NewsService, READ_OPS, AUTH_OPS, ROOT
from telegram_news_credentials import ApiCredentials
from telegram_news_session import EncryptedSession

PORT = 8769
PROTOCOL = 'telegram-news-1'


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def server_bind(self):
        if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def __init__(self, port=PORT, service=None):
        self.service = service or NewsService(credentials=ApiCredentials(ROOT),sessions=EncryptedSession(ROOT))
        self.loop = asyncio.new_event_loop()
        self.worker = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.worker.start()
        self.cookie = secrets.token_urlsafe(32)
        self.ui_token = secrets.token_urlsafe(32)
        self.read_token = secrets.token_urlsafe(32)
        try:
            super().__init__(('127.0.0.1', port), Handler)
        except BaseException:
            if not self.loop.is_closed():
                self.loop.call_soon_threadsafe(self.loop.stop)
                self.worker.join(); self.loop.close()
            raise
        self.origin = f'http://127.0.0.1:{self.server_port}'
        self.restore_future=asyncio.run_coroutine_threadsafe(self.service.request('restore',{}),self.loop)

    def invoke(self, op, args):
        future = asyncio.run_coroutine_threadsafe(self.service.request(op, args), self.loop)
        try:
            return future.result(timeout=50)
        except FutureTimeout:
            future.cancel()
            return {'error':'connector_busy_or_timeout'}

    def handle_error(self, request, address):
        pass  # No request/exception logging: credentials and news are transient.

    def server_close(self):
        if self.loop.is_closed():
            return super().server_close()
        try:
            future=asyncio.run_coroutine_threadsafe(self.service.close(),self.loop)
            try:future.result(timeout=50)
            except FutureTimeout:future.cancel()
        finally:
            super().server_close()
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.worker.join(timeout=2)
            self.loop.close()


class Handler(BaseHTTPRequestHandler):
    server_version = 'TelegramNews'
    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, *args):
        pass

    def reply(self, value, status=200, mime='application/json; charset=utf-8', cookie=False):
        body = (json.dumps(value, ensure_ascii=False) if mime.startswith('application/json') else value).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        if cookie:
            self.send_header('Set-Cookie', f'tgnews={self.server.cookie}; HttpOnly; SameSite=Strict; Path=/')
        self.end_headers()
        self.wfile.write(body)

    def boundary(self):
        return (self.headers.get('Host') == self.server.origin.removeprefix('http://')
                and self.headers.get('Sec-Fetch-Site') not in ('cross-site','same-site')
                and self.headers.get('Origin') in (None, self.server.origin))

    def browser_auth(self):
        try:
            cookies = SimpleCookie(self.headers.get('Cookie',''))
            value = cookies['tgnews'].value if 'tgnews' in cookies else ''
            return secrets.compare_digest(value, self.server.cookie)
        except Exception:
            return False

    def native_request(self):
        return not self.headers.get('Origin') and not any(k.lower().startswith('sec-fetch-') for k in self.headers)

    def do_GET(self):
        if not self.boundary():
            return self.reply({'error':'forbidden_origin'}, 403)
        if self.path == '/health':
            result = dict(app=PROTOCOL, root=str(ROOT), login_url=self.server.origin, session_persistence_supported=True)
            if self.native_request():
                result['read_token'] = self.server.read_token
            return self.reply(result)
        if self.path == '/ui-state':
            if not self.browser_auth():
                return self.reply({'error':'authentication_required'},403)
            return self.reply(dict(token=self.server.ui_token, **self.server.invoke('status',{})))
        assets = {'/':('telegram_news.html','text/html; charset=utf-8'),
                  '/app.js':('telegram_news_ui.js','text/javascript; charset=utf-8'),
                  '/style.css':('telegram_news.css','text/css; charset=utf-8')}
        if self.path not in assets:
            return self.reply({'error':'not_found'},404)
        name, mime = assets[self.path]
        return self.reply((Path(__file__).parent/name).read_text(encoding='utf-8'), mime=mime, cookie=self.path=='/')

    def do_POST(self):
        if not self.boundary():
            return self.reply({'error':'forbidden_origin'},403)
        ui = self.path == '/ui'
        if ui:
            allowed = (self.browser_auth() and self.headers.get('Origin') == self.server.origin
                       and secrets.compare_digest(self.headers.get('X-CSRF-Token',''),self.server.ui_token))
        else:
            allowed = (self.path == '/mcp' and self.native_request()
                       and secrets.compare_digest(self.headers.get('Authorization',''),'Bearer '+self.server.read_token))
        if not allowed:
            return self.reply({'error':'forbidden'},403)
        try:
            size = int(self.headers.get('Content-Length','0'))
            if not 0 < size <= 16384 or self.headers.get('Content-Type') != 'application/json':
                raise ValueError()
            payload = json.loads(self.rfile.read(size))
            if not isinstance(payload,dict) or set(payload) != {'op','args'}:
                raise ValueError()
            op, args = payload['op'], payload['args']
            if not isinstance(op,str) or not isinstance(args,dict):
                raise ValueError()
        except (ValueError,UnicodeError):
            return self.reply({'error':'invalid_request'},400)
        if op not in (READ_OPS | AUTH_OPS if ui else READ_OPS):
            return self.reply({'error':'read_only_operation'},403)
        return self.reply(self.server.invoke(op,args))


if __name__ == '__main__':
    with Server() as server:
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
