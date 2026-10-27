"""Shared analysis contracts: stable IDs, canonical JSON/hashes and denial errors.

service coordinates input/report work; collaboration owns discussion/plan versions;
report renders saved records. This module imports neither workflow.
"""
from datetime import datetime, timezone
import hashlib
import json
import re
import uuid


class Forbidden(ValueError):
    pass


def packed(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def digest(value): return hashlib.sha256(packed(value).encode()).hexdigest()
def now(): return datetime.now(timezone.utc).isoformat()
def ident(prefix): return prefix + uuid.uuid4().hex


def key(value, prefix):
    if not isinstance(value, str) or not re.fullmatch(prefix + '[a-f0-9]{32}', value): raise ValueError('invalid_identifier')
    return value
