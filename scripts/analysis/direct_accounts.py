"""Normalized saved balances only; no broker client, refresh, or account DB writes."""
from copy import deepcopy
from datetime import datetime, timezone
import sqlite3
from pathlib import Path

from accounts.store import AccountStore
from accounts.saved import merge_saved


class ReadOnlyAccountStore(AccountStore):
    def connect(self):
        db = sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True, timeout=10)
        db.execute('PRAGMA query_only=ON')
        return db

    def save(self, value):
        raise ValueError('direct_account_read_cannot_write')


def saved_accounts(root):
    missing = dict(accounts=[], as_of=None, attempted_at=None, query_state='not_requested',
                   reason='saved_accounts_unavailable', source_mode='missing', stale=True,
                   accounts_complete=False, risk=None, limitations=[], endpoint_calls=[])
    path = Path(root) / 'runtime/accounts.sqlite3'
    if not path.is_file():
        return missing
    store = ReadOnlyAccountStore(path)
    receipt, attempt = store.latest('nh'), store.latest_attempt('nh')
    view = dict(missing)
    if receipt:
        snapshot = store.read(receipt['snapshot_ref'])
        meta = attempt or receipt
        stamp = snapshot['as_of']
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(stamp.replace('Z', '+00:00'))).total_seconds()
        view.update(accounts=deepcopy(snapshot['accounts']), as_of=stamp,
                    attempted_at=meta.get('attempted_at'), query_state=meta.get('query_state'),
                    reason=meta.get('reason'), source_mode='historical_saved' if meta.get('query_state') == 'failed' else 'shared_saved',
                    stale=not 0 <= age <= 86400, accounts_complete=snapshot.get('accounts_complete', False),
                    valuation={k: snapshot.get(k) for k in ('base_currency', 'fx_to_base')},
                    limitations=snapshot.get('limitations', []))
    elif attempt:
        view.update(query_state=attempt.get('query_state'), reason=attempt.get('reason'), attempted_at=attempt.get('attempted_at'))
    value = merge_saved(store, view, persist=False)
    # Only normalized account views are exposed. Never forward transport/auth
    # material or raw receipt references if a legacy snapshot contains them.
    forbidden = {'raw', 'rawjson', 'rawref', 'token', 'lease', 'authorization', 'accesstoken',
                 'refreshtoken', 'appkey', 'appsecret', 'secret', 'clientid', 'clientsecret',
                 'accountnumber', 'accountno', 'acctno', 'accountid'}
    def clean(item):
        if isinstance(item, dict):
            return {k: clean(v) for k, v in item.items() if k.lower().replace('_', '').replace('-', '') not in forbidden}
        if isinstance(item, list):
            return [clean(v) for v in item]
        return item
    return clean(value)
