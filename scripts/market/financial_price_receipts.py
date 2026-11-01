"""Keep immutable financial inputs reproducible using the shared OHLCV store."""
from contextlib import closing, nullcontext
import hashlib
import json
from pathlib import Path

from market.price_files import PriceFiles
from market.prices import FIELDS, identity
from market.retire_history import HistoricalFiles


MARKER = 'financial-price-input-v1'


def canonical_input(stock):
    return (isinstance(stock, dict) and stock.get('market') in ('KR', 'US')
            and all(stock.get(k) for k in ('symbol', 'provider', 'price_basis', 'currency', 'bars'))
            and all(isinstance(b, dict) and set(b) == {'date', *FIELDS} for b in stock['bars']))


class FinancialPriceFiles(PriceFiles):
    """Price references never update a current price or rewrite a previous financial input."""
    def __init__(self, store, *, allow_variants=False):
        super().__init__(store)
        self.allow_variants = allow_variants
        self.db = None
        self.history = None

    def reference(self, series):
        # Older financial views can omit the ISIN even when the archived series
        # recorded it. Only inherit one after the entire requested range matches.
        ident = identity(series)
        supplied = series.get('bars_ref')
        if isinstance(supplied,dict) and self.store.path.is_file():
            supplied_ident=supplied.get('identity') or {}
            same_instrument=all(ident[k]==supplied_ident.get(k) for k in ident if k!='isin')
            if same_instrument and (not ident['isin'] or ident['isin']==supplied_ident.get('isin')):
                if self.store.read(supplied)==series['bars']:
                    return supplied
        candidates = [series]
        if self.store.path.is_file() and not ident['isin']:
            with closing(self.store.connect()) as db:
                matches = db.execute(
                    "SELECT identity FROM price_series WHERE json_extract(identity,'$.market')=? AND json_extract(identity,'$.symbol')=?",
                    (ident['market'], ident['symbol'])).fetchall()
            candidates += [{**series,'isin_code':i['isin']} for raw, in matches for i in [json.loads(raw)]
                           if i['isin'] and all(i[k]==ident[k] for k in ident if k!='isin')]
        matches = {}
        if self.store.path.is_file():
            for candidate in candidates:
                try:
                    ref = self.store.pin(candidate)
                except ValueError as exc:
                    if str(exc) != 'ohlcv_view_reference_unavailable':
                        raise
                    continue
                matches[ref['series_id']] = ref
        if len(matches)==1:
            return next(iter(matches.values()))
        if not self.allow_variants:
            raise ValueError('financial_price_version_unavailable')
        if self.db is None:
            self.db = self.store.connect(write=True)
            self.db.execute('BEGIN IMMEDIATE')
            self.history = HistoricalFiles(self.store,self.db,only_identity=ident)
        return self.history.reference(series)

    def reader(self):
        return nullcontext(self.db) if self.db is not None else super().reader()

    def save_record(self,path,expected,size,recipe_hash,compressed):
        if self.db is not None:
            self.db.commit()
        super().save_record(path,expected,size,recipe_hash,compressed)

    def close(self):
        if self.db is not None:
            if self.history is not None:
                self.history.year.cache_clear()
            self.db.close()
            self.db = None
            self.history = None


def price_input_bytes(path, store):
    path=Path(path)
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return PriceFiles(store).recover(path)


def read_price_input(raw, path, store):
    value=json.loads(raw)
    if isinstance(value,dict) and value.get('schema')==MARKER:
        body=PriceFiles(store).recover(path)
        if hashlib.sha256(body).hexdigest()!=value.get('input_sha256'):
            raise ValueError('saved_input_hash_mismatch')
        return json.loads(body)
    return value


def pack_price_input(stock,path,store):
    if not canonical_input(stock):
        return stock
    files=FinancialPriceFiles(store,allow_variants=True)
    try:
        # FinancialStore's original format is UTF-8 JSON, sorted keys, default separators.
        raw=json.dumps(stock,ensure_ascii=False,sort_keys=True,allow_nan=False).encode('utf-8')
        receipt=files.prepare_bytes(path,raw)
        return {'schema':MARKER,'input_sha256':receipt['sha256']}
    finally:
        files.close()
