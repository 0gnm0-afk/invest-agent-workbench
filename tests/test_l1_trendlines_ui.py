"""Packaging contract; behavioral JS runs as a direct M00 guarded Node node."""
from pathlib import Path
import unittest


class L1UIContract(unittest.TestCase):
    def test_standalone_fragment_has_no_storage_or_transport(self):
        root = Path(__file__).resolve().parents[1]
        source = (root / 'dashboard/drawings/l1-trendlines.js').read_text(encoding='utf-8')
        self.assertIn('window.WorkbenchL1Trendlines', source)
        for forbidden in ('fetch(', 'localStorage', 'sessionStorage', '/api/', 'getDrawings', 'onChange', 'JSON.stringify'):
            self.assertNotIn(forbidden, source)
        suite = (root / 'tests/suites/M06.txt').read_text(encoding='utf-8').splitlines()
        self.assertIn('tests/test_l1_trendlines_ui.cjs', suite)
