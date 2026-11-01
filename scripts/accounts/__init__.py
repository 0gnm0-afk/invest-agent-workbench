"""Read-only NH balances and immutable account receipts shared by UI and daily jobs."""
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import sys
from atomic_io import load_bom as load, sha, write_new_json as write, checked_saved as checked, utc_now as now
from workbench_paths import ROOT
from accounts.store import AccountStore
from accounts.brokers import nh_requests, nh_success, nh_pages, normalize_nh, query_state
import uuid


def collect_balances(reader, pages, error_type):
    """Explicit three-endpoint allowlist; no order/reservation/market calls."""
    allowed = {'/n2/acctinfo', '/krstock/inquiry/v1/balance', '/gbstock/inquiry/v1/balance'}
    calls = []
    def restricted(path, inputs, continuation=None):
        if path not in allowed:
            raise ValueError('dashboard_balance_read_only')
        calls.append(path)
        return reader(path, inputs, continuation)
    started = now()
    body, _ = restricted('/n2/acctinfo', {})
    rows = body.get('Output_0')
    if not isinstance(rows, list) or any(not isinstance(r,dict) for r in rows) or not nh_success(body):
        raise ValueError('account_list_not_confirmed')
    accounts, seen = [], set()
    for row in rows:
        if row.get('acct_type') not in ('01', '02'):
            continue
        account = row.get('acct_no')
        if not isinstance(account, str) or not account.isdigit() or len(account) != 11 or account in seen:
            raise ValueError('account_identity_invalid')
        seen.add(account)
        item = {'alias': 'nh-' + hashlib.sha256(account.encode()).hexdigest()[:12],
                'account_type': row['acct_type'], 'markets': {}, 'details': {}}
        requests = nh_requests(account)
        for market, (path, inputs) in requests.items():
            try:
                item['markets'][market] = {'state': 'received', 'pages': pages(path, inputs, restricted)}
            except error_type as exc:
                # Transport errors are controlled codes from the existing adapter.
                item['markets'][market] = {'state': 'unavailable', 'reason': str(exc),
                                          'pages': getattr(exc,'pages',[])}
        accounts.append(item)
    return {'provider': 'NH PLUG', 'source': 'broker_export', 'as_of': started,
            'collected_until': now(), 'scope': 'KR_US_balances_only_no_orders',
            'accounts': accounts, 'accounts_complete': True, 'endpoint_calls': calls,
            'query_basis': {m: inputs for m, (_, inputs) in nh_requests('<account>').items()}}


def _nh_account_view(config, output, refresh=True, saved_dashboard_root=None, request_id=None):
    store = AccountStore(Path(saved_dashboard_root or output.parent).parent/'accounts.sqlite3')
    mother = Path(config['mother'])
    # Reuse account-only adapters without shadowing current market/chart modules.
    mother_scripts = str(mother / 'scripts')
    if mother_scripts not in sys.path:
        sys.path.append(mother_scripts)
    from nh_api import read, NHError
    def pages(path, inputs, reader):
        return nh_pages(path, inputs, reader, NHError)
    previous_paths = list((mother / 'runtime/nh-live/exports').glob('account-*.json'))
    previous_paths += list(Path(saved_dashboard_root or output.parent).glob('*/account-snapshot.private.json'))
    previous = None
    previous_meta = None
    previous_score = None
    latest_record = store.latest('nh')
    if latest_record:
        previous = store.read(latest_record['snapshot_ref'])
        previous_meta = store.latest_attempt('nh') or latest_record
        previous_score = (previous['as_of'], previous_meta.get('attempted_at',''))
    for path in previous_paths:
        try:
            candidate = load(path)
            if candidate.get('provider') != 'NH PLUG':
                continue  # Combined views must not become a provider's observation.
            meta = None
            if path.name.endswith('.private.json'):
                hashes = load(path.parent / 'manifest.json')['files']
                candidate = checked(path, hashes.get(path.name))
                if hashes.get('account-view.json'):
                    meta = checked(path.parent/'account-view.json', hashes['account-view.json'])
            score = (candidate['as_of'], (meta or {}).get('attempted_at', ''))
            if previous_score is None or score > previous_score:
                previous, previous_meta, previous_score = candidate, meta, score
        except (OSError, ValueError, KeyError):
            continue
    result = {'attempted_at': now(), 'query_state': 'not_requested', 'reason': None,
              'accounts': [], 'as_of': None, 'source_mode': 'missing', 'risk': None,
              'endpoint_calls': [], 'limitations': [], 'snapshot_sha256': None}
    snapshot = None
    observation_ref = None
    lease = store.begin('nh', request_id or ('manual-'+uuid.uuid4().hex), 'api') if refresh else None
    if lease and not lease['execute']:
        refresh = False
        result.update(query_state=lease['state'], reason='shared_account_query',
                      request_id=lease['request_id'])
        if lease.get('result'):
            result.update({k:v for k,v in lease['result'].items() if k not in ('snapshot_ref','raw_ref')})
            if lease['result'].get('snapshot_ref'):
                snapshot = store.read(lease['result']['snapshot_ref'])
                result['source_mode'] = 'shared_saved'
    if refresh:
        try:
            raw = collect_balances(read, pages, NHError)
            result['endpoint_calls'] = raw['endpoint_calls']
            listings = {}
            # Reuse the verified official saved listing master; no new market fetch.
            evidence = (previous or {}).get('listing_source', {})
            if evidence.get('path') and sha(evidence['path']) == evidence.get('sha256'):
                from nh_master import parse
                listings = parse(Path(evidence['path']).read_bytes())
            snapshot = normalize_nh(raw, listings)
            snapshot['listing_source'] = evidence
            for account, source in zip(snapshot['accounts'], raw['accounts']):
                account['query_markets'] = {m: {
                    'state': ('received' if v.get('pages') and all(nh_success(p) for p in v['pages']) and isinstance(v['pages'][-1].get('Output_1'), list)
                              and isinstance(v['pages'][-1].get('Output_0'), dict) else 'unavailable'),
                    'reason': v.get('reason'), 'pages': len(v.get('pages', []))}
                    for m, v in source['markets'].items()}
                for value in account['query_markets'].values():
                    if value['state'] == 'unavailable' and not value['reason']:
                        value['reason'] = 'broker_response_or_shape_unconfirmed'
            result['query_state'] = query_state(snapshot)
            result['source_mode'] = 'live_read_only'
            receipt = store.complete('nh',lease['request_id'],lease['lease'],metadata=result,snapshot=snapshot,raw=raw)
            result.update(snapshot_ref=receipt['snapshot_ref'], request_id=lease['request_id'])
            observation_ref = receipt['raw_ref']
        except (NHError, OSError, ValueError, KeyError, TypeError) as exc:
            snapshot = None
            result.update(query_state='failed', reason=str(exc) if isinstance(exc, NHError) else type(exc).__name__)
            store.complete('nh',lease['request_id'],lease['lease'],metadata=result)
    if observation_ref:
        write(output / 'account-observation.private.json', {'account_store_ref':observation_ref})
    if snapshot is None and previous:
        snapshot = previous
        result['source_mode'] = 'historical_saved'
        if not refresh and previous_meta and not lease:
            result.update({k:previous_meta.get(k) for k in ('query_state','reason','attempted_at')})
    if snapshot is None:
        return result
    result['snapshot_ref'] = store.save(snapshot)
    result.update(accounts=snapshot['accounts'], as_of=snapshot['as_of'],
                  limitations=result['limitations'] + snapshot.get('limitations', []))
    result['stale'] = (datetime.now(timezone.utc) - datetime.fromisoformat(snapshot['as_of'])).total_seconds() > 86400
    result['valuation'] = {k: snapshot.get(k) for k in ('base_currency', 'fx_to_base')}
    result['accounts_complete'] = snapshot.get('accounts_complete', False)
    return result


def account_view(config, output, refresh=True, saved_dashboard_root=None, request_id=None):
    """Daily owns the NH refresh; other providers are read from their saved receipts."""
    from accounts.saved import merge_saved
    nh = _nh_account_view(config, output, refresh, saved_dashboard_root, request_id)
    store = AccountStore(Path(saved_dashboard_root or output.parent).parent/'accounts.sqlite3')
    result = merge_saved(store, nh)
    if result.get('snapshot_ref'):
        path = output/'account-snapshot.private.json'
        write(path, {'account_store_ref':result['snapshot_ref']})
        result['snapshot_sha256'] = sha(path)
    return result


def publish_receipt(output, accounts, *, view=None, **metadata):
    """Publish the same account receipt for a headless refresh or a dashboard build."""
    receipt = {'account_view_ref':{'metadata':{k:v for k,v in accounts.items() if k != 'accounts'},
                                   'snapshot_ref':accounts['snapshot_ref']}} if accounts.get('snapshot_ref') else accounts
    write(output/'account-view.json', receipt)
    if view is not None:
        write(output/'dashboard.json', view)
    write(output/'manifest.json', {'files': {p.name: sha(p) for p in output.iterdir() if p.is_file()},
                                  'authority': 'read_only_no_orders', **metadata})


def refresh_account_snapshot(root=ROOT, request_id=None):
    """One balance-only refresh; immutable receipt is reusable by scheduled and UI paths."""
    output = root/'runtime/dashboard'/('account-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    output.mkdir(parents=True)
    accounts = account_view(load(root/'config.json'), output, refresh=True, request_id=request_id)
    publish_receipt(output, accounts)
    return accounts
