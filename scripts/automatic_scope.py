"""User-approved US instrument and problem-size exclusions; ETFs stay in scope."""
import hashlib
import json
import math
import re
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from invest_agent.trading.runner import atomic_write
from invest_agent.trading.store import instance_lock

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT/'runtime/market-scope-auto/records.json'
PROBLEMS = {'finviz_public_evidence_absent', 'finviz_direct_evidence_not_acquired',
    'new_listing_classification_not_acquired', 'original_conflict:security_type_conflict',
    'finviz_security_type_conflict', 'stale_or_invalid_last_session',
    'no_completed_session_bars', 'historical_price_gaps'}


def read_registry():
    if not REGISTRY.exists():
        return [], b''
    raw = REGISTRY.read_bytes()
    data = json.loads(raw)
    if data.get('schema') != 1:
        raise ValueError('invalid_automatic_scope_registry')
    return data['records'], raw


@lru_cache(maxsize=4)
def facts(path, expected):
    path = Path(path)
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError('automatic_scope_facts_hash_mismatch')
    data = json.loads(raw)
    if data.get('schema') != 1:
        raise ValueError('automatic_scope_facts_schema')
    proofs = {p['path']:p['sha256'] for r in data['records'] for p in r['proofs']}
    proofs[data['source_snapshot']['path']] = data['source_snapshot']['sha256']
    for name, want in proofs.items():
        with Path(name).open('rb') as stream:
            if hashlib.file_digest(stream, 'sha256').hexdigest() != want:
                raise ValueError('automatic_scope_proof_hash_mismatch')
    result = {}
    for row in data['records']:
        key = tuple(row[k] for k in ('market','symbol','isin_code','exchange'))
        if key in result or not row['isin_code']:
            raise ValueError('automatic_scope_ambiguous_identity')
        result[key] = row
    return result


def instrument(row, rule):
    key = (row.get('market'), row.get('symbol'), row.get('isin_code'),
           row.get('listing_exchange_family', row.get('exchange')))
    ref = rule.get('facts')
    fact = facts(ref['path'], ref['sha256']).get(key) if ref else None
    name = ' '.join(str(row.get(k) or '') for k in ('name','english_name')).upper()
    explicit = str(row.get('security_type', row.get('securityType',''))).upper()
    # The ETF exception precedes every name/type/market-cap rule, including bond ETFs.
    if explicit == 'ETF' or re.search(r'\bETF\b', name) or fact and fact['type'] == 'etf':
        return 'etf', fact
    if fact and fact['type'] in ('preferred_or_bond','non_etf_fund'):
        return fact['type'], fact
    if explicit in ('BOND','NOTE') or '채권' in name or re.search(r'\b(?:SENIOR NOTES|SUBORDINATED NOTES|DEBENTURES|NOTES DUE)\b',name):
        return 'bond', fact
    if explicit in ('PREFERRED','PREFERRED_STOCK') or '우선주' in name or re.search(r'\b(?:PREFERRED|PRF|PFD)\b',name):
        return 'preferred_or_bond', fact
    if explicit in ('CLOSED_END_FUND','MUTUAL_FUND') or '폐쇄형 펀드' in name:
        return 'non_etf_fund', fact
    return fact['type'] if fact else 'unconfirmed', fact


def record(row, rule, reason, evidence):
    return {'market':row['market'],'symbol':row['symbol'],'name':row.get('name'),
        'provider_symbol':row.get('provider_symbol'), 'isin_code':row.get('isin_code'),
        'reason':reason,'approved_on':rule['approved_on'],'approval':rule['approval'],
        'evidence_run':evidence.get('run') or 'automatic_scope_listing_identity',
        'automatic_rule':rule['rule_id'],'evidence':evidence}


def problem_exclusion(row, reasons, policy, *, as_of, run):
    rule = policy.get('automatic') or {}
    if not rule.get('enabled') or row.get('market') != 'US' or not set(reasons) & PROBLEMS:
        return None
    typ, fact = instrument(row, rule)
    if typ in rule['preserve_types']:
        return None
    if typ in rule['exclude_types']:
        return record(row, rule, 'automatic_problem_instrument_type:'+typ,
            {'run':str(run),'problems':sorted(set(reasons)&PROBLEMS),'type':typ,
             'fact':fact,'listing_name':row.get('name'),'listing_isin':row.get('isin_code')})
    # A share-class value is not a verified company-wide market cap.
    name = str((fact or {}).get('source_name') or row.get('name') or '')
    if '.' in row['symbol'] or re.search(r'\bClass\s+[A-Z]\b', name, re.I):
        return None
    cap = row.get('market_cap')
    if (isinstance(cap,bool) or not isinstance(cap,(int,float)) or not math.isfinite(cap)
            or not 0 < cap < rule['problem_market_cap_below_usd']
            or row.get('market_cap_currency') != 'USD'
            or row.get('market_cap_quality') not in ('quoted_price','completed_close_estimate')):
        return None
    try:
        day = datetime.fromisoformat(row['market_cap_as_of']).date()
        applied = datetime.fromisoformat(as_of).date()
        if not 0 <= (applied-day).days <= rule['max_cap_age_days']:
            return None
    except (TypeError,ValueError,KeyError):
        return None
    return record(row, rule, 'automatic_low_importance_problem',
        {'run':str(run),'problems':sorted(set(reasons)&PROBLEMS),'market_cap':cap,
         'currency':'USD','market_cap_as_of':row['market_cap_as_of'],
         'market_cap_basis':row.get('market_cap_basis'),'market_cap_quality':row['market_cap_quality'],
         'threshold_exclusive':rule['problem_market_cap_below_usd']})


def remember(records, policy):
    rule = policy.get('automatic') or {}
    items = [r for r in records if r.get('automatic_rule') == rule.get('rule_id')]
    if not items or not rule.get('enabled'):
        return 0
    with instance_lock(REGISTRY.parent):
        prior, _ = read_registry()
        keys = {(r['market'],r['symbol']) for r in prior}
        added = []
        for r in items:
            key = r['market'],r['symbol']
            if key not in keys:
                keys.add(key);added.append(r)
        if added:
            atomic_write(REGISTRY,json.dumps({'schema':1,'records':prior+added},ensure_ascii=False,indent=2))
        return len(added)


def decisions(snapshot, groups, policy, *, as_of, run):
    """Only unresolved collection/classification evidence triggers the policy."""
    problems = {}
    blocked = set()
    for row in snapshot.get('errors', []):
        key = row['market'],row['symbol']
        if row.get('error_code') not in PROBLEMS:
            blocked.add(key)
        problems.setdefault(key,set()).add(row.get('error_code'))
    for row in groups:
        if row['group_state'] != 'matched':
            problems.setdefault((row['market'],row['symbol']),set()).add(row.get('reason'))
    result = []
    for row in snapshot['universe']:
        if (row['market'],row['symbol']) in blocked:
            continue
        reasons = problems.get((row['market'],row['symbol']),set())
        found = problem_exclusion(row,reasons,policy,as_of=as_of,run=run)
        if found:
            result.append(found)
    return result


def derive(source, groups, policy, output, *, as_of):
    """Freeze the diagnosed input and policy before deriving the narrower scope."""
    import market_scope
    import sector_observation as so
    from market_collect import audit, write_json
    source, output = Path(source).resolve(), Path(output).resolve()
    original = so.load(source/'snapshot.json')
    found = decisions(original,groups,policy,as_of=as_of,run=source)
    if not found:
        return source, []
    output.mkdir(parents=True,exist_ok=False)
    write_json(output/'policy.json',policy)
    write_json(output/'problem-groups.json',groups)
    keys = {(r['market'],r['symbol']) for r in found}
    frozen = {**policy,'records':[r for r in policy['records'] if (r['market'],r['symbol']) not in keys]+found,
              'path':str(output/'policy.json'),'sha256':so.sha(output/'policy.json')}
    snapshot = market_scope.apply_snapshot(original,frozen)
    write_json(output/'snapshot.json',snapshot)
    write_json(output/'summary.json',audit(snapshot))
    write_json(output/'automatic-derivation.json',{'schema':1,'source':str(source),
        'source_manifest_sha256':so.sha(source/'manifest.json'),'source_snapshot_sha256':so.sha(source/'snapshot.json'),
        'as_of':as_of,'decisions':found,'network_calls':0})
    write_json(output/'manifest.json',{p.name:so.sha(p) for p in output.iterdir() if p.is_file()})
    return output, found


def verify_derivation(source):
    """Replay decisions with frozen inputs; never read or update the live registry."""
    import market_scope
    import sector_observation as so
    source = Path(source)
    path = source/'automatic-derivation.json'
    if not path.exists():
        return
    meta = so.load(path)
    parent = Path(meta['source'])
    so.checked(parent/'manifest.json',meta['source_manifest_sha256'],{})
    so.checked(parent/'snapshot.json',meta['source_snapshot_sha256'],{})
    original = so.load(parent/'snapshot.json')
    policy = so.load(source/'policy.json')
    found = decisions(original,so.load(source/'problem-groups.json'),policy,as_of=meta['as_of'],run=parent)
    if found != meta['decisions']:
        raise ValueError('automatic_scope_decision_replay_mismatch')
    keys = {(r['market'],r['symbol']) for r in found}
    frozen = {**policy,'records':[r for r in policy['records'] if (r['market'],r['symbol']) not in keys]+found,
              'path':str(source.resolve()/'policy.json'),'sha256':so.sha(source/'policy.json')}
    if market_scope.apply_snapshot(original,frozen) != so.load(source/'snapshot.json'):
        raise ValueError('automatic_scope_snapshot_replay_mismatch')


def filter_screen(screening, snapshot):
    """Reuse independent calculations, recomputing scope-dependent totals."""
    from invest_agent.trading.market import screen
    from invest_agent.trading.universe_gate import audit
    keys = {(r['market'],r['symbol']) for r in snapshot['universe']}
    rows = [r for r in screening['rows'] if (r['market'],r['symbol']) in keys]
    result = screen({**snapshot,'series':[],'errors':[],'previous_screen':[]})
    result.update(rows=rows,candidate_count=sum(r['state']=='candidate' for r in rows),
        unavailable_count=sum(r['state']=='unavailable' for r in rows),universe_audit=audit(rows))
    return result


def refresh_classification_source(source, reference, output):
    """A review may refresh classification without changing any market observations."""
    import sector_observation as so
    from market_collect import write_json
    source,output=Path(source).resolve(),Path(output).resolve()
    snapshot=so.load(source/'snapshot.json')
    if snapshot.get('classification_refresh')==reference:
        return source
    output.mkdir(parents=True,exist_ok=False)
    snapshot['classification_refresh']=reference
    write_json(output/'snapshot.json',snapshot)
    write_json(output/'summary.json',so.load(source/'summary.json'))
    write_json(output/'classification-derivation.json',{'source':str(source),
        'source_manifest_sha256':so.sha(source/'manifest.json'),'source_snapshot_sha256':so.sha(source/'snapshot.json')})
    write_json(output/'manifest.json',{p.name:so.sha(p) for p in output.iterdir() if p.is_file()})
    return output
