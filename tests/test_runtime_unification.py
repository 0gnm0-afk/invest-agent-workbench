"""Unified source imports and immutable pre-migration calculation receipts."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))

from runtime_paths import local_runtime_path
from market.daily import screen_code_hashes
import atomic_io


class RuntimeUnification(unittest.TestCase):
    def test_local_interpreter_imports_local_package(self):
        import invest_agent
        self.assertTrue(Path(invest_agent.__file__).resolve().is_relative_to(ROOT / 'src'))
        self.assertFalse(any('invest-agent-engine' in p for p in sys.path))

    def test_replay_compares_package_sources_without_accidental_config_boundary(self):
        with tempfile.TemporaryDirectory() as folder:
            current = Path(folder) / 'app'
            old = Path(folder) / 'old-engine'
            code = str(current / 'src/invest_agent/trading/market.py')
            prior_code = str(old / 'src/invest_agent/trading/market.py')
            config_file = str(current / 'config.json')
            ui = str(current / 'scripts/web/server.py')
            current_screen = str(current / 'scripts/screening/engine.py')
            entries = {code: 'a', prior_code: 'b', config_file: 'c', ui: 'd', current_screen: 'e'}
            config = {'engine': str(current), 'records': {'previous_engine_roots': [str(old)]}}
            with patch('market.daily.ROOT', current):
                self.assertEqual(screen_code_hashes(entries, config), {code: 'a', prior_code: 'b', current_screen: 'e'})

    def test_old_source_path_resolves_only_declared_package_and_preserves_hash(self):
        with tempfile.TemporaryDirectory() as folder:
            current = Path(folder) / 'app'
            old = Path(folder) / 'old-engine'
            current.mkdir()
            config = {'records': {'previous_engine_roots': [str(old)]}}
            (current / 'config.json').write_text(json.dumps(config), encoding='utf-8')
            prior = old / 'src/invest_agent/trading/market.py'
            copied = current / 'src/invest_agent/trading/market.py'
            copied.parent.mkdir(parents=True)
            copied.write_bytes(b'original source\n')
            expected = hashlib.sha256(copied.read_bytes()).hexdigest()
            self.assertEqual(local_runtime_path(prior, root=current), copied)
            self.assertEqual(local_runtime_path(old / 'private.txt', root=current), old / 'private.txt')
            with patch('runtime_paths.ROOT', current):
                self.assertEqual(atomic_io.sha(prior), expected)
                copied.write_bytes(b'changed source\n')
                with self.assertRaisesRegex(ValueError, 'input_hash_mismatch'):
                    atomic_io.checked(prior, expected, {})


if __name__ == '__main__':
    unittest.main()
