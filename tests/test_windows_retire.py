"""Real Windows handles against disposable OS-temp files; no DB or network."""
import ctypes
from ctypes import wintypes
import errno
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import records.windows_retire as native


@unittest.skipUnless(os.name == 'nt', 'Windows handle contract')
class WindowsRetirementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.collection = self.root / 'collection'
        (self.collection / 'responses').mkdir(parents=True)
        self.raw = b'exact response bytes\x00\xff' * 20
        self.name = hashlib.sha256(self.raw).hexdigest() + '.json.gz'
        self.path = self.collection / 'responses' / self.name
        self.write(self.raw)

    def write(self, raw):
        self.path.write_bytes(raw)
        self.initial = self.path.lstat()

    def retire(self, **changes):
        values = dict(collection=self.collection, name=self.name, path=self.path,
                      initial=self.initial, raw=self.raw)
        values.update(changes)
        return native.retire_verified(**values)

    def preserved(self, expected=None):
        self.assertEqual(self.path.read_bytes(), self.raw if expected is None else expected)
        # This open also fails if our READ|DELETE handle leaked on an exception.
        with self.path.open('r+b'):
            pass

    def test_success_uses_one_handle_exact_flags_and_two_reads(self):
        self.assertEqual(ctypes.sizeof(wintypes.HANDLE), ctypes.sizeof(ctypes.c_void_p))
        self.assertEqual(ctypes.sizeof(native._Disposition), 1)
        self.assertEqual(ctypes.sizeof(native._FileInformation), 52)
        with patch.object(native._api, 'CreateFileW', wraps=native._api.CreateFileW) as opened, \
             patch.object(native._api, 'CloseHandle', wraps=native._api.CloseHandle) as closed, \
             patch.object(native, '_read_bytes', wraps=native._read_bytes) as reads, \
             patch.object(native, '_mark_delete', wraps=native._mark_delete) as marked:
            self.assertEqual(self.retire(), 1)
        opened.assert_called_once()
        args = opened.call_args.args
        self.assertEqual((args[1], args[2], args[4], args[5]),
                         (0x80010000, 1, 3, 0x00200000))
        self.assertFalse(args[5] & 0x04000000)  # Never DELETE_ON_CLOSE at open.
        self.assertEqual(reads.call_count, 2)
        handle = reads.call_args_list[0].args[0]
        self.assertEqual(reads.call_args_list[1].args[0], handle)
        marked.assert_called_once_with(handle)
        closed.assert_called_once_with(handle)
        self.assertFalse(self.path.exists())

    def test_large_response_uses_bounded_read_loop(self):
        self.raw = bytes(range(256)) * 1025
        self.write(self.raw)
        self.assertEqual(self.retire(), 1)
        self.assertFalse(self.path.exists())

    def test_same_size_same_timestamp_mismatch_is_preserved(self):
        changed = bytes([self.raw[0] ^ 1]) + self.raw[1:]
        self.path.write_bytes(changed)
        os.utime(self.path, ns=(self.initial.st_atime_ns, self.initial.st_mtime_ns))
        with patch.object(native, '_mark_delete') as marked:
            with self.assertRaisesRegex(ValueError, 'original_changed'):
                self.retire()
        marked.assert_not_called()
        self.preserved(changed)

    def test_second_read_mismatch_prevents_disposition(self):
        original = native._read_bytes
        reads = []
        def changed(handle, size):
            value = original(handle, size)
            reads.append(handle)
            return value if len(reads) == 1 else bytes([value[0] ^ 1]) + value[1:]
        with patch.object(native, '_read_bytes', changed), patch.object(native, '_mark_delete') as marked:
            with self.assertRaisesRegex(ValueError, 'original_changed'):
                self.retire()
        self.assertEqual(len(reads), 2)
        marked.assert_not_called()
        self.preserved()

    def test_extra_byte_is_detected_even_if_metadata_misses_growth(self):
        self.path.write_bytes(self.raw + b'x')
        with patch.object(native, '_information', return_value=(self.initial.st_mtime_ns, len(self.raw))), \
             patch.object(native, '_mark_delete') as marked:
            with self.assertRaisesRegex(ValueError, 'original_changed'):
                self.retire()
        marked.assert_not_called()
        self.preserved(self.raw + b'x')

    def test_initial_and_fresh_metadata_mismatch_preserve_source(self):
        initial = SimpleNamespace(st_size=len(self.raw), st_mtime_ns=self.initial.st_mtime_ns + 100)
        with self.assertRaisesRegex(ValueError, 'original_changed'):
            self.retire(initial=initial)
        self.preserved()
        values = [(self.initial.st_mtime_ns, len(self.raw)),
                  (self.initial.st_mtime_ns + 100, len(self.raw))]
        with patch.object(native, '_information', side_effect=values), \
             patch.object(native, '_mark_delete') as marked:
            with self.assertRaisesRegex(ValueError, 'original_changed'):
                self.retire()
        marked.assert_not_called()
        self.preserved()

    def test_final_path_mismatch_before_delete_preserves_source(self):
        with patch.object(native, '_final_path', side_effect=[str(self.path), str(self.root / self.name)]), \
             patch.object(native, '_mark_delete') as marked:
            with self.assertRaisesRegex(ValueError, 'database_path_invalid'):
                self.retire()
        marked.assert_not_called()
        self.preserved()
        self.assertEqual(native._normal_path('\\\\?\\UNC\\server\\share\\File'), '\\\\server\\share\\file')

    def test_open_writer_conflict_never_falls_back_to_deletion(self):
        from records.history import _retire_response
        with self.path.open('r+b'), patch.object(native, '_mark_delete') as marked:
            with self.assertRaises(OSError) as failed:
                _retire_response(self.collection, self.name, self.path, self.initial, self.raw)
        self.assertEqual(failed.exception.winerror, 32)
        marked.assert_not_called()
        self.preserved()

    def test_writer_and_rename_are_blocked_between_reads(self):
        original = native._read_bytes
        attempts = []
        def observed(handle, size):
            raw = original(handle, size)
            if not attempts:
                with self.assertRaises(OSError) as writer:
                    with self.path.open('r+b'):
                        pass
                # Python's CRT fopen maps the sharing violation to EACCES and
                # may omit winerror; direct CreateFileW is checked separately.
                self.assertEqual(writer.exception.errno, errno.EACCES)
                self.assertIn(writer.exception.winerror, (None, 32))
                with self.assertRaises(OSError) as rename:
                    self.path.rename(self.path.with_suffix('.moved'))
                self.assertEqual(rename.exception.winerror, 32)
            attempts.append(handle)
            return raw
        with patch.object(native, '_read_bytes', observed):
            self.assertEqual(self.retire(), 1)
        self.assertEqual(len(attempts), 2)
        self.assertFalse(self.path.exists())

    def test_win32_read_query_and_disposition_failures_close_handle(self):
        def denied(*_):
            ctypes.set_last_error(5)
            return 0
        for function in ('GetFileInformationByHandle', 'GetFinalPathNameByHandleW',
                         'ReadFile', 'SetFileInformationByHandle'):
            with self.subTest(function=function):
                with patch.object(native._api, function, side_effect=denied), \
                     patch.object(native._api, 'CloseHandle', wraps=native._api.CloseHandle) as closed:
                    with self.assertRaises(OSError) as failed:
                        self.retire()
                self.assertEqual(failed.exception.winerror, 5)
                closed.assert_called_once()
                self.preserved()

    def test_interrupt_before_delete_keeps_source_and_closes_handle(self):
        with patch.object(native, '_read_bytes', side_effect=KeyboardInterrupt), \
             patch.object(native, '_mark_delete') as marked:
            with self.assertRaises(KeyboardInterrupt):
                self.retire()
        marked.assert_not_called()
        self.preserved()

    def junction(self, link, target):
        # Creation only; no shell deletion. Both paths belong to this OS-temp
        # fixture and the junction is removed with one nonrecursive rmdir.
        self.assertTrue(link.is_relative_to(self.root) and target.is_relative_to(self.root))
        result = subprocess.run(['cmd.exe', '/d', '/c', 'mklink', '/J', str(link), str(target)],
                                capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0, 'OS-temp junction fixture creation failed')
        self.addCleanup(lambda: os.rmdir(link) if link.is_junction() else None)

    def test_real_reparse_leaf_is_rejected_and_target_preserved(self):
        target = self.root / 'target'
        target.mkdir()
        (target / 'keep').write_bytes(self.raw)
        self.path.unlink()
        self.junction(self.path, target)
        with patch.object(native, '_mark_delete') as marked:
            with self.assertRaises((OSError, ValueError)):
                self.retire()
        marked.assert_not_called()
        self.assertTrue(self.path.is_junction())
        self.assertEqual((target / 'keep').read_bytes(), self.raw)

    def test_real_parent_junction_cannot_redirect_verified_deletion(self):
        redirected = self.root / 'redirected'
        redirected.mkdir()
        link_collection = self.root / 'linked-collection'
        link_collection.mkdir()
        self.junction(link_collection / 'responses', redirected)
        target = redirected / self.name
        target.write_bytes(self.raw)
        initial = target.lstat()
        with patch.object(native, '_mark_delete') as marked:
            with self.assertRaisesRegex(ValueError, 'database_path_invalid'):
                self.retire(collection=link_collection, path=link_collection / 'responses' / self.name,
                            initial=initial)
        marked.assert_not_called()
        self.assertEqual(target.read_bytes(), self.raw)

    def test_repeated_success_and_failures_do_not_leak_handles(self):
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.GetCurrentProcess.argtypes = ()
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        kernel.GetProcessHandleCount.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel.GetProcessHandleCount.restype = wintypes.BOOL
        def count():
            result = wintypes.DWORD()
            if not kernel.GetProcessHandleCount(kernel.GetCurrentProcess(), ctypes.byref(result)):
                raise ctypes.WinError(ctypes.get_last_error())
            return result.value
        # Warm DLL/ctypes paths before measuring the process-local handle count.
        self.assertEqual(self.retire(), 1)
        before = count()
        for _ in range(20):
            self.write(self.raw)
            with self.assertRaisesRegex(ValueError, 'original_changed'):
                self.retire(raw=b'x' * len(self.raw))
            self.assertEqual(self.retire(), 1)
        self.assertLessEqual(count(), before)

    def test_non_windows_branch_retains_portable_path(self):
        from records.history import _retire_response
        with patch.object(native, '_WINDOWS', False), \
             patch.object(native._api, 'CreateFileW', side_effect=AssertionError('native forbidden')):
            self.assertEqual(_retire_response(self.collection, self.name, self.path, self.initial, self.raw), 1)
        self.assertFalse(self.path.exists())


if __name__ == '__main__':
    unittest.main()
