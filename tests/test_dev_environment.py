import hashlib
import importlib
import json
from pathlib import Path
import struct
import sys
import unittest

import isolation
from setup_dev_environment import check_manifest, installed_packages, canonical, lock_digest


class DevEnvironment(unittest.TestCase):
    def test_independent_base(self):
        spec = check_manifest(Path(isolation.ACTIVE.config['code']))
        self.assertEqual(struct.calcsize('P') * 8, spec['python']['bits'])
        self.assertNotIn('codex', sys.base_prefix.lower())
        self.assertTrue(Path(importlib.__file__).resolve().is_relative_to(Path(sys.base_prefix)))
        self.assertTrue(Path(sys.prefix).is_relative_to(Path(sys.executable).parents[2]))
        self.assertNotEqual(sys.prefix, sys.base_prefix)

    def test_locked_packages(self):
        root = Path(isolation.ACTIVE.config['code'])
        spec = check_manifest(root)
        self.assertEqual(installed_packages(isolation.ACTIVE.config['site_packages']),
                         {canonical(r['name']): r['version'] for r in spec['packages']})
        for name, expected in spec['locks'].items():
            self.assertEqual(lock_digest(root / name), expected)

    def test_import_paths(self):
        import invest_agent
        import workbench_paths
        root = Path(isolation.ACTIVE.config['code'])
        self.assertTrue(Path(invest_agent.__file__).resolve().is_relative_to(root / 'src'))
        self.assertTrue(Path(workbench_paths.__file__).resolve().is_relative_to(root / 'scripts'))
        self.assertEqual(workbench_paths.ROOT, root)
        pth = Path(isolation.ACTIVE.config['site_packages']) / 'invest_agent_local.pth'
        self.assertEqual([Path(line).name for line in pth.read_text(encoding='utf-8').splitlines()], ['src', 'scripts'])
        self.assertEqual(json.loads((root / 'config.json').read_text())['mother'],
                         str(Path(isolation.ACTIVE.config['workspace']) / 'mother'))
