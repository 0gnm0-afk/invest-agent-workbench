"""Discover/reopen the private loopback dashboard, never collect on startup."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request
import urllib.parse

ROOT=Path(__file__).resolve().parents[1]
ENDPOINT=ROOT/'runtime/dashboard/analysis-endpoint.json'


def endpoint():
    value=json.loads(ENDPOINT.read_text(encoding='utf-8-sig'))
    url=urllib.parse.urlsplit(value['url'])
    if url.scheme!='http' or url.hostname!='127.0.0.1' or not url.port or url.path or url.query or url.fragment or url.username: raise ValueError('invalid_local_endpoint')
    with urllib.request.urlopen(value['url']+'/health',timeout=3) as r: health=json.load(r)
    if health.get('analysis_protocol')!=1 or health.get('workspace')!=str(ROOT): raise ValueError('incompatible_dashboard')
    return {'url':value['url'],'token':health['token'], 'analysis_capabilities':value.get('analysis_capabilities', {}),
            'collaboration_protocol':health.get('collaboration_protocol')}


def ensure():
    try: return endpoint()
    except (OSError,ValueError,KeyError): pass
    ENDPOINT.parent.mkdir(parents=True,exist_ok=True)
    # Shared existing lock primitive prevents concurrent MCP/launcher starts.
    from financial_store import lock_file,unlock
    lease=lock_file(ENDPOINT.parent/'analysis-start.lock')
    if lease:
        try:
            try: return endpoint()
            except (OSError,ValueError,KeyError): pass
            log=(ENDPOINT.parent/'analysis-server.log').open('ab')
            try:
                subprocess.Popen([sys.executable,'-u',str(ROOT/'scripts/dashboard.py'),'--saved-accounts','--ready-file',str(ENDPOINT)],cwd=ROOT,stdin=subprocess.DEVNULL,stdout=log,stderr=log,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            finally: log.close()
            for _ in range(480):
                try: return endpoint()
                except (OSError,ValueError,KeyError): time.sleep(.5)
            raise ValueError('dashboard_start_timeout')
        finally: unlock(lease)
    for _ in range(480):
        try: return endpoint()
        except (OSError,ValueError,KeyError): time.sleep(.5)
    raise ValueError('dashboard_start_timeout')


def call(op, arguments):
    target=ensure()
    role = os.environ.get('INVEST_ANALYSIS_ROLE', 'collaboration')
    if role not in ('collaboration', 'risk'): raise ValueError('invalid_analysis_role')
    capability = target.get('analysis_capabilities', {}).get(role)
    if not capability:
        # Copying local code must not restart or break the currently running legacy server.
        legacy = {'list','detail','read','record','propose','calculate','holding_links','refresh'}
        readonly = {'list','detail','holding_links','calculate'}
        permitted = op in legacy if role == 'collaboration' else op in readonly or (op == 'read' and bool(arguments.get('snapshot_id')))
        if not permitted:
            raise ValueError('collaboration_server_upgrade_required_no_automatic_restart')
        capability = target['token']
    req=urllib.request.Request(target['url']+'/api/analysis-rpc',data=json.dumps({'op':op,'arguments':arguments},allow_nan=False).encode(),headers={'Content-Type':'application/json','Origin':target['url'],'X-Workbench-Token':capability})
    try:
        with urllib.request.urlopen(req,timeout=60) as r: return json.load(r)
    except urllib.error.HTTPError as e:
        try: message=json.load(e).get('error','analysis_request_failed')
        except ValueError: message='analysis_request_failed'
        raise ValueError(message) from None


if __name__=='__main__':
    import argparse,webbrowser
    parser=argparse.ArgumentParser(); parser.add_argument('--analysis-key',required=True); parser.add_argument('--no-browser',action='store_true'); a=parser.parse_args()
    detail=call('detail',{'analysis_key':a.analysis_key})
    url=detail['links']['dashboard_url']
    if not a.no_browser: webbrowser.open(url)
    print(url)
