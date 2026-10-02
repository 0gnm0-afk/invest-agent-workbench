"""Apply immutable, directly verified Finviz refreshes to exact saved identities."""
import json
from pathlib import Path
from urllib.parse import urlparse
from finviz_public_classification import parse_alias_quote


def apply(groups, snapshot, checked, inputs, applied, age_issue, allowed_sectors):
    reference = snapshot.get('classification_refresh')
    if not reference:
        return groups
    manifest_path = checked(Path(reference['path']), reference['sha256'], inputs)
    root = manifest_path.parent
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    if manifest.get('schema') != 1:
        raise ValueError('invalid_classification_refresh_schema')
    source = checked(Path(manifest['identity_snapshot']['path']), manifest['identity_snapshot']['sha256'], inputs)
    identities = {(r['market'],r['symbol'],r.get('isin_code'),r['listing_exchange_family']) for r in json.loads(source.read_text(encoding='utf-8'))['universe']}
    updates = {}
    for record in manifest['records']:
        key = tuple(record[k] for k in ('market','symbol','isin_code','exchange'))
        if key in updates or key not in identities or record['market'] != 'US':
            raise ValueError('classification_refresh_identity_unverified')
        paths = [(root/record[k]).resolve() for k in ('document','meta')]
        if any(not p.is_relative_to(root.resolve()) for p in paths):
            raise ValueError('classification_refresh_path_outside_root')
        document, metadata = paths
        meta = json.loads(checked(metadata,record['meta_sha256'],inputs).read_text(encoding='utf-8'))
        if meta['status'] != 200 or any(urlparse(meta[k]).scheme!='https' or urlparse(meta[k]).hostname!='finviz.com' for k in ('url','final_url')):
            raise ValueError('classification_refresh_source_invalid')
        if age_issue(meta['observed_at'],applied):
            raise ValueError('classification_refresh_timestamp_invalid')
        parsed = parse_alias_quote(checked(document,meta['sha256'],inputs).read_bytes(), record['finviz_symbol'],record['symbol'],record['exchange'])
        if parsed['sector'] not in allowed_sectors or parsed['industry'].startswith('Closed-End Fund') or parsed['industry']=='Exchange Traded Fund':
            raise ValueError('classification_refresh_security_type_conflict')
        updates[key] = (parsed,{'path':str(document),'sha256':meta['sha256'],'url':meta['url'],
                              'observed_at':meta['observed_at'],'identity_method':'exact_saved_isin_and_finviz_ticker_exchange_link'})
    result=[]
    for row in groups:
        key = tuple(row.get(k) for k in ('market','symbol','isin_code','exchange'))
        found = updates.get(key)
        if not found or row['group_state']=='matched' or 'security_type_conflict' in (row.get('reason') or ''):
            result.append(row);continue
        parsed, proof = found
        result.append({**row,'group_code':parsed['sector'],'group_name':parsed['sector'],'group_state':'matched',
                       'reason':None,'evidence':[proof],'mapping_version':'direct_finviz_identity_refresh_v1',
                       'classification_refresh_previous_reason':row.get('reason')})
    return result
