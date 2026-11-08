"""Verify and retire one Windows response through a single owned file handle.

The caller has verified the independent DB copy and holds the collection lock.
Only non-Windows returns None for the existing portable implementation. Every
Windows failure propagates; it never selects a less restrictive fallback.
"""
import ctypes
from ctypes import wintypes
import ntpath
import os
from pathlib import Path
import re


_WINDOWS = os.name == 'nt'
_INVALID_HANDLE = ctypes.c_void_p(-1).value
_READ_DELETE = 0x80000000 | 0x00010000
_SHARE_READ = 0x00000001
_OPEN_EXISTING = 3
_OPEN_REPARSE_POINT = 0x00200000
_FILETIME_EPOCH = 116444736000000000
_MAX_RAW = 8 * 1024 * 1024


class _FileInformation(ctypes.Structure):
    _fields_ = [('attributes', wintypes.DWORD), ('created', wintypes.FILETIME),
                ('accessed', wintypes.FILETIME), ('written', wintypes.FILETIME),
                ('volume', wintypes.DWORD), ('size_high', wintypes.DWORD),
                ('size_low', wintypes.DWORD), ('links', wintypes.DWORD),
                ('index_high', wintypes.DWORD), ('index_low', wintypes.DWORD)]


class _Disposition(ctypes.Structure):
    _fields_ = [('delete', ctypes.c_ubyte)]  # Win32 BOOLEAN, not BOOL.


if _WINDOWS:
    _api = ctypes.WinDLL('kernel32', use_last_error=True)
    for name, args, result in (
        ('CreateFileW', (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                         wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE), wintypes.HANDLE),
        ('CloseHandle', (wintypes.HANDLE,), wintypes.BOOL),
        ('GetFileType', (wintypes.HANDLE,), wintypes.DWORD),
        ('GetFileInformationByHandle', (wintypes.HANDLE, ctypes.POINTER(_FileInformation)), wintypes.BOOL),
        ('GetFinalPathNameByHandleW', (wintypes.HANDLE, wintypes.LPWSTR,
                                    wintypes.DWORD, wintypes.DWORD), wintypes.DWORD),
        ('SetFilePointerEx', (wintypes.HANDLE, ctypes.c_longlong,
                             ctypes.POINTER(ctypes.c_longlong), wintypes.DWORD), wintypes.BOOL),
        ('ReadFile', (wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                      ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID), wintypes.BOOL),
        ('SetFileInformationByHandle', (wintypes.HANDLE, ctypes.c_int,
                                       wintypes.LPVOID, wintypes.DWORD), wintypes.BOOL),
    ):
        function = getattr(_api, name)
        function.argtypes, function.restype = args, result


def _win_error():
    return ctypes.WinError(ctypes.get_last_error())


def _normal_path(value):
    if value.startswith('\\\\?\\UNC\\'):
        value = '\\\\' + value[8:]
    elif value.startswith('\\\\?\\'):
        value = value[4:]
    return ntpath.normcase(ntpath.normpath(value))


def _final_path(handle):
    size = 512
    while size <= 32768:
        buffer = ctypes.create_unicode_buffer(size)
        length = _api.GetFinalPathNameByHandleW(handle, buffer, size, 0)
        if not length:
            raise _win_error()
        if length < size:
            return buffer.value
        size = length + 1
    raise ValueError('history_response_final_path_too_long')


def _information(handle):
    if _api.GetFileType(handle) != 1:  # FILE_TYPE_DISK
        raise ValueError('history_response_regular_file_required')
    info = _FileInformation()
    if not _api.GetFileInformationByHandle(handle, ctypes.byref(info)):
        raise _win_error()
    if info.attributes & 0x400:  # FILE_ATTRIBUTE_REPARSE_POINT
        raise ValueError('history_response_link_forbidden')
    if info.attributes & 0x10:  # FILE_ATTRIBUTE_DIRECTORY
        raise ValueError('history_response_regular_file_required')
    ticks = (info.written.dwHighDateTime << 32) | info.written.dwLowDateTime
    return ((ticks - _FILETIME_EPOCH) * 100, (info.size_high << 32) | info.size_low)


def _read_bytes(handle, size):
    if not _api.SetFilePointerEx(handle, 0, None, 0):  # FILE_BEGIN
        raise _win_error()
    result = bytearray()
    buffer = ctypes.create_string_buffer(min(size + 1, 65536))
    while len(result) <= size:
        count = wintypes.DWORD()
        requested = min(len(buffer), size + 1 - len(result))
        if not _api.ReadFile(handle, buffer, requested, ctypes.byref(count), None):
            raise _win_error()
        if not count.value:
            break
        if count.value > requested:
            raise ValueError('history_response_read_size_invalid')
        result.extend(buffer.raw[:count.value])
    return bytes(result)


def _mark_delete(handle):
    disposition = _Disposition(1)
    if not _api.SetFileInformationByHandle(handle, 4, ctypes.byref(disposition),
                                         ctypes.sizeof(disposition)):  # FileDispositionInfo
        raise _win_error()


def retire_verified(collection, name, path, initial, raw):
    """Return 1 after verified deletion/close, or None only on non-Windows.

    READ|DELETE with only FILE_SHARE_READ rejects ordinary concurrent writers
    and rename/delete handles. No delete-on-close flag is set while opening:
    deletion is requested only after both reads and fresh handle checks pass.
    """
    if not _WINDOWS:
        return None
    collection, path = Path(collection), Path(path)
    if (not collection.is_absolute() or not path.is_absolute()
            or not re.fullmatch(r'[0-9a-f]{64}\.json\.gz', name)):
        raise ValueError('history_response_name_invalid')
    expected_path = _normal_path(str(collection / 'responses' / name))
    if _normal_path(str(path)) != expected_path:
        raise ValueError('history_response_database_path_invalid')
    if (not isinstance(raw, bytes) or len(raw) > _MAX_RAW or initial is None
            or initial.st_size != len(raw)):
        raise ValueError('history_response_original_changed')
    expected_info = (initial.st_mtime_ns, len(raw))
    handle = _api.CreateFileW(str(path), _READ_DELETE, _SHARE_READ, None,
                              _OPEN_EXISTING, _OPEN_REPARSE_POINT, None)
    if handle in (None, _INVALID_HANDLE):
        raise _win_error()
    try:
        if _information(handle) != expected_info:
            raise ValueError('history_response_original_changed')
        if _normal_path(_final_path(handle)) != expected_path:
            raise ValueError('history_response_database_path_invalid')
        if _read_bytes(handle, len(raw)) != raw or _read_bytes(handle, len(raw)) != raw:
            raise ValueError('history_response_original_changed')
        if _information(handle) != expected_info:
            raise ValueError('history_response_original_changed')
        if _normal_path(_final_path(handle)) != expected_path:
            raise ValueError('history_response_database_path_invalid')
        _mark_delete(handle)
    finally:
        if not _api.CloseHandle(handle):
            raise _win_error()
    return 1
