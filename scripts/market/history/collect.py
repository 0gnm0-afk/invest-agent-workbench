"""Resumable stock/US ADR history collection, isolated from web.server runs."""

if not __package__:
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

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

from market.providers.toss_client import TossMarketClient, MarketReadError
from market.providers.toss_universe import listing_requests, validate_listing_rows
from invest_agent.trading.market_provider import completed_sessions
from invest_agent.trading.store import instance_lock
from atomic_io import read_bytes
from market.price_files import available


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
    packet = json.loads(read_bytes(path))
    if hashlib.sha256(packed(packet['data'])).hexdigest() != packet['sha256']:
        raise ValueError('saved_hash_mismatch')
    return packet['data']


def parse_page(payload, end, currency="USD"):
    body = payload.get('result')
    if not isinstance(body, dict) or not isinstance(body.get('candles'), list):
        raise ValueError('invalid_candle_envelope')
    bars, stamps = [], []
    for c in body['candles']:
        stamp = datetime.fromisoformat(c['timestamp'])
        if stamp.tzinfo is None or c.get('currency') != currency:
            raise ValueError('invalid_timestamp_or_currency')
        # Toss 1d timestamps represent local midnight. Fixed +09:00 wire offsets
        # can encode 23:00 on the preceding date during historic Korean DST.
        day = (stamp.astimezone(ZoneInfo('Asia/Seoul')) if currency == 'KRW' else stamp).date().isoformat()
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
    def __init__(self, root, client=None, market="US"):
        if market not in ("US", "KR"): raise ValueError("unsupported_market")
        self.market = market
        self.currency = "KRW" if market == "KR" else "USD"
        self.exchanges = ("KOSPI", "KOSDAQ") if market == "KR" else ("NASDAQ", "NYSE")
        self.offset = "+09:00" if market == "KR" else "-04:00"
        self.root = Path(root)
        config_path = self.root/'collection.json'
        # A saved collection is immutable in scope: schema 1 remains STOCK-only.
        schema = read_checked(config_path).get('schema', 1) if config_path.exists() else 2
        if schema not in (1, 2): raise ValueError('unsupported_collection_schema')
        self.include_adr = market == 'US' and schema == 2
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
        security_types = ['STOCK', 'DEPOSITARY_RECEIPT'] if self.include_adr else ['STOCK']
        if saved.exists():
            value = read_checked(saved)
            if value.get('security_types', ['STOCK']) != security_types:
                raise ValueError('saved_listing_scope_mismatch')
            return value
        by_symbol = {}
        provider_symbols = set()
        exchanges = {}
        for exchange in self.exchanges:
            requests = {}
            for params in listing_requests(exchange, include_adr=self.include_adr):
                payload,key = self.get('/api/v1/stocks/all',params)
                rows = validate_listing_rows(payload.get('result'), params)
                requests[params['securityType']] = dict(count=len(rows),response=key)
                for r in rows:
                    symbol = r['symbol']
                    if symbol in provider_symbols:
                        raise ValueError('cross_exchange_symbol_collision')
                    provider_symbols.add(symbol)
                    if self.market == 'KR':
                        if not re.fullmatch(r'[0-9A-Z]{6}', symbol): raise ValueError('invalid_kr_symbol')
                        normalized = symbol + ('.KS' if exchange == 'KOSPI' else '.KQ')
                        by_symbol[normalized] = {**r, 'symbol': normalized, 'provider_symbol': symbol, 'market': self.market, 'listing_exchange': exchange}
                    else:
                        by_symbol[symbol] = {**r,'market':self.market,'listing_exchange':exchange}
            exchanges[exchange] = (dict(count=sum(m['count'] for m in requests.values()), requests=requests)
                                   if self.include_adr else requests['STOCK'])
        value = dict(rows=[by_symbol[s] for s in sorted(by_symbol)],exchanges=exchanges,observed_at=datetime.now(timezone.utc).isoformat())
        if self.include_adr:
            value['security_types'] = security_types
        save_checked(saved,value)
        return value

    def collect_one(self, item, end, retry=False):
        from market.scope import match
        exclusion = match({'market':self.market,'symbol':item['symbol']})
        if exclusion:
            return {'symbol':item['symbol'],'market':self.market,'state':'excluded','reason':exclusion['reason'],'bars':[]}
        symbol = item['symbol']
        saved = self.root/'series'/(hashlib.sha256(symbol.encode()).hexdigest()+'.json')
        if available(saved):
            old = read_checked(saved)
            if old['symbol'] != symbol or old['requested_end'] != end or old['market'] != self.market or old['listing'] != item:
                raise ValueError('saved_identity_mismatch')
            if not retry or old['state'] != 'failed':
                return old
        result = dict(symbol=symbol,market=self.market,currency=self.currency,listing=item,requested_end=end,provider='Toss OpenAPI candles',price_basis='toss_adjusted_ohlc_no_order_authority',source_url='https://openapi.tossinvest.com/api/v1/candles',collected_at=datetime.now(timezone.utc).isoformat())
        before = end+'T23:59:59'+self.offset
        bars, sources, cursors = {}, [], set()
        try:
            for _ in range(300):
                params = dict(symbol=item.get('provider_symbol', symbol),interval='1d',count=200,adjusted='true',before=before)
                payload,key = self.get('/api/v1/candles',params)
                sources.append(key)
                rows,cursor = parse_page(payload,end,self.currency)
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


def run(root,workers=6,retry=False,market="US"):
    root=Path(root);root.mkdir(parents=True,exist_ok=True)
    with instance_lock(root/'lock'):
        collector=Collector(root,market=market)
        config_path=root/'collection.json'
        if config_path.exists():
            config=read_checked(config_path)
            if config.get("markets", ["US"]) != [market]: raise ValueError("collection_market_mismatch")
        else:
            now=datetime.now(timezone.utc)
            cap=(now.astimezone(ZoneInfo('Asia/Seoul')).date()-timedelta(days=1)).isoformat()
            end=max(s for s in completed_sessions(market,now) if s<=cap)
            scope = 'ACTIVE STOCK commonShare=true'
            if collector.include_adr:
                scope += ' plus ACTIVE DEPOSITARY_RECEIPT (no commonShare filter)'
            config=dict(schema=2 if collector.include_adr else 1,started_at=now.isoformat(),end=end,start='provider_earliest_available',markets=[market],exchanges=list(collector.exchanges),scope=scope+'; current provider listing, not historical delisted universe',adjusted=True)
            if collector.include_adr:
                config['security_types'] = ['STOCK', 'DEPOSITARY_RECEIPT']
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
    parser=argparse.ArgumentParser();parser.add_argument('--output',required=True);parser.add_argument('--market',choices=['US','KR'],default='US');parser.add_argument('--workers',type=int,default=6);parser.add_argument('--retry-errors',action='store_true');args=parser.parse_args()
    if not 1<=args.workers<=8:parser.error('workers must be 1..8')
    run(args.output,args.workers,args.retry_errors,args.market)
