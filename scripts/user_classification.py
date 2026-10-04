"""Frozen, user-approved Other assignments, separate from provider taxonomy."""
from copy import deepcopy


def apply(groups, snapshot):
    policy = snapshot.get('user_classification')
    if not policy:
        return groups
    if policy.get('schema') != 1 or not policy.get('approval') or not policy.get('approved_on'):
        raise ValueError('invalid_user_classification_policy')
    records = {}
    for row in policy['records']:
        key = tuple(row.get(k) for k in ('market','symbol','isin_code','exchange'))
        if key in records or not all(key) or key[0] != 'US' or key[3] not in ('NASDAQ','NYSE'):
            raise ValueError('invalid_user_classification_identity')
        records[key] = row
    result = []
    for row in groups:
        key = tuple(row.get(k) for k in ('market','symbol','isin_code','exchange'))
        decision = records.get(key)
        if not decision:
            result.append(row)
            continue
        result.append({**row, 'group_state':'matched', 'group_code':'USER_OTHER_US',
            'group_name':'기타', 'group_taxonomy':'US_USER_OTHER', 'reason':None,
            'mapping_version':'user_other_20261004_v1',
            'user_classification_previous':deepcopy(row),
            'user_classification_reason':decision['previous_reason'],
            'evidence':[{'source':'user_decision','approved_on':policy['approved_on'],
                         'approval':policy['approval'],'source_run':policy.get('source_run')}],
            'classification_freshness':{'state':'user_defined','approved_on':policy['approved_on']}})
    return result
