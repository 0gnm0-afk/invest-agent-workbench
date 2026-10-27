"""Offline audit of every saved stock and its exact source candle pages."""

if not __package__:
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import argparse
from collections import Counter
from datetime import datetime, timezone, timedelta
import gzip
import hashlib
import json
from pathlib import Path

import exchange_calendars as xcals
from invest_agent.trading.market_provider import KR_CLOSURES
from market.history.collect import read_checked, packed, parse_page, write
from market.providers.toss_universe import listing_requests, validate_listing_rows


def verify(root, require_finished=False):
    root=Path(root)
    config=read_checked(root/'collection.json')
    market=config.get('markets',['US'])[0]
    assert market in ('KR','US'), 'unsupported_market'
    currency='KRW' if market=='KR' else 'USD'
    offset='+09:00' if market=='KR' else '-04:00'
    calendar_name='XKRX' if market=='KR' else 'XNYS'
    universe=read_checked(root/'universe.json')
    targets={r['symbol']:r for r in universe['rows']}
    assert len(targets)==len(universe['rows']), 'duplicate_universe_symbol'
    schema=config.get('schema',1)
    assert schema in (1,2), 'unsupported_collection_schema'
    include_adr=market=='US' and schema==2
    expected_types=['STOCK','DEPOSITARY_RECEIPT'] if include_adr else ['STOCK']
    assert universe.get('security_types',['STOCK'])==expected_types, 'listing_scope_mismatch'
    if include_adr:
        assert config.get('security_types')==expected_types and universe.get('exchanges'), 'collection_scope_mismatch'
    packets={}
    first_dates=[]
    for f in (root/'series').glob('*.json'):
        d=read_checked(f)
        assert d['symbol'] not in packets, 'duplicate_saved_symbol'
        assert f.stem==hashlib.sha256(d['symbol'].encode()).hexdigest(), 'filename_identity_mismatch'
        assert d['listing']==targets[d['symbol']], 'listing_identity_mismatch'
        assert d['requested_end']==config['end'], 'cutoff_mismatch'
        packets[d['symbol']]=f
        if d.get('first'):first_dates.append(d['first'])
    first=min(first_dates,default=config['end'])
    calendar_start=min(first,(datetime.fromisoformat(config['end'])-timedelta(days=1)).date().isoformat())
    calendar=xcals.get_calendar(calendar_name,start=calendar_start,end=config['end'])
    sessions={s.date().isoformat() for s in calendar.sessions}
    extra_closures=sorted(KR_CLOSURES) if market=='KR' else []
    sessions.difference_update(extra_closures)
    counts=Counter();rows=[];earliest=Counter();total=0;raw_count=0
    manifest={}
    if universe.get('exchanges'):
        expected={}
        expected_exchanges=['KOSPI','KOSDAQ'] if market=='KR' else ['NASDAQ','NYSE']
        assert sorted(universe['exchanges'])==sorted(expected_exchanges), 'listing_exchanges_mismatch'
        for exchange,meta in universe['exchanges'].items():
            metadata = meta['requests'] if include_adr else {'STOCK':meta}
            assert set(metadata)==set(expected_types), 'listing_request_types_mismatch'
            if include_adr:
                assert meta['count']==sum(m['count'] for m in metadata.values()), 'listing_exchange_count_mismatch'
            for params in listing_requests(exchange, include_adr=include_adr):
                request_meta=metadata[params['securityType']]
                key=request_meta['response'];p=root/'responses'/(key+'.json.gz')
                raw=p.read_bytes();packet=json.loads(gzip.decompress(raw))
                request=['/api/v1/stocks/all',params]
                assert packet['request']==request and hashlib.sha256(packed(request)).hexdigest()==key, 'listing_request_mismatch'
                assert hashlib.sha256(packed(packet['payload'])).hexdigest()==packet['sha256'], 'listing_payload_mismatch'
                listings=validate_listing_rows(packet['payload'].get('result'),params)
                assert len(listings)==request_meta['count'], 'listing_count_mismatch'
                for item in listings:
                    symbol=item['symbol'];row={**item,'market':market,'listing_exchange':exchange}
                    if market=='KR':
                        row.update(symbol=symbol+('.KS' if exchange=='KOSPI' else '.KQ'),provider_symbol=symbol)
                    assert row['symbol'] not in expected, 'duplicate_listing'
                    expected[row['symbol']]=row
                manifest[str(p.relative_to(root))]=hashlib.sha256(raw).hexdigest()
        assert targets==expected and len(universe['rows'])==len(expected), 'listing_union_mismatch'
    for symbol,saved_path in sorted(packets.items()):
        d=read_checked(saved_path)
        assert d['market']==market and d['currency']==currency, 'market_currency_mismatch'
        merged={};last_cursor=None
        for n,key in enumerate(d['responses']):
            p=root/'responses'/(key+'.json.gz')
            raw=p.read_bytes();packet=json.loads(gzip.decompress(raw))
            path,params=packet['request']
            assert hashlib.sha256(packed(packet['request'])).hexdigest()==key, 'request_hash_mismatch'
            assert hashlib.sha256(packed(packet['payload'])).hexdigest()==packet['sha256'], 'payload_hash_mismatch'
            assert path=='/api/v1/candles' and params['symbol']==d['listing'].get('provider_symbol',symbol) and params['interval']=='1d' and params['adjusted']=='true', 'unexpected_request'
            assert params['before']==(config['end']+'T23:59:59'+offset if n==0 else last_cursor), 'broken_page_chain'
            manifest[str(p.relative_to(root))]=hashlib.sha256(raw).hexdigest();raw_count+=1
            try:
                page,last_cursor=parse_page(packet['payload'],config['end'],currency)
                bad_cursor = bool(last_cursor) and datetime.fromisoformat(last_cursor)>=datetime.fromisoformat(params['before'])
                for b in page:
                    assert b['date'] not in merged or merged[b['date']]==b, 'conflicting_overlap'
                    merged[b['date']]=b
                if bad_cursor:
                    assert d['state']=='failed' and n==len(d['responses'])-1 and 'cursor_not_descending' in d.get('error',''), 'unreported_cursor_failure'
                    break
            except (ValueError,KeyError,TypeError):
                assert d['state']=='failed' and n==len(d['responses'])-1, 'unreported_parse_failure'
                break
        assert d['bars']==[merged[x] for x in sorted(merged)], 'source_series_mismatch'
        if d['state'] in ('received','empty'):
            assert d.get('provider_end_reached') and d['responses'] and last_cursor is None, 'history_not_exhausted'
        dates=[b['date'] for b in d['bars']]
        assert dates==sorted(set(dates)), 'invalid_saved_dates'
        assert d['first']==(dates[0] if dates else None) and d['last']==(dates[-1] if dates else None), 'saved_date_bounds_mismatch'
        assert d['state'] in ('received','empty','failed'), 'invalid_collection_state'
        assert d['state']!='received' or dates, 'received_without_bars'
        assert d['state']!='empty' or not dates, 'empty_with_bars'
        invalid=[b['date'] for b in d['bars'] if not 0<b['low']<=min(b['open'],b['close'])<=max(b['open'],b['close'])<=b['high']]
        missing=sorted(x for x in sessions if dates and dates[0]<=x<=dates[-1] and x not in merged)
        non_sessions=sorted(set(dates)-sessions)
        stale=bool(dates) and dates[-1]!=config['end']
        record=dict(symbol=symbol,state=d['state'],first=d['first'],last=d['last'],bars=len(dates),pages=len(d['responses']),provider_end_reached=d.get('provider_end_reached',False),invalid_ohlc=invalid,missing_sessions=missing,non_sessions=non_sessions,latest_session_missing=stale,error=d.get('error'))
        rows.append(record);counts[d['state']]+=1;total+=len(dates)
        if d['first']:earliest[d['first']]+=1
        series_path=root/'series'/(hashlib.sha256(symbol.encode()).hexdigest()+'.json')
        manifest[str(series_path.relative_to(root))]=hashlib.sha256(series_path.read_bytes()).hexdigest()
    not_attempted=sorted(set(targets)-set(packets))
    if require_finished:assert not not_attempted, 'unattempted_symbols'
    summary=dict(verified_at=datetime.now(timezone.utc).isoformat(),scope=config['scope'],requested=len(targets),verified=len(packets),counts=dict(counts),not_attempted=not_attempted,total_bars=total,source_pages=raw_count,earliest=min(earliest,default=None),latest=config['end'],start_distribution=dict(sorted(earliest.items())),with_invalid_ohlc=sum(bool(r['invalid_ohlc']) for r in rows),with_internal_gaps=sum(bool(r['missing_sessions']) for r in rows),with_non_sessions=sum(bool(r['non_sessions']) for r in rows),without_latest_session=sum(r['latest_session_missing'] for r in rows),before_2016=sum(bool(r['first']) and r['first']<'2016-01-01' for r in rows),calendar='exchange_calendars '+calendar_name+' '+xcals.__version__,extra_closures=extra_closures,integrity='PASS',all_attempted=not not_attempted)
    write(root/'coverage.json',rows)
    write(root/'verification.json',summary)
    for name in ('collection.json','universe.json','coverage.json','verification.json'):
        manifest[name]=hashlib.sha256((root/name).read_bytes()).hexdigest()
    write(root/'manifest.json',manifest)
    print(json.dumps({k:v for k,v in summary.items() if k not in ('not_attempted','start_distribution')},ensure_ascii=False))
    return summary


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',required=True);p.add_argument('--require-finished',action='store_true');a=p.parse_args();verify(a.output,a.require_finished)
