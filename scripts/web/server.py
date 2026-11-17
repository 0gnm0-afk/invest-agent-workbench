"""Private loopback dashboard; serves only curated views and bundled assets."""
from datetime import date, datetime, timezone
import argparse
import json
from web import ASSETS
import mimetypes
import secrets
import sqlite3
from http.cookies import SimpleCookie
from launch.daily import DailyLaunch, plan_for
from web.refresh import DashboardDaily, RetryConflict
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from web.data import ROOT, build, StockSearch, home_view, market_update
from market.history.chart import HistoryStore, timeframe
from financials.store import FinancialStore, selected_stock
from drawings.store import WorkspaceStore, Conflict, moving_averages
from drawings.l1_trendlines import l1_trendlines
from analysis.service import AnalysisService
from market.history.price_events import build as price_events
from analysis import Forbidden
from alerts.trailing import TrailingStore
from runtime_paths import local_runtime_path


def handler_for(data, stocks, assets, token, workspace_path=None, *, views=None, daily_status=None, daily_retry=None,
                analysis_capabilities=None, analysis_dependencies=None, risk_options=None, offline=False, exclusion_review=None, launch=None, root=None):
    views = views if views is not None else {'view': (data, stocks)}
    data_root = Path(root) if root is not None else (Path(workspace_path).parent if workspace_path else ROOT)
    if root is None and workspace_path and data_root.name == 'runtime':
        data_root = data_root.parent
    store = WorkspaceStore(workspace_path or ROOT / "runtime/chart-workspaces.sqlite3")
    histories = (analysis_dependencies['histories'] if analysis_dependencies else
                 HistoryStore((Path(workspace_path).parent if workspace_path else ROOT / 'runtime') / 'chart-history.sqlite3'))
    search_listings = {history.market + ':' + symbol: row
                       for history in histories.saved.markets.values()
                       for symbol, row in getattr(history, 'listings', {}).items()}
    search_cache = [None, None]
    financials = (analysis_dependencies['financials'] if analysis_dependencies else
                  FinancialStore((Path(workspace_path).parent if workspace_path else ROOT / 'runtime') / 'financials'))
    analyses = AnalysisService(store, histories, financials, lambda: views['view'], ROOT if workspace_path is None else Path(workspace_path).parent)
    capabilities = dict(analysis_capabilities or {})
    if set(capabilities) - {'collaboration', 'risk'} or any(not isinstance(v,str) or not v or v == token for v in capabilities.values()) or len(set(capabilities.values())) != len(capabilities):
        raise ValueError('invalid_analysis_capabilities')
    browser_session = secrets.token_urlsafe(32)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def send(self, body, kind='application/json; charset=utf-8', status=200, cookie=False):
            self.send_response(status)
            self.send_header('Content-Type', kind)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            if cookie:
                self.send_header('Set-Cookie', 'workbench_confirm_' + str(self.server.server_port) + '=' + browser_session + '; HttpOnly; SameSite=Strict; Path=/api/analysis-rpc')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            data, current_stocks = views['view']
            stocks = {**histories.saved.stocks, **current_stocks}
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
                return self.send(json.dumps({'app': 'invest-workbench', 'token': token, 'analysis_protocol': 1, 'price_events_protocol': 1, 'pivot_rule': 'weekly-swing-2', 'collaboration_protocol': 1, 'scenario_monitor_protocol': 1, 'report_reading_protocol': 2, 'offline': offline, 'workspace': str(ROOT)}).encode(), cookie=self.headers.get('Sec-Fetch-Site') == 'same-origin')
            if path == '/api/daily-update':
                return self.send(json.dumps(market_update(daily_status()) if daily_status else {'enabled':False}, ensure_ascii=False).encode('utf-8'))
            if path == '/api/trailing':
                target = (Path(workspace_path).parent if workspace_path else ROOT/'runtime')/'trailing-warnings.sqlite3'
                return self.send(json.dumps(TrailingStore(target).public(datetime.now(timezone.utc), plan_for(datetime.now(timezone.utc))['targets']), ensure_ascii=False, allow_nan=False).encode('utf-8'))
            if path == '/api/home':
                return self.send(json.dumps(home_view(data), ensure_ascii=False, allow_nan=False).encode('utf-8'))
            if path.startswith('/api/l1-trendlines/'):
                key = urllib.parse.unquote(path[len('/api/l1-trendlines/'):])
                if key not in stocks:
                    return self.send(b'{"error":"unknown_stock"}', status=404)
                try:
                    if set(query) - {'start', 'end', 'timeframe'} or any(len(v) != 1 for v in query.values()):
                        raise ValueError('invalid_l1_request')
                    start, end = (query.get(field, [None])[0] for field in ('start', 'end'))
                    for value in (start, end):
                        if value is not None and (len(value) != 10 or date.fromisoformat(value).isoformat() != value):
                            raise ValueError('invalid_l1_window')
                    if start is not None and end is not None and start > end:
                        raise ValueError('invalid_l1_window')
                except (ValueError, TypeError):
                    return self.send(b'{"error":"invalid_l1_request"}', status=400)
                result = l1_trendlines(key, root=data_root, start=start, end=end, timeframe=frame)
                return self.send(json.dumps(result, ensure_ascii=False, allow_nan=False).encode('utf-8'))
            if path == '/api/stock-search':
                if set(query) - {'q'} or len(query.get('q', [''])) != 1:
                    return self.send(b'{"error":"invalid_search_query"}', status=400)
                try:
                    phrase = query.get('q', [''])[0]
                    if len(phrase) > 100:
                        raise ValueError('invalid_search_query')
                    cached_source, index = search_cache
                    if cached_source is not current_stocks:
                        index = StockSearch(stocks, search_listings)
                        search_cache[:] = [current_stocks, index]
                    return self.send(json.dumps(index.search(phrase), ensure_ascii=False, allow_nan=False).encode('utf-8'))
                except ValueError:
                    return self.send(b'{"error":"invalid_search_query"}', status=400)
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
                value = histories.view(stocks[key], frame)
                daily = value if frame == '1d' else histories.view(stocks[key], '1d')
                value['price_events'] = price_events(daily, financials.read(key))
                return self.send(json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8'))
            if path.startswith('/api/stock/'):
                key = urllib.parse.unquote(path[len('/api/stock/'):])
                value = stocks.get(key)
                if value is None:
                    value = {'state': 'unavailable', 'reason': 'saved_price_series_missing', 'bars': []}
                else:
                    from market.scope import match
                    excluded = match(value)
                    if excluded:
                        value = {**value,'state':'excluded','reason':excluded['reason'],'bars':[]}
                return self.send(json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8'))
            names = ASSETS
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
            data, stocks = views['view']
            stocks = {**histories.saved.stocks, **stocks}
            path = urllib.parse.urlsplit(self.path).path
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query, keep_blank_values=True)
            try:
                frame = timeframe(query.get('timeframe', ['1d'])[0])
            except ValueError:
                return self.send(b'{"error":"invalid_timeframe"}', status=400)
            if path not in ('/api/launch', '/api/daily-update', '/api/analysis-rpc') and not path.startswith(('/api/chart-state/', '/api/chart-history/', '/api/financials/')):
                return self.send(b'{"error":"read_only"}', status=405)
            expected = '127.0.0.1:' + str(self.server.server_port)
            supplied_token = self.headers.get('X-Workbench-Token', '')
            role = next((name for name, secret in capabilities.items() if secrets.compare_digest(supplied_token, secret)), None) if path == '/api/analysis-rpc' else None
            if (self.headers.get('Host') != expected or self.headers.get('Origin') != 'http://' + expected
                    or not (role or secrets.compare_digest(supplied_token, token))):
                return self.send(b'{"error":"local_origin_required"}', status=403)
            if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                return self.send(b'{"error":"json_required"}', status=415)
            key = urllib.parse.unquote(path.rsplit('/', 1)[-1])
            if path not in ('/api/launch', '/api/analysis-rpc', '/api/daily-update') and key not in stocks:
                return self.send(b'{"error":"unknown_stock"}', status=404)
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 131072:
                    return self.send(b'{"error":"invalid_body_size"}', status=413)
                self.connection.settimeout(5)
                raw = self.rfile.read(length)
                try:
                    body = json.loads(raw)
                except ValueError:
                    if path == '/api/daily-update':
                        return self.send(b'{"error":"invalid_daily_request"}', status=400)
                    raise
                if path == '/api/launch':
                    if (query or not isinstance(body, dict) or set(body) != {'daily_refresh', 'retry_market', 'saved_accounts'}
                            or any(type(v) is not bool for v in body.values())
                            or body['retry_market'] and not body['daily_refresh']):
                        return self.send(b'{"error":"invalid_launch_request"}', status=400)
                    if offline or not launch:
                        return self.send(b'{"error":"launch_unavailable"}', status=409)
                    # Old launchers can send saved_accounts=False; it never
                    # grants an account query through the dashboard.
                    return self.send(json.dumps(market_update(launch(**{**body, 'saved_accounts': True})), ensure_ascii=False).encode('utf-8'), status=202)
                if path == '/api/daily-update':
                    if (query or not isinstance(body, dict) or set(body) != {'retry', 'attempt_id'}
                            or body['retry'] is not True or not isinstance(body['attempt_id'], str)
                            or not 0 < len(body['attempt_id']) <= 128):
                        return self.send(b'{"error":"invalid_daily_request"}', status=400)
                    if offline:
                        return self.send(b'{"error":"offline_collection_disabled"}', status=403)
                    if not daily_retry:
                        return self.send(b'{"error":"daily_retry_unavailable"}', status=503)
                    try:
                        result = daily_retry(body['attempt_id'])
                    except RetryConflict:
                        return self.send(b'{"error":"daily_attempt_changed"}', status=409)
                    return self.send(json.dumps(market_update(result), ensure_ascii=False, allow_nan=False).encode('utf-8'), status=202)
                if path == '/api/analysis-rpc':
                    if not isinstance(body, dict) or set(body) != {'op', 'arguments'}:
                        raise ValueError('invalid_analysis_request')
                    try:
                        if offline and body['op'] == 'refresh':
                            return self.send(b'{"error":"saved_only_session_refresh_disabled"}', status=403)
                        principal = role or 'legacy'
                        if role is None and self.headers.get('Sec-Fetch-Site') == 'same-origin':
                            cookies = SimpleCookie(self.headers.get('Cookie', ''))
                            candidate = cookies.get('workbench_confirm_' + str(self.server.server_port))
                            if candidate and secrets.compare_digest(candidate.value, browser_session): principal = 'browser'
                        if body['op'] in ('decide', 'holding') and principal != 'browser':
                            raise Forbidden('local_browser_confirmation_required')
                        service = analyses
                        result = service.dispatch(body['op'], body['arguments'], 'http://' + expected,
                                                   principal=principal, session=browser_session if principal == 'browser' else role)
                        return self.send(json.dumps(result, ensure_ascii=False, allow_nan=False).encode('utf-8'))
                    except Forbidden as exc:
                        return self.send(json.dumps({'error':str(exc)}).encode(), status=403)
                    except Conflict as exc:
                        return self.send(json.dumps({'error':str(exc)}).encode(), status=409)
                    except (ValueError, KeyError, TypeError) as exc:
                        # Validation messages only; never forward arbitrary provider/OS exceptions.
                        message = str(exc) if isinstance(exc, ValueError) and str(exc).replace('_','').isalnum() else 'invalid_analysis_request'
                        return self.send(json.dumps({'error':message}).encode(), status=400)
                if path.startswith('/api/financials/'):
                    if offline:
                        return self.send(b'{"error":"saved_only_session_refresh_disabled"}', status=403)
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
                    result = histories.view(stocks[key], frame) if offline else histories.ensure(stocks[key], retry=body['retry'])
                    return self.send(json.dumps(result, ensure_ascii=False, allow_nan=False).encode('utf-8'))
                if not isinstance(body, dict) or set(body) != {'revision', 'state'}:
                    raise ValueError('invalid_body')
                result = store.shared(key, frame, revision=body['revision'], state=body['state'])
                from scenarios.monitor import ScenarioStore
                ScenarioStore(store.path).sync_workspace(key)
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
    parser.add_argument('--offline', action='store_true', help='Saved inputs only; disable selected-stock refresh as well as startup collection')
    parser.add_argument('--build-only', action='store_true')
    parser.add_argument('--ready-file', type=Path)
    parser.add_argument('--port', type=int, default=0)
    parser.add_argument('--daily-refresh', action='store_true', help='One market update per KST launch day')
    parser.add_argument('--retry-market', action='store_true', help='Explicitly request another market attempt today')
    args = parser.parse_args()
    if args.offline:
        if args.daily_refresh or args.retry_market:
            parser.error('--offline cannot be combined with market refresh')
        args.saved_accounts = True
    if args.retry_market and not args.daily_refresh:
        parser.error('--retry-market requires --daily-refresh')
    if args.build_only and args.daily_refresh:
        parser.error('--build-only is saved-market only')
    daily = DailyLaunch(ROOT, accounts=False) if args.daily_refresh else None
    base_run = (daily or DailyLaunch(ROOT, accounts=False)).display_run()
    saved_notice = None
    if not daily and base_run:
        try:
            latest = json.loads((ROOT / 'runtime/latest.json').read_text(encoding='utf-8'))
            saved_notice = dict(saved_only=True, latest_state=latest.get('state'),
                                display_stale=local_runtime_path(latest['output'], root=ROOT).resolve() != Path(base_run).resolve())
        except (OSError, ValueError, KeyError, TypeError):
            saved_notice = dict(saved_only=True, latest_state='unavailable', display_stale=True)
    # Capture the observed market receipt. Only a subsequent
    # receipt may require another full build of the same market.
    baseline_record = (DailyLaunch(ROOT, accounts=False).read(plan_for(datetime.now(timezone.utc))['day'])
                       if not daily and not args.offline else None)
    output, data, stocks = build(refresh=False, market_run=base_run, daily_update=saved_notice, persist_view=args.build_only)
    views = {'view':(data, stocks)}
    controller = (DashboardDaily(daily or DailyLaunch(ROOT, accounts=False), views, build, base_run,
                                 saved_accounts=True, observe_only=not daily, baseline_record=baseline_record)
                  if not args.offline else None)
    if args.build_only:
        print(json.dumps({'output': str(output), 'market_state': data['market']['state'],
                          'account_query_state': data['accounts']['query_state']}))
        return
    token = secrets.token_urlsafe(18)
    capabilities = {role: secrets.token_urlsafe(32) for role in ('collaboration','risk')}
    server = ThreadingHTTPServer(('127.0.0.1', args.port), handler_for(data, stocks, ROOT / 'dashboard', token, views=views, daily_status=controller.status if controller else None, daily_retry=controller.retry if controller else None, analysis_capabilities=capabilities, offline=args.offline, launch=controller.launch if controller else None))
    server.daemon_threads = True
    del data, stocks  # Only the replaceable view owns the large price graph.
    ready = {'url': 'http://127.0.0.1:' + str(server.server_port), 'output': str(output) if output else None, 'token': token}
    if args.ready_file:
        args.ready_file.write_text(json.dumps(ready), encoding='utf-8')
    from atomic_io import atomic_write
    atomic_write(ROOT / 'runtime/dashboard/analysis-endpoint.json', json.dumps({**ready, 'analysis_capabilities':capabilities}, ensure_ascii=False, sort_keys=True, allow_nan=False))
    print(json.dumps(ready), flush=True)
    if args.open_browser:
        webbrowser.open(ready['url'] + '/#search')
    if controller:
        controller.start(retry=args.retry_market)
    server.serve_forever()


if __name__ == '__main__':
    main()
