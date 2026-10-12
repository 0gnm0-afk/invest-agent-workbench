"""Recent confirmed pivot envelopes shared by chart and screening; no network/storage."""
import math
from invest_agent.trading.sperandeo import pivots, PIVOT_RIGHT

VERSION = 'recent-pivots-v1'
WINDOW = 60
MIN_TREND_BARS = 10
MAX_TOUCH_AGE = 20
WICK_ATR = .25


def atr_series(bars):
    values = [None]*len(bars)
    ranges = [max(b['high']-b['low'], abs(b['high']-a['close']), abs(b['low']-a['close']))
              for a,b in zip(bars,bars[1:])]
    if len(ranges) >= 14:
        value = sum(ranges[:14])/14
        values[14] = value
        for i,tr in enumerate(ranges[14:],15):
            value = (value*13+tr)/14
            values[i] = value
    return values


def clean(raw, as_of=None):
    bars = [b for b in raw if b.get('complete',True) and (as_of is None or b['date']<=as_of)]
    for i,b in enumerate(bars):
        if i and b['date'] <= bars[i-1]['date']:
            raise ValueError('invalid_history_dates')
        if any(type(b.get(k)) not in (int,float) or not math.isfinite(b[k]) or b[k]<=0
               for k in ('open','high','low','close')):
            raise ValueError('invalid_prices')
        if not b['low'] <= min(b['open'],b['close']) <= max(b['open'],b['close']) <= b['high']:
            raise ValueError('invalid_ohlc')
    return bars


def candidates(raw, key='high', *, as_of=None, window=WINDOW, min_trend_bars=MIN_TREND_BARS):
    """Two confirmed pivots, bounded age, intact bodies and ATR-bounded wick noise.

    No regression through candle bodies. The first close break is retained even
    after price retraces. Rank before testing approach, to avoid cherry-picking
    whichever of many old lines happens to sit next to today's close.
    """
    bars = clean(raw,as_of)
    if len(bars)<15:return []
    atrs=atr_series(bars)
    offset=max(0,len(bars)-min(window,WINDOW))
    points=[i+offset for i in pivots(bars[offset:],key)]
    end=len(bars)-1
    sign=1 if key=='high' else -1
    result=[]
    for b in reversed(points):
        if end-b>MAX_TOUCH_AGE:continue
        for a in reversed(points):
            if b-a<5 or end-a<min_trend_bars or atrs[a] is None:continue
            slope=(bars[b][key]-bars[a][key])/(b-a)
            if slope*sign>=0:continue
            at=lambda i:bars[a][key]+slope*(i-a)
            known=b+PIVOT_RIGHT
            if at(end)<=0:continue
            eps=max(bars[a][key],bars[b][key])*1e-10
            broken=None;breach=None;valid=True
            for i in range(a,end+1):
                value=at(i)
                crossed=sign*(bars[i]['close']-value)>eps
                if crossed:
                    if i<=known:valid=False
                    else:broken=i
                    break
                body=max(bars[i]['open'],bars[i]['close']) if key=='high' else min(bars[i]['open'],bars[i]['close'])
                if sign*(body-value)>eps or sign*(bars[i][key]-value)>WICK_ATR*atrs[i]+eps:
                    valid=False;break
                if sign*(bars[i][key]-value)>eps and breach is None:breach=i
            if not valid or broken is not None and broken-a<min_trend_bars:continue
            touch_end=broken if broken is not None else end+1
            touches=[i for i in points if a<=i<touch_end and abs(bars[i][key]-at(i))<=WICK_ATR*atrs[i]+eps]
            result.append(dict(state='available',algorithm=VERSION,side='resistance' if key=='high' else 'support',
                anchor_a_date=bars[a]['date'],anchor_a_price=bars[a][key],anchor_b_date=bars[b]['date'],anchor_b_price=bars[b][key],
                anchor_a_confirmed_at=bars[a+PIVOT_RIGHT]['date'],confirmed_at=bars[known]['date'],
                slope_per_bar=slope,value_latest=at(end),as_of=bars[end]['date'],time_axis='trading_sessions',price_scale='linear',
                atr14=atrs[-1],gap_atr=sign*(at(end)-bars[end]['close'])/atrs[-1] if atrs[-1] else None,
                trend_bars=end-a,touch_count=len(touches),touch_dates=[bars[i]['date'] for i in touches],
                last_touch_age=end-b,wick_breach=breach is not None,
                first_price_breach_date=bars[breach]['date'] if breach is not None else None,
                first_close_break_date=bars[broken]['date'] if broken is not None else None,
                bars_since_break=end-broken if broken is not None else None,
                latest_close_beyond_line=sign*(bars[end]['close']-at(end))>eps))
    result.sort(key=lambda l:(l['last_touch_age'],-min(l['touch_count'],3),l['trend_bars']))
    return result


def select(raw,key='high',**kwargs):
    return next((l for l in candidates(raw,key,**kwargs) if l['first_close_break_date'] is None),None)


def overlay(raw,display_bars,*,as_of=None,screening_line=None):
    """Daily-derived lines projected to the actual last session in each displayed bar."""
    try:
        bars=clean(raw,as_of);dates={b['date']:i for i,b in enumerate(bars)}
        lines=[]
        for key in ('high','low'):
            found=candidates(bars,key)
            line=next((l for l in found if l['first_close_break_date'] is None),None)
            if line is None:
                line=next((l for l in found if l['bars_since_break'] is not None and l['bars_since_break']<=5),None)
            if key=='high' and screening_line and screening_line.get('as_of')==(bars[-1]['date'] if bars else None):
                matched=next((l for l in found if all(l[k]==screening_line.get(k) for k in
                    ('anchor_a_date','anchor_b_date','anchor_a_price','anchor_b_price'))),None)
                if matched is not None:line={**matched,'screening':True}
            if line is None:continue
            a=dates[line['anchor_a_date']];b=dates[line['anchor_b_date']]
            solid=[];extension=[]
            for bar in display_bars:
                i=dates.get(bar.get('last_session',bar['date']))
                if i is None or i<a:continue
                point=dict(time=bar['date'],value=line['anchor_a_price']+line['slope_per_bar']*(i-a))
                (solid if i<=b else extension).append(point)
            if solid and extension:extension.insert(0,solid[-1])
            lines.append({**line,'solid':solid,'extension':extension})
        return dict(algorithm=VERSION,window=WINDOW,timeframe='1d',as_of=bars[-1]['date'] if bars else None,lines=lines)
    except (ValueError,TypeError,KeyError):
        return dict(algorithm=VERSION,window=WINDOW,timeframe='1d',state='invalid_history',lines=[])
