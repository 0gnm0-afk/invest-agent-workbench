"""Explicit failed-symbol retries. No account reads, no implicit scope exclusions."""
import atomic_io as artifact_io
from copy import deepcopy
from pathlib import Path
import shutil
import socket
from unittest.mock import patch

from invest_agent.trading.market import check_bars, usable_tail
from invest_agent.trading.store import instance_lock
from market.collect import audit
from atomic_io import write_json
from market.quality import complete_cap
from market.providers.toss_candles import daily_series
from market.providers.toss_client import TossMarketClient
from market.providers.toss_universe import _number
from market.toss_collection import failure_code
from market.prices import digest
import market.exclusions as review


def key(row):
    return row['market'],row['symbol']


def toss_retry(snapshot, issues, get):
    """Replace only validated failed fields; leave every other security untouched."""
    result=deepcopy(snapshot)
    listings={key(r):r for r in result['universe']}
    series={key(r):r for r in result['series']}
    errors={key(r):r for r in result['errors']}
    reports={key(r):r for r in result.get('nh_fallback',{}).get('results',[])}
    outcomes=[]
    for issue in issues:
        k=key(issue);row=listings[k];sessions=result['sessions'][row['market']]
        reports.setdefault(k, {'market':k[0],'symbol':k[1]})
        code=row.get('provider_symbol') or row['symbol'];at=review.now()
        outcome={'market':k[0],'symbol':k[1],'provider':'toss','state':'unavailable'}
        try:
            payload=get('/api/v1/stocks',{'symbols':code}).get('result')
            matches=[r for r in payload if isinstance(r,dict) and r.get('symbol')==code] if isinstance(payload,list) else []
            if len(matches)!=1: raise ValueError('toss_retry_identity_unverified')
            detail=matches[0]
            if (not row.get('isin_code') or detail.get('isinCode')!=row['isin_code']
                    or detail.get('market')!=row['listing_exchange_family']
                    or detail.get('currency')!=('KRW' if row['market']=='KR' else 'USD')):
                raise ValueError('toss_retry_identity_mismatch')
            if issue['field']=='가격 이력':
                value=daily_series(get,row['symbol'],row['market'],sessions,benchmark=row.get('benchmark'),provider_symbol=code)
                check_bars(value['bars'],sessions)
                usable_tail(value['bars'],sessions)
                series[k]={**row,**value,'collected_at':at}
                errors.pop(k,None)
            else:
                shares=_number(detail.get('sharesOutstanding'))
                fresh={**row,'shares_outstanding':int(shares) if shares is not None and shares>0 and shares==shares.to_integral_value() else None,
                       'shares_outstanding_observed_at':at,'quote_time_state':'not_provided',
                       'tradable':detail.get('status')=='ACTIVE' and (detail.get('koreanMarketDetail') or {}).get('krxTradingSuspended') is not True}
                fields=complete_cap(get,fresh,sessions)
                if not fields.get('market_cap') or str(fields.get('market_cap_quality','')).startswith('unavailable'):
                    raise ValueError('toss_retry_market_cap_unavailable')
                fields={**fields,'market_cap_error':None,'shares_outstanding':fresh['shares_outstanding'],
                        'shares_outstanding_observed_at':at}
                listings[k]={**row,**fields};series[k]={**series[k],**fields}
            outcome['state']='adopted'
        except Exception as exc:
            outcome['reason']=str(exc) if str(exc) in ('toss_retry_identity_mismatch','toss_retry_identity_unverified','toss_retry_market_cap_unavailable') else failure_code(exc)
        reports[k]={**reports[k], 'toss_retry':outcome}
        outcomes.append(outcome)
    result['universe']=[listings[key(r)] for r in result['universe']]
    result['series']=list(series.values());result['errors']=list(errors.values())
    # A repaired price series may expose a still-missing cap; keep it actionable.
    from market.nh_fallback import fallback_targets
    remaining={key(t):t for t in fallback_targets(result)}
    for issue in issues:
        k=key(issue)
        if k in remaining and reports[k].get('toss_error_code')!=remaining[k]['error_code']:
            reports[k].update(toss_error_code=remaining[k]['error_code'],state='unavailable',reason='manual_retry_pending',nh_requested=False)
    result.setdefault('nh_fallback',{})['results']=list(reports.values())
    result['collected_until']=review.now()
    result['collection_stats'].update(requested=len(result['universe']),received=len(result['series']),failed=len(result['errors']))
    from market.scope import refresh_coverage
    refresh_coverage(result)
    return result,outcomes


def seal(source, snapshot):
    write_json(source/'snapshot.json',snapshot)
    write_json(source/'summary.json',audit(snapshot))
    write_json(source/'manifest.json',{str(p.relative_to(source)):artifact_io.sha(p) for p in source.rglob('*') if p.is_file() and p!=source/'manifest.json'})


def apply_retry(root, registry_path, job_id):
    import market.daily as daily_market, market.repair as repair_market_scope, market.nh_fallback as nh_fallback
    root=Path(root)
    with instance_lock(root/'runtime/dashboard-daily'),instance_lock(root/'runtime'):
        value,_=review.registry(registry_path);job=value['jobs'][-1]
        if job['id']!=job_id: raise review.ReviewConflict('exclusion_job_changed')
        if job.get('provider') != 'toss': raise ValueError('single_provider_toss')
        if job.get('result_run'): return
        current,_,_=review.latest(root)
        if not review.same_run(current,job['source_run'],root):
            raise review.ReviewConflict('retry_source_changed')
        config=review.load(root/'config.json')
        review.update_job(registry_path,job_id,state='applying',error=None)
        if job.get('result_source'):
            source=review.local_runtime_path(job['result_source'],root=root)
            daily_market.source_check(source)
        else:
            # If a worker died during collection, do not silently repeat requests.
            if job.get('collection_started_at'):
                raise ValueError('retry_collection_interrupted')
            source=Path(repair_market_scope.derive(current,config)['source'])
            snapshot=review.load(source/'snapshot.json')
            known={key(r):r for r in snapshot['universe']}
            for issue in job['issues']:
                row=known.get(key(issue))
                if not row or row.get('isin_code')!=issue.get('isin_code') or row.get('listing_exchange_family')!=issue.get('exchange'):
                    raise ValueError('retry_identity_changed')
            review.update_job(registry_path,job_id,collection_started_at=review.now())
            if job['provider']=='toss':
                counter=0
                def observer(row):
                    nonlocal counter
                    receipt={k:v for k,v in row.items() if k != 'payload'}
                    if 'payload' in row: receipt['payload_sha256']=digest(row['payload'])
                    counter+=1;write_json(source/'retry-evidence'/f'{counter:06}.json',receipt)
                get=TossMarketClient(observer=observer)
                snapshot,outcomes=toss_retry(snapshot,job['issues'],get)
                write_json(source/'retry-results.json',outcomes)
                seal(source,snapshot)
            else:
                recovered,_,_=nh_fallback.recover(source,source.parent/(source.name+'-nh'),config,
                    prior_source=source,only_keys={key(r) for r in job['issues']},allow_auto_exclusions=False)
                if (source/'basis').exists(): shutil.copytree(source/'basis',recovered/'basis')
                source=recovered
                seal(source,review.load(source/'snapshot.json'))
            review.update_job(registry_path,job_id,result_source=str(source))
        before=review.load(root/'runtime/latest.json')
        try:
            # Replay only: never turn a targeted retry into another whole-market collection.
            with patch.object(socket.socket,'connect',side_effect=AssertionError('retry_replay_network_forbidden')):
                result=daily_market.run(config,source=source)
            if result.get('error') or result.get('stages',{}).get('report')!='completed':
                raise ValueError('retry_calculation_failed')
        except BaseException:
            review.save(root/'runtime/latest.json',before)
            raise
        review.update_job(registry_path,job_id,state='computed',result_run=result['output'],calculated_at=review.now())
