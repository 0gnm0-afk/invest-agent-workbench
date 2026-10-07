"""Transient read-only bridge. Does not start services or persist responses."""
import json
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, build_opener, ProxyHandler

URL = 'http://127.0.0.1:8769'
ROOT = Path(__file__).resolve().parents[1]


def call(op, args):
    if op not in ('status','dialogs','search'):
        return {'error':'read_only_operation'}
    opener = build_opener(ProxyHandler({}))
    try:
        with opener.open(URL+'/health',timeout=3) as response:
            health = json.load(response)
        if health.get('app') != 'telegram-news-1' or Path(health.get('root','')).resolve() != ROOT:
            return {'error':'connector_identity_mismatch'}
        request = Request(URL+'/mcp',data=json.dumps({'op':op,'args':args}).encode('utf-8'),
                          headers={'Content-Type':'application/json','Authorization':'Bearer '+health['read_token']})
        with opener.open(request,timeout=55) as response:
            return json.load(response)
    except (URLError,TimeoutError,ValueError,KeyError,OSError):
        return {'error':'telegram_connector_unavailable', 'login_url':URL,
                'action':'워크벤치 telegram-news.ps1을 실행하고 로컬 화면에서 로그인하세요. 뉴스나 인증값을 채팅에 붙이지 마세요.'}
