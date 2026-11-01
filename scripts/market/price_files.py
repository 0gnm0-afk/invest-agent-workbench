"""Recover retired price JSON files exactly from metadata and pinned OHLCV rows.

Preparation does not delete files or import/correct prices. A file is eligible
only after its original bytes can be reconstructed with the original SHA-256.
"""
if not __package__:
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from contextlib import closing
from copy import deepcopy
import hashlib
import json
import os
import re
from pathlib import Path
import zlib
from market.price_receipts import packed_record, packed_exists, save_one

from market.prices import PriceStore, default_path, digest, identity, normalized, FIELDS


def file_key(path):
    return os.path.normcase(str(Path(path).resolve()))


class PriceFiles:
    def __init__(self, store=None):
        self.store = store or PriceStore(default_path())
        self.references = None

    def connect(self):
        return self.store.connect()

    def _references(self):
        if self.references is None:
            self.references = {}
            with closing(self.store.connect()) as db:
                for raw, in db.execute('SELECT reference FROM price_observations'):
                    ref = json.loads(raw)
                    ident = ref['identity']
                    key = (*(ident[k] for k in ('market','symbol','currency','provider','price_basis')),
                           ref['count'], ref['first'], ref['last'])
                    self.references.setdefault(key, []).append(ref)
        return self.references

    def reference(self, series):
        bars = series['bars']
        ident = identity(series)
        key = (*(ident[k] for k in ('market','symbol','currency','provider','price_basis')),
               len(bars), bars[0]['date'], bars[-1]['date'])
        candidates = self._references().get(key, [])
        matches = {}
        for ref in candidates:
            other = ref['identity']
            if any(ident[k] != other[k] for k in ident if k != 'isin'):
                continue
            if ident['isin'] and ident['isin'] != other['isin']:
                continue
            matches[(ref['series_id'], ref.get('numeric_sha256',ref['sha256']))] = ref
        if len(matches) > 1:
            wanted = digest(normalized(bars))
            matches = {k:v for k,v in matches.items() if k[1] == wanted}
        if len(matches) != 1:
            raise ValueError('price_file_reference_unavailable:' + series['symbol'])
        # The whole reconstructed file is checked against its original byte
        # hash, including these bars, metadata, number types and formatting.
        return next(iter(matches.values()))

    @staticmethod
    def _at(value, path):
        for key in path:
            value = value[key]
        return value

    def prepare(self, path):
        return self.prepare_bytes(path,Path(path).read_bytes())

    def prepare_bytes(self, path, raw):
        path = Path(path).resolve()
        expected = hashlib.sha256(raw).hexdigest()
        size = len(raw)
        row = self._record(path)
        if row:
            if row[0] != expected:
                raise ValueError('price_file_source_changed')
            if self.recover(path) != raw:
                raise ValueError('price_file_byte_roundtrip_mismatch')
            recipe = json.loads(zlib.decompress(row[3]))
            return dict(path=str(path),sha256=expected,bytes=size,recipe_bytes=len(row[3]),
                        series=len(recipe['patches']),bars=sum(p['reference']['count'] for p in recipe['patches']))
        bom = raw.startswith(b'\xef\xbb\xbf')
        text = raw.decode('utf-8-sig')
        value = json.loads(text)
        crlf = '\r\n' in text
        suffix = text[len(text.rstrip()):].replace('\r\n','\n')
        indentation = re.match(r'^\{\r?\n( +)"', text)
        encoder = dict(ensure_ascii=text.isascii(), indent=len(indentation[1]) if indentation else None, allow_nan=False)
        if not indentation and re.match(r'^\{"[^"]+":(?! )',text):
            encoder['separators'] = (',', ':')
        del raw, text
        return self._prepare_value(path,value,encoder,suffix,bom,crlf,expected,size)

    def _prepare_value(self,path,value,encoder,suffix,bom,crlf,expected,size,load_bars=None):
        paths = []
        if isinstance(value, dict):
            if 'bars' in value and 'market' in value and 'symbol' in value:
                paths.append([])
            paths.extend([key] for key,row in value.items() if isinstance(row,dict) and 'bars' in row
                         and key==str(row.get('market'))+':'+str(row.get('symbol')))
            for name in ('series', 'risk_series'):
                if isinstance(value.get(name), list):
                    paths.extend([name, i] for i, row in enumerate(value[name]) if 'bars' in row)
            paths.extend(['benchmarks', k] for k, row in value.get('benchmarks', {}).items() if 'bars' in row)
            if isinstance(value.get('data'), dict) and 'bars' in value['data']:
                paths.append(['data'])
            if isinstance(value.get('payload'), dict) and 'bars' in value['payload']:
                paths.append(['payload'])
        if not paths:
            raise ValueError('price_file_format_not_supported')
        patches = []
        count = 0
        for address in paths:
            series = self._at(value, address)
            if load_bars:
                series['bars']=load_bars(series['bars'])
            bars = series['bars']
            if not bars:
                continue
            ref = self.reference(series)
            order = list(bars[0])
            if any(set(b) != {'date', *FIELDS} for b in bars):
                raise ValueError('price_file_bar_shape_not_supported')
            integers = [[i, k] for i, b in enumerate(bars) for k in FIELDS if type(b[k]) is int]
            overrides = [[i,list(b)] for i,b in enumerate(bars) if list(b) != order]
            patches.append(dict(path=address, reference=ref, order=order, integers=integers,
                                order_overrides=overrides))
            count += len(bars)
            series['bars'] = []
        recipe = dict(schema=1, value=value, patches=patches, encoder=encoder, suffix=suffix, bom=bom, crlf=crlf)
        self.check_recipe(recipe,size,expected)
        body = json.dumps(recipe, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')
        compressed = zlib.compress(body)
        self.save_record(path, expected, size, hashlib.sha256(body).hexdigest(), compressed)
        return dict(path=str(path), sha256=expected, bytes=size, recipe_bytes=len(compressed),
                    series=len(patches), bars=count)

    def check_recipe(self, recipe, size, expected):
        restored = self._restore(recipe)
        if len(restored)!=size or hashlib.sha256(restored).hexdigest()!=expected:
            raise ValueError('price_file_byte_roundtrip_mismatch')

    def save_record(self, path, expected, size, recipe_hash, compressed):
        save_one(self.store.path.parent,path,expected,size,recipe_hash,compressed)

    def reader(self):
        return closing(self.store.connect())

    def _expand(self, recipe):
        value = deepcopy(recipe['value'])
        with self.reader() as db:
            for patch in recipe['patches']:
                # The outer original-file SHA verifies every restored value.
                bars = self.store.read(patch['reference'], verify=False, connection=db)
                for i, key in patch['integers']:
                    original = bars[i][key]
                    if original is None or int(original) != original:
                        raise ValueError('price_file_integer_mismatch')
                    bars[i][key] = int(original)
                orders = dict(patch.get('order_overrides', []))
                self._at(value, patch['path'])['bars'] = [{k: b[k] for k in orders.get(i,patch['order'])}
                                                        for i,b in enumerate(bars)]
        return value

    @staticmethod
    def _encode(value, recipe):
        raw = (json.dumps(value, **recipe['encoder']) + recipe['suffix']).encode('utf-8')
        if recipe.get('crlf'):
            raw = raw.replace(b'\n', b'\r\n')
        return (b'\xef\xbb\xbf' if recipe['bom'] else b'') + raw

    def _restore(self, recipe):
        return self._encode(self._expand(recipe),recipe)

    def market_snapshot(self, path, expected):
        """Return a verified market view with the same pinned bar references used in replay."""
        from atomic_io import checked_saved,sha
        record = self._record(path)
        if record is None:
            return checked_saved(path,expected)
        source,size,recipe_hash,compressed = record
        if source!=expected or (Path(path).is_file() and sha(path)!=expected):
            raise ValueError('saved_artifact_hash_mismatch:'+Path(path).name)
        body=zlib.decompress(compressed)
        if hashlib.sha256(body).hexdigest()!=recipe_hash:
            raise ValueError('price_file_recipe_hash_mismatch')
        recipe=json.loads(body)
        value=self._expand(recipe)
        raw=self._encode(value,recipe)
        if len(raw)!=size or hashlib.sha256(raw).hexdigest()!=expected:
            raise ValueError('price_file_recovery_hash_mismatch')
        for patch in recipe['patches']:
            self._at(value,patch['path'])['bars_ref']=patch['reference']
        value['ohlcv_store']={'schema':1,'path':str(self.store.path)}
        return value

    def _record(self, path):
        record = packed_record(self.store.path.parent, path)
        if record is not None:
            return record
        if not self.store.path.is_file():
            return None
        with closing(self.connect()) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='price_files'").fetchone():
                return None
            return db.execute('SELECT source_sha256,original_bytes,recipe_sha256,recipe FROM price_files WHERE path=?',
                              (file_key(path),)).fetchone()

    def recover(self, path):
        row = self._record(path)
        if row is None:
            raise FileNotFoundError(path)
        expected, size, recipe_hash, compressed = row
        body = zlib.decompress(compressed)
        if hashlib.sha256(body).hexdigest() != recipe_hash:
            raise ValueError('price_file_recipe_hash_mismatch')
        raw = self._restore(json.loads(body))
        if len(raw) != size or hashlib.sha256(raw).hexdigest() != expected:
            raise ValueError('price_file_recovery_hash_mismatch')
        return raw

    def contains(self, path):
        if packed_exists(self.store.path.parent, path):
            return True
        if not self.store.path.is_file():
            return False
        with closing(self.connect()) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='price_files'").fetchone():
                return False
            return bool(db.execute('SELECT 1 FROM price_files WHERE path=?', (file_key(path),)).fetchone())


def recover_bytes(path):
    return PriceFiles().recover(path)


def available(path):
    return Path(path).is_file() or PriceFiles().contains(path)


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--restore', required=True, type=Path, help='Original retired JSON path')
    parser.add_argument('--output', type=Path, help='Destination; defaults to the original path; never overwrites')
    args = parser.parse_args()
    content = PriceFiles().recover(args.restore)
    destination = args.output or args.restore
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open('xb') as stream:
        stream.write(content)
    print(json.dumps(dict(restored=str(destination),bytes=len(content),sha256=hashlib.sha256(content).hexdigest())))
