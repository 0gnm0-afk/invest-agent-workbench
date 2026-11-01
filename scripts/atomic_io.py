"""Atomic runtime publication resilient to short Windows reader locks."""
import os
import hashlib
import json
import tempfile
import time
from pathlib import Path


def atomic_write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        for attempt in range(9):
            try:
                os.replace(temporary, path)
                return
            except PermissionError:
                if attempt == 8:
                    raise
                time.sleep(min(.05 * 2 ** attempt, .25))
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def load(path):
    return _hydrate(json.loads(Path(path).read_text(encoding='utf-8')))


def _hydrate(value):
    if isinstance(value, dict) and ('account_store_ref' in value or 'account_view_ref' in value):
        from accounts.store import hydrate
        return hydrate(value)
    if isinstance(value, dict) and 'ohlcv_store' in value:
        from market.prices import hydrate_snapshot
        return hydrate_snapshot(value)
    return value

def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()

def write(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')

def checked(path, expected, inputs):
    path = Path(path).resolve()
    actual = sha(path)
    if expected is not None and actual != expected:
        raise ValueError('input_hash_mismatch:' + str(path))
    inputs[str(path)] = actual
    return path


def write_json(path, value):
    if isinstance(value, dict) and 'ohlcv_store' in value:
        from market.prices import thin_snapshot
        value = thin_snapshot(value, path)
    atomic_write(path, json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))


def load_bom(path):
    """Read existing dashboard/account receipts, including UTF-8 BOM files."""
    return _hydrate(json.loads(Path(path).read_text(encoding='utf-8-sig')))


def checked_saved(path, expected):
    if not expected or sha(path) != expected:
        raise ValueError('saved_artifact_hash_mismatch:' + Path(path).name)
    return load_bom(path)


def write_new_json(path, payload):
    """Create an immutable receipt; never overwrite an earlier file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x', encoding='utf-8') as stream:
        json.dump(payload, stream, ensure_ascii=False, allow_nan=False)


def utc_now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def load_json_bytes(path):
    """Preserve the research readers' JSON byte-encoding detection."""
    return json.loads(Path(path).read_bytes())
