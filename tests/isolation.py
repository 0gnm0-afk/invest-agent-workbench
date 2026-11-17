"""C8 accidental-I/O containment for reviewed test code (not hostile-code sandbox).

No operating data is used, even for negative tests. The controller copies code,
then a fresh -I -S worker installs this guard before any business/test import.
Guard events are written immediately and are counted independently of unittest.
Only the fixed worker launcher below may create a child. Windows Job Objects
own that child's descendants; closing a job kills and releases the whole tree.
"""
import contextlib
import ctypes
from ctypes import wintypes
import hashlib
import importlib.abc
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import types
from urllib.parse import unquote, urlsplit
import uuid


_OPEN = open
_POPEN = subprocess.Popen
_REALPATH = os.path.realpath
_LOCAL = threading.local()
ACTIVE = None


def clean_environment(folder):
    """Never enumerate or copy the parent environment."""
    folder = str(folder)
    environment = {'SystemRoot': 'C:\\Windows', 'WINDIR': 'C:\\Windows',
            'TEMP': folder, 'TMP': folder, 'HOME': folder, 'USERPROFILE': folder,
            'APPDATA': folder, 'LOCALAPPDATA': folder, 'MPLCONFIGDIR': folder,
            'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONNOUSERSITE': '1',
            'PYTHONUTF8': '1', 'TERM': 'dumb', 'NO_COLOR': '1',
            'MERITZ_READ_ONLY': '1', 'NHPLUG_ENABLE_TRADING': 'false'}
    # platformdirs' documented overrides prevent even read-only native Windows
    # known-folder/registry queries while importing the pinned MCP client.
    for name in ('APPDATA', 'LOCAL_APPDATA', 'COMMON_APPDATA', 'PROFILE', 'PERSONAL',
                 'MYPICTURES', 'MYVIDEO', 'MYMUSIC', 'DOWNLOADS', 'DESKTOPDIRECTORY',
                 'PROGRAMS', 'COMMON_PROGRAMS'):
        environment['WIN_PD_OVERRIDE_' + name] = folder
    return environment


@contextlib.contextmanager
def _internal():
    old = getattr(_LOCAL, 'internal', False)
    _LOCAL.internal = True
    try:
        yield
    finally:
        _LOCAL.internal = old


def normalized(path):
    with _internal():
        return os.path.normcase(_REALPATH(os.path.abspath(os.fsdecode(path))))


def within(path, root):
    return path == root or path.startswith(root + os.sep)


class GuardImport(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] == 'lxml':
            # BeautifulSoup supports its pure Python parser when the optional
            # native parser is absent. Native filename/URL parsing is not allowed.
            raise ImportError('native_xml_parser_unavailable_in_fixture')
        if fullname == 'dateutil.tz.win':
            # Optional registry timezones are unavailable; dateutil's own
            # documented ImportError fallback keeps fixed/zoneinfo zones intact.
            raise ImportError('synthetic_environment_has_no_registry_timezones')
        if fullname == 'rich._win32_console':
            ACTIVE.deny('native', 'interactive_console')
        top = fullname.split('.')[0]
        if (top.startswith('win32') or top in {'pythoncom', 'pywintypes',
                '_cffi_backend', 'cffi'}):
            ACTIVE.deny('credentials', 'native_module')


class Guard:
    def __init__(self, config):
        self.config = config
        self.workspace = normalized(config['workspace'])
        self.read_roots = [normalized(p) for p in config['read_roots']]
        self.protected = [normalized(p) for p in config.get('protected', [])]
        self.event_file = config['events']
        self.events = []
        self.ports = set()
        self.fd_paths = {}

    def deny(self, category, operation):
        # Deliberately omit path, arguments, exception text and environment values.
        event = {'category': category, 'operation': operation, 'node': self.config['node']}
        frame = sys._getframe(1)
        locations = []
        for _ in range(6):
            if frame is None:
                break
            locations.append({'file': os.path.basename(frame.f_code.co_filename), 'line': frame.f_lineno})
            frame = frame.f_back
        event['locations'] = locations
        self.events.append(event)
        with _internal(), _OPEN(self.event_file, 'a', encoding='utf-8') as stream:
            stream.write(json.dumps(event) + '\n')
            stream.flush()
        raise PermissionError('isolation_denied:' + category + ':' + operation)

    def path(self, value, *, write=False, operation='path', dir_fd=None):
        if dir_fd is not None:
            self.deny('filesystem', 'dir_fd')
        if isinstance(value, int):
            if value in (0, 1, 2):
                return
            if value in self.fd_paths:
                return self.path(self.fd_paths[value], write=write, operation=operation)
            self.deny('filesystem', 'unknown_fd')
        if value is None:
            value = os.getcwd()
        raw = os.fsdecode(value)
        if raw.lower().startswith(('\\\\', '//', '\\\\?\\', '\\\\.\\')) or ':' in raw[2:]:
            self.deny('filesystem', 'device_or_stream')
        lexical = os.path.normcase(os.path.abspath(raw))
        if not within(lexical, self.workspace) and (write or not any(within(lexical, p) for p in self.read_roots)):
            self.deny('filesystem', operation)
        full = normalized(raw)
        if any(within(full, p) or (write and within(p, full)) for p in self.protected):
            self.deny('filesystem', operation)
        if within(full, self.workspace):
            return
        if not write and any(within(full, p) for p in self.read_roots):
            return
        self.deny('filesystem', operation)

    def database(self, value):
        value = str(value)
        if value == ':memory:':
            return
        if value.startswith('file:'):
            parsed = urlsplit(value)
            if parsed.netloc:
                self.deny('sqlite', 'uri_host')
            value = unquote(parsed.path)
            if len(value) > 2 and value[0] == '/' and value[2] == ':':
                value = value[1:]
        self.path(value, write=True, operation='sqlite')

    def audit(self, event, args):
        if getattr(_LOCAL, 'internal', False):
            return
        if event == 'open':
            path, mode, flags = args
            self.path(path, write=bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)), operation='open')
        elif event in ('os.listdir', 'os.scandir'):
            self.path(args[0], operation='enumerate')
        elif event in ('os.remove', 'os.rmdir', 'os.mkdir', 'os.chmod', 'os.utime', 'os.truncate'):
            self.path(args[0], write=True, operation=event)
            if event in ('os.remove', 'os.rmdir') and args[1] not in (None, -1):
                self.deny('filesystem', 'dir_fd')
        elif event in ('os.rename', 'os.link'):
            self.path(args[0], write=True, operation=event)
            self.path(args[1], write=True, operation=event)
            if any(fd not in (None, -1) for fd in args[2:4]):
                self.deny('filesystem', 'dir_fd')
            if event == 'os.link':
                self.deny('filesystem', 'hardlink')
        elif event == 'os.symlink':
            self.deny('filesystem', 'symlink')
        elif event == 'sqlite3.connect':
            self.database(args[0])
        elif event in ('sqlite3.enable_load_extension', 'sqlite3.load_extension'):
            self.deny('native', event)
        elif event.startswith('winreg.') or event.startswith('ctypes.'):
            self.deny('credentials', event)
        elif event in ('os.system', 'subprocess.Popen', 'os.startfile', 'os.exec', 'os.posix_spawn', 'os.spawn', 'os.kill'):
            self.deny('process', event)
        elif event in ('socket.gethostbyaddr', 'socket.getnameinfo'):
            self.deny('network', 'reverse_dns')
        elif event in ('socket.getaddrinfo', 'socket.gethostbyname'):
            if args[0] not in ('127.0.0.1', b'127.0.0.1'):
                self.deny('network', 'dns')
        elif event in ('socket.connect', 'socket.bind', 'socket.sendto'):
            address = args[-1]
            if not isinstance(address, tuple) or address[0] != '127.0.0.1':
                self.deny('network', event)
            if event == 'socket.bind':
                if address[1] != 0:
                    self.deny('network', 'non_ephemeral_bind')
            elif address[1] not in self.ports:
                self.deny('network', 'unowned_port')

    def install(self):
        global ACTIVE
        ACTIVE = self
        socket.has_ipv6 = False  # C8 permits only IPv4 127.0.0.1 fixtures.
        # Only noninteractive pipes are provided. Replace optional UI adapters,
        # never business imports, so rich/click cannot query console DLLs.
        rich_windows = types.ModuleType('rich._windows')
        rich_windows.get_windows_console_features = lambda: types.SimpleNamespace(vt=False, truecolor=False)
        click_console = types.ModuleType('click._winconsole')
        click_console._get_windows_console_stream = lambda *args, **kwargs: None
        sys.modules['rich._windows'] = rich_windows
        sys.modules['click._winconsole'] = click_console
        # The MCP client imports these native spawn helpers eagerly. Replace
        # the process boundary with a deny adapter, not a working MCP transport.
        mcp_process = types.ModuleType('mcp.os.win32.utilities')
        def no_mcp_process(*args, **kwargs):
            self.deny('process', 'mcp_stdio')
        class NoFallbackProcess:
            def __init__(self, *args, **kwargs):
                no_mcp_process()
        mcp_process.FallbackProcess = NoFallbackProcess
        for name in ('create_windows_process', 'get_windows_executable_command', 'terminate_windows_process_tree'):
            setattr(mcp_process, name, no_mcp_process)
        sys.modules[mcp_process.__name__] = mcp_process
        auth = types.ModuleType('nh_auth')
        def no_auth(*args, **kwargs):
            self.deny('credentials', 'nh_auth')
        auth.BASE, auth.ROOT = 'https://m00.invalid', Path(self.config['workspace']) / 'mother'
        auth.NHError = type('NHError', (Exception,), {})
        for name in ('NoRedirect', 'protect', 'token', 'http_error', 'provider_code'):
            setattr(auth, name, no_auth)
        sys.modules['nh_auth'] = auth
        # Temp resolution and pathlib metadata probes must obey the same boundary.
        tempfile.tempdir = self.config['temp']
        original_open_fd, original_close_fd = os.open, os.close
        def open_fd(path, flags, *args, **kwargs):
            if getattr(_LOCAL, 'internal', False):
                return original_open_fd(path, flags, *args, **kwargs)
            self.path(path, write=bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC)),
                      operation='open_fd', dir_fd=kwargs.get('dir_fd'))
            fd = original_open_fd(path, flags, *args, **kwargs)
            self.fd_paths[fd] = normalized(path)
            return fd
        def close_fd(fd):
            self.fd_paths.pop(fd, None)
            return original_close_fd(fd)
        os.open, os.close = open_fd, close_fd
        for name in ('stat', 'lstat', 'readlink', 'chdir'):
            original = getattr(os, name)
            def guarded(path, *args, _name=name, _original=original, **kwargs):
                if not getattr(_LOCAL, 'internal', False):
                    self.path(path, operation=_name, dir_fd=kwargs.get('dir_fd'))
                return _original(path, *args, **kwargs)
            setattr(os, name, guarded)
        original_bind, original_close = socket.socket.bind, socket.socket.close
        bound = {}
        def bind(sock, address):
            result = original_bind(sock, address)
            port = sock.getsockname()[1]
            self.ports.add(port)
            bound[id(sock)] = port
            return result
        def close(sock):
            port = bound.pop(id(sock), None)
            if port is not None:
                self.ports.discard(port)
            return original_close(sock)
        socket.socket.bind, socket.socket.close = bind, close
        # SQLite ATTACH/VACUUM bypass Python file-open auditing; authorize the
        # database filename before SQLite's native VFS opens it.
        original_connection = sqlite3.Connection
        original_connect = sqlite3.connect
        guard = self
        class GuardedConnection(original_connection):
            def __init__(connection, *args, **kwargs):
                super().__init__(*args, **kwargs)
                def authorize(action, first, second, database, trigger):
                    if action == sqlite3.SQLITE_ATTACH:
                        if first is None:
                            guard.deny('sqlite', 'attach_expression')
                        guard.database(first)
                    elif action == sqlite3.SQLITE_PRAGMA and first.lower() in ('temp_store_directory', 'data_store_directory'):
                        guard.deny('sqlite', 'directory_pragma')
                    return sqlite3.SQLITE_OK
                original_connection.set_authorizer(connection, authorize)
            def set_authorizer(connection, callback):
                guard.deny('sqlite', 'replace_authorizer')
        def connect(*args, **kwargs):
            if len(args) > 5 or 'factory' in kwargs:
                guard.deny('sqlite', 'unverified_factory')
            return original_connect(*args, factory=GuardedConnection, **kwargs)
        import _sqlite3
        sqlite3.connect = sqlite3.dbapi2.connect = _sqlite3.connect = connect
        sqlite3.Connection = sqlite3.dbapi2.Connection = _sqlite3.Connection = GuardedConnection
        # CPython's Windows socketpair implementation itself uses loopback.
        # It goes through bind/connect above, so it gets no external exemption.
        sys.meta_path.insert(0, GuardImport())
        # _winapi and _overlapped expose unaudited file/process primitives.
        import _winapi
        # unittest.mock imports asyncio on Windows. Keep _overlapped constants
        # importable, but deny its unaudited native I/O constructors/functions.
        import _overlapped
        for name in dir(_overlapped):
            if not name.startswith('_') and callable(getattr(_overlapped, name)):
                def blocked_overlapped(*args, _name=name, **kwargs):
                    self.deny('native', 'overlapped_' + _name)
                setattr(_overlapped, name, blocked_overlapped)
        for name in ('CreateProcess', 'CreateFile', 'CreateNamedPipe', 'OpenProcess', 'TerminateProcess',
                     'GetEnvironmentVariable', 'CreateJunction', 'CopyFile2'):
            if hasattr(_winapi, name):
                original = getattr(_winapi, name)
                def blocked(*args, _original=original, _name=name, **kwargs):
                    if not getattr(_LOCAL, 'internal', False):
                        self.deny('native', _name)
                    return _original(*args, **kwargs)
                setattr(_winapi, name, blocked)
        sys.addaudithook(self.audit)
        return self


# Resolve Job Object APIs before installing the native-call deny guard.
class _BasicLimit(ctypes.Structure):
    _fields_ = [('process_time', ctypes.c_int64), ('job_time', ctypes.c_int64),
                ('flags', wintypes.DWORD), ('min_working', ctypes.c_size_t),
                ('max_working', ctypes.c_size_t), ('active', wintypes.DWORD),
                ('affinity', ctypes.c_size_t), ('priority', wintypes.DWORD),
                ('scheduling', wintypes.DWORD)]


class _IO(ctypes.Structure):
    _fields_ = [(n, ctypes.c_uint64) for n in ('read_ops', 'write_ops', 'other_ops', 'read_bytes', 'write_bytes', 'other_bytes')]


class _ExtendedLimit(ctypes.Structure):
    _fields_ = [('basic', _BasicLimit), ('io', _IO), ('process_memory', ctypes.c_size_t),
                ('job_memory', ctypes.c_size_t), ('peak_process', ctypes.c_size_t), ('peak_job', ctypes.c_size_t)]


_kernel = ctypes.WinDLL('kernel32', use_last_error=True)
_create_job = _kernel.CreateJobObjectW
_create_job.argtypes, _create_job.restype = [ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE
_set_job = _kernel.SetInformationJobObject
_set_job.argtypes, _set_job.restype = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD], wintypes.BOOL
_assign_job = _kernel.AssignProcessToJobObject
_assign_job.argtypes, _assign_job.restype = [wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL
_close_handle = _kernel.CloseHandle
_close_handle.argtypes, _close_handle.restype = [wintypes.HANDLE], wintypes.BOOL
_terminate_job = _kernel.TerminateJobObject
_terminate_job.argtypes, _terminate_job.restype = [wintypes.HANDLE, wintypes.UINT], wintypes.BOOL
_query_job = _kernel.QueryInformationJobObject
_query_job.argtypes, _query_job.restype = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p], wintypes.BOOL


class _Accounting(ctypes.Structure):
    _fields_ = [(n, ctypes.c_int64) for n in ('user', 'kernel', 'period_user', 'period_kernel')] + [
        (n, wintypes.DWORD) for n in ('faults', 'total', 'active', 'terminated')]


class Job:
    def __init__(self):
        self.handle = _create_job(None, None)
        limits = _ExtendedLimit()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.handle or not _set_job(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            self.close()
            raise RuntimeError('job_guard_unavailable')

    def assign(self, process):
        if not _assign_job(self.handle, int(process._handle)):
            raise RuntimeError('job_assignment_failed')

    def close(self):
        if getattr(self, 'handle', None):
            _close_handle(self.handle)
            self.handle = None

    def terminate_and_count(self):
        before = _Accounting()
        if not _query_job(self.handle, 1, ctypes.byref(before), ctypes.sizeof(before), None):
            raise RuntimeError('job_query_failed')
        self.active_before_cleanup = before.active
        self.total_processes = before.total
        if not _terminate_job(self.handle, 1):
            raise RuntimeError('job_termination_failed')
        count = _Accounting()
        deadline = time.monotonic() + 3
        while True:
            if not _query_job(self.handle, 1, ctypes.byref(count), ctypes.sizeof(count), None):
                raise RuntimeError('job_query_failed')
            if count.active == 0 or time.monotonic() >= deadline:
                return count.active
            time.sleep(.01)


def launch(config, *, timeout=60):
    """Only accepts an internally constructed worker config, never shell/argv.

    A job is attached and a random challenge acknowledged before the go-file
    permits imports. Every exit path closes the process, pipes and job handle.
    """
    if ACTIVE is not None:
        for field in ('workspace', 'code', 'temp', 'protected', 'site_packages', 'environment',
                      'python_executable', 'node_executable', 'read_roots'):
            if config.get(field) != ACTIVE.config[field]:
                raise ValueError('child_configuration_changed')
        if config.get('probe') not in PROBES:
            raise ValueError('only_fixed_fixture_children_allowed')
        expected_node = 'tests/test_isolation_node.cjs' if config['language'] == 'node' else 'probe.' + config['probe']
        if config['node'] != expected_node:
            raise ValueError('child_bootstrap_changed')
    folder = Path(config['workspace']) / 'results' / uuid.uuid4().hex
    folder.mkdir(parents=True)
    config = dict(config, token=uuid.uuid4().hex, events=str(folder / 'events.jsonl'),
                  ready=str(folder / 'ready.json'), go=str(folder / 'go'),
                  result=str(folder / 'result.json'))
    config_path = folder / 'config.json'
    config_path.write_text(json.dumps(config), encoding='utf-8')
    root = Path(config['code'])
    env = clean_environment(config['temp'])
    if config['language'] == 'node':
        env['M00_CONFIG'] = str(config_path)
        command = [config['node_executable'], '--require', str(root / 'tests/isolation_node.cjs'),
                   str(root / config['node'])]
    else:
        command = [config['python_executable'], '-I', '-S', '-B', '-X', 'utf8',
                   str(root / 'tests/run_isolated.py'), '--worker', str(config_path)]
    process = None
    job = None
    reason = None
    handshake = False
    live_children = None
    cleanup = {}
    try:
        with _internal():
            job = Job()
            process = _POPEN(command, cwd=root, env=env, stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             creationflags=subprocess.CREATE_NO_WINDOW)
            job.assign(process)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if Path(config['ready']).exists():
                ready = json.loads(Path(config['ready']).read_text(encoding='utf-8'))
                if ready.get('token') != config['token'] or ready.get('guard') != 'ready':
                    raise RuntimeError('child_handshake_invalid')
                handshake = True
                Path(config['go']).write_text(config['token'], encoding='ascii')
                break
            if process.poll() is not None:
                break
            time.sleep(.01)
        if not handshake:
            reason = 'guard_not_ready'
            raise subprocess.TimeoutExpired(command, timeout)
        process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        reason = reason or 'timeout'
    except KeyboardInterrupt:
        reason = 'interrupted'
    except Exception as exc:
        reason = type(exc).__name__  # never emit arbitrary exception values
    finally:
        with _internal():
            try:
                if job:
                    live_children = job.terminate_and_count()
            except Exception:
                reason = 'job_cleanup_failed'
            finally:
                if job:
                    job.close()
                if process:
                    try:
                        if process.poll() is None:
                            process.kill()
                        process.communicate(timeout=5)
                    except Exception:
                        reason = 'process_cleanup_failed'
                    finally:
                        for stream in (process.stdin, process.stdout, process.stderr):
                            if stream:
                                stream.close()
                        process._handle.Close()
                        cleanup = {'process_exited': process.returncode is not None,
                                   'pipes_closed': all(s is None or s.closed for s in (process.stdin, process.stdout, process.stderr)),
                                   'process_handle_closed': process._handle.closed,
                                   'job_handle_closed': job is not None and job.handle is None}
    events = []
    if Path(config['events']).exists():
        events = [json.loads(line) for line in Path(config['events']).read_text(encoding='utf-8').splitlines()]
    result = {'node': config['node'], 'language': config['language'], 'status': 'unverified',
              'reason': reason or 'result_missing', 'executed': handshake, 'guard_events': events}
    if not reason and Path(config['result']).exists():
        result.update(json.loads(Path(config['result']).read_text(encoding='utf-8')))
    if events:
        result.update(status='blocked', reason='guard_violation')
    elif reason:
        result.update(status='failed' if handshake else 'unverified', reason=reason)
    elif process is None or process.returncode != 0:
        if result['status'] == 'passed':
            result.update(status='failed', reason='nonzero_exit')
    result.update(guard_events=events, process_exit_code=process.returncode if process else None,
                  exit_code=0 if result['status'] == 'passed' else 1,
                  handshake=handshake, cleaned=bool(cleanup) and all(cleanup.values()) and live_children == 0,
                  cleanup=cleanup, live_children=live_children,
                  job_processes=getattr(job, 'total_processes', None),
                  active_before_cleanup=getattr(job, 'active_before_cleanup', None),
                  receipt=str(folder / 'receipt.json'))
    if not result['cleaned']:
        result.update(status='failed', reason='cleanup_incomplete', exit_code=1)
    Path(result['receipt']).write_text(json.dumps(result, indent=2), encoding='utf-8')
    return result


PROBES = frozenset({'read', 'write', 'truncate', 'recursive', 'enumerate', 'rename', 'delete', 'sqlite', 'sqlite_attach', 'escape',
                    'link', 'drive', 'dns', 'connect', 'bind', 'credentials', 'registry',
                    'process', 'unowned', 'swallow', 'ready', 'not_ready', 'sleep', 'sleep_tree', 'interrupt',
                    'failure', 'skip', 'unknown', 'node_boundaries', 'native_import', 'resolver', 'esm_native',
                    'recursive_read', 'glob', 'mkdtemp', 'reverse_dns'})


def probe(name, *, timeout=5, language='python'):
    """Fixed synthetic negative subruns. Never accepts executable names or argv."""
    if name not in PROBES or language not in ('python', 'node'):
        raise ValueError('unregistered_probe')
    config = dict(ACTIVE.config, probe=name, language=language)
    config['node'] = 'tests/test_isolation_node.cjs' if language == 'node' else 'probe.' + name
    return launch(config, timeout=timeout)


def count_results(results):
    counts = {key: 0 for key in ('selected', 'executed', 'passed', 'failed', 'blocked', 'skipped', 'unverified')}
    counts['selected'] = len(results)
    for result in results:
        counts['executed'] += bool(result.get('executed'))
        counts[result['status']] += 1
    return counts
