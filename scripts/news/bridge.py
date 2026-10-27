"""Transient read bridge; starts the connector on demand for saved sessions."""
import json
from pathlib import Path
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import Request, build_opener, ProxyHandler

URL = 'http://127.0.0.1:8769'
from workbench_paths import ROOT


def health(opener):
    with opener.open(URL+'/health',timeout=3) as response:
        return json.load(response)


def ensure_health(opener):
    try:
        return health(opener)
    except URLError as exc:
        reason=getattr(exc,'reason',None)
        refused=isinstance(reason,ConnectionRefusedError) or getattr(reason,'errno',None) in (61,111,10061)
        if not refused or not (ROOT/'secrets/telegram-news-session.dpapi.json').is_file():
            raise
    # Only an absent listener with saved authorization permits on-demand startup.
    # Port binding serializes simultaneous callers; losing processes exit before restore.
    subprocess.Popen([sys.executable,'-B','-X','utf8',str(ROOT/'scripts/telegram_news_server.py')],
        cwd=ROOT,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0),close_fds=True)
    for _ in range(50):
        try:return health(opener)
        except URLError:time.sleep(.1)
    raise TimeoutError()


def call(op, args):
    if op not in ('status','dialogs','search'):
        return {'error':'read_only_operation'}
    opener = build_opener(ProxyHandler({}))
    try:
        found = ensure_health(opener)
        if found.get('app') != 'telegram-news-1' or Path(found.get('root','')).resolve() != ROOT:
            return {'error':'connector_identity_mismatch'}
        request = Request(URL+'/mcp',data=json.dumps({'op':op,'args':args}).encode('utf-8'),
                          headers={'Content-Type':'application/json','Authorization':'Bearer '+found['read_token']})
        with opener.open(request,timeout=55) as response:
            return json.load(response)
    except (URLError,TimeoutError,ValueError,KeyError,OSError):
        return {'error':'telegram_connector_unavailable', 'login_url':URL,
                'action':'워크벤치 telegram-news.ps1 -NoBrowser로 연결 상태를 확인하세요. 저장 세션은 자동 복원하며 세션이 없거나 무효일 때만 로컬 인증이 필요합니다. 인증값을 채팅에 붙이지 마세요.'}
