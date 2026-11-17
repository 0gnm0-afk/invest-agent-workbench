"""Compatibility adapter for M08's read-only, allowlisted session balances."""
from accounts import saved
from accounts.saved import ReadOnlyAccountStore  # Existing import path remains valid.


def saved_accounts(root):
    """Expose normalized observations only; M08 owns reading and projection."""
    return saved.session_projection(saved.saved_accounts(root))


def project_response(op, result):
    """Project known account slots at export, preserving stored inputs and hashes.

    Snapshot/component/record hashes still identify the stored original, not the
    projected response. User-authored plan aliases and prose are not account data.
    """
    projected = dict(result)
    changed = False
    if op == 'read' and 'accounts' in result:
        projected['accounts'] = saved.session_projection(result['accounts'])
        changed = True
    if op == 'plan_input' and 'accounts' in result.get('snapshot', {}):
        projected['snapshot'] = {**result['snapshot'],
                                 'accounts': saved.session_projection(result['snapshot']['accounts'])}
        changed = True
    field = {'holding_links': 'links', 'detail': 'items', 'collaboration_read': 'legacy_records'}.get(op)
    if field and field in result:
        records = []
        for record in result[field]:
            if record.get('kind') == 'holding_link':
                account = saved.session_projection({'accounts': [{
                    'alias': record.get('account_alias'), 'positions': [record.get('observed_position')]}]})['accounts'][0]
                record = {**record, 'account_alias': account['alias'],
                          'observed_position': next(iter(account['positions']), None),
                          'account_projection': 'session_allowlist', 'hashes_refer_to': 'stored_original'}
                changed = True
            records.append(record)
        projected[field] = records
    if changed:
        projected.update(account_projection='session_allowlist', hashes_refer_to='stored_original')
    return projected
