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
    return json.loads(Path(path).read_text(encoding='utf-8'))

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
    atomic_write(path, json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))
