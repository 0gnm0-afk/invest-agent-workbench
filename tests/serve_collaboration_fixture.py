"""Synthetic UI fixture. Never constructs real history/financial/account adapters."""
import json
import os
from pathlib import Path
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_collaboration import CollaborationTests, ROOT
from web.server import handler_for, ThreadingHTTPServer


def main():
    fixture=CollaborationTests();fixture.setUp()
    fixture.discussion();fixture.plan()
    assets=fixture.root/'assets';assets.mkdir()
    for name in ('analysis/collaboration.js','format.js','style.css','drawings/chart-tools.css'):
        (assets/name).parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(ROOT/'dashboard'/name,assets/name)
    cid=fixture.cid
    (assets/'index.html').write_text('''<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="stylesheet" href="/style.css"><link rel="stylesheet" href="/chart-tools.css"><script defer src="/format.js"></script><script defer src="/collaboration.js"></script><script defer src="/app.js"></script>
<body class="workbench-dark"><main style="max-width:1000px;margin:30px auto;padding:20px"><h1>합성 종목 · 후속 협업 격리 검증</h1><p>실제 기업·계좌·시장과 연결되지 않은 테스트 화면</p><section id="anchor"></section></main></body></html>''',encoding='utf-8')
    script='''async function rpc(op,args){const h=await fetch('/health');const {token}=await h.json();const r=await fetch('/api/analysis-rpc',{method:'POST',headers:{'Content-Type':'application/json','X-Workbench-Token':token},body:JSON.stringify({op,arguments:args})});const v=await r.json();if(!r.ok)throw Error(v.error);return v;}
async function tick(){const id=CASE;const detail=await rpc('detail',{analysis_key:id});await WorkbenchCollaboration.show(document.querySelector('#anchor'),id,rpc,detail.links);setTimeout(tick,2000);}tick();'''.replace('CASE',json.dumps(cid))
    (assets/'app.js').write_text(script,encoding='utf-8')
    server=ThreadingHTTPServer(('127.0.0.1',0),handler_for(fixture.data,{'US:SYNTH':fixture.stock},assets,'synthetic-browser',fixture.store.path,
        analysis_capabilities={'collaboration':'synthetic-collaboration','risk':'synthetic-risk'},analysis_dependencies={'histories':fixture.hist,'financials':fixture.fin}))
    print(json.dumps(dict(url='http://127.0.0.1:'+str(server.server_port),analysis_key=cid,isolated_root=str(fixture.root),pid=os.getpid())),flush=True)
    try:server.serve_forever()
    finally:server.server_close();fixture.tearDown()


if __name__=='__main__':main()
