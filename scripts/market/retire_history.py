"""Prepare historical market snapshots and checkpoints without changing the current price rows."""
from collections import defaultdict
from contextlib import nullcontext
from functools import lru_cache
import ctypes
import hashlib
import json
import mmap
import os
import re
import time
from pathlib import Path
import zipfile
import zlib

from market.price_files import PriceFiles, file_key
from market.price_receipts import address
from market.prices import FIELDS, identity, packed, digest, normalized, validate


class HistoricalFiles(PriceFiles):
    """Use pinned current bars for matching dates. Store only missing or different historical values."""
    def __init__(self, store, db, *, only_identity=None, independent_verifier=False):
        super().__init__(store)
        self.db = db
        self.db.execute('''CREATE TABLE IF NOT EXISTS price_variants(
            id INTEGER PRIMARY KEY, series_id INTEGER NOT NULL, day TEXT NOT NULL,
            open REAL NOT NULL,high REAL NOT NULL,low REAL NOT NULL,close REAL NOT NULL,volume REAL)''')
        self.db.execute('CREATE INDEX IF NOT EXISTS historical_day ON price_variants(series_id,day)')
        self.db.execute('CREATE UNIQUE INDEX IF NOT EXISTS historical_bar_value ON price_variants(series_id,day,open,high,low,close,ifnull(volume,-1))')
        self.independent_verifier=independent_verifier
        self.last_commit=time.monotonic()
        self.revision = db.execute('SELECT max(id) FROM price_revisions').fetchone()[0]
        if only_identity is None:
            self.ids = dict(db.execute('SELECT identity,id FROM price_series'))
            where,params = '',()
        else:
            key = packed(only_identity)
            self.ids = dict(db.execute('SELECT identity,id FROM price_series WHERE identity=?',(key,)))
            where,params = ' WHERE series_id=?',(self.ids.get(key,-1),)
        self.variants = {(sid,day,o,h,l,c,v): i for i,sid,day,o,h,l,c,v in db.execute('SELECT * FROM price_variants'+where,params)}
        self.corrections = {(sid,day,o,h,l,c,v): rev for sid,day,o,h,l,c,v,rev in db.execute(
            'SELECT series_id,day,open,high,low,close,volume,valid_from FROM price_corrections'+where,params)}
        self.context = {}
        self.cache = {}
        self.year = lru_cache(maxsize=2800 if only_identity is None else 128)(self._year)

    def reader(self):
        return nullcontext(self.db)

    def check_recipe(self, recipe, size, expected):
        # The bulk runner verifies the committed ZIP and DB independently.
        # A published pack by itself is never authorization to retire a file.
        if not self.independent_verifier:
            super().check_recipe(recipe,size,expected)

    def _record(self, path):
        # Only unpublished groups reach prepare(); published groups are verified on resume.
        return None

    def prepare(self,path):
        if not self.independent_verifier or path.stat().st_size<3_000_000:
            return super().prepare(path)
        # Old snapshots are hundreds of MB of formatted JSON. Keep only their
        # metadata in memory and decode one flat OHLCV array at a time. The
        # independent reader must still reproduce the complete original SHA.
        path=path.resolve()
        with path.open('rb') as stream,mmap.mmap(stream.fileno(),0,access=mmap.ACCESS_READ) as source:
            offsets=[];pieces=[];last=0
            pattern=re.compile(rb'(?<!\\)"bars"\s*:\s*\[')
            while match:=pattern.search(source,last):
                begin=match.end()-1
                end=source.find(b']',begin)+1
                if end<=begin:raise ValueError('unterminated OHLCV array')
                pieces.append(source[last:begin])
                pieces.append(str(len(offsets)).encode())
                offsets.append((begin,end));last=end
            pieces.append(source[last:])
            text=b''.join(pieces).decode('utf-8-sig')
            del pieces
            value=json.loads(text)
            indent=re.match(r'^\{\r?\n( +)"',text)
            encoder=dict(ensure_ascii=text.isascii(),indent=len(indent[1]) if indent else None,allow_nan=False)
            if not indent and re.match(r'^\{"[^"]+":(?! )',text):encoder['separators']=(',',':')
            suffix=text[len(text.rstrip()):].replace('\r\n','\n')
            crlf='\r\n' in text
            del text
            seen=set()
            def read(index):
                if type(index) is not int or index in seen:raise ValueError('invalid historical array index')
                seen.add(index)
                a,b=offsets[index]
                return json.loads(source[a:b])
            item=self._prepare_value(path,value,encoder,suffix,source[:3]==b'\xef\xbb\xbf',crlf,
                                     hashlib.sha256(source).hexdigest(),len(source),read)
            if len(seen)!=len(offsets):raise ValueError('unmapped historical arrays')
            return item

    def _prepare_value(self, path, value, *args):
        if isinstance(value,dict) and value.get('universe'):
            self.context.update({(s['market'],s['symbol']):s['isin_code'] for s in value['universe'] if s.get('isin_code')})
        return super()._prepare_value(path,value,*args)

    def _year(self, sid, year):
        return {day:values for day,*values in self.db.execute(
            'SELECT day,open,high,low,close,volume FROM prices WHERE series_id=? AND day BETWEEN ? AND ?',
            (sid,year+'-01-01',year+'-12-31'))}

    def reference(self, series):
        if self.independent_verifier and self.db.in_transaction and time.monotonic()-self.last_commit > .75:
            self.db.commit()
            self.last_commit=time.monotonic()
        bars = series['bars']
        ident = identity(series)
        if not ident['isin']:
            ident['isin'] = self.context.get((ident['market'],ident['symbol']), '')
        identity_key = packed(ident)
        sid = self.ids.get(identity_key)
        if sid is None:
            self.db.execute('INSERT OR IGNORE INTO price_series(identity,metadata) VALUES(?,?)',(identity_key,identity_key))
            sid=self.db.execute('SELECT id FROM price_series WHERE identity=?',(identity_key,)).fetchone()[0]
            self.ids[identity_key] = sid
        token = hashlib.sha256(json.dumps(bars,separators=(',',':')).encode()).digest()
        if (sid,token) in self.cache:
            return self.cache[sid,token]
        validate(bars)
        first,last = bars[0]['date'],bars[-1]['date']
        base = {}
        for year in range(int(first[:4]),int(last[:4])+1):
            base.update({day:v for day,v in self.year(sid,str(year)).items() if first <= day <= last})
        present, variants, old = set(), [], []
        for b in bars:
            day = b['date']
            present.add(day)
            values = [b[k] for k in FIELDS]
            if base.get(day) == values:
                continue
            key = (sid,day,*values)
            if key in self.corrections:
                old.append([day,self.corrections[key]])
            else:
                if key not in self.variants:
                    insert=self.db.execute('INSERT OR IGNORE INTO price_variants(series_id,day,open,high,low,close,volume) VALUES(?,?,?,?,?,?,?)',key)
                    self.variants[key] = insert.lastrowid if insert.rowcount else self.db.execute(
                        'SELECT id FROM price_variants WHERE series_id=? AND day=? AND open=? AND high=? AND low=? AND close=? AND volume IS ?',key).fetchone()[0]
                variants.append(self.variants[key])
        ref = dict(series_id=sid,identity=ident,revision=self.revision,first=first,last=last,
                   count=len(bars),sha256=digest(normalized(bars)))
        if variants: ref['variants'] = variants
        if old: ref['old_versions'] = old
        missing = sorted(base.keys()-present)
        if missing: ref['excluded_dates'] = missing
        self.cache[sid,token] = ref
        if self.independent_verifier and self.db.in_transaction:
            self.db.commit()
            self.last_commit=time.monotonic()
        return ref

    def save_record(self, path, expected, size, recipe_hash, compressed):
        target, name = address(self.store.path.parent,path)
        if target != self.target:
            raise ValueError('unexpected_receipt_group')
        header = json.dumps(dict(path=str(path),sha256=expected,bytes=size,recipe_sha256=recipe_hash),ensure_ascii=False).encode()
        self.bundle.writestr(name,header+b'\n'+compressed)

    def prepare_group(self, paths, ledger, old_records):
        target,_ = address(self.store.path.parent,paths[0])
        self.target = target
        target.parent.mkdir(exist_ok=True)
        if target.exists():
            raise ValueError('receipt_group_already_published')
        tmp = target.with_suffix('.building')
        self.context = {}
        # Interrupted runs can have checkpoints but no completed snapshot.
        # Use their own hash-checked Toss listing evidence, never a current
        # ticker lookup, to identify those historical prices.
        from invest_agent.trading.contracts import digest as cache_digest
        cache_dirs={p.parent.parent for p in paths if p.parts[-3:-1]==('cache','series')}
        ambiguous=set()
        for directory in cache_dirs:
            for listing in sorted((directory/'universe').glob('*.json')):
                bundle=json.loads(listing.read_bytes())
                rows=bundle['rows']
                if cache_digest(rows)!=bundle.get('hash') or bundle['meta'].get('state')!='available':
                    raise ValueError('historical_listing_cache_mismatch:'+str(listing))
                for row in rows:
                    key=(row['market'],row['symbol'])
                    isin=row.get('isin_code')
                    if isin and key in self.context and self.context[key]!=isin:ambiguous.add(key)
                    elif isin:self.context[key]=isin
        for key in ambiguous:self.context.pop(key,None)
        entries = []
        with zipfile.ZipFile(tmp,'w',compression=zipfile.ZIP_STORED,allowZip64=True) as self.bundle:
            for p in paths:
                if p.name=='snapshot.json':
                    expected = json.loads((p.parent/'manifest.json').read_bytes()).get('snapshot.json')
                    if not expected:
                        raise ValueError('missing_snapshot_manifest:'+str(p))
                else:
                    expected = None
                old = old_records.get(file_key(p))
                if old:
                    source,size,h,blob = old
                    if physical_sha(p) != source or hashlib.sha256(zlib.decompress(blob)).hexdigest()!=h:
                        raise ValueError('existing_receipt_changed')
                    self.save_record(p,source,size,h,blob)
                    item = dict(path=str(p),sha256=source,bytes=size)
                else:
                    item = self.prepare(p)
                if expected and expected != item['sha256']:
                    raise ValueError('snapshot_source_hash_mismatch:'+str(p))
                item.update(stored_bytes=storage(p),mtime_ns=p.stat().st_mtime_ns)
                entries.append(item)
        self.db.commit()
        os.replace(tmp,target)
        with ledger.open('a',encoding='utf-8') as stream:
            for item in entries: stream.write(json.dumps(item,ensure_ascii=False)+'\n')
        return len(entries)


def physical_sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream,'sha256').hexdigest()


def storage(path):
    if os.name != 'nt': return path.stat().st_size
    high = ctypes.c_uint32()
    lo = ctypes.windll.kernel32.GetCompressedFileSizeW(str(path),ctypes.byref(high))
    return (high.value << 32) | (lo & 0xffffffff)
