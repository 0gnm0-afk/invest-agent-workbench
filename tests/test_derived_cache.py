import hashlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import derived_cache as cache
from invest_agent.trading.store import instance_lock
from sectors import focus


def key(value):
    return hashlib.sha256(str(value).encode()).hexdigest()


class DerivedCacheTests(unittest.TestCase):
    def test_key_limit_preserves_old_content_and_rejected_keys_create_nothing(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(cache, 'MAX_ENTRIES', 2):
            root = Path(temporary)
            for n in range(2):
                self.assertTrue(cache.publish(root, 'recent-screen', key(n), {'review.json': str(n)}))
            base = root / 'runtime/recent-screen'
            original = {p.relative_to(base): p.read_bytes() for p in base.rglob('*') if p.is_file()}
            self.assertFalse(cache.publish(root, 'recent-screen', key(0), {'review.json': 'changed'}))
            for n in range(2, 22):
                self.assertFalse(cache.publish(root, 'recent-screen', key(n), {'review.json': str(n)}))
            self.assertEqual({p.relative_to(base): p.read_bytes() for p in base.rglob('*') if p.is_file()}, original)
            self.assertEqual(len([p for p in base.iterdir() if p.is_dir()]), 2)

    def test_byte_limit_counts_utf8_existing_files_and_rejects_oversize(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(cache, 'MAX_BYTES', 20):
            root = Path(temporary)
            self.assertTrue(cache.publish(root, 'recent-screen', key(1), {'review.json': '가' * 4}))
            self.assertFalse(cache.publish(root, 'recent-screen', key(2), {'review.json': '가' * 4}))
            self.assertFalse(cache.publish(root, 'recent-screen', key(3), {'review.json': '가' * 8}))
            base = root / 'runtime/recent-screen'
            self.assertLessEqual(sum(p.stat().st_size for p in base.rglob('*') if p.is_file()), 20)
            self.assertEqual(len([p for p in base.iterdir() if p.is_dir()]), 1)

    def test_unknown_evidence_blocks_admission_without_touching_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base = root / 'runtime/sector-focus'
            base.mkdir(parents=True)
            evidence = base / 'fixed-document-reference.json'
            evidence.write_bytes(b'preserve this original')
            self.assertFalse(cache.publish(root, 'sector-focus', key(1), {'focus.json': '{}', 'manifest.json': '{}'}))
            self.assertEqual(evidence.read_bytes(), b'preserve this original')
            self.assertEqual([p for p in base.iterdir() if p.is_dir()], [])

    def test_failed_publication_cleans_only_its_unpublished_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertTrue(cache.publish(root, 'recent-screen', key(0), {'review.json': 'old'}))
            base = root / 'runtime/recent-screen'
            with patch.object(cache.os, 'rename', side_effect=OSError('disk failure')):
                self.assertFalse(cache.publish(root, 'recent-screen', key(1), {'review.json': 'new'}))
            self.assertEqual((base / key(0) / 'review.json').read_text(), 'old')
            self.assertEqual(sorted(p.name for p in base.iterdir() if p.is_dir()), [key(0)])
            self.assertFalse(list(base.glob('.pending-*')))

    def test_concurrent_admission_cannot_exceed_capacity(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(cache, 'MAX_ENTRIES', 3):
            root = Path(temporary)
            base = root / 'runtime/recent-screen'
            base.mkdir(parents=True)
            (base / '.writer.lock').write_bytes(b'0')
            barrier = threading.Barrier(8)
            def write(n):
                barrier.wait()
                return cache.publish(root, 'recent-screen', key(n), {'review.json': str(n)})
            with ThreadPoolExecutor(max_workers=8) as pool:
                accepted = list(pool.map(write, range(8)))
            self.assertTrue(any(accepted))
            for n in range(8, 12):
                cache.publish(root, 'recent-screen', key(n), {'review.json': str(n)})
            folders = [p for p in base.iterdir() if p.is_dir()]
            self.assertEqual(len(folders), 3)
            self.assertTrue(all((p / 'review.json').is_file() for p in folders))
            self.assertFalse(list(base.glob('.pending-*')))

    def test_capacity_and_busy_locks_return_calculation_without_new_entries(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = (root, 'run', {}, [], [], 'source')
            expected = {'items': [], 'network_calls': 0}
            with patch.object(focus, 'build', return_value=expected):
                self.assertEqual(focus.cached(*args), expected)
            base = root / 'runtime/sector-focus'
            entry = next(p for p in base.iterdir() if p.is_dir())
            original = (entry / 'focus.json').read_bytes()
            with patch.object(cache, 'MAX_ENTRIES', 0), patch.object(focus, 'build', side_effect=AssertionError('cache not reused')):
                self.assertEqual(focus.cached(*args), expected)
            with instance_lock(entry), patch.object(focus, 'build', return_value=expected):
                self.assertEqual(focus.cached(*args), expected)
            with instance_lock(base), patch.object(focus, 'build', return_value=expected):
                self.assertEqual(focus.cached(root, 'run', {}, [], [], 'new'), expected)
            with patch.object(cache, 'MAX_ENTRIES', 0), patch.object(focus, 'build', return_value=expected):
                self.assertEqual(focus.cached(root, 'run', {}, [], [], 'overflow'), expected)
            self.assertEqual((entry / 'focus.json').read_bytes(), original)
            self.assertEqual([p for p in base.iterdir() if p.is_dir()], [entry])


if __name__ == '__main__':
    unittest.main()
