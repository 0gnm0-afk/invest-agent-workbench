"""Read-only integrity audit of actual existing analysis evidence; no invented decisions."""
from pathlib import Path
import hashlib
import json
import sqlite3
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from analysis import digest

root=Path(__file__).resolve().parents[1]
path=root/'runtime/chart-workspaces.sqlite3'
before=hashlib.sha256(path.read_bytes()).hexdigest()
db=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)
counts={table:db.execute('SELECT count(*) FROM '+table).fetchone()[0] for table in ('analysis_cases','analysis_snapshots','analysis_items','shared_chart_workspaces')}
cases=[json.loads(r[0]) for r in db.execute('SELECT payload FROM analysis_cases')]
snapshots=0
for payload,expected in db.execute('SELECT payload,sha256 FROM analysis_snapshots'):
    assert digest(json.loads(payload))==expected
    snapshots+=1
documents=0
for payload, in db.execute('SELECT payload FROM analysis_items'):
    row=json.loads(payload)
    if row.get('markdown_path'):
        doc=(root/row['markdown_path']).resolve()
        assert doc.is_relative_to(root)
        assert hashlib.sha256(doc.read_bytes()).hexdigest()==row['markdown_sha256']
        documents+=1
db.close()
after=hashlib.sha256(path.read_bytes()).hexdigest()
assert before==after
result=dict(mode='actual_saved_read_only',counts=counts,verified_snapshots=snapshots,verified_documents=documents,
    real_stock_keys=sorted({c['stock_key'] for c in cases if not c.get('test_record')}),database_sha256=after,
    database_unchanged=True,new_user_statements=0,new_trades=0,new_market_requests=0,real_telegram_messages=0)
output=root/'runtime/scenario-monitor-verification';output.mkdir(parents=True,exist_ok=True)
(output/'actual-saved-integrity.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(result,ensure_ascii=False))
