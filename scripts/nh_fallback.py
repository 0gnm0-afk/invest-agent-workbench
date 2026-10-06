"""Qualified NH whole-series fallback for unavailable or unusable Toss observations."""
import hashlib
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

from invest_agent.trading.market import check_bars
from invest_agent.trading.universe_gate import evaluate as gate

from market_collect import audit, write_json
from market_quality import adr_cap_unavailable, is_depositary_receipt
from nh_market_auth import NHMarketError
from nh_market_client import MarketClient, PERIOD, KR_PERIOD, CURRENT, us_period_inputs, kr_period_inputs


# Provider/data failures are recoverable; calendar/configuration/programming errors are not.
RECOVERABLE_ERRORS = frozenset((
    'market_transport_or_payload_unavailable', 'stale_or_invalid_last_session',
    'no_completed_session_bars', 'historical_price_gaps', 'duplicate_or_unordered_dates',
    'Invalid candle envelope', 'Unexpected candle currency', 'Invalid candle value',
    'Conflicting overlapping candle pages', 'Candle cursor does not progress',
    'Candle cursor does not move backwards', 'Candle page budget exhausted before history boundary',
))


def fallback_targets(snapshot):
    targets = {(r['market'],r['symbol']):r for r in snapshot['errors'] if r['market'] in ('KR', 'US') and (
        r.get('error_code') in RECOVERABLE_ERRORS or str(r.get('error_code', '')).startswith('market_http_'))}
    for row in snapshot.get('series', []):
        code = None
        bars = row.get('bars', [])
        dates = {b['date'] for b in bars}
        sessions = snapshot.get('sessions', {}).get(row['market'], [])
        if bars and any(bars[0]['date'] <= d <= bars[-1]['date'] and d not in dates for d in sessions):
            code = 'historical_price_gaps'
        elif cap_missing(row) and not (is_depositary_receipt(row)
                and row.get('market_cap_quality') == 'unavailable_adr_share_basis'):
            code = 'unavailable_market_cap'
        if code:
            targets.setdefault((row['market'],row['symbol']),{'market':row['market'],'symbol':row['symbol'],'error_code':code})
    return list(targets.values())


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream,'sha256').hexdigest()


def load_checked(path, expected):
    if sha(path) != expected:
        raise ValueError('nh_reference_hash_mismatch')
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def _reference_rows(config, prior_source, inputs):
    fixed = Path(config['nh_reference_snapshot'])
    frozen = load_checked(fixed,config['nh_reference_snapshot_sha256'])
    inputs[str(fixed)] = config['nh_reference_snapshot_sha256']
    rows = {(r['market'],r['symbol']):r for r in frozen['series']
            if r.get('provider') == 'Toss OpenAPI candles'}
    if prior_source:
        path = Path(prior_source)/'snapshot.json'
        manifest = Path(prior_source)/'manifest.json'
        if path.is_file() and manifest.is_file():
            expected = json.loads(manifest.read_text(encoding='utf-8')).get('snapshot.json')
            if expected:
                previous = load_checked(path,expected)
                inputs[str(path)] = expected
                inputs[str(manifest)] = sha(manifest)
                for r in previous['series']:
                    if r.get('provider') != 'Toss OpenAPI candles':
                        continue
                    key = r['market'],r['symbol']
                    old = rows.get(key)
                    if old is None or r['bars'][-1]['date'] > old['bars'][-1]['date']:
                        rows[key] = r
    return rows


def _nh_bars(client, symbol, sessions, market='US'):
    end = sessions[-1].replace('-','')
    collected, files = {}, []
    path = PERIOD if market == 'US' else KR_PERIOD
    for _ in range(4):
        inputs = us_period_inputs(symbol,end) if market == 'US' else kr_period_inputs(symbol,end)
        response, saved = client.read(path,inputs)
        files.append(saved)
        payload = response['payload']
        if payload.get('rsp_cd') != '00000' or not isinstance(payload.get('Output_1'),list):
            raise ValueError('nh_period_business_response_unavailable')
        rows = payload['Output_1']
        dates = []
        for item in rows:
            day = str(item.get('trade_date' if market == 'US' else 'bsop_date',''))
            if len(day)!=8 or not day.isdigit():
                raise ValueError('nh_period_invalid_date')
            dates.append(day)
            if day in collected and collected[day] != item:
                raise ValueError('nh_period_conflicting_overlapping_date')
            collected[day] = item
        if not dates:
            if collected:
                break
            raise ValueError('nh_period_empty')
        if len(dates) != len(set(dates)):
            raise ValueError('nh_period_duplicate_dates')
        if len(files) == 1 and max(dates) < sessions[-1].replace('-',''):
            raise ValueError('nh_period_completed_session_missing')
        if min(dates) <= sessions[0].replace('-',''):
            break
        next_end = (datetime.strptime(min(dates),'%Y%m%d')-timedelta(days=1)).strftime('%Y%m%d')
        if next_end >= end:
            raise ValueError('nh_period_cursor_not_descending')
        end = next_end
    else:
        raise ValueError('nh_period_page_limit')
    if sessions[-1].replace('-','') not in collected:
        raise ValueError('nh_period_completed_session_missing')
    if any(s.replace('-','') not in collected for s in sessions):
        raise ValueError('nh_period_history_incomplete')
    bars = []
    for day in sessions:
        row = collected[day.replace('-','')]
        def number(key):
            value = row.get(key)
            if isinstance(value,bool) or value is None:
                raise ValueError('nh_period_invalid_'+key)
            value = float(value)
            if not math.isfinite(value) or value < 0:
                raise ValueError('nh_period_invalid_'+key)
            return value
        volume = number('movolume' if market == 'US' else 'vol')
        if volume != int(volume):
            raise ValueError('nh_period_fractional_volume')
        if market == 'US':
            bars.append({'date':day,'open':number('open_prc'),'high':number('high'),
                         'low':number('low'),'close':number('close_prc'),'volume':volume*1000})
        else:
            bars.append({'date':day,'open':number('stck_oprc'),'high':number('stck_hgpr'),
                         'low':number('stck_lwpr'),'close':number('stck_prpr'),'volume':volume})
    check_bars(bars,sessions)
    if market == 'US' and any(b['volume']==0 for b in bars[-20:]):
        raise ValueError('nh_volume_lower_bound_zero_recent')
    return bars, files


def _compare(bars, prior, sessions, market='US'):
    if prior is None or prior.get('provider') != 'Toss OpenAPI candles':
        raise ValueError('verified_prior_toss_series_required')
    prior_bars = {b['date']:b for b in prior['bars']}
    overlap = [b for b in bars if b['date'] in prior_bars]
    if len(overlap) < 126 or prior['bars'][-1]['date'] not in sessions[-21:]:
        raise ValueError('recent_toss_overlap_insufficient')
    mismatch = []
    for bar in overlap:
        old = prior_bars[bar['date']]
        if any(abs(bar[k]-old[k]) > 1e-6 for k in ('open','high','low','close')):
            mismatch.append(bar['date'])
        gap = old['volume']-bar['volume']
        valid_volume = 0 <= gap < 1000 if market == 'US' else gap == 0
        if not valid_volume:
            raise ValueError('nh_volume_relation_unverified')
    if mismatch:
        raise ValueError('nh_toss_historical_ohlc_mismatch')
    return {'overlap_sessions':len(overlap),'latest_prior_toss':prior['bars'][-1]['date'],
            'ohlc_exact':True,'volume_relation':(
                'toss_volume_in_[nh_x1000,nh_x1000_plus_999]' if market == 'US'
                else 'nh_volume_exactly_matches_prior_toss')}


def cap_missing(row):
    value = row.get('market_cap')
    return (str(row.get('market_cap_quality','')).startswith('unavailable')
            or 'market_cap' in row and (type(value) not in (int,float)
                or not math.isfinite(value) or value <= 0))


def completed_cap(listing, bar, payload, exchange):
    """Dated regular-session NH quote anchors raw price × observed shares."""
    unavailable = adr_cap_unavailable(listing)
    if unavailable:
        return unavailable
    q = payload.get('Output_0') or {}
    shares = listing.get('shares_outstanding')
    if type(shares) is not int or shares <= 0:
        observed = q.get('list_num')
        shares = int(observed) if type(observed) in (int,float) and math.isfinite(observed) and observed > 0 and observed == int(observed) else None
    if (payload.get('rsp_cd') != '00000' or q.get('iem_cd') != listing['symbol']
            or q.get('exch_id') != exchange or q.get('currency_unit') != 'USD'
            or str(q.get('trade_date')) != bar['date'].replace('-','')
            or type(shares) is not int or shares <= 0
            or type(q.get('normal_trdprc')) not in (int,float)
            or not math.isfinite(q['normal_trdprc']) or q['normal_trdprc'] <= 0
            or abs(q['normal_trdprc']-bar['close']) > 1e-6):
        raise ValueError('nh_dated_market_cap_unavailable')
    cap = q['normal_trdprc']*shares
    if not math.isfinite(cap):
        raise ValueError('nh_dated_market_cap_unavailable')
    return {'market_cap':cap,'market_cap_as_of':bar['date'],'market_cap_currency':'USD',
            'market_cap_quality':'completed_close_estimate','market_cap_error':None,
            'market_cap_basis':'nh_dated_regular_close_times_observed_shares_estimate',
            'market_cap_shares':shares,
            'market_cap_shares_source':'toss' if shares == listing.get('shares_outstanding') else 'nh_current_list_num',
            'market_cap_price':q['normal_trdprc'],'market_cap_price_date':bar['date'],
            'market_cap_price_source':'https://api.nhplug.com:8443'+CURRENT,
            'market_cap_price_basis':'nh_dated_regular_session_close'}


def recover(source, output, config, *, evidence_root=None, prior_source=None):
    source, output = Path(source).resolve(), Path(output).resolve()
    if output.exists():
        raise ValueError('nh_fallback_output_already_exists')
    output.mkdir(parents=True)
    original = json.loads((source/'snapshot.json').read_text(encoding='utf-8'))
    import market_scope
    policy = market_scope.load()
    write_json(output/'input-scope-policy.json',policy)
    original = market_scope.apply_snapshot(original, policy)
    # Older saved inputs could claim quoted_price while carrying a null cap.
    for row in original.get('universe', []) + original.get('series', []):
        if cap_missing(row) and not str(row.get('market_cap_quality','')).startswith('unavailable'):
            row['market_cap_quality'] = 'unavailable_market_cap'
    targets = fallback_targets(original)
    inputs = {}
    us_path, kr_path = Path(config['nh_us_master']), Path(config['nh_kr_master'])
    us_master = load_checked(us_path,config['nh_us_master_sha256'])
    kr_master = load_checked(kr_path,config['nh_kr_master_sha256'])
    inputs.update({str(us_path):config['nh_us_master_sha256'],
                   str(kr_path):config['nh_kr_master_sha256']})
    references = _reference_rows(config,prior_source or source,inputs)
    matches = {}
    for row in us_master:
        if row.get('sNationCode') == 'USA':
            matches.setdefault((row.get('sStockCode'),row.get('sSymbol')),[]).append(row)
    domestic = {}
    for row in kr_master:
        domestic.setdefault((row.get('sMarket'),row.get('sCode')),[]).append(row)
    universe = {(r['market'],r['symbol']):r for r in original['universe']}
    client = MarketClient(output,evidence_root)
    results, recovered = [], []
    metadata_recovered = {}
    new_exclusions = []
    original_series = {(r['market'],r['symbol']):r for r in original.get('series',[])}
    for error in targets:
        symbol = error['symbol']
        listing = universe[(error['market'],symbol)]
        market = error['market']
        report = {'market':market,'symbol':symbol,'toss_error_code':error['error_code']}
        identity_verified = False
        identity_conflict = False
        try:
            sessions = original['sessions'][market]
            provider_symbol = symbol if market == 'US' else listing['provider_symbol']
            if market == 'US':
                exchange = {'NASDAQ':'NQQ','NYSE':'NYY'}[listing['listing_exchange_family']]
                rows = matches.get((exchange,symbol),[])
                if not rows and listing.get('isin_code'):
                    rows = [r for r in us_master if r.get('sStockCode') == exchange
                            and r.get('sNationCode') == 'USA' and r.get('sStandardCode') == listing['isin_code']]
                    if len(rows) == 1:
                        provider_symbol = rows[0]['sSymbol']
            if market == 'US':
                identity_verified = len(rows)==1 and bool(listing.get('isin_code')) and rows[0].get('sStandardCode') == listing['isin_code']
            else:
                rows = domestic.get(('1',provider_symbol),[])
                identity_verified = listing.get('listing_exchange_family') == 'KOSPI' and len(rows)==1
            identity_conflict = bool(rows) and not identity_verified
            report['nh_identity_verified'] = identity_verified
            report['nh_requested'] = True
            if error['error_code'] == 'unavailable_market_cap':
                # Preserve valid Toss OHLCV. Only request the missing metadata.
                if market == 'US':
                    quote, quote_file = client.read(CURRENT, {'iem_cd':provider_symbol})
                    inputs[str(quote_file)] = sha(quote_file)
                    if len(rows)!=1 or rows[0].get('sStandardCode') != listing.get('isin_code'):
                        raise ValueError('nh_identity_exchange_isin_mismatch')
                    fields = completed_cap({**listing,'symbol':provider_symbol},original_series[(market,symbol)]['bars'][-1],quote['payload'],exchange)
                    metadata_recovered[(market,symbol)] = fields
                    universe[(market,symbol)] = {**listing, **fields}
                    report.update(state='adopted_metadata', retained_price_provider='Toss', fields=list(fields))
                else:
                    # The allowlisted domestic period endpoint supplies no verified shares/cap contract.
                    _nh_bars(client,provider_symbol,sessions[-1:],market)
                    raise ValueError('nh_market_cap_field_unavailable')
                results.append(report)
                continue
            bars, files = _nh_bars(client,provider_symbol,sessions,market)
            report.update(nh_pages=len(files),nh_bars=len(bars),nh_latest_session=bars[-1]['date'],nh_price_observed=True)
            if market == 'US':
                if len(rows)!=1 or rows[0].get('sStandardCode') != listing.get('isin_code'):
                    raise ValueError('nh_identity_exchange_isin_mismatch')
            else:
                if listing['listing_exchange_family'] != 'KOSPI':
                    raise ValueError('nh_kr_exchange_unverified')
                provider_symbol = listing['provider_symbol']
                rows = domestic.get(('1',provider_symbol),[])
                if len(rows)!=1:
                    raise ValueError('nh_kr_short_code_market_mismatch')
            for file in files:
                inputs[str(file)] = sha(file)
            prior = references.get((market,symbol))
            if prior and any(prior.get(k) != listing.get(k) for k in ('isin_code','listing_exchange_family')):
                raise ValueError('prior_toss_identity_mismatch')
            comparison = _compare(bars,prior,sessions,market)
            currency = 'USD' if market == 'US' else 'KRW'
            lower = gate({**listing,'bars':bars,'currency':currency,'instrument_type':'EQUITY'},bars)
            upper_bars = ([{**b,'volume':b['volume']+999} for b in bars]
                          if market == 'US' else bars)
            upper = gate({**listing,'bars':upper_bars,'currency':currency,'instrument_type':'EQUITY'},upper_bars)
            if (lower['universe_pass'],lower['universe_rejection_reasons']) != (
                    upper['universe_pass'],upper['universe_rejection_reasons']):
                raise ValueError('nh_volume_interval_changes_universe_decision')
            candidate = {**listing,'bars':bars,'currency':currency,'benchmark':listing['benchmark'],
                'instrument_type':'EQUITY','provider_symbol':provider_symbol,
                'provider':'NH PLUG period','source_url':'https://api.nhplug.com:8443'+(
                    PERIOD if market == 'US' else KR_PERIOD),
                'price_basis':'nh_'+market.lower()+'_ohlc_historical_toss_parity',
                'volume_precision':('floor_thousand_lower_bound_with_999_share_uncertainty'
                                    if market == 'US' else 'matched_prior_toss_exactly'),
                'collection':{'source':'NH PLUG market-only period','regular_session':market=='US',
                              'pages':len(files),'first_requested_session':sessions[0],
                              'last_requested_session':sessions[-1],
                              'comparison':comparison}}
            recovered.append(candidate)
            universe[(market,symbol)] = listing
            report.update(state='adopted',comparison=comparison,
                          volume_universe_decision_stable=True)
        except (Exception, SystemExit) as exc:
            report.update(state='observed_not_adopted' if report.get('nh_price_observed') else 'unavailable',
                          reason=str(exc) if isinstance(exc,(ValueError,NHMarketError)) else type(exc).__name__+': '+str(exc))
        if report['state'] != 'adopted' and policy.get('dual_provider',{}).get('enabled'):
            reason = report.get('reason','')
            missing_prices = {'stale_or_invalid_last_session','no_completed_session_bars','historical_price_gaps','market_http_400','market_http_404'}
            absent = {'market_http_400','market_http_404','nh_period_empty','nh_period_completed_session_missing','nh_period_history_incomplete'}
            if reason in absent and (identity_conflict or reason in ('nh_period_history_incomplete','nh_period_completed_session_missing') and not identity_verified):
                report['reason'] = 'nh_identity_exchange_isin_mismatch' if market == 'US' else 'nh_kr_short_code_market_mismatch'
                reason = report['reason']
            # Identity/parity/auth/outage failures are never evidence of data absence.
            # No healthy Toss security is queried just to cross-check a failed security.
            if (error.get('error_code') in missing_prices and reason in absent
                    and report.get('nh_requested') and not report.get('nh_price_observed') and listing.get('isin_code')):
                rule = policy['dual_provider']
                item = {k:listing.get(k) for k in ('market','symbol','name','isin_code','provider_symbol')}
                item.update(reason='dual_provider_data_unavailable',approved_on=rule['approved_on'],
                    approval=rule['approval'],dual_provider_rule=rule['rule_id'],effective_session=sessions[-1],
                    evidence_run=str(output),evidence={'toss_source':str(source),'toss_error':error,
                        'nh_result':dict(report),'completed_session':sessions[-1]})
                new_exclusions.append(item)
                report['scope_decision'] = 'excluded_dual_provider_unavailable'
        results.append(report)
        write_json(output/'progress.json',{'targets':len(targets),'completed':len(results),
                                           'adopted':len(recovered)+len(metadata_recovered),'results':results})
    adopted = {(r['market'],r['symbol']) for r in recovered}
    original['series'] = sorted([r for r in original['series'] if (r['market'],r['symbol']) not in adopted]
                                +recovered,key=lambda r:(r['market'],r['symbol']))
    original['errors'] = [r for r in original['errors'] if (r['market'],r['symbol']) not in adopted]
    for row in original['series']:
        row.update(metadata_recovered.get((row['market'],row['symbol']),{}))
    original['universe'] = [universe[(r['market'],r['symbol'])] for r in original['universe']]
    stats = original['collection_stats']
    stats.update(received=len(original['series']),failed=len(original['errors']),nh_recovered=len(recovered))
    coverage = original['coverage']
    coverage.update(received=len(original['series']),market_wide=bool(coverage['listings']
        and not coverage['scope_errors'] and not original['errors']
        and not any(str(r.get('market_cap_quality','')).startswith('unavailable')
                    for r in original['universe']) and len(original['series'])==len(original['universe'])))
    if 'active_scope_complete' in coverage:
        from market_scope import refresh_coverage
        refresh_coverage(original)
    original['source'] = 'live_public_toss_with_qualified_nh_fallback'
    original['collection_mode'] = 'toss_main_nh_unusable_symbols_v2'
    for market in {r['market'] for r in recovered}:
        original['providers'][market] = 'Toss adjusted candles; qualified NH PLUG period for adopted rows'
    original['nh_fallback'] = {'attempted':len(targets),'adopted':len(recovered)+len(metadata_recovered),
                               'observed_not_adopted':sum(r['state']=='observed_not_adopted' for r in results),
                               'original_source':str(source),'excluded':len(new_exclusions),'results':results}
    if new_exclusions:
        frozen = {**policy,'records':policy['records']+new_exclusions}
        write_json(output/'scope-policy.json',frozen)
        frozen.update(path=str(output/'scope-policy.json'),sha256=sha(output/'scope-policy.json'))
        original = market_scope.apply_snapshot(original,frozen)
    original['collected_until'] = datetime.now(timezone.utc).isoformat()
    summary = {**audit(original),'schema':1,'original_source':str(source),'original_snapshot_sha256':sha(source/'snapshot.json'),
               'nh_evidence_root':str(evidence_root) if evidence_root else str(output),
               'completed_at':original['collected_until'],'attempted':len(targets),
               'adopted':len(recovered)+len(metadata_recovered),'unrecovered':len(targets)-len(recovered)-len(metadata_recovered)-len(new_exclusions),'results':results,'automatic_exclusions':new_exclusions,'metadata_recovered':len(metadata_recovered)}
    for file in Path(evidence_root or output).joinpath('responses').glob('*.json'):
        inputs[str(file.resolve())] = sha(file)
    write_json(output/'fallback-inputs.json',inputs)
    write_json(output/'snapshot.json',original)
    write_json(output/'summary.json',summary)
    write_json(output/'manifest.json',{str(p.relative_to(output)):sha(p) for p in output.rglob('*') if p.is_file()})
    return output, summary, inputs
