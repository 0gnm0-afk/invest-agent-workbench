"""The account adapter must not redirect subsequent chart/market imports."""
import json
from pathlib import Path
import subprocess
import sys
import unittest


class AdapterImportBoundary(unittest.TestCase):
    def test_account_then_chart_keeps_workbench_modules(self):
        root = Path(__file__).resolve().parents[1]
        config = json.loads((root / 'config.json').read_text(encoding='utf-8-sig'))
        script = r'''
import sys, tempfile
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, str(Path(sys.argv[1])/'scripts'))
sys.path.insert(0, str(Path(sys.argv[2])/'src'))
import dashboard_data as d
from chart_history import HistoryStore
with tempfile.TemporaryDirectory() as tmp:
    mother=Path(tmp)/'mother'; adapters=mother/'scripts'; adapters.mkdir(parents=True)
    for name, source in {
        'nh_snapshot':'normalize=None', 'nh_collect':'pages=None',
        'nh_api':'read=None\nclass NHError(Exception): pass',
        'nh_diagnostics':'successful=None', 'account_stops':'attach_stops=None',
        'market_quality':"raise AssertionError('legacy market module shadowed workbench')"
    }.items():
        (adapters/(name+'.py')).write_text(source, encoding='utf-8')
    output=Path(tmp)/'output'; output.mkdir()
    assert d.account_view({'mother':str(mother)},output,refresh=False)['query_state']=='not_requested'
    store=HistoryStore(Path(tmp)/'history.sqlite3')
    stock={'market':'US','symbol':'IMPORT-TEST','bars':[{'date':'2026-09-30','open':10,'high':12,'low':9,'close':11,'volume':100}]}
    store.saved=SimpleNamespace(packet=lambda _: {'series':stock,'history':{'state':'complete'}})
    result=store.ensure(stock)
    assert result['bars']==stock['bars']
    import market_quality
    assert Path(market_quality.__file__).parent==Path(sys.argv[1])/'scripts'
'''
        result = subprocess.run([sys.executable, '-c', script, str(root), config['engine']],
                                capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
