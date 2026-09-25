"""토스증권 Open API 공용 헬퍼 — 조회 전용 (주문 API는 이 프로젝트에서 사용하지 않는다).

자격증명: Windows 사용자 환경변수
    TOSS_CLIENT_ID=...
    TOSS_CLIENT_SECRET=...
"""
import json
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


def get_token():
    # 토큰은 24시간 유효 — 캐시해서 불필요한 재발급을 피한다
    if TOKEN_CACHE.exists():
        try:
            cache = json.loads(TOKEN_CACHE.read_text(encoding="utf-8"))
            if cache.get("expires_at", 0) > time.time() + 60:
                return cache["access_token"]
        except (json.JSONDecodeError, KeyError):
            pass
    creds = load_env()
    body = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": creds["TOSS_CLIENT_ID"],
        "client_secret": creds["TOSS_CLIENT_SECRET"],
    }).encode()
    req = urllib.request.Request(
        f"{BASE}/oauth2/token",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        token = json.load(resp)
    TOKEN_CACHE.write_text(
        json.dumps({
            "access_token": token["access_token"],
            "expires_at": time.time() + token.get("expires_in", 3600),
        }),
        encoding="utf-8",
    )
    return token["access_token"]


