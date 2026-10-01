"""Resumable US common-stock history collection, isolated from dashboard runs."""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
import gzip
import hashlib
import json
import math
from pathlib import Path
import re
import threading
from zoneinfo import ZoneInfo

from toss_market_client import TossMarketClient, MarketReadError
from invest_agent.trading.market_provider import completed_sessions
from invest_agent.trading.store import instance_lock


def packed(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode('utf-8')


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_bytes(packed(value))
    temp.replace(path)


def save_checked(path, value):
    write(path, {'sha256': hashlib.sha256(packed(value)).hexdigest(), 'data': value})


def read_checked(path):
    packet = json.loads(path.read_bytes())
    if hashlib.sha256(packed(packet['data'])).hexdigest() != packet['sha256']:
        raise ValueError('saved_hash_mismatch')
    return packet['data']


def parse_page(payload, end):
    body = payload.get('result')
    if not isinstance(body, dict) or not isinstance(body.get('candles'), list):
        raise ValueError('invalid_candle_envelope')
    bars, stamps = [], []
    for c in body['candles']:
        stamp = datetime.fromisoformat(c['timestamp'])
        if stamp.tzinfo is None or c.get('currency') != 'USD':
            raise ValueError('invalid_timestamp_or_currency')
        day = stamp.date().isoformat()
        stamps.append(stamp)
        if day > end:
            raise ValueError('incomplete_session_received')
        values = []
        for k in ('openPrice', 'highPrice', 'lowPrice', 'closePrice', 'volume'):
            raw = c[k]
            if raw is None or isinstance(raw, bool):
                raise ValueError('invalid_candle_value')
            v = float(raw)
            if not math.isfinite(v) or v < 0:
                raise ValueError('invalid_candle_value')
            values.append(v)
        bars.append(dict(date=day, **dict(zip(('open','high','low','close','volume'), values))))
    if stamps != sorted(set(stamps), reverse=True) or len({b['date'] for b in bars}) != len(bars):
        raise ValueError('duplicate_or_unordered_page')
    cursor = body.get('nextBefore')
    if cursor is not None:
        parsed = datetime.fromisoformat(cursor)
        if not stamps or parsed.tzinfo is None or parsed > stamps[-1]:
            raise ValueError('invalid_next_cursor')
    return bars, cursor


class Collector:
    def __init__(self, root, client=None):
        self.root = Path(root)
        self.log_lock = threading.Lock()
        self.client = client
        self.client_lock = threading.Lock()
        self.http_counts = Counter()

    def observe(self, event):
        event = {k:v for k,v in event.items() if k != 'payload'}
        with self.log_lock:
            self.http_counts[str(event['status'])] += 1
            with (self.root/'http.jsonl').open('ab') as f:
                f.write(packed(event)+b'\n')

    def get(self, path, params):
        key = hashlib.sha256(packed([path,params])).hexdigest()
        saved = self.root/'responses'/(key+'.json.gz')
        if saved.exists():
            packet = json.loads(gzip.decompress(saved.read_bytes()))
            if packet['request'] != [path,params] or hashlib.sha256(packed(packet['payload'])).hexdigest() != packet['sha256']:
                raise ValueError('response_cache_hash_mismatch')
            return packet['payload'], key
        with self.client_lock:
            if self.client is None:
                self.client = TossMarketClient(observer=self.observe, min_interval=.1)
        payload = self.client(path,params)
        packet = dict(request=[path,params],payload=payload,sha256=hashlib.sha256(packed(payload)).hexdigest(),fetched_at=datetime.now(timezone.utc).isoformat())
        saved.parent.mkdir(parents=True,exist_ok=True)
        temp = saved.with_suffix('.tmp')
        temp.write_bytes(gzip.compress(packed(packet)))
        temp.replace(saved)
        return payload,key

    def universe(self):
        saved = self.root/'universe.json'
        if saved.exists():
            return read_checked(saved)
        by_symbol = {}
        exchanges = {}
        for exchange in ('NASDAQ','NYSE'):
            params = dict(market=exchange,status='ACTIVE',securityType='STOCK',commonShare='true')
            payload,key = self.get('/api/v1/stocks/all',params)
            rows = payload.get('result')
            if not isinstance(rows,list) or not rows:
                raise ValueError('listing_unavailable')
            exchanges[exchange] = dict(count=len(rows),response=key)
            seen = set()
            for r in rows:
                symbol = r.get('symbol')
                if not isinstance(symbol,str) or not re.fullmatch(r'[A-Za-z0-9.\-]+',symbol) or symbol in seen:
                    raise ValueError('invalid_listing_symbol')
                if r.get('securityType') not in (None,'STOCK') or r.get('isCommonShare') is False:
                    raise ValueError('listing_filter_mismatch')
                seen.add(symbol)
                if symbol in by_symbol:
                    raise ValueError('cross_exchange_symbol_collision')
                by_symbol[symbol] = {**r,'market':'US','listing_exchange':exchange}
        value = dict(rows=[by_symbol[s] for s in sorted(by_symbol)],exchanges=exchanges,observed_at=datetime.now(timezone.utc).isoformat())
        save_checked(saved,value)
        return value

    def collect_one(self, item, end, retry=False):
        symbol = item['symbol']
        saved = self.root/'series'/(hashlib.sha256(symbol.encode()).hexdigest()+'.json')
        if saved.exists():
            old = read_checked(saved)
            if old['symbol'] != symbol or old['requested_end'] != end:
                raise ValueError('saved_identity_mismatch')
            if not retry or old['state'] != 'failed':
                return old
        result = dict(symbol=symbol,market='US',currency='USD',listing=item,requested_end=end,provider='Toss OpenAPI candles',price_basis='toss_adjusted_ohlc_no_order_authority',source_url='https://openapi.tossinvest.com/api/v1/candles',collected_at=datetime.now(timezone.utc).isoformat())
        before = end+'T23:59:59-04:00'
        bars, sources, cursors = {}, [], set()
        try:
            for _ in range(300):
                params = dict(symbol=symbol,interval='1d',count=200,adjusted='true',before=before)
                payload,key = self.get('/api/v1/candles',params)
                sources.append(key)
                rows,cursor = parse_page(payload,end)
                for b in rows:
                    if b['date'] in bars and bars[b['date']] != b:
                        raise ValueError('conflicting_overlap')
                    bars[b['date']] = b
                if not cursor:
                    result['provider_end_reached'] = True
                    break
                if cursor in cursors or datetime.fromisoformat(cursor) >= datetime.fromisoformat(before):
                    raise ValueError('cursor_not_descending')
                cursors.add(cursor)
                before = cursor
            else:
                raise ValueError('page_budget_exhausted')
            result['state'] = 'received' if bars else 'empty'
        except (ValueError,KeyError,TypeError,OverflowError) as exc:
            result.update(state='failed',error=str(exc) if isinstance(exc,MarketReadError) else type(exc).__name__+':'+str(exc)[:120])
        result.update(bars=[bars[d] for d in sorted(bars)],responses=sources,first=min(bars) if bars else None,last=max(bars) if bars else None)
        save_checked(saved,result)
        return result

    def collect_summary(self, item, end, retry=False):
        result = self.collect_one(item, end, retry)
        return {'state': result['state'], 'bar_count': len(result['bars'])}


def run(root,workers=6,retry=False):
    root=Path(root);root.mkdir(parents=True,exist_ok=True)
    with instance_lock(root/'lock'):
        collector=Collector(root)
        config_path=root/'collection.json'
        if config_path.exists():
            config=read_checked(config_path)
        else:
            now=datetime.now(timezone.utc)
            cap=(now.astimezone(ZoneInfo('Asia/Seoul')).date()-timedelta(days=1)).isoformat()
            end=max(s for s in completed_sessions('US',now) if s<=cap)
            config=dict(schema=1,started_at=now.isoformat(),end=end,start='provider_earliest_available',markets=['US'],exchanges=['NASDAQ','NYSE'],scope='ACTIVE STOCK commonShare=true; current provider listing, not historical delisted universe',adjusted=True)
            save_checked(config_path,config)
        universe=collector.universe();items=universe['rows']
        counts=Counter();total_bars=0
        def progress(state):
            p=dict(state=state,total=len(items),processed=sum(counts.values()),counts=dict(counts),bars=total_bars,http_this_process=dict(collector.http_counts),updated_at=datetime.now(timezone.utc).isoformat(),end=config['end'])
            write(root/'progress.json',p);print(json.dumps(p),flush=True)
        progress('running')
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures=[pool.submit(collector.collect_summary,item,config['end'],retry) for item in items]
            for future in as_completed(futures):
                result=future.result();counts[result['state']]+=1;total_bars+=result['bar_count']
                if sum(counts.values())%25==0:progress('running')
        progress('collection_finished')


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',required=True);parser.add_argument('--workers',type=int,default=6);parser.add_argument('--retry-errors',action='store_true');args=parser.parse_args()
    if not 1<=args.workers<=8:parser.error('workers must be 1..8')
    run(args.output,args.workers,args.retry_errors)
