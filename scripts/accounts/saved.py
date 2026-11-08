"""Compose saved provider observations without advancing any original timestamp."""
from copy import deepcopy
from datetime import datetime, timezone


def merge_saved(store, nh_view, *, persist=True):
    view = deepcopy(nh_view)
    receipt, attempt = store.latest('meritz'), store.latest_attempt('meritz')
    if not receipt and not attempt:
        return view  # Preserve the pre-existing NH-only representation.
    providers = {'nh': {k: view.get(k) for k in (
        'query_state', 'reason', 'as_of', 'attempted_at', 'source_mode', 'stale', 'snapshot_ref')}}
    snapshot = store.read(receipt['snapshot_ref']) if receipt else None
    meta = attempt or receipt
    meritz = {**meta, 'as_of': (snapshot or {}).get('as_of'),
              'source_mode': 'historical_saved' if meta.get('query_state') == 'failed' else 'shared_saved'}
    meritz['stale'] = (snapshot is None or
        (datetime.now(timezone.utc) - datetime.fromisoformat(snapshot['as_of'].replace('Z','+00:00'))).total_seconds() > 86400)
    providers['meritz'] = meritz
    nh_fx = view.get('valuation', {}).get('fx_to_base', {})
    for account in view.get('accounts', []):
        account.setdefault('provider', 'nh')
        account.setdefault('as_of', nh_view.get('as_of'))
        account['fx_to_base'] = account.get('fx_to_base', nh_fx)
        account['query_state'] = nh_view.get('query_state')
        account['stale'] = nh_view.get('stale', True)
        if account['query_state'] in ('failed', 'partial', 'pending'):
            account['holdings_complete'] = False
    for source in (snapshot or {}).get('accounts', []):
        account = deepcopy(source)
        account.update(provider='meritz', as_of=source.get('as_of', snapshot['as_of']),
                       query_state=meta.get('query_state'), stale=meritz['stale'])
        account['fx_to_base'] = source.get('fx_to_base', snapshot.get('fx_to_base', {}))
        if account['query_state'] in ('failed', 'partial', 'pending'):
            account['holdings_complete'] = False
        view.setdefault('accounts', []).append(account)
    aliases = [a['alias'] for a in view.get('accounts', [])]
    if len(aliases) != len(set(aliases)):
        raise ValueError('account_provider_alias_collision')
    observed = [p['as_of'] for p in providers.values() if p.get('as_of')]
    view.update(providers=providers, provider='NH PLUG / Meritz',
                as_of=min(observed, key=lambda v: datetime.fromisoformat(v.replace('Z','+00:00'))) if observed else None,
                stale=any(p.get('stale', True) for p in providers.values() if p.get('as_of')),
                source_mode='shared_saved', accounts_complete=bool(snapshot and snapshot.get('accounts_complete'))
                    and bool(view.get('accounts_complete')))
    states = [p.get('query_state') for p in providers.values()]
    view['query_state'] = ('failed' if not view.get('accounts') and all(s=='failed' for s in states)
                           else 'partial' if any(s in ('failed','partial','pending') for s in states)
                           else 'received')
    view['reason'] = '; '.join(provider+':'+str(p['reason']) for provider,p in providers.items() if p.get('reason')) or None
    # Different provider reference rates must not be applied to another provider.
    fx = {'KRW': '1'}
    for currency in {c for a in view.get('accounts', []) for c in a.get('fx_to_base', {})}:
        rates = {str(a['fx_to_base'][currency]) for a in view['accounts'] if currency in a.get('fx_to_base', {})}
        if len(rates)==1:
            fx[currency] = rates.pop()
    view['valuation'] = {'base_currency':'KRW', 'fx_to_base':fx}
    # A merged receipt must reference the merged payload, never only the NH snapshot.
    merged = {'schema_version':1, 'provider':'NH PLUG / Meritz', 'source':'broker_export',
              'as_of':view['as_of'], 'accounts':view.get('accounts', []), 'accounts_complete':view['accounts_complete'],
              'base_currency':'KRW', 'fx_to_base':fx, 'max_age_hours':'24', 'limitations':view.get('limitations', [])}
    if persist:
        view['snapshot_ref'] = store.save(merged)
    else:
        view.pop('snapshot_ref', None)
    return view
