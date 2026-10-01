"""Private loopback dashboard; serves only curated views and bundled assets."""
import argparse
import json
import mimetypes
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from daily_launch import DailyLaunch
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from dashboard_data import ROOT, build
from chart_history import HistoryStore, timeframe
from financial_store import FinancialStore
from financial_summary import selected_stock
from chart_workspace import WorkspaceStore, Conflict, moving_averages


def handler_for(data, stocks, assets, token, workspace_path=None, *, views=None, daily_status=None):
    initial_view = (data, stocks)
    store = WorkspaceStore(workspace_path or ROOT / "runtime/chart-workspaces.sqlite3")
    histories = HistoryStore((Path(workspace_path).parent if workspace_path else ROOT / 'runtime') / 'chart-history.sqlite3')
    financials = FinancialStore((Path(workspace_path).parent if workspace_path else ROOT / 'runtime') / 'financials')
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
            data, stocks = views['view'] if views is not None else initial_view
            stocks = {**histories.saved.stocks, **stocks}
            expected = '127.0.0.1:' + str(self.server.server_port)
            if self.headers.get('Host') != expected or self.headers.get('Origin', 'http://' + expected) != 'http://' + expected:
                return self.send(b'{"error":"local_origin_required"}', status=403)
            path = urllib.parse.urlsplit(self.path).path
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query, keep_blank_values=True)
            try:
                frame = timeframe(query.get('timeframe', ['1d'])[0])
            except ValueError:
                return self.send(b'{"error":"invalid_timeframe"}', status=400)
            if path == '/health':
                return self.send(json.dumps({'app': 'invest-workbench', 'token': token}).encode())
            if path == '/api/daily-update':
                return self.send(json.dumps(daily_status() if daily_status else {'enabled':False}, ensure_ascii=False).encode('utf-8'))
            if path == '/api/home':
                return self.send(json.dumps(data, ensure_ascii=False, allow_nan=False).encode('utf-8'))
            if path.startswith('/api/financials/'):
                key = urllib.parse.unquote(path[len('/api/financials/'):])
                if key not in stocks:
                    return self.send(b'{"error":"unknown_stock"}', status=404)
                try:
                    return self.send(json.dumps(financials.read(key), ensure_ascii=False, allow_nan=False).encode('utf-8'))
                except (ValueError, OSError):
                    return self.send(b'{"error":"financial_read_failed"}', status=503)
            if path.startswith('/api/chart-state/'):
                key = urllib.parse.unquote(path[len('/api/chart-state/'):])
                if key not in stocks:
                    return self.send(b'{"error":"unknown_stock"}', status=404)
                try:
                    result = store.shared(key, frame)
                    return self.send(json.dumps({**result, 'token': token}, ensure_ascii=False).encode('utf-8'))
                except (sqlite3.Error, OSError, ValueError):
                    return self.send(b'{"error":"workspace_read_failed"}', status=503)
            if path.startswith('/api/averages/'):
                key = urllib.parse.unquote(path[len('/api/averages/'):])
                if key not in stocks:
                    return self.send(b'{"error":"unknown_stock"}', status=404)
                try:
                    query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query, keep_blank_values=True)
                    periods = [int(n) for n in query.get('periods', ['20,50,150,200'])[0].split(',') if n]
                    result = moving_averages(histories.view(stocks[key], frame), periods)
                    return self.send(json.dumps(result, allow_nan=False).encode())
                except (ValueError, TypeError):
                    return self.send(b'{"error":"invalid_periods"}', status=400)
            if path.startswith('/api/chart/'):
                key = urllib.parse.unquote(path[len('/api/chart/'):])
                if key not in stocks:
                    return self.send(b'{"error":"unknown_stock"}', status=404)
                return self.send(json.dumps(histories.view(stocks[key], frame), ensure_ascii=False, allow_nan=False).encode('utf-8'))
            if path.startswith('/api/stock/'):
                key = urllib.parse.unquote(path[len('/api/stock/'):])
                value = stocks.get(key)
                if value is None:
                    value = {'state': 'unavailable', 'reason': 'saved_price_series_missing', 'bars': []}
                return self.send(json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8'))
            names = {'/': 'index.html', '/app.js': 'app.js', '/financials.js': 'financials.js', '/financials.css': 'financials.css', '/style.css': 'style.css',
                     '/stock-chart.js': 'stock-chart.js', '/drawing-tools.js': 'drawing-tools.js', '/chart-workspace.js': 'chart-workspace.js', '/chart-tools.css': 'chart-tools.css', '/format.js': 'format.js', '/charts.js': 'charts.js', '/workbench.png': 'workbench.png',
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
            data, stocks = views['view'] if views is not None else initial_view
            stocks = {**histories.saved.stocks, **stocks}
            path = urllib.parse.urlsplit(self.path).path
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query, keep_blank_values=True)
            try:
                frame = timeframe(query.get('timeframe', ['1d'])[0])
            except ValueError:
                return self.send(b'{"error":"invalid_timeframe"}', status=400)
            if not path.startswith(('/api/chart-state/', '/api/chart-history/', '/api/financials/')):
                return self.send(b'{"error":"read_only"}', status=405)
            expected = '127.0.0.1:' + str(self.server.server_port)
            if (self.headers.get('Host') != expected or self.headers.get('Origin') != 'http://' + expected
                    or not secrets.compare_digest(self.headers.get('X-Workbench-Token', ''), token)):
                return self.send(b'{"error":"local_origin_required"}', status=403)
            if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                return self.send(b'{"error":"json_required"}', status=415)
            key = urllib.parse.unquote(path.rsplit('/', 1)[-1])
            if key not in stocks:
                return self.send(b'{"error":"unknown_stock"}', status=404)
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 131072:
                    return self.send(b'{"error":"invalid_body_size"}', status=413)
                self.connection.settimeout(5)
                body = json.loads(self.rfile.read(length))
                if path.startswith('/api/financials/'):
                    if not isinstance(body, dict) or set(body) != {'refresh'} or type(body['refresh']) is not bool:
                        raise ValueError('invalid_financial_request')
                    stock = {**stocks[key], **histories.view(stocks[key], '1d')}
                    if key == 'KR:005930.KS':
                        _, stock = selected_stock('KR', '005930.KS', histories)
                    result = financials.start(key, stock, refresh=body['refresh'])
                    return self.send(json.dumps(result, ensure_ascii=False, allow_nan=False).encode('utf-8'), status=202 if result['state'] == 'running' else 200)
                if path.startswith('/api/chart-history/'):
                    if not isinstance(body, dict) or set(body) != {'retry'} or type(body['retry']) is not bool:
                        raise ValueError('invalid_history_request')
                    result = histories.ensure(stocks[key], retry=body['retry'])
                    return self.send(json.dumps(result, ensure_ascii=False, allow_nan=False).encode('utf-8'))
                if not isinstance(body, dict) or set(body) != {'revision', 'state'}:
                    raise ValueError('invalid_body')
                result = store.shared(key, frame, revision=body['revision'], state=body['state'])
                self.send(json.dumps(result).encode())
            except Conflict:
                self.send(b'{"error":"workspace_changed_elsewhere"}', status=409)
            except (ValueError, TypeError, KeyError):
                self.send(b'{"error":"invalid_workspace"}', status=400)
            except (sqlite3.Error, OSError):
                self.send(b'{"error":"workspace_save_failed"}', status=503)
    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--open-browser', action='store_true')
    parser.add_argument('--saved-accounts', action='store_true')
    parser.add_argument('--build-only', action='store_true')
    parser.add_argument('--ready-file', type=Path)
    parser.add_argument('--port', type=int, default=0)
    parser.add_argument('--daily-refresh', action='store_true', help='One market update per KST launch day')
    parser.add_argument('--retry-market', action='store_true', help='Explicitly request another market attempt today')
    args = parser.parse_args()
    if args.retry_market and not args.daily_refresh:
        parser.error('--retry-market requires --daily-refresh')
    if args.build_only and args.daily_refresh:
        parser.error('--build-only is saved-market only')
    daily = DailyLaunch(ROOT) if args.daily_refresh else None
    started = datetime.now(timezone.utc)
    day = started.astimezone(ZoneInfo('Asia/Seoul')).date().isoformat()
    base_run = (daily or DailyLaunch(ROOT)).display_run()
    saved_notice = None
    if not daily and base_run:
        try:
            latest = json.loads((ROOT / 'runtime/latest.json').read_text(encoding='utf-8'))
            saved_notice = dict(saved_only=True, latest_state=latest.get('state'),
                                display_stale=Path(latest['output']).resolve() != Path(base_run).resolve())
        except (OSError, ValueError, KeyError, TypeError):
            saved_notice = dict(saved_only=True, latest_state='unavailable', display_stale=True)
    live = {'record':{'day':day,'state':'running','stage':'starting','targets':{}},'ready':False}
    output, data, stocks = build(refresh=not args.saved_accounts and not daily, market_run=base_run, daily_update=saved_notice)
    views = {'view':(data, stocks)}
    def daily_status():
        if not daily:
            return {'enabled':False}
        try:
            record = live['record'] if live['ready'] else daily.read(day) or live['record']
        except (OSError, ValueError):
            record = live['record']
        return {**daily.public(record), 'enabled':True,'view_ready':live['ready'],
                'reused':live['record'].get('reused',False)}
    def prepare():
        try:
            record = daily.once(started, retry=args.retry_market)
            while record.get('following'):
                live['record'] = record
                time.sleep(.5)
                record = daily.once(started)
            live['record'] = record
            update = daily.public(record)
            refresh_accounts = not args.saved_accounts and not record.get('reused')
            if record.get('display_run') == base_run and not refresh_accounts:
                # Same immutable market/account view: no second adapter/risk calculation.
                views['view'] = ({**data, 'daily_update':update}, stocks)
            else:
                result_output, result_data, result_stocks = build(
                    refresh=refresh_accounts, market_run=record.get('display_run'), daily_update=update)
                views['view'] = (result_data, result_stocks)
        except Exception:
            live['record'] = {**live['record'],'state':'failed','error':'dashboard_preparation_failed'}
        finally:
            live['ready'] = True
    if args.build_only:
        print(json.dumps({'output': str(output), 'market_state': data['market']['state'],
                          'account_query_state': data['accounts']['query_state']}))
        return
    token = secrets.token_urlsafe(18)
    server = ThreadingHTTPServer(('127.0.0.1', args.port), handler_for(data, stocks, ROOT / 'dashboard', token, views=views, daily_status=daily_status))
    server.daemon_threads = True
    ready = {'url': 'http://127.0.0.1:' + str(server.server_port), 'output': str(output), 'token': token}
    if args.ready_file:
        args.ready_file.write_text(json.dumps(ready), encoding='utf-8')
    print(json.dumps(ready), flush=True)
    if args.open_browser:
        webbrowser.open(ready['url'])
    if daily:
        threading.Thread(target=prepare, daemon=True).start()
    server.serve_forever()


if __name__ == '__main__':
    main()
