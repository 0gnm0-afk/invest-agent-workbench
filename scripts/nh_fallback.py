"""Qualified NH whole-series fallback for Toss transport failures only."""
import hashlib
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

from invest_agent.trading.market import check_bars
from invest_agent.trading.universe_gate import evaluate as gate

from market_collect import audit, write_json
from nh_market_client import MarketClient, PERIOD, KR_PERIOD, us_period_inputs, kr_period_inputs


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
        if len(dates) != len(set(dates)) or not dates:
            raise ValueError('nh_period_empty_or_duplicate')
        if min(dates) <= sessions[0].replace('-',''):
            break
        next_end = (datetime.strptime(min(dates),'%Y%m%d')-timedelta(days=1)).strftime('%Y%m%d')
        if next_end >= end:
            raise ValueError('nh_period_cursor_not_descending')
        end = next_end
    else:
        raise ValueError('nh_period_page_limit')
    if any(s.replace('-','') not in collected for s in sessions):
        raise ValueError('nh_period_completed_session_missing')
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


def recover(source, output, config, *, evidence_root=None, prior_source=None):
    source, output = Path(source).resolve(), Path(output).resolve()
    if output.exists():
        raise ValueError('nh_fallback_output_already_exists')
    output.mkdir(parents=True)
    original = json.loads((source/'snapshot.json').read_text(encoding='utf-8'))
    import market_scope
    original = market_scope.apply_snapshot(original, market_scope.load())
    targets = [r for r in original['errors']
               if r['market'] in ('KR','US') and r.get('error_code') == 'market_transport_or_payload_unavailable']
    inputs = {}
    us_path, kr_path = Path(config['nh_us_master']), Path(config['nh_kr_master'])
    us_master = load_checked(us_path,config['nh_us_master_sha256'])
    kr_master = load_checked(kr_path,config['nh_kr_master_sha256'])
    inputs.update({str(us_path):config['nh_us_master_sha256'],
                   str(kr_path):config['nh_kr_master_sha256']})
    references = _reference_rows(config,prior_source,inputs)
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
    for error in targets:
        symbol = error['symbol']
        listing = universe[(error['market'],symbol)]
        market = error['market']
        report = {'market':market,'symbol':symbol,'toss_error_code':error['error_code']}
        try:
            if market == 'US':
                exchange = {'NASDAQ':'NQQ','NYSE':'NYY'}[listing['listing_exchange_family']]
                rows = matches.get((exchange,symbol),[])
                if len(rows)!=1 or rows[0].get('sStandardCode') != listing.get('isin_code'):
                    raise ValueError('nh_identity_exchange_isin_mismatch')
                provider_symbol = symbol
            else:
                if listing['listing_exchange_family'] != 'KOSPI':
                    raise ValueError('nh_kr_exchange_unverified')
                provider_symbol = listing['provider_symbol']
                rows = domestic.get(('1',provider_symbol),[])
                if len(rows)!=1:
                    raise ValueError('nh_kr_short_code_market_mismatch')
            sessions = original['sessions'][market]
            bars, files = _nh_bars(client,provider_symbol,sessions,market)
            for file in files:
                inputs[str(file)] = sha(file)
            report['nh_pages'] = len(files)
            report['nh_bars'] = len(bars)
            report['nh_latest_session'] = bars[-1]['date']
            report['nh_price_observed'] = True
            comparison = _compare(bars,references.get((market,symbol)),sessions,market)
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
            report.update(state='adopted',comparison=comparison,
                          volume_universe_decision_stable=True)
        except (Exception, SystemExit) as exc:
            report.update(state='observed_not_adopted' if report.get('nh_price_observed') else 'unavailable',
                          reason=str(exc) if isinstance(exc,ValueError) else type(exc).__name__+': '+str(exc))
        results.append(report)
        write_json(output/'progress.json',{'targets':len(targets),'completed':len(results),
                                           'adopted':len(recovered),'results':results})
    adopted = {(r['market'],r['symbol']) for r in recovered}
    original['series'] = sorted(original['series']+recovered,key=lambda r:(r['market'],r['symbol']))
    original['errors'] = [r for r in original['errors'] if (r['market'],r['symbol']) not in adopted]
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
    original['collection_mode'] = 'toss_main_nh_failed_symbols_only_v1'
    for market in {r['market'] for r in recovered}:
        original['providers'][market] = 'Toss adjusted candles; qualified NH PLUG period for adopted rows'
    original['nh_fallback'] = {'attempted':len(targets),'adopted':len(recovered),
                               'observed_not_adopted':sum(r['state']=='observed_not_adopted' for r in results),
                               'original_source':str(source)}
    original['collected_until'] = datetime.now(timezone.utc).isoformat()
    summary = {**audit(original),'schema':1,'original_source':str(source),'original_snapshot_sha256':sha(source/'snapshot.json'),
               'nh_evidence_root':str(evidence_root) if evidence_root else str(output),
               'completed_at':original['collected_until'],'attempted':len(targets),
               'adopted':len(recovered),'unrecovered':len(targets)-len(recovered),'results':results}
    write_json(output/'snapshot.json',original)
    write_json(output/'summary.json',summary)
    write_json(output/'manifest.json',{p.name:sha(p) for p in output.iterdir() if p.is_file()})
    return output, summary, inputs
