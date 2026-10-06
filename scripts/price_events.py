"""Deterministic saved-price evidence. No network, orders or inferred news causes."""
from bisect import bisect_left, bisect_right
from datetime import date, timedelta
from functools import lru_cache
import hashlib
import json
import math

VERSION = 'price-events-1'
POLICY = dict(gap_pct=3, large_gap_pct=5, pivot_left=2, pivot_right=2,
              group_max_sessions=3, followup_sessions=20, trend_pct=15,
              range_shift_pct=10, meaningful_move_pct=5, volume_ratio=1.5)


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    allow_nan=False).encode()).hexdigest()


def period(day, frame):
    d = date.fromisoformat(day)
    return (d - timedelta(days=d.weekday())).isoformat() if frame == '1w' else d.replace(day=1).isoformat()


@lru_cache(maxsize=32)
def sessions(market, start, end):
    import exchange_calendars as xcals
    from invest_agent.trading.market_provider import KR_CLOSURES
    cal = xcals.get_calendar('XKRX' if market == 'KR' else 'XNYS', start=start, end=end)
    return tuple(s.date().isoformat() for s in cal.sessions
                 if market != 'KR' or s.date().isoformat() not in KR_CLOSURES)


def ratio(a, b):
    return a / b if a is not None and b is not None and b > 0 else None


def pct(a, b):
    r = ratio(a, b)
    return (r - 1) * 100 if r is not None else None


def build(daily, financial=None):
    """All events over five calendar years ending at the last saved completed day.

    Pre-window bars supply pivot neighbours. Calendar failure keeps candidates
    provisional, never fabricates completion dates. Daily source must be completed.
    """
    financial = financial or {}
    raw = daily.get('bars') or []
    bars = [b for b in raw if all(type(b.get(k)) in (int, float) and
            math.isfinite(b[k]) and b[k] > 0 for k in ('open','high','low','close'))
            and b['low'] <= min(b['open'], b['close']) <= max(b['open'], b['close']) <= b['high']]
    if [b['date'] for b in bars] != sorted({b['date'] for b in bars}):
        raise ValueError('event_dates_must_be_unique_sorted')
    as_of = bars[-1]['date'] if bars else None
    start = None
    if as_of:
        end = date.fromisoformat(as_of)
        start = end.replace(year=end.year-5, day=min(end.day, 28) if end.month == 2 else end.day).isoformat()
    source = {k: daily.get(k) for k in ('provider','price_basis','market','symbol','collected_at','history')}
    packet = dict(schema=VERSION, as_of=as_of, start=start, source=source, policy=POLICY,
                  daily_sha256=fingerprint(raw), events=[], groups=[],
                  news_inputs={'telegram':{'state':'not_connected','items':[]}},
                  limitations=['Historical prioritization uses later prices; it is not a contemporaneous signal.',
                               'Nearby dates are a research bundle, not evidence of a shared cause.',
                               'Calendar/price gaps and corporate actions require source review.'],
                  financial_snapshot_id=(financial.get('result') or {}).get('snapshot_id'))
    if not bars:
        return {**packet, 'state':'unavailable', 'reason':'saved_daily_prices_missing'}
    # Two months of buffer on either side cover pivot closure and search windows.
    buffered = (date.fromisoformat(start) - timedelta(days=100)).isoformat()
    bars = [b for b in bars if b['date'] >= buffered]
    dates = [b['date'] for b in bars]
    try:
        calendar = list(sessions(daily.get('market'), buffered,
                        (date.fromisoformat(as_of)+timedelta(days=65)).isoformat()))
        calendar_state = 'exchange_calendar'
    except (ImportError, ValueError, KeyError):
        calendar = dates
        calendar_state = 'unavailable_observed_sessions_only'
    positions = {d:i for i,d in enumerate(calendar)}
    known = set(dates)
    packet.update(state='available', calendar_state=calendar_state,
                  actual_start=dates[0], coverage_complete=dates[0] <= start,
                  excluded_invalid_bars=sum(b['date'] >= buffered for b in raw)-len(bars))

    def window(day, before, after):
        i = positions.get(day)
        if i is None:
            return dict(start=None,end=None,before=before,after=after,state='calendar_date_missing')
        return dict(start=calendar[max(0,i-before)], end=calendar[min(len(calendar)-1,i+after)],
                    before=before, after=after, state=calendar_state,
                    prices_available_through=as_of)

    def metrics(i):
        b=bars[i]; prev=bars[i-1] if i else {}; prior=bars[max(0,i-20):i]
        vols=[r.get('volume') for r in prior]
        mean=sum(vols)/len(vols) if len(vols)==20 and all(v is not None and v>=0 for v in vols) else None
        after=bars[i+1:i+21]
        valid_followup = len(after)==20 and calendar_state=='exchange_calendar' and all(
            positions.get(r['date']) == positions.get(b['date'], -100)+n+1 for n,r in enumerate(after))
        followup=pct(after[-1]['close'],b['close']) if valid_followup else None
        excursion=max((max(abs(pct(r['high'],b['close'])),abs(pct(r['low'],b['close']))) for r in after),default=None)
        shift=None
        if valid_followup and len(prior)==20:
            shift=pct((max(r['high'] for r in after)+min(r['low'] for r in after))/2,
                      (max(r['high'] for r in prior)+min(r['low'] for r in prior))/2)
        return dict(volume=b.get('volume'),previous_volume=prev.get('volume'),
                    volume_change_pct=pct(b.get('volume'),prev.get('volume')),
                    volume_ratio_20=ratio(b.get('volume'),mean),followup_return_20_pct=followup,
                    followup_observed_sessions=len(after),followup_through=after[-1]['date'] if after else None,
                    excursion_observed_pct=excursion,range_midpoint_shift_20_pct=shift,
                    returned_within_5= len(bars[i+1:i+6])==5 and abs(pct(bars[i+5]['close'],b['close']))<=2,
                    metrics_as_of=as_of)

    def event(kind, frame, i, confirmed_at, **extra):
        day=dates[i]
        if day not in positions: confirmed_at=None
        if day < start: return
        eid='pe_'+fingerprint([daily.get('market'),daily.get('symbol'),kind,frame,day])[:24]
        packet['events'].append(dict(id=eid,kind=kind,timeframe=frame,event_date=day,
            confirmed_at=confirmed_at,status='confirmed' if confirmed_at else 'provisional',
            metrics=metrics(i),news_window=window(day,3 if kind=='gap' else 10,2 if kind=='gap' else 10),
            **extra))

    for i in range(1,len(bars)):
        b,p=bars[i],bars[i-1]
        change=pct(b['open'],p['close'])
        if abs(change)+1e-10 < POLICY['gap_pct']: continue
        consecutive=positions.get(b['date']) is not None and positions.get(p['date'])==positions[b['date']]-1
        event('gap','1d',i,b['date'] if consecutive and calendar_state=='exchange_calendar' else None,
              gap_pct=change,full_gap=b['low']>p['high'] if change>0 else b['high']<p['low'],
              previous_session=p['date'],previous_session_verified=consecutive and calendar_state=='exchange_calendar',
              direction='up' if change>0 else 'down')

    for frame in ('1w','1mo'):
        groups={}
        for i,b in enumerate(bars): groups.setdefault(period(b['date'],frame),[]).append(i)
        expected={}
        for d in calendar: expected.setdefault(period(d,frame),[]).append(d)
        periods=[]
        for label,indices in groups.items():
            days=expected.get(label,[])
            complete=bool(days) and calendar_state=='exchange_calendar' and days[-1]<=as_of and all(d in known for d in days)
            periods.append(dict(label=label,indices=indices,complete=complete,
                close_date=days[-1] if complete else None,
                high=max(bars[i]['high'] for i in indices),low=min(bars[i]['low'] for i in indices)))
        for j in range(2,len(periods)):
            p=periods[j]; left=periods[j-2:j]; right=periods[j+1:j+3]
            for kind in ('high','low'):
                # Earliest equal plateau wins: strict on left, equality on right.
                valid=all(p[kind]>x[kind] if kind=='high' else p[kind]<x[kind] for x in left) and all(
                          p[kind]>=x[kind] if kind=='high' else p[kind]<=x[kind] for x in right)
                if not valid: continue
                i=next(i for i in p['indices'] if bars[i][kind]==p[kind])
                labels=list(expected); n=labels.index(p['label']) if p['label'] in labels else -1
                adjacent=n>=2 and [x['label'] for x in left+[p]+right]==labels[n-2:n+3]
                confirmed=len(right)==2 and adjacent and all(x['complete'] for x in left+[p]+right)
                event(kind,frame,i,right[-1]['close_date'] if confirmed else None,
                      period_start=p['label'],extreme_price=p[kind],right_bars_available=len(right),
                      confirmation_rule='two_complete_left_and_right_periods')

    packet['events'].sort(key=lambda e:(e['event_date'],e['timeframe'],e['kind']))
    # Non-chaining bundles: span includes at most three exchange sessions.
    bundles=[]
    for e in packet['events']:
        pos=positions.get(e['event_date'],bisect_left(calendar,e['event_date']))
        if not bundles or pos-bundles[-1][0]>=POLICY['group_max_sessions']:
            bundles.append((pos,[e]))
        else: bundles[-1][1].append(e)
    sources=list((financial.get('result') or {}).get('sources') or [])
    # Public filing dates belong to facts; source fetch timestamps are not publication dates.
    def filings(value):
        if isinstance(value,dict):
            if value.get('filed') and value.get('sources'):
                sources.append({k:value.get(k) for k in ('filed','sources','accession','receipt','end')})
            for v in value.values(): filings(v)
        elif isinstance(value,list):
            for v in value: filings(v)
    result=financial.get('result') or {}
    for field in ('annual','quarterly'): filings(result.get(field,[]))
    sources=list({fingerprint(s):s for s in sources}.values())
    for _,events in bundles:
        confirmed=[e for e in events if e['status']=='confirmed']
        reasons=[]; priority='low'; frames={e['timeframe'] for e in confirmed}
        if '1mo' in frames: reasons.append('monthly_extreme')
        if {'1w','1mo'}<=frames: reasons.append('weekly_monthly_overlap')
        if any(e['kind']!='gap' for e in confirmed) and any(abs(e.get('gap_pct',0))>=5 for e in confirmed): reasons.append('extreme_with_large_gap')
        if any(abs(e['metrics']['followup_return_20_pct'] or 0)>=15 or abs(e['metrics']['range_midpoint_shift_20_pct'] or 0)>=10 for e in confirmed): reasons.append('strong_followup_or_range_shift')
        if reasons: priority='high'
        windows=[e['news_window'] for e in confirmed if e['news_window']['start']]
        disclosures=[]
        for s in sources:
            published=s.get('published_at') or s.get('filing_date') or s.get('filed')
            if isinstance(published,str) and any(w['start']<=published[:10]<=w['end'] for w in windows):
                disclosures.append(s)
        if priority=='low' and confirmed:
            if any((e['metrics']['excursion_observed_pct'] or 0)>=5 or (e['metrics']['volume_ratio_20'] or 0)>=1.5 for e in confirmed) or disclosures:
                priority='medium';reasons.append('meaningful_move_volume_or_available_disclosure')
            elif any(abs(e.get('gap_pct',0))>=5 for e in confirmed):
                priority='medium';reasons.append('large_gap')
        if not reasons: reasons=['provisional_only' if not confirmed else 'small_or_quick_return_without_material_evidence']
        gid='pg_'+fingerprint([e['id'] for e in events])[:24]
        packet['groups'].append(dict(id=gid,event_ids=[e['id'] for e in events],
            confirmed_event_ids=[e['id'] for e in confirmed],start=events[0]['event_date'],end=events[-1]['event_date'],
            priority=priority,reasons=reasons,research_depth={'high':'detailed','medium':'brief','low':'record_only'}[priority],
            research_status='unresearched',research_status_basis='snapshot_initial_read_analysis_history_for_results',eligible_for_news=bool(confirmed) and priority!='low',
            news_windows=windows,available_disclosures=disclosures,
            evidence_as_of=as_of,shared_cause_assumed=False))
        for e in events: e['group_id']=gid
    return packet


def validate_research(rows, packet):
    """Explicit report annotations, bound to frozen groups; absence stays unresearched."""
    if not isinstance(rows,list) or len(rows)>1000: raise ValueError('invalid_event_research')
    groups={g['id']:g for g in (packet or {}).get('groups',[])}; seen=set()
    for row in rows:
        if not isinstance(row,dict) or set(row)-{'group_id','status','finding','searched_windows','sources','failure_reason'}: raise ValueError('invalid_event_research_fields')
        gid=row.get('group_id'); status=row.get('status')
        if gid not in groups or gid in seen: raise ValueError('unknown_or_duplicate_research_group')
        seen.add(gid)
        if status not in ('unresearched','failed','cause_unconfirmed','evidence_found'): raise ValueError('invalid_research_status')
        if status!='unresearched' and not groups[gid]['confirmed_event_ids']: raise ValueError('provisional_not_historical_research')
        if not isinstance(row.get('finding',''),str) or len(row.get('finding',''))>8000: raise ValueError('invalid_research_finding')
        windows=row.get('searched_windows',[]); sources=row.get('sources',[])
        if not isinstance(windows,list) or not isinstance(sources,list) or len(windows)>30 or len(sources)>30: raise ValueError('invalid_research_evidence')
        for w in windows:
            if not isinstance(w,dict) or set(w)!={'start','end','query'}: raise ValueError('invalid_searched_window')
            date.fromisoformat(w['start']);date.fromisoformat(w['end'])
            if w['start']>w['end'] or not isinstance(w['query'],str) or not 0<len(w['query'])<=2000: raise ValueError('invalid_searched_window')
        for s in sources:
            if not isinstance(s,dict) or set(s)-{'url','published_at','accessed_at','label'} or not isinstance(s.get('url'),str) or not s['url'].startswith(('https://','http://')): raise ValueError('invalid_research_source')
            if any(not isinstance(v,(str,type(None))) or isinstance(v,str) and len(v)>2000 for v in s.values()): raise ValueError('invalid_research_source')
        if status in ('cause_unconfirmed','evidence_found') and (not windows or not row.get('finding')): raise ValueError('research_trace_required')
        if status=='evidence_found' and not sources: raise ValueError('research_sources_required')
        if status=='failed' and (not isinstance(row.get('failure_reason'),str) or not 0<len(row['failure_reason'])<=4000): raise ValueError('research_failure_reason_required')
    return rows
