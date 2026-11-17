"""Explicit user-approved market exclusions; never infer consent from a failure."""
from workbench_paths import ROOT, SCRIPTS
from atomic_io import atomic_write
from invest_agent.trading.store import instance_lock
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

POLICY = (SCRIPTS / 'market/policies/exclusions.json')


REGISTRY = ROOT/'runtime/market-scope-auto/records.json'


def read_registry(*, root=None):
    registry = Path(root)/'runtime/market-scope-auto/records.json' if root is not None else REGISTRY
    if not registry.exists():
        return [], b''
    raw = registry.read_bytes()
    data = json.loads(raw)
    if data.get('schema') != 1:
        raise ValueError('invalid_automatic_scope_registry')
    return data['records'], raw


def remember(records, policy):
    rule = policy.get('dual_provider') or {}
    if not rule.get('enabled'):
        return 0
    items = [r for r in records if r.get('dual_provider_rule') == rule.get('rule_id')
             and r.get('reason') == 'dual_provider_data_unavailable' and r.get('isin_code')]
    if not items:
        return 0
    with instance_lock(REGISTRY.parent):
        prior, _ = read_registry()
        keys = {(r['market'],r['symbol'],r.get('isin_code')) for r in prior}
        added = []
        for r in items:
            key = r['market'],r['symbol'],r['isin_code']
            if key not in keys:
                keys.add(key); added.append(r)
        if added:
            atomic_write(REGISTRY,json.dumps({'schema':1,'records':prior+added},ensure_ascii=False,indent=2))
        return len(added)


def load(path=None, *, user_registry=None, root=None):
    path = Path(path or POLICY)
    payload = json.loads(path.read_text(encoding='utf-8'))
    if payload.get('schema') != 1 or payload.get('scope') != 'market_symbol_until_user_restores':
        raise ValueError('invalid_market_exclusion_policy')
    registry_raw = b''
    if (payload.get('dual_provider') or {}).get('enabled'):
        automatic, registry_raw = read_registry(root=root)
        manual_keys = {(r['market'],r['symbol']) for r in payload['records']}
        payload['records'] += [r for r in automatic if (r['market'],r['symbol']) not in manual_keys
            and r.get('dual_provider_rule') == payload['dual_provider'].get('rule_id')
            and r.get('reason') == 'dual_provider_data_unavailable']
    from market.exclusions import read_records
    if user_registry is None and root is not None:
        user_registry = Path(root)/'runtime/market-scope-user/records.json'
    approved, user_raw = read_records(user_registry)
    existing = {(r['market'],r['symbol'],r.get('isin_code'),r.get('exchange')) for r in payload['records']}
    payload['records'] += [r for r in approved if (r['market'],r['symbol'],r.get('isin_code'),r.get('exchange')) not in existing]
    lifecycle_path = (SCRIPTS / 'market/policies/lifecycle.json')
    lifecycle_raw = lifecycle_path.read_bytes()
    payload['lifecycle_records'] = json.loads(lifecycle_raw)['records']
    seen = set()
    for r in payload['records']:
        key = r['market'], r['symbol'], r.get('isin_code'), r.get('exchange')
        if key in seen or r['market'] not in ('KR','US') or not all(r.get(k) for k in ('symbol','reason','approved_on','approval','evidence_run')):
            raise ValueError('invalid_or_duplicate_market_exclusion')
        seen.add(key)
    return {**payload, 'path': str(path.resolve()), 'sha256': hashlib.sha256(path.read_bytes()+registry_raw+lifecycle_raw+user_raw).hexdigest()}


def match(row, policy=None, *, completed_session=None, root=None):
    policy = policy if policy is not None else load(root=root)
    as_of = completed_session if completed_session is not None else policy.get('completed_sessions', {}).get(
        row['market'], datetime.now(timezone.utc).date().isoformat())
    for found in policy['records']:
        if (found['market'],found['symbol']) != (row['market'],row['symbol']):
            continue
        if found.get('identity_bound') and row.get('isin_code') and found.get('isin_code') != row['isin_code']:
            continue
        exchange = row.get('listing_exchange_family') or row.get('exchange')
        if found.get('approval_id') and exchange and found.get('exchange') != exchange:
            continue
        if found.get('dual_provider_rule'):
            if not row.get('isin_code') or found.get('isin_code') != row['isin_code'] or as_of < found.get('effective_session', ''):
                continue
        return found
    from market.quality import lifecycle_record
    ended = lifecycle_record(row, as_of, policy.get('lifecycle_records', []))
    if ended:
        return {**ended, 'reason':'confirmed_trading_ended', 'approved_on':'2026-10-02',
                'approval':'user_approved_automatic_confirmed_trading_ended',
                'evidence_run':ended['source_url'], 'lifecycle_rule':'confirmed_ended_exact_identity_v1'}
    # Type/size alone never creates an exclusion. Only diagnosed records apply.
    return None


def partition(rows, policy, *, completed_sessions=None):
    active, excluded = [], []
    for row in rows:
        record = match(row, policy, completed_session=(completed_sessions.get(row['market'], '')
                       if completed_sessions is not None else None))
        if record:
            excluded.append({**record, 'observed_isin_code': row.get('isin_code'), 'exchange':row.get('listing_exchange_family',row.get('exchange'))})
        else:
            active.append(row)
    return active, excluded


def apply_snapshot(snapshot, policy):
    """Create a new active-scope snapshot, retaining an explicit exclusion audit."""
    out = deepcopy(snapshot)
    active, excluded = partition(out['universe'], policy, completed_sessions={
        k:v[-1] for k,v in out.get('sessions', {}).items() if v})
    if not active:
        raise ValueError('market_scope_empty_after_exclusions')
    keys = {(r['market'],r['symbol']) for r in active}
    out['universe'] = active
    for name in ('series','errors'):
        out[name] = [r for r in out[name] if (r['market'],r['symbol']) in keys]
    prior = {(r['market'],r['symbol']):r for r in out.get('scope_exclusions',[])}
    prior.update({(r['market'],r['symbol']):r for r in excluded})
    out['scope_exclusions'] = list(prior.values())
    out['scope_policy'] = {k:policy[k] for k in ('scope','path','sha256')}
    cov = out['coverage']
    cov.update(scope='user_approved_active_market_scope',requested=len(active),received=len(out['series']),
               excluded=len(prior),provider_listed_count=len(active)+len(prior))
    refresh_coverage(out)
    for exchange,meta in (cov.get('listings') or {}).items():
        meta.update(requested=sum(r.get('listing_exchange_family')==exchange for r in active),
                    approved_excluded=sum(r.get('market')==('KR' if exchange in ('KOSPI','KOSDAQ') else 'US')
                        and (r.get('exchange')==exchange) for r in prior.values()))
    out['collection_stats'].update(requested=len(active),received=len(out['series']),failed=len(out['errors']),
                                   completed=len(out['series'])+len(out['errors']),approved_excluded=len(prior))
    return out


def refresh_coverage(snapshot):
    """Recompute active completeness after qualified provider recovery."""
    cov = snapshot['coverage']
    active = snapshot['universe']
    complete = (bool(cov.get('listings')) and all(m.get('state')=='available' and m.get('limit') is None for m in cov['listings'].values())
                and not cov['scope_errors'] and not snapshot['errors'] and len(active)==len(snapshot['series'])
                and (snapshot.get('price_collection_provider') == 'toss' or
                     not any(str(r.get('market_cap_quality','')).startswith('unavailable') for r in active)))
    cov['active_scope_complete'] = bool(complete)
    cov['market_wide'] = False if snapshot.get('scope_exclusions') else cov['market_wide']
