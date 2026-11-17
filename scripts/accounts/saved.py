"""Compose saved provider observations without advancing any original timestamp."""
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import re
import sqlite3

from accounts.brokers import decimal
from accounts.store import AccountStore


class ReadOnlyAccountStore(AccountStore):
    """Read observations without schema initialization or journal-mode changes."""

    def connect(self):
        db = sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True, timeout=10)
        db.execute('PRAGMA query_only=ON')
        return db

    def save(self, value):
        raise ValueError('direct_account_read_cannot_write')


def _stale(stamp):
    if not stamp:
        return True
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(stamp.replace('Z', '+00:00'))).total_seconds()
    return not 0 <= age <= 86400


def saved_accounts(root):
    """Return internal saved observations, including lots; no exports or refresh.

    Missing files return source_mode=missing. Read/integrity errors propagate;
    they must never be rendered as a successful empty balance. Call
    session_projection before exposing this internal view to a session.
    """
    view = dict(accounts=[], as_of=None, attempted_at=None, query_state='not_requested',
                reason='saved_accounts_unavailable', source_mode='missing', stale=True,
                accounts_complete=False, risk=None, limitations=[], endpoint_calls=[])
    path = Path(root) / 'runtime/accounts.sqlite3'
    try:
        path.stat()
    except FileNotFoundError:
        return view
    store = ReadOnlyAccountStore(path)
    receipt, attempt = store.latest('nh'), store.latest_attempt('nh')
    if receipt:
        snapshot = store.read(receipt['snapshot_ref'])
        meta = attempt or receipt
        view.update(accounts=deepcopy(snapshot['accounts']), as_of=snapshot['as_of'],
                    attempted_at=meta.get('attempted_at'), query_state=meta.get('query_state'),
                    reason=meta.get('reason'),
                    source_mode='historical_saved' if meta.get('query_state') == 'failed' else 'shared_saved',
                    stale=_stale(snapshot['as_of']), accounts_complete=snapshot.get('accounts_complete', False),
                    valuation={k: snapshot.get(k) for k in ('base_currency', 'fx_to_base')},
                    limitations=deepcopy(snapshot.get('limitations', [])))
        for account in view['accounts']:
            account.update(provider='nh', as_of=account.get('as_of', snapshot['as_of']),
                           attempted_at=meta.get('attempted_at'), query_state=meta.get('query_state'),
                           stale=view['stale'])
            account.setdefault('fx_to_base', deepcopy(snapshot.get('fx_to_base', {})))
            if meta.get('query_state') in ('failed', 'partial', 'pending'):
                account['holdings_complete'] = False
    elif attempt:
        view.update(query_state=attempt.get('query_state'), reason=attempt.get('reason'),
                    attempted_at=attempt.get('attempted_at'))
    return merge_saved(store, view, persist=False)


def merge_saved(store, nh_view, *, persist=True):
    view = deepcopy(nh_view)
    receipt, attempt = store.latest('meritz'), store.latest_attempt('meritz')
    if not receipt and not attempt:
        return view  # Preserve the pre-existing NH-only representation.
    providers = {'nh': {k: view.get(k) for k in (
        'query_state', 'reason', 'as_of', 'attempted_at', 'source_mode', 'stale', 'snapshot_ref',
        'accounts_complete', 'limitations', 'valuation')}}
    snapshot = store.read(receipt['snapshot_ref']) if receipt else None
    meta = attempt or receipt
    meritz = {**meta, 'as_of': (snapshot or {}).get('as_of'),
              'source_mode': 'historical_saved' if meta.get('query_state') == 'failed' else 'shared_saved'}
    meritz.update(stale=_stale((snapshot or {}).get('as_of')),
                  accounts_complete=(snapshot or {}).get('accounts_complete', False),
                  limitations=deepcopy((snapshot or {}).get('limitations', [])),
                  valuation={k: (snapshot or {}).get(k) for k in ('base_currency', 'fx_to_base')})
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
                       attempted_at=meta.get('attempted_at'),
                       query_state=meta.get('query_state'), stale=meritz['stale'])
        account['fx_to_base'] = source.get('fx_to_base', snapshot.get('fx_to_base', {}))
        if account['query_state'] in ('failed', 'partial', 'pending'):
            account['holdings_complete'] = False
        view.setdefault('accounts', []).append(account)
    aliases = [a['alias'] for a in view.get('accounts', [])]
    if len(aliases) != len(set(aliases)):
        raise ValueError('account_provider_alias_collision')
    observed = [p['as_of'] for p in providers.values() if p.get('as_of')]
    attempted = [p['attempted_at'] for p in providers.values() if p.get('attempted_at')]
    view['attempted_at'] = max(attempted, key=lambda v: datetime.fromisoformat(v.replace('Z', '+00:00'))) if attempted else None
    view['limitations'] = list(dict.fromkeys(view.get('limitations', []) + (snapshot or {}).get('limitations', [])))
    view.update(providers=providers, provider='NH PLUG / Meritz',
                as_of=min(observed, key=lambda v: datetime.fromisoformat(v.replace('Z','+00:00'))) if observed else None,
                stale=not observed or any(p.get('stale', True) for p in providers.values() if p.get('as_of')),
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


# Session output is an allowlist, not a recursive blacklist. In particular, the
# normalized internal observed_balances/lots still contain broker evidence.
_REASONS = frozenset('''
saved_accounts_unavailable broker_normalization_failed shared_account_query
broker_response_or_shape_unconfirmed broker_response_or_pagination_unconfirmed
balance_transport_or_body_unconfirmed balance_business_response_unconfirmed
pagination_completion_unconfirmed pagination_cursor_invalid pagination_limit pagination_incomplete
broker_number_missing broker_number_invalid broker_mcp_error broker_mcp_body_unconfirmed
broker_numeric_precision_unconfirmed broker_body_invalid broker_observation_shape_invalid
broker_duplicate_account_alias broker_account_alias_invalid broker_markets_shape_invalid
broker_responses_shape_invalid broker_holding_value_or_instrument_unconfirmed
duplicate_lot_or_page_unconfirmed lot_value_unconfirmed lot_valuation_price_conflict
instrument_unconfirmed NH_CMA_note_separate_valuation_required
NH_average_cost_basis_unconfirmed NH_overseas_scope_unconfirmed
meritz_balance_scope_invalid meritz_business_response_unconfirmed meritz_empty_response_conflict
meritz_pagination_conflict meritz_pagination_cursor_invalid meritz_pagination_completion_unconfirmed
meritz_pages_after_completion meritz_pagination_incomplete meritz_aggregate_shape_invalid
meritz_holdings_shape_invalid meritz_currency_scope_or_duplicate meritz_fx_conversion_unconfirmed
deposit_unconfirmed valuation_unconfirmed ovs_currencies_unconfirmed
KR_US_scope_not_all_assets cash_and_liabilities_not_reconciled working_orders_not_reconciled stops_not_adopted
credentials_missing auth_expired permission_denied transport_failed transport_failure
'''.split())
_STATES = frozenset(('not_requested', 'pending', 'received', 'partial', 'failed', 'unavailable'))
_MODES = frozenset(('missing', 'historical_saved', 'shared_saved'))
_PROVIDERS = frozenset(('nh', 'meritz', 'NH PLUG', 'Meritz Open API', 'NH PLUG / Meritz'))
_CURRENCIES = ('KRW', 'USD')
_DEPOSITS = ('dca', 'nxt_dd_dca', 'nxt2_dd_dca', 'fc_dca', 'krw_dca')


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _rows(value):
    return value if isinstance(value, list) else []


def _choice(value, choices):
    return value if isinstance(value, str) and value in choices else None


def _reason(value):
    if value is None:
        return None
    if isinstance(value, str) and re.fullmatch(r'http_[1-5][0-9]{2}', value):
        return value
    return _choice(value, _REASONS) or 'unavailable_reason_redacted'


def _stamp(value):
    if isinstance(value, str):
        try:
            if datetime.fromisoformat(value.replace('Z', '+00:00')).tzinfo is not None:
                return value
        except ValueError:
            pass
    return None


def _number(value):
    # Keep the exact decimal text, including fractional quantity trailing zeros.
    if type(value) in (str, int, float):
        try:
            decimal(value)
            return value
        except ValueError:
            pass
    return None


def _numbers(source, keys):
    source = _mapping(source)
    return {key: _number(source[key]) for key in keys if key in source}


def _status(source):
    source = _mapping(source)
    result = {}
    for key in ('as_of', 'attempted_at'):
        if key in source:
            result[key] = _stamp(source[key])
    for key in ('stale', 'accounts_complete', 'cash_complete', 'holdings_complete'):
        if key in source:
            result[key] = source[key] if type(source[key]) is bool else False
    for key, choices in (('provider', _PROVIDERS), ('query_state', _STATES), ('source_mode', _MODES)):
        if key in source:
            result[key] = _choice(source[key], choices)
    if 'reason' in source:
        result['reason'] = _reason(source['reason'])
    for key in ('limitations', 'balance_limitations'):
        if key in source:
            result[key] = [_reason(code) for code in _rows(source[key])]
    return result


def _valuation(source):
    source = _mapping(source)
    return {'base_currency': _choice(source.get('base_currency'), _CURRENCIES),
            'fx_to_base': _numbers(source.get('fx_to_base'), _CURRENCIES)}


def _alias(value):
    # Existing hashed aliases and short human aliases are safe identifiers;
    # raw numeric account IDs and arbitrary broker/free-form text are not.
    if (isinstance(value, str) and re.fullmatch(r'(nh|meritz)-[A-Za-z0-9][A-Za-z0-9-]{0,63}', value)
            and not re.search(r'\d{8,}', value.split('-', 1)[1].replace('-', ''))):
        return value
    return None


def _symbol(value, market):
    if isinstance(value, str) and (
        market == 'KR' and re.fullmatch(r'[0-9]{6}', value)
        or market == 'US' and re.fullmatch(r'[A-Z]{1,10}(?:[.-][A-Z]{1,2})?', value)
        or value == 'unidentified'
    ):
        return value
    return None


def _position(source):
    source = _mapping(source)
    result = _numbers(source, ('quantity', 'price', 'average_cost', 'fx_to_base',
                               'observed_evaluation_krw', 'observed_evaluation',
                               'observed_fx_rate', 'observed_purchase_unit_price'))
    market = _choice(source.get('market'), ('KR', 'US'))
    result.update(market=market, symbol=_symbol(source.get('symbol'), market),
                  currency=_choice(source.get('currency'), _CURRENCIES))
    if 'quote_symbol' in source:
        result['quote_symbol'] = _symbol(source['quote_symbol'], market)
    for key, choices in (
        ('asset_type', ('stock', 'cash_equivalent')),
        ('price_basis', ('broker_last_price_observation',)),
        ('cost_basis', ('NH_fc_avg_phs_pr',)),
        ('instrument_class_hint', ('CMA_note_from_broker_label',)),
        ('benchmark', ('^GSPC',)),
    ):
        if key in source:
            result[key] = _choice(source[key], choices)
    if 'data_unavailable_reason' in source:
        result['data_unavailable_reason'] = _reason(source['data_unavailable_reason'])
    return result


def _balances(source):
    """Select numeric observations, never entire broker aggregate dictionaries."""
    source = _mapping(source)
    result = {}
    for section, keys in (
        ('KR', _DEPOSITS), ('US', _DEPOSITS),
        ('deposit', ('tfam', 'd1_aft_tfam', 'd2_aft_tfam', 'oder_able_cash_amt', 'drng_able_amt')),
        ('valuation', ('aset_vltn_amt', 'frcr_tfam', 'loan_amt')),
    ):
        if section in source:
            result[section] = _numbers(source[section], keys)
    if 'ovs_currencies' in source:
        result['ovs_currencies'] = [
            {'crcd': row['crcd'], **_numbers(row, ('frcr_amt', 'estm_frcr_amt', 'vltn_exrt', 'excl_wncr_vltn_amt'))}
            for row in _rows(source['ovs_currencies']) if isinstance(row, dict)
            and _choice(row.get('crcd'), _CURRENCIES)]
    return result


def _account(source):
    source = _mapping(source)
    result = _status(source)
    result.update(alias=_alias(source.get('alias')),
                  positions=[_position(row) for row in _rows(source.get('positions')) if isinstance(row, dict)])
    for key in ('cash', 'fx_to_base', 'observed_foreign_deposits'):
        if key in source:
            result[key] = _numbers(source[key], _CURRENCIES)
    if 'observed_deposits' in source:
        result['observed_deposits'] = {market: _numbers(values, _DEPOSITS)
            for market, values in _mapping(source['observed_deposits']).items() if market in ('KR', 'US')}
    if 'observed_balances' in source:
        result['observed_balances'] = _balances(source['observed_balances'])
    if 'query_markets' in source:
        result['query_markets'] = {}
        for market in ('KR', 'US'):
            if market not in _mapping(source['query_markets']):
                continue
            query = _mapping(source['query_markets'][market])
            selected = {'state': _choice(query.get('state'), _STATES)}
            if 'reason' in query:
                selected['reason'] = _reason(query['reason'])
            if type(query.get('pages')) is int and query['pages'] >= 0:
                selected['pages'] = query['pages']
            result['query_markets'][market] = selected
    # Raw unsupported rows cannot leave the account boundary, but their limits
    # must remain visible, including out-of-scope currencies and assets.
    for key in ('unsupported_rows', 'out_of_scope_rows', 'out_of_scope_currency_rows'):
        if _rows(source.get(key)):
            result.setdefault('balance_limitations', []).append(key + '_excluded')
    return result


def session_projection(view):
    """Return a fresh, JSON-safe allowlist for M05; never read refs or mutate input.

    Unknown text reasons become unavailable_reason_redacted. Invalid/missing
    scalars remain null/absent, never zero or confirmed completeness. Names,
    lots, raw evidence/refs, credentials, leases and account numbers are omitted.
    """
    source = _mapping(view)
    result = _status(source)
    result['accounts'] = [_account(row) for row in _rows(source.get('accounts')) if isinstance(row, dict)]
    if 'valuation' in source:
        result['valuation'] = _valuation(source['valuation'])
    if 'providers' in source:
        result['providers'] = {}
        for provider in ('nh', 'meritz'):
            if provider in _mapping(source['providers']):
                original = _mapping(source['providers'][provider])
                selected = _status(original)
                if 'valuation' in original:
                    selected['valuation'] = _valuation(original['valuation'])
                result['providers'][provider] = selected
    return result
