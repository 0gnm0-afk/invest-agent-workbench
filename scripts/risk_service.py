"""B integration over A's canonical store and already loaded account data.

No provider, filesystem account discovery, DB migration, adoption or delivery.
Browser saves consume a server-held calculation ticket, never a submitted result.
"""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import secrets
import threading
import time

from analysis_collaboration import Forbidden
from chart_workspace import Conflict
from portfolio_scenarios import compare
from risk_adapter import account_projection, read_plan, project_plan, assemble, save_review, shared_hash
from risk_values import fingerprint, instant

OPS = {'risk_overview', 'risk_calculate', 'risk_save'}
CODE_HASH = hashlib.sha256(b''.join((Path(__file__).parent/name).read_bytes() for name in
    ('risk_service.py', 'risk_adapter.py', 'risk_values.py', 'risk_conditions.py', 'portfolio_scenarios.py', 'analysis_geometry.py'))).hexdigest()


def saved_account_input(snapshot):
    """Whitelist normalized facts; never expose credentials or raw broker responses."""
    result = {k: deepcopy(snapshot.get(k)) for k in ('schema_version', 'source', 'as_of',
        'base_currency', 'max_age_hours', 'accounts_complete', 'fx_to_base', 'fx_observation')}
    result['accounts'] = []
    for account in snapshot.get('accounts', []):
        row = {k: deepcopy(account.get(k)) for k in ('alias', 'holdings_complete', 'cash_complete',
            'liabilities_complete', 'cash_reconciliation', 'cash', 'liabilities') if k in account}
        row['positions'] = [{k: deepcopy(p.get(k)) for k in ('market', 'symbol', 'currency', 'quantity',
            'price', 'price_as_of', 'price_basis', 'isin_code', 'isin', 'instrument_class_hint',
            'observed_evaluation_krw', 'data_unavailable_reason') if k in p} for p in account.get('positions', [])]
        result['accounts'].append(row)
    return result


class RiskService:
    def __init__(self, analyses, view, *, clock=None, allow_test_records=False):
        self.analyses, self.view = analyses, view
        self.clock = clock or (lambda: datetime.now(timezone.utc).isoformat())
        self.allow_test_records = allow_test_records
        self.tickets, self.lock = {}, threading.Lock()

    def rpc(self, origin):
        return lambda op, args: self.analyses.dispatch(op, args, origin, principal='risk')

    def account(self, alias=None):
        data, _ = self.view()
        bundle = data.get('accounts', {}).get('scenario_input')
        if bundle is None:
            raise ValueError('saved_account_input_unavailable')
        full = deepcopy(bundle)
        source_hash = shared_hash(full)
        if alias:
            full['accounts'] = [a for a in full['accounts'] if a['alias'] == alias]
            if not full['accounts']:
                raise ValueError('unknown_account_alias')
            # Universe completeness remains conservative even for a selected account.
        snapshot = account_projection(full, evaluated_at=self.clock())
        external = {'kind': 'account_market_fx', 'id': source_hash, 'sha256': source_hash,
                    'as_of': bundle.get('as_of'), 'source': bundle.get('source'),
                    'file_sha256': data['accounts'].get('snapshot_sha256')}
        return snapshot, external

    def eligible(self, cid):
        case = self.analyses.case(cid)
        if case.get('test_record') and not self.allow_test_records:
            raise ValueError('test_record_not_operational')
        return case

    def overview(self, origin, alias=None):
        snapshot, baseline, issues = None, None, []
        try:
            snapshot, _ = self.account(alias)
            baseline = compare(snapshot, {'id': 'current-account-valuation', 'version': 1,
                'horizon': snapshot['evaluated_at'], 'fx_to_base': {}, 'prices': {}})['baseline']
        except (ValueError, KeyError, TypeError) as exc:
            issues.append(str(exc) if isinstance(exc, ValueError) else 'saved_account_input_incomplete')
        plans, reviews = [], []
        for case in self.analyses.rows('analysis_cases'):
            if case.get('test_record') and not self.allow_test_records:
                continue
            current = self.rpc(origin)('collaboration_read', {'analysis_key': case['id']})
            active = {p['record']['id'] for p in current['active_plans']}
            # Include active older versions as well as the latest draft of each plan.
            selected = {p['id']: p for p in current['plans']}
            selected.update({p['record']['id']: p['record'] for p in current['active_plans']})
            for record in selected.values():
                if alias and record['plan']['account_alias'] != alias:
                    continue
                plans.append({'analysis_key': case['id'], 'revision': current['revision'],
                    'record': record, 'adopted': record['id'] in active, 'links': current['links']})
            for record in current['records']:
                if record['kind'] == 'risk_review' and record.get('calculation'):
                    scope = record['calculation']['assumptions'].get('account_alias')
                    if scope == alias:
                        reviews.append({'record': record, 'links': current['links']})
        return {'contract_version': 'collaboration.v1', 'snapshot': snapshot, 'baseline': baseline,
                'issues': issues, 'plans': plans, 'reviews': reviews[-20:][::-1],
                'account_alias': alias, 'test_mode': self.allow_test_records}

    def calculate(self, args, origin, session):
        if set(args) != {'selections', 'assumptions', 'account_alias'}:
            raise ValueError('invalid_risk_calculation_request')
        selections = args['selections']
        if not isinstance(selections, list) or not 1 <= len(selections) <= 50:
            raise ValueError('explicit_plan_selection_required')
        assumptions = deepcopy(args['assumptions'])
        instant(assumptions['horizon'])
        snapshot, external = self.account(args['account_alias'])
        contexts, projections, revisions = [], [], {}
        for item in selections:
            self.eligible(item['analysis_key'])
            context = read_plan(self.rpc(origin), item['analysis_key'], item['record_id'],
                                mode=item['mode'], evaluation_at=instant(assumptions['horizon']).date().isoformat())
            if (item['plan_version'], item['plan_sha256'], item['snapshot_sha256']) != (
                    context['plan_version'], context['plan_sha256'], context['snapshot_sha256']):
                raise Conflict('selected_plan_input_changed')
            if context['plan_record']['id'] in [c['plan_record']['id'] for c in contexts]:
                raise ValueError('duplicate_selected_plan')
            per_plan = assumptions.get('per_plan', {}).get(item['record_id'], {})
            if set(per_plan) - {'path_observations', 'fills', 'allocation_basis'}:
                raise ValueError('unsupported_per_plan_assumption')
            local = {**assumptions, **per_plan}
            # A per-plan override may complete timing, never change the common horizon.
            local['horizon'] = assumptions['horizon']
            projections.append(project_plan(context, snapshot, scenario_id=item['scenario_id'], assumptions=local))
            contexts.append(context)
            if len({(c['analysis_key'], c['plan_id']) for c in contexts}) != len(contexts):
                raise ValueError('select_one_version_per_plan')
            revisions[item['analysis_key']] = self.rpc(origin)('collaboration_read', {'analysis_key': item['analysis_key']})['revision']
        scenario = assemble(projections, assumptions)
        extras = assumptions.get('unplanned_prices', {})
        pids = {p['id'] for p in snapshot['positions']}
        if set(extras) - pids or set(extras) & set(scenario['prices']):
            raise ValueError('unplanned_price_override_conflicts')
        for pid, price in extras.items():
            if price is not None and price != '':
                scenario['prices'][pid] = {'price': price, 'horizon': assumptions['horizon'], 'source_ref': 'explicit_calculation_assumption'}
        result = compare(snapshot, scenario)
        result['integration_version'] = 'risk-collaboration-v1.1'
        result['code_sha256'] = CODE_HASH
        result['scope'] = {'account_alias': args['account_alias'], 'positions': [p['id'] for p in snapshot['positions']]}
        result['limitations'] = ['hypothetical_not_execution', 'saved_balances_only',
                                 'qualitative_conditions_require_review', 'missing_values_not_zero']
        result['limitations'] += list(dict.fromkeys(x for p in projections for x in p['limitations']))
        frozen = {**assumptions, 'account_snapshot': snapshot, 'scenario': scenario,
                  'external_inputs': [external], 'account_alias': args['account_alias'],
                  'selected_inputs': deepcopy(selections)}
        ticket = secrets.token_urlsafe(32)
        with self.lock:
            self.tickets = {k: v for k, v in self.tickets.items() if v['expires'] > time.monotonic()}
            if len(self.tickets) >= 100:
                self.tickets.pop(next(iter(self.tickets)))
            self.tickets[ticket] = {'contexts': contexts, 'revisions': revisions, 'result': result,
                'assumptions': frozen, 'session': session, 'expires': time.monotonic()+900}
        return {'result': result, 'projections': projections, 'ticket': ticket,
                'save_targets': list(revisions), 'input_hash': fingerprint(frozen), 'expires_in_seconds': 900}

    def save(self, args, origin, session):
        if set(args) != {'ticket', 'analysis_key', 'title', 'body'}:
            raise ValueError('invalid_risk_save_request')
        with self.lock:
            value = self.tickets.get(args['ticket'])
            if not value or value['expires'] <= time.monotonic() or value['session'] != session:
                raise Conflict('calculation_ticket_expired_or_used')
            # Consume once before writing: uncertain delivery requires a reread, never retry.
            del self.tickets[args['ticket']]
        for context in value['contexts']:
            read_plan(self.rpc(origin), context['analysis_key'], context['plan_record']['id'], mode=context['mode'])
            current = self.rpc(origin)('collaboration_read', {'analysis_key': context['analysis_key']})
            if current['revision'] != value['revisions'][context['analysis_key']]:
                raise Conflict('collaboration_changed_recalculate')
        _, external = self.account(value['assumptions']['account_alias'])
        if external != value['assumptions']['external_inputs'][0]:
            raise Conflict('saved_account_changed_recalculate')
        contexts = [c for c in value['contexts'] if c['analysis_key'] == args['analysis_key']]
        if not contexts:
            raise ValueError('review_target_not_selected')
        frozen = {**value['assumptions'], 'expected_revision': value['revisions'][args['analysis_key']]}
        return save_review(self.rpc(origin), contexts[0], value['result'], frozen,
                           title=args['title'], body=args['body'], related_contexts=contexts[1:])

    def dispatch(self, op, args, origin, *, principal, session=None):
        if principal not in ('browser', 'risk'):
            raise Forbidden('risk_or_browser_required')
        if op == 'risk_overview':
            if set(args) - {'account_alias'}: raise ValueError('invalid_risk_overview_request')
            return self.overview(origin, args.get('account_alias'))
        if not session: raise Forbidden('risk_calculation_session_required')
        if op == 'risk_calculate': return self.calculate(args, origin, session)
        if op == 'risk_save': return self.save(args, origin, session)
        raise ValueError('unknown_risk_operation')
