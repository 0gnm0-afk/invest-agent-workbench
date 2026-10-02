"""토스증권 Open API 공용 헬퍼 — 조회 전용 (주문 API는 이 프로젝트에서 사용하지 않는다).

자격증명: Windows 사용자 환경변수
    TOSS_CLIENT_ID=...
    TOSS_CLIENT_SECRET=...
"""
import json
import math
import tempfile
import threading
from contextlib import contextmanager

from invest_agent.trading.store import instance_lock, BusyError
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = "https://openapi.tossinvest.com"
TOKEN_CACHE = Path.home() / ".tossinvest" / "token_cache.json"
REQUIRED_ENV_VARS = ("TOSS_CLIENT_ID", "TOSS_CLIENT_SECRET")


def utf8_console():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")


def load_env():
    creds = {name: os.getenv(name, "").strip() for name in REQUIRED_ENV_VARS}
    missing = [name for name, value in creds.items() if not value]
    if missing:
        sys.exit(
            "Windows 사용자 환경변수가 설정되지 않았습니다: "
            + ", ".join(missing)
            + "\n환경변수를 등록한 뒤 Codex와 터미널을 완전히 재시작하세요."
        )
    return creds


_TOKEN_LOCK = threading.Lock()


def _cached_token(rejected_token=None):
    try:
        cache = json.loads(TOKEN_CACHE.read_text(encoding="utf-8"))
        value, expiry = cache["access_token"], cache["expires_at"]
        if (isinstance(value, str) and value and value != rejected_token
                and isinstance(expiry, (int, float)) and not isinstance(expiry, bool)
                and math.isfinite(expiry) and expiry > time.time() + 60):
            return value
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


@contextmanager
def _issuance_lock():
    # Serialize threads and processes; a late 401 must reuse another caller's refresh.
    with _TOKEN_LOCK:
        deadline = time.monotonic() + 35
        while True:
            lock = instance_lock(TOKEN_CACHE.parent)
            try:
                lock.__enter__()
                break
            except BusyError:
                if time.monotonic() >= deadline:
                    raise RuntimeError('token_refresh_busy') from None
                time.sleep(.1)
        try:
            yield
        finally:
            lock.__exit__(None, None, None)


def get_token(rejected_token=None):
    value = _cached_token(rejected_token)
    if value:
        return value
    with _issuance_lock():
        value = _cached_token(rejected_token)
        if value:
            return value
        creds = load_env()
        body = urllib.parse.urlencode({
            "grant_type": "client_credentials",
            "client_id": creds["TOSS_CLIENT_ID"],
            "client_secret": creds["TOSS_CLIENT_SECRET"],
        }).encode()
        req = urllib.request.Request(
            f"{BASE}/oauth2/token", data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            token = json.load(resp)
        value, lifetime = token.get('access_token'), token.get('expires_in')
        if (not isinstance(value, str) or not value
                or not isinstance(lifetime, (int, float)) or isinstance(lifetime, bool)
                or not math.isfinite(lifetime) or lifetime <= 60
                or str(token.get('token_type', '')).lower() != 'bearer'):
            raise ValueError('token_metadata_invalid')
        saved = {'access_token': value, 'expires_at': time.time() + lifetime}
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8',
                    dir=TOKEN_CACHE.parent, prefix='token-', suffix='.tmp', delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(saved, stream)
            os.replace(temporary, TOKEN_CACHE)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return value
