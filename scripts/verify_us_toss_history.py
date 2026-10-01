"""Offline audit of every saved stock and its exact source candle pages."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import gzip
import hashlib
import json
from pathlib import Path

import exchange_calendars as xcals
from us_toss_history import read_checked, packed, parse_page, write


def verify(root, require_finished=False):
    root=Path(root)
    config=read_checked(root/'collection.json')
    universe=read_checked(root/'universe.json')
    targets={r['symbol']:r for r in universe['rows']}
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
    calendar=xcals.get_calendar('XNYS',start=first,end=config['end'])
    sessions={s.date().isoformat() for s in calendar.sessions}
    counts=Counter();rows=[];earliest=Counter();total=0;raw_count=0
    manifest={}
    for symbol,saved_path in sorted(packets.items()):
        d=read_checked(saved_path)
        merged={};last_cursor=None
        for n,key in enumerate(d['responses']):
            p=root/'responses'/(key+'.json.gz')
            raw=p.read_bytes();packet=json.loads(gzip.decompress(raw))
            path,params=packet['request']
            assert hashlib.sha256(packed(packet['request'])).hexdigest()==key, 'request_hash_mismatch'
            assert hashlib.sha256(packed(packet['payload'])).hexdigest()==packet['sha256'], 'payload_hash_mismatch'
            assert path=='/api/v1/candles' and params['symbol']==symbol and params['interval']=='1d' and params['adjusted']=='true', 'unexpected_request'
            assert params['before']==(config['end']+'T23:59:59-04:00' if n==0 else last_cursor), 'broken_page_chain'
            manifest[str(p.relative_to(root))]=hashlib.sha256(raw).hexdigest();raw_count+=1
            try:
                page,last_cursor=parse_page(packet['payload'],config['end'])
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
    summary=dict(verified_at=datetime.now(timezone.utc).isoformat(),scope=config['scope'],requested=len(targets),verified=len(packets),counts=dict(counts),not_attempted=not_attempted,total_bars=total,source_pages=raw_count,earliest=min(earliest,default=None),latest=config['end'],start_distribution=dict(sorted(earliest.items())),with_invalid_ohlc=sum(bool(r['invalid_ohlc']) for r in rows),with_internal_gaps=sum(bool(r['missing_sessions']) for r in rows),with_non_sessions=sum(bool(r['non_sessions']) for r in rows),without_latest_session=sum(r['latest_session_missing'] for r in rows),before_2016=sum(bool(r['first']) and r['first']<'2016-01-01' for r in rows),calendar='exchange_calendars XNYS '+xcals.__version__,integrity='PASS',all_attempted=not not_attempted)
    write(root/'coverage.json',rows)
    write(root/'verification.json',summary)
    for name in ('collection.json','universe.json','coverage.json','verification.json'):
        manifest[name]=hashlib.sha256((root/name).read_bytes()).hexdigest()
    write(root/'manifest.json',manifest)
    print(json.dumps({k:v for k,v in summary.items() if k not in ('not_attempted','start_distribution')},ensure_ascii=False))
    return summary


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',required=True);p.add_argument('--require-finished',action='store_true');a=p.parse_args();verify(a.output,a.require_finished)
