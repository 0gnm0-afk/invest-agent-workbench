"""Retained daily collection input and report checks; selection tests retired by M01."""
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import market.daily as daily


class FailureTest(unittest.TestCase):
    def test_input_tamper_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp)/'input.json'
            file.write_text('{}')
            with self.assertRaisesRegex(ValueError, 'input_hash_mismatch'):
                daily.check_hashes({str(file):'wrong'})

    def test_report_escapes_failure_text(self):
        run = dict(state='failed', processing_state='failed', mode='replay', started_at='now',
                   stages={'collection':'failed'}, error='<script>fixture</script>')
        with tempfile.TemporaryDirectory() as tmp:
            daily.render(run, Path(tmp))
            page = (Path(tmp)/'report.html').read_text(encoding='utf-8')
            self.assertNotIn('<script>', page)
            self.assertIn('&lt;script&gt;', page)
            self.assertNotIn('candidates.csv', page)
