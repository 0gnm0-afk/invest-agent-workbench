"""Historical run metadata belongs with run evidence. Price values live in PriceStore."""
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import struct
import tempfile
import zipfile
import zlib

from runtime_paths import local_runtime_path, runtime_lookup_paths


def address(runtime, path, *, original_path=None):
    path = Path(path).resolve()
    from workbench_paths import ROOT
    if path.is_relative_to(ROOT/'runtime'):
        runtime=ROOT/'runtime'
    try:
        relative = path.relative_to(runtime)
    except ValueError:
        return None, None
    depth = 3 if (len(relative.parts)>=3 and relative.parts[0] in ('kr-history','us-history')
                  and relative.parts[2]=='pre-local-date-normalization') else 2
    group = '/'.join(relative.parts[:depth])
    key = Path(original_path).resolve() if original_path is not None else path
    name = hashlib.sha256(os.path.normcase(str(key)).encode()).hexdigest()
    return runtime/'price-run-receipts'/(hashlib.sha256(group.encode()).hexdigest()+'.zip'), name


@lru_cache(maxsize=8)
def _entries(path, timestamp, size):
    with zipfile.ZipFile(path) as z:
        return z.NameToInfo.copy()


def _entry(runtime, source, *, root=None):
    physical = local_runtime_path(Path(source).resolve(), root=root)
    for original in runtime_lookup_paths(source, root=root):
        path, name = address(runtime, physical, original_path=original)
        if path is None or not path.is_file():
            continue
        stat = path.stat()
        info = _entries(path, stat.st_mtime_ns, stat.st_size).get(name)
        if info is not None:
            return path, info
    return None


def packed_exists(runtime, source, *, root=None):
    return _entry(runtime, source, root=root) is not None


def packed_record(runtime, source, *, root=None):
    entry = _entry(runtime, source, root=root)
    if entry is None:
        return None
    path, info = entry
    with path.open('rb') as stream:
        stream.seek(info.header_offset)
        local = stream.read(30)
        if local[:4] != b'PK\x03\x04' or info.compress_type != zipfile.ZIP_STORED:
            raise ValueError('invalid_price_receipt_pack')
        stream.seek(sum(struct.unpack('<HH',local[26:30])),1)
        value = stream.read(info.file_size)
    if zlib.crc32(value) != info.CRC:
        raise ValueError('price_receipt_crc_mismatch')
    header, body = value.split(b'\n',1)
    h = json.loads(header)
    return h['sha256'],h['bytes'],h['recipe_sha256'],body


def save_one(runtime, source, expected, size, recipe_hash, compressed):
    from invest_agent.trading.store import instance_lock
    path,name = address(runtime,source)
    if path is None:
        raise ValueError('price_receipt_outside_runtime')
    path.parent.mkdir(parents=True,exist_ok=True)
    header=json.dumps(dict(path=str(source),sha256=expected,bytes=size,recipe_sha256=recipe_hash)).encode()
    with instance_lock(path.parent/(path.stem+'.lock')):
        prior=packed_record(runtime,source)
        if prior:
            if prior[0]!=expected:raise ValueError('price_file_source_changed')
            return
        fd,temp=tempfile.mkstemp(dir=path.parent)
        os.close(fd)
        try:
            with zipfile.ZipFile(temp,'w',compression=zipfile.ZIP_STORED) as dst:
                if path.exists():
                    with zipfile.ZipFile(path) as src:
                        for n in src.namelist():dst.writestr(n,src.read(n))
                dst.writestr(name,header+b'\n'+compressed)
            os.replace(temp,path)
        finally:
            Path(temp).unlink(missing_ok=True)
