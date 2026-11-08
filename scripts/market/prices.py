"""One private OHLCV store. Identical bars are never copied for another run.

References pin a revision and date range. Only actual corrections create an old
bar version, so past inputs remain readable while the current series advances.
"""
from contextlib import closing, nullcontext
from datetime import date, datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3

from workbench_paths import ROOT


FIELDS = ('open', 'high', 'low', 'close', 'volume')


def packed(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(packed(value).encode()).hexdigest()


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def default_path(root=ROOT):
    root = Path(root)
    config = root / 'config.json'
    settings = json.loads(config.read_text(encoding='utf-8-sig')) if config.exists() else {}
    value = Path(settings.get('ohlcv', {}).get('path', 'runtime/ohlcv.sqlite3'))
    return value if value.is_absolute() else root / value


def identity(series):
    listing = series.get('listing') or {}
    provider = series.get('provider') or 'Toss OpenAPI candles'
    if provider.startswith('Toss'):
        provider = 'Toss OpenAPI candles'
    value = dict(market=series['market'], symbol=series['symbol'],
                 currency=series['currency'], provider=provider,
                 price_basis=series['price_basis'],
                 isin=series.get('isin_code') or listing.get('isinCode') or '')
    if value['market'] not in ('KR', 'US') or not all(isinstance(v, str) for v in value.values()):
        raise ValueError('ohlcv_identity_invalid')
    if not all(value[k] for k in ('symbol', 'currency', 'provider', 'price_basis')):
        raise ValueError('ohlcv_identity_incomplete')
    return value


def validate(bars):
    previous = None
    for bar in bars:
        day = bar['date']
        if date.fromisoformat(day).isoformat() != day or previous is not None and day <= previous:
            raise ValueError('ohlcv_dates_invalid')
        for field in FIELDS:
            value = bar[field]
            if value is None and field == 'volume':
                continue  # A historical missing volume remains missing, never zero.
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError('ohlcv_value_invalid')
        previous = day


class PriceStore:
    def __init__(self, path=None):
        self.path = Path(path or default_path()).resolve()

    def connect(self, *, write=False):
        if not write:
            db = sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True, timeout=30)
            db.execute('PRAGMA query_only=ON')
            return db
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=30)
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA journal_mode=WAL')
        db.executescript('''
            CREATE TABLE IF NOT EXISTS price_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS price_series(
                id INTEGER PRIMARY KEY,identity TEXT UNIQUE NOT NULL,metadata TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS price_lookup ON price_series(
                json_extract(identity,'$.market'),json_extract(identity,'$.symbol'));
            CREATE TABLE IF NOT EXISTS price_revisions(
                id INTEGER PRIMARY KEY,observed_at TEXT NOT NULL,source TEXT NOT NULL,
                source_sha256 TEXT,kind TEXT NOT NULL,details TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS prices(
                series_id INTEGER NOT NULL,day TEXT NOT NULL,
                open REAL NOT NULL,high REAL NOT NULL,low REAL NOT NULL,close REAL NOT NULL,
                volume REAL,revision INTEGER NOT NULL,
                PRIMARY KEY(series_id,day)) WITHOUT ROWID;
            CREATE TABLE IF NOT EXISTS price_corrections(
                series_id INTEGER NOT NULL,day TEXT NOT NULL,
                open REAL NOT NULL,high REAL NOT NULL,low REAL NOT NULL,close REAL NOT NULL,
                volume REAL,valid_from INTEGER NOT NULL,valid_to INTEGER NOT NULL,
                PRIMARY KEY(series_id,day,valid_from)) WITHOUT ROWID;
            CREATE TABLE IF NOT EXISTS price_observations(
                id INTEGER PRIMARY KEY,series_id INTEGER NOT NULL,revision INTEGER NOT NULL,
                kind TEXT NOT NULL,source TEXT NOT NULL,source_sha256 TEXT NOT NULL,
                reference TEXT NOT NULL,metadata TEXT NOT NULL,
                UNIQUE(series_id,kind,source,source_sha256));
            CREATE INDEX IF NOT EXISTS price_observation_kind
                ON price_observations(series_id,kind,id DESC);
            CREATE TABLE IF NOT EXISTS price_failures(
                id INTEGER PRIMARY KEY,observed_at TEXT NOT NULL,market TEXT,symbol TEXT,
                category TEXT NOT NULL,reason TEXT NOT NULL,details TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS price_imports(
                source TEXT PRIMARY KEY,sha256 TEXT NOT NULL,completed_at TEXT NOT NULL);
        ''')
        return db

    def meta(self, key, default=None):
        if not self.path.exists():
            return default
        with closing(self.connect()) as db:
            row = db.execute('SELECT value FROM price_meta WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_meta(self, key, value):
        with closing(self.connect(write=True)) as db, db:
            db.execute('INSERT OR REPLACE INTO price_meta VALUES (?,?)', (key, packed(value)))

    def put(self, series, *, source, source_sha256='', kind='daily', metadata=None,
            allow_corrections=True):
        ident = identity(series)
        bars = series.get('bars') or []
        validate(bars)
        from market.price_metadata import observation_metadata
        meta = observation_metadata({**series, **(metadata or {})})
        source = str(source)
        observed = series.get('collected_at') or utc_now()
        values = [(b['date'], *(b[k] for k in FIELDS)) for b in bars]
        with closing(self.connect(write=True)) as db, db:
            db.execute('BEGIN IMMEDIATE')
            key = packed(ident)
            row = db.execute('SELECT id FROM price_series WHERE identity=?', (key,)).fetchone()
            if row:
                sid = row[0]
            else:
                sid = db.execute('INSERT INTO price_series(identity,metadata) VALUES (?,?)',
                                 (key, packed(ident))).lastrowid
            reference_hash = digest(normalized(bars))
            prior = db.execute('SELECT reference FROM price_observations WHERE series_id=? AND kind=? AND source=? AND source_sha256=?',
                               (sid, kind, source, source_sha256)).fetchone()
            if prior and json.loads(prior[0])['sha256'] == reference_hash:
                return json.loads(prior[0])
            old = {r[0]: r[1:] for r in db.execute(
                'SELECT day,open,high,low,close,volume,revision FROM prices WHERE series_id=? AND day>=? AND day<=?',
                (sid, values[0][0], values[-1][0]))} if values else {}
            changes = [v for v in values if v[0] in old and tuple(v[1:]) != tuple(old[v[0]][:5])]
            if changes and not allow_corrections:
                raise ValueError('ohlcv_overlap_changed')
            revision = db.execute('INSERT INTO price_revisions(observed_at,source,source_sha256,kind,details) VALUES (?,?,?,?,?)',
                (observed, source, source_sha256, kind, packed({'received': len(bars), 'new': sum(v[0] not in old for v in values),
                                                               'corrected': len(changes),
                                                               'correction_reason': 'verified_saved_source_difference' if kind in ('series','risk_series','benchmark')
                                                                 else 'provider_overlap_refetch' if kind == 'checkpoint' else kind}))).lastrowid
            db.executemany('INSERT INTO price_corrections VALUES (?,?,?,?,?,?,?,?,?)',
                [(sid, v[0], *old[v[0]][:5], old[v[0]][5], revision) for v in changes])
            changed_days = {v[0] for v in changes}
            db.executemany('INSERT INTO prices VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(series_id,day) DO UPDATE SET '
                'open=excluded.open,high=excluded.high,low=excluded.low,close=excluded.close,volume=excluded.volume,revision=excluded.revision',
                [(sid, *v, revision) for v in values if v[0] not in old or v[0] in changed_days])
            ref = dict(series_id=sid, revision=revision, first=values[0][0] if values else None,
                       last=values[-1][0] if values else None, count=len(bars), sha256=reference_hash,
                       identity=ident, numeric_sha256=reference_hash)
            excluded = sorted(old.keys() - {v[0] for v in values})
            if excluded:
                ref['excluded_dates'] = excluded
            db.execute('INSERT OR REPLACE INTO price_observations(series_id,revision,kind,source,source_sha256,reference,metadata) VALUES (?,?,?,?,?,?,?)',
                       (sid, revision, kind, source, source_sha256, packed(ref), packed(meta)))
            return ref

    def read(self, reference, *, verify=True, connection=None):
        ref = reference
        with (closing(self.connect()) if connection is None else nullcontext(connection)) as db:
            row = db.execute('SELECT identity FROM price_series WHERE id=?', (ref['series_id'],)).fetchone()
            if not row or json.loads(row[0]) != ref['identity']:
                raise ValueError('ohlcv_reference_identity_mismatch')
            if 'segments' in ref:
                parts=ref['segments']
                if len(parts)!=2 or any(p['series_id']!=ref['series_id'] or 'segments' in p for p in parts):
                    raise ValueError('invalid_composed_price_reference')
                prefix,suffix=parts
                left=self.read(prefix,connection=db)
                right=self.read(suffix,connection=db)
                combined=[b for b in left if ref['first']<=b['date']<suffix['first']]+right
                rows=[(b['date'],*(b[k] for k in FIELDS)) for b in combined if b['date']<=ref['last']]
            elif ref['count'] == 0:
                rows = []
            else:
                args = (ref['series_id'], ref['first'], ref['last'], ref['revision'])
                rows = db.execute('''SELECT day,open,high,low,close,volume FROM prices
                    WHERE series_id=? AND day>=? AND day<=? AND revision<=?
                    UNION ALL SELECT day,open,high,low,close,volume FROM price_corrections
                    WHERE series_id=? AND day>=? AND day<=? AND valid_from<=? AND valid_to>?
                    ORDER BY day''', (*args, *args, ref['revision'])).fetchall()
            if ref.get('variants') or ref.get('old_versions'):
                by_day = {r[0]: r for r in rows}
                variants = ref.get('variants', [])
                for start in range(0, len(variants), 400):
                    ids = variants[start:start+400]
                    fetched = db.execute('SELECT day,open,high,low,close,volume FROM price_variants WHERE series_id=? AND id IN (' +
                                         ','.join('?' for _ in ids) + ')', (ref['series_id'], *ids)).fetchall()
                    if len(fetched) != len(ids):
                        raise ValueError('missing_historical_variant')
                    by_day.update((r[0], r) for r in fetched)
                for day, version in ref.get('old_versions', []):
                    old = db.execute('SELECT day,open,high,low,close,volume FROM price_corrections WHERE series_id=? AND day=? AND valid_from=?',
                                     (ref['series_id'], day, version)).fetchone()
                    if not old:
                        raise ValueError('missing_historical_correction')
                    by_day[day] = old
                rows = sorted(by_day.values())
        bars = [dict(zip(('date', *FIELDS), r)) for r in rows]
        if ref.get('excluded_dates'):
            excluded=set(ref['excluded_dates'])
            bars=[b for b in bars if b['date'] not in excluded]
        # JSON numbers may have originally been int or float; compare numerically.
        if len(bars) != ref['count'] or len({b['date'] for b in bars}) != len(bars):
            raise ValueError('ohlcv_reference_count_mismatch')
        if verify and digest(normalized(bars)) != ref.get('numeric_sha256', ref['sha256']):
            # Old references are accepted only if their exact JSON hash matches.
            if digest(bars) != ref['sha256']:
                raise ValueError('ohlcv_reference_hash_mismatch')
        return bars

    def observe_historical(self, series, *, source, kind, source_sha256=''):
        """Pin a fetched historical view without replacing current daily prices."""
        from market.price_metadata import observation_metadata
        from market.retire_history import HistoricalFiles
        bars = series.get('bars') or []
        validate(bars)
        if not bars:
            raise ValueError('ohlcv_history_empty')
        source_sha256 = source_sha256 or digest(series)
        with closing(self.connect(write=True)) as db, db:
            db.execute('BEGIN IMMEDIATE')
            revision = db.execute(
                'INSERT INTO price_revisions(observed_at,source,source_sha256,kind,details) VALUES (?,?,?,?,?)',
                (series.get('collected_at') or utc_now(),str(source),source_sha256,kind,
                 packed({'received':len(bars),'mode':'historical_backfill'}))).lastrowid
            ref = HistoricalFiles(self,db,only_identity=identity(series)).reference(series)
            self.read(ref,connection=db)
            db.execute('''INSERT OR REPLACE INTO price_observations
                (series_id,revision,kind,source,source_sha256,reference,metadata) VALUES (?,?,?,?,?,?,?)''',
                (ref['series_id'],revision,kind,str(source),source_sha256,packed(ref),
                 packed(observation_metadata(series))))
            return ref

    def failure(self, category, reason, *, market=None, symbol=None, details=None):
        with closing(self.connect(write=True)) as db, db:
            db.execute('INSERT INTO price_failures(observed_at,market,symbol,category,reason,details) VALUES (?,?,?,?,?,?)',
                       (utc_now(), market, symbol, category, reason, packed(details or {})))

    def pin(self, series):
        """Reference an existing view without correcting the canonical prices during a read."""
        ident, bars = identity(series), series.get('bars') or []
        validate(bars)
        wanted = digest(normalized(bars))
        for candidate in (series.get('bars_ref'),series.get('daily_bars_ref')):
            if (candidate and candidate.get('identity')==ident and candidate.get('count')==len(bars)
                    and candidate.get('numeric_sha256',candidate.get('sha256'))==wanted):
                self.read(candidate)
                return candidate
        with closing(self.connect()) as db:
            db.execute('BEGIN')
            row=db.execute('SELECT id FROM price_series WHERE identity=?',(packed(ident),)).fetchone()
            if not row: raise ValueError('ohlcv_view_reference_unavailable')
            sid=row[0]
            refs=db.execute('SELECT reference FROM price_observations WHERE series_id=? ORDER BY id DESC',(sid,)).fetchall()
            for raw, in refs:
                ref=json.loads(raw)
                if ref.get('numeric_sha256',ref['sha256']) == wanted:
                    return ref
            revision=db.execute('SELECT max(id) FROM price_revisions').fetchone()[0]
            rows=db.execute('SELECT day,open,high,low,close,volume FROM prices WHERE series_id=? AND day>=? AND day<=? ORDER BY day',
                            (sid,bars[0]['date'],bars[-1]['date'])).fetchall() if bars else []
            current=[dict(zip(('date',*FIELDS),r)) for r in rows]
            excluded=sorted(set((series.get('history') or {}).get('quality',{}).get('excluded_dates',[])))
            current=[b for b in current if b['date'] not in excluded]
            reference=dict(series_id=sid,revision=revision,first=bars[0]['date'] if bars else None,
                        last=bars[-1]['date'] if bars else None,count=len(bars),sha256=wanted,
                        numeric_sha256=wanted,identity=ident,excluded_dates=excluded)
            if digest(normalized(current)) == wanted:
                return reference
            daily=series.get('daily_bars_ref')
            if daily and daily.get('identity')==ident and daily.get('first') and bars and bars[0]['date']<daily['first']:
                prefixes=db.execute("SELECT reference FROM price_observations WHERE series_id=? AND kind IN ('archive','selected') ORDER BY id DESC",(sid,)).fetchall()
                for raw, in prefixes:
                    prefix=json.loads(raw)
                    if not prefix.get('first') or prefix['first']>bars[0]['date']:
                        continue
                    composed={**reference,'segments':[prefix,daily]}
                    try:
                        self.read(composed,connection=db)
                        return composed
                    except ValueError:
                        continue
        # A former daily revision can pin the composed archive+daily view as it
        # existed then, including the explicit quality exclusions. No price write.
        for previous in dict.fromkeys(json.loads(r[0])['revision'] for r in refs):
            candidate={**reference,'revision':previous}
            try:
                self.read(candidate)
                return candidate
            except ValueError:
                pass
        raise ValueError('ohlcv_view_reference_unavailable')

    def imported(self, path, sha256):
        if not self.path.exists():
            return False
        with closing(self.connect()) as db:
            row = db.execute('SELECT sha256 FROM price_imports WHERE source=?', (str(path),)).fetchone()
        return bool(row and row[0] == sha256)

    def mark_imported(self, path, sha256):
        with closing(self.connect(write=True)) as db, db:
            db.execute('INSERT OR REPLACE INTO price_imports VALUES (?,?,?)', (str(path), sha256, utc_now()))

    def observation(self, series_id, kind=None):
        with closing(self.connect()) as db:
            clause, args = (' AND kind=?', (series_id, kind)) if kind else ('', (series_id,))
            row = db.execute('SELECT reference,metadata,kind FROM price_observations WHERE series_id=?'+clause+' ORDER BY id DESC LIMIT 1', args).fetchone()
        return (json.loads(row[0]), json.loads(row[1]), row[2]) if row else None

    def daily_seed(self, stock, cutoff, revision=None):
        """Return a provider observation pinned at or before this collection's cutoff."""
        if not self.path.is_file():
            return None
        sid = self.find(stock)
        if sid is None:
            return None
        with closing(self.connect()) as db:
            row = db.execute('''SELECT reference,metadata FROM price_observations
                WHERE series_id=? AND kind IN ('series','benchmark','risk_series','checkpoint')
                AND json_extract(reference,'$.last')<=? AND (? IS NULL OR revision<=?)
                ORDER BY json_extract(reference,'$.last') DESC,revision DESC LIMIT 1''',
                (sid,cutoff,revision,revision)).fetchone()
            if row:
                ref,metadata = map(json.loads,row)
                return {**metadata,'bars':self.read(ref,connection=db),'bars_ref':ref}
        return None

    def source_observation(self, source, kind):
        with closing(self.connect()) as db:
            row=db.execute('SELECT reference,metadata,kind FROM price_observations WHERE source=? AND kind=? ORDER BY id DESC LIMIT 1',
                           (str(source),kind)).fetchone()
        return (json.loads(row[0]),json.loads(row[1]),row[2]) if row else None

    def find(self, stock):
        """An explicit identity conflict never inherits another listing's bars."""
        with closing(self.connect()) as db:
            rows = db.execute('SELECT id,identity FROM price_series WHERE json_extract(identity,\'$.market\')=? AND json_extract(identity,\'$.symbol\')=? AND EXISTS (SELECT 1 FROM price_observations o WHERE o.series_id=price_series.id)',
                              (stock['market'], stock['symbol'])).fetchall()
        candidates = []
        for sid, raw in rows:
            ident = json.loads(raw)
            if any(stock.get(k) is not None and stock[k] != ident[k] for k in ('currency', 'price_basis')):
                continue
            isin = stock.get('isin_code') or (stock.get('listing') or {}).get('isinCode')
            if isin and ident['isin'] and isin != ident['isin']:
                continue
            if stock.get('provider') and identity_provider(stock['provider']) != ident['provider']:
                continue
            candidates.append((sid, bool(isin and isin == ident['isin'])))
        exact = [sid for sid, same in candidates if same]
        if len(exact) == 1:
            return exact[0]
        return candidates[0][0] if len(candidates) == 1 else None


def identity_provider(provider):
    return 'Toss OpenAPI candles' if provider.startswith('Toss') else provider


def normalized(bars):
    return [dict(date=b['date'], **{k: float(b[k]) if b[k] is not None else None for k in FIELDS}) for b in bars]


def freeze_series(store, series, *, source, kind='daily', source_sha256=''):
    value = {k: v for k, v in series.items() if k not in ('bars', 'bars_ref')}
    ref = store.put(series, source=source, source_sha256=source_sha256, kind=kind)
    ref['numeric_sha256'] = digest(normalized(series.get('bars') or []))
    value['bars_ref'] = ref
    return value


def freeze_snapshot(snapshot, path, store=None, *, source_sha256=''):
    """Persist only metadata/references; no raw price arrays in a new snapshot."""
    store = store or PriceStore((snapshot.get('ohlcv_store') or {}).get('path'))
    value = dict(snapshot)
    def freeze(series, kind):
        if 'bars' not in series:
            return series
        if 'bars_ref' in series:
            ref = store.pin(series)
            store.read(ref)
            return {**{k:v for k,v in series.items() if k!='bars'},'bars_ref':ref}
        return freeze_series(store,series,source=str(path),kind=kind,source_sha256=source_sha256)
    for name in ('series', 'risk_series'):
        value[name] = [freeze(s,name) for s in snapshot.get(name, [])]
    value['benchmarks'] = {k:freeze(s,'benchmark') for k,s in snapshot.get('benchmarks',{}).items()}
    value['ohlcv_store'] = {'schema': 1, 'path': str(store.path)}
    return value


def hydrate_snapshot(value):
    if not isinstance(value, dict) or 'ohlcv_store' not in value:
        return value
    from runtime_paths import local_runtime_path
    store = PriceStore(local_runtime_path(value['ohlcv_store']['path']))
    def expand(series):
        return {**series, 'bars': store.read(series['bars_ref'],connection=db)} if 'bars_ref' in series else series
    with closing(store.connect()) as db:
        return {**value, 'series': [expand(s) for s in value.get('series', [])],
                'risk_series': [expand(s) for s in value.get('risk_series', [])],
                'benchmarks': {k: expand(s) for k, s in value.get('benchmarks', {}).items()}}


def thin_snapshot(snapshot, path):
    """Metadata derivations reuse a frozen reference without a second observation."""
    if not isinstance(snapshot, dict) or 'ohlcv_store' not in snapshot:
        return snapshot
    store = PriceStore(snapshot['ohlcv_store']['path'])
    def thin(series):
        if 'bars' not in series:
            return series
        ref = series.get('bars_ref')
        if ref and digest(normalized(series['bars'])) == ref.get('numeric_sha256', ref['sha256']):
            return {k: v for k, v in series.items() if k != 'bars'}
        return freeze_series(store, series, source=str(path))
    return {**snapshot, 'series': [thin(s) for s in snapshot.get('series', [])],
            'risk_series': [thin(s) for s in snapshot.get('risk_series', [])],
            'benchmarks': {k: thin(s) for k, s in snapshot.get('benchmarks', {}).items()}}
