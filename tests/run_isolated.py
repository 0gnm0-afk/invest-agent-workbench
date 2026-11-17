"""M00 C8 entry: --module M00..M13 [--node REGISTERED_ID ...].

No discovery, shell/argv option, network installation or operating-data mode.
Only copied source and synthetic config enter the guarded workers. Result JSON
is retained under OS temp; stdout is a summary, never a test's raw traceback.
"""
import argparse
import ast
import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import time
import unittest

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

# -I removes this directory from sys.path; -S skips all .pth startup hooks.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import isolation

ROOT = Path(__file__).resolve().parents[1]
SUPPORT = {'test_market_collect': ['test_hybrid_market.py']}
SUFFIXES = {'.py', '.js', '.cjs', '.css', '.html', '.svg'}
POLICIES = {'scripts/market/policies/exclusions.json', 'scripts/market/policies/lifecycle.json'}
STATIC_LAUNCH_SOURCES = ('dashboard.ps1', 'daily.ps1', 'scheduled-market.ps1',
                         'register-morning-task.ps1')


def registered_method_exists(root, module, cls, method, seen=None):
    seen = set() if seen is None else seen
    key = (module, cls)
    if key in seen or not re.fullmatch(r'test_[A-Za-z0-9_]+', module):
        return False
    seen.add(key)
    file = root / 'tests' / (module + '.py')
    if not file.is_file():
        return False
    tree = ast.parse(file.read_text(encoding='utf-8-sig'))
    imports = {}
    for item in tree.body:
        if isinstance(item, ast.Import):
            imports.update({a.asname or a.name: (a.name, None) for a in item.names})
        elif isinstance(item, ast.ImportFrom):
            imports.update({a.asname or a.name: (item.module, a.name) for a in item.names})
    for item in tree.body:
        if not isinstance(item, ast.ClassDef) or item.name != cls:
            continue
        if any(isinstance(fn, ast.FunctionDef) and fn.name == method for fn in item.body):
            return True
        for base in item.bases:
            if isinstance(base, ast.Name):
                source, name = imports.get(base.id, (module, base.id))
                if registered_method_exists(root, source or '', name, method, seen):
                    return True
            elif isinstance(base, ast.Attribute) and isinstance(base.value, ast.Name):
                source, _ = imports.get(base.value.id, ('', None))
                if registered_method_exists(root, source, base.attr, method, seen):
                    return True
    return False


def selection(root, module, subset=None):
    if not re.fullmatch(r'M(?:0[0-9]|1[0-3])', module):
        raise ValueError('unregistered_module')
    path = root / 'tests/suites' / (module + '.txt')
    if not path.is_file():
        raise ValueError('module_manifest_missing')
    entries, excluded = [], []
    for raw in path.read_text(encoding='utf-8').splitlines():
        node = raw.strip()
        if not node:
            continue
        if node.startswith('#'):
            if node.startswith('# exclude:'):
                excluded.append(node[len('# exclude:'):].strip())
            continue
        if re.fullmatch(r'tests/test_[A-Za-z0-9_]+\.cjs', node):
            file = root / node
        elif re.fullmatch(r'test_[A-Za-z0-9_]+\.[A-Za-z][A-Za-z0-9_]*\.test_[A-Za-z0-9_]+', node):
            file = root / 'tests' / (node.split('.')[0] + '.py')
        else:
            raise ValueError('invalid_node')
        if node in entries:
            raise ValueError('duplicate_node')
        if not file.is_file() or not file.resolve().is_relative_to((root / 'tests').resolve()):
            raise ValueError('node_file_missing_or_escape')
        if not node.endswith('.cjs') and not registered_method_exists(root, *node.split('.')):
            raise ValueError('registered_method_missing')
        entries.append(node)
    if not entries:
        raise ValueError('empty_selection')
    if subset is not None:
        if not subset or len(subset) != len(set(subset)) or any(n not in entries for n in subset):
            raise ValueError('unregistered_or_duplicate_selection')
        chosen = [n for n in entries if n in subset]
    else:
        chosen = entries
    return entries, chosen, excluded


def prepare(root, nodes, workspace):
    code = workspace / 'code'
    code.mkdir()
    copied = []
    for folder in ('src', 'scripts', 'dashboard'):
        for path in sorted((root / folder).rglob('*')):
            relative = path.relative_to(root)
            if any(p in {'runtime', 'judgments', 'secrets', '__pycache__', '.venv'} for p in relative.parts):
                continue
            if path.is_file() and (path.suffix in SUFFIXES or relative.as_posix() in POLICIES or path.name == 'LICENSE'):
                if path.is_symlink() or not path.resolve().is_relative_to((root / folder).resolve()):
                    raise ValueError('source_link_not_allowed')
                target = code / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(path.read_bytes())
                copied.append(relative.as_posix())
    # Fixed text fixtures only; this does not grant PowerShell execution rights.
    for name in STATIC_LAUNCH_SOURCES:
        source = root / name
        if source.is_symlink() or source.resolve().parent != root.resolve():
            raise ValueError('source_link_not_allowed')
        (code / name).write_bytes(source.read_bytes())
        copied.append(name)
    names = {'run_isolated.py', 'isolation.py', 'isolation_node.cjs',
             'test_isolation_node.cjs'}
    for node in nodes:
        if node.endswith('.cjs'):
            names.add(Path(node).name)
        else:
            name = node.split('.')[0]
            names.add(name + '.py')
            names.update(SUPPORT.get(name, []))
    (code / 'tests/suites').mkdir(parents=True)
    for name in sorted(names):
        source = root / 'tests' / name
        if source.is_symlink():
            raise ValueError('test_link_not_allowed')
        (code / 'tests' / name).write_bytes(source.read_bytes())
        copied.append('tests/' + name)
    for path in (root / 'tests/suites').glob('M[0-1][0-9].txt'):
        (code / 'tests/suites' / path.name).write_bytes(path.read_bytes())
    for name in ('dev-environment.json', 'requirements.lock.txt', 'requirements-dev.lock.txt', 'pyproject.toml'):
        (code / name).write_bytes((root / name).read_bytes())
    for name in ('temp', 'results', 'mother', 'protected'):
        (workspace / name).mkdir()
    (code / 'runtime').mkdir()
    (code / 'config.json').write_text(json.dumps({
        'mother': str(workspace / 'mother'), 'engine': str(code),
        'records': {'path': str(code / 'runtime/records.sqlite3')},
        'ohlcv': {'path': str(code / 'runtime/ohlcv.sqlite3')}}), encoding='utf-8')
    protected = workspace / 'protected'
    (protected / 'sentinel.txt').write_bytes(b'M00 synthetic protected data\n')
    import sqlite3
    with sqlite3.connect(protected / 'fixture.sqlite3') as connection:
        connection.execute('CREATE TABLE synthetic (value TEXT)')
        connection.execute("INSERT INTO synthetic VALUES ('fixture')")
    # Junctions do not require symlink privileges. Never point at real data.
    import _winapi
    _winapi.CreateJunction(str(protected), str(workspace / 'protected-link'))
    return code, copied


class RecordedResult(unittest.TestResult):
    def __init__(self):
        super().__init__()
        self.state = 'passed'
        self.reason = ''
        self.locations = []

    def location(self, err):
        trace = err[2]
        while trace:
            self.locations.append({'file': Path(trace.tb_frame.f_code.co_filename).name,
                                   'line': trace.tb_lineno})
            trace = trace.tb_next

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self.state, self.reason = 'failed', type(err[1]).__name__
        self.location(err)

    def addError(self, test, err):
        super().addError(test, err)
        self.state, self.reason = 'failed', type(err[1]).__name__
        self.location(err)

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self.state, self.reason = 'skipped', 'unittest_skip'

    def addExpectedFailure(self, test, err):
        super().addExpectedFailure(test, err)
        self.state, self.reason = 'unverified', 'expected_failure'

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        self.state, self.reason = 'failed', 'unexpected_success'

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err:
            self.state, self.reason = 'failed', type(err[1]).__name__
            self.location(err)


def synthetic_probe(name, config):
    import socket
    import sqlite3
    import subprocess
    protected = Path(config['workspace']) / 'protected'
    sentinel = protected / 'sentinel.txt'
    if name in ('read', 'swallow'):
        try:
            sentinel.read_bytes()
        except PermissionError:
            if name != 'swallow':
                raise
    elif name == 'write':
        sentinel.write_bytes(b'changed')
    elif name == 'truncate':
        os.truncate(sentinel, 0)
    elif name == 'enumerate':
        list(protected.iterdir())
    elif name == 'rename':
        sentinel.rename(Path(config['temp']) / 'moved')
    elif name == 'delete':
        sentinel.unlink()
    elif name == 'sqlite':
        sqlite3.connect((protected / 'fixture.sqlite3').as_uri() + '?mode=ro', uri=True)
    elif name == 'sqlite_attach':
        with sqlite3.connect(':memory:') as connection:
            connection.execute('ATTACH DATABASE ? AS forbidden', ((protected / 'fixture.sqlite3').as_uri() + '?mode=ro',))
    elif name == 'escape':
        (Path(config['temp']) / '../protected/sentinel.txt').read_bytes()
    elif name == 'link':
        (Path(config['workspace']) / 'protected-link/sentinel.txt').read_bytes()
    elif name == 'drive':
        Path('Z:/M00-synthetic-nonexistent/sentinel.txt').read_bytes()
    elif name == 'dns':
        socket.getaddrinfo('m00.invalid', 443)
    elif name == 'reverse_dns':
        socket.gethostbyaddr('127.0.0.1')
    elif name in ('connect', 'bind', 'unowned'):
        with socket.socket() as sock:
            if name == 'connect':
                sock.connect(('192.0.2.1', 443))
            elif name == 'unowned':
                sock.connect(('127.0.0.1', 9))
            else:
                sock.bind(('0.0.0.0', 0))
    elif name == 'credentials':
        import ctypes
        ctypes.WinDLL('crypt32')
    elif name == 'native_import':
        import _overlapped
        _overlapped.Overlapped(0)
    elif name == 'registry':
        import winreg
        winreg.OpenKey(winreg.HKEY_CURRENT_USER, 'Environment')
    elif name == 'process':
        subprocess.run(['powershell', '-NoProfile', '-Command', 'exit'])
    elif name == 'sleep':
        time.sleep(120)
    elif name == 'sleep_tree':
        isolation.probe('sleep', timeout=120)
    elif name == 'interrupt':
        raise KeyboardInterrupt()
    elif name == 'failure':
        raise AssertionError('synthetic')
    elif name == 'skip':
        raise unittest.SkipTest('synthetic')
    elif name == 'unknown':
        raise RuntimeError('synthetic_unknown')
    elif name != 'ready':
        raise ValueError('unknown_probe')


def worker(path):
    config = json.loads(Path(path).read_text(encoding='utf-8'))
    workspace = Path(config['workspace']).resolve()
    if (ROOT != workspace / 'code' or Path(config['code']).resolve() != ROOT
            or Path(config['temp']).resolve() != workspace / 'temp'
            or not Path(path).resolve().is_relative_to(workspace / 'results')
            or config['protected'] != [str(workspace / 'protected')]
            or config['python_executable'] != sys.executable
            or Path(config['site_packages']) != Path(config['environment']) / 'Lib/site-packages'
            or config['read_roots'] != [sys.base_prefix, config['site_packages']]):
        raise ValueError('worker_boundary_mismatch')
    # No .pth processing: both roots now refer to the synthetic source copy.
    sys.path[:] = [str(ROOT / 'tests'), str(ROOT / 'src'), str(ROOT / 'scripts'),
                   *[p for p in sys.path if p and Path(p).is_relative_to(Path(sys.base_prefix))],
                   config['site_packages']]
    sys.prefix = config['environment']
    sys.exec_prefix = config['environment']
    os.chdir(ROOT)
    guard = isolation.Guard(config).install()
    if config.get('probe') == 'not_ready':
        return 2
    Path(config['ready']).write_text(json.dumps({'token': config['token'], 'guard': 'ready'}), encoding='utf-8')
    deadline = time.monotonic() + 15
    while not Path(config['go']).exists():
        if time.monotonic() > deadline:
            return 2
        time.sleep(.01)
    if Path(config['go']).read_text(encoding='ascii') != config['token']:
        return 2
    status, reason, executed = 'failed', 'worker_incomplete', False
    locations = []
    try:
        executed = True
        if config.get('probe'):
            synthetic_probe(config['probe'], config)
            status, reason = 'passed', ''
        else:
            module, cls, method = config['node'].split('.')
            test_cls = getattr(importlib.import_module(module), cls)
            if not issubclass(test_cls, unittest.TestCase) or not callable(getattr(test_cls, method, None)):
                raise ValueError('registered_node_missing')
            result = RecordedResult()
            unittest.TestSuite([test_cls(method)]).run(result)
            status, reason = result.state, result.reason
            locations = result.locations
            if result.testsRun != 1:
                status, reason = 'unverified', 'required_node_not_executed'
    except unittest.SkipTest:
        status, reason = 'skipped', 'synthetic_skip'
    except KeyboardInterrupt:
        status, reason = 'failed', 'interrupted'
    except BaseException as exc:
        status, reason = 'failed', type(exc).__name__
        trace = exc.__traceback__
        while trace:
            locations.append({'file': Path(trace.tb_frame.f_code.co_filename).name, 'line': trace.tb_lineno})
            trace = trace.tb_next
    if guard.events:
        status, reason = 'blocked', 'guard_violation'
    result = {'node': config['node'], 'language': 'python', 'status': status,
              'reason': reason, 'executed': executed, 'version': sys.version.split()[0]}
    result['locations'] = locations
    Path(config['result']).write_text(json.dumps(result), encoding='utf-8')
    return 0 if status == 'passed' else 1


def run(module, subset=None):
    workspace = Path(tempfile.mkdtemp(prefix='invest-isolated-')).resolve()
    receipt = {'schema': 1, 'module': module, 'scope': 'partial' if subset is not None else 'full',
               'workspace': str(workspace), 'receipt': str(workspace / 'receipt.json'),
               'status': 'configuration_error', 'nodes': [], 'guard_events': [], 'excluded': [],
               'operating_data_access': 'not_attempted_by_controller'}
    exit_code = 2
    try:
        registered, nodes, excluded = selection(ROOT, module, subset)
        receipt.update(registered=registered, selected=nodes, excluded=excluded)
        sys.path.insert(0, str(ROOT / 'scripts'))
        from setup_dev_environment import check_manifest, installed_packages, canonical
        spec = check_manifest(ROOT)
        site = Path(sys.prefix) / 'Lib/site-packages'
        actual = installed_packages(site)
        if actual != {canonical(r['name']): r['version'] for r in spec['packages']}:
            raise ValueError('locked_environment_mismatch')
        receipt.update(python=spec['python'], locks=spec['locks'], packages=actual)
        receipt['boundary_adapters'] = ['platformdirs synthetic directory overrides',
            'rich/click noninteractive console', 'MCP stdio process denial',
            'dateutil Windows registry timezone unavailable', 'overlapped native I/O denial',
            'NH authentication denial', 'optional native XML parser unavailable', 'IPv4-only fixtures']
        code, copied = prepare(ROOT, nodes, workspace)
        receipt['copied_sources'] = copied
        receipt['source_sha256'] = {name: hashlib.sha256((code / name).read_bytes()).hexdigest()
                                    for name in copied + ['tests/suites/' + module + '.txt',
                                                         'dev-environment.json', 'pyproject.toml']}
        # Read only a fixed system installation location, never inherited PATH.
        node_exe = Path('C:/Program Files/nodejs/node.exe')
        if any(n.endswith('.cjs') for n in nodes) and not node_exe.is_file():
            raise ValueError('node_guard_unavailable')
        protected = workspace / 'protected'
        before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in protected.iterdir()}
        receipt['synthetic_inputs_sha256'] = before
        config = {'workspace': str(workspace), 'code': str(code), 'temp': str(workspace / 'temp'),
                  'protected': [str(protected)], 'site_packages': str(site),
                  'environment': sys.prefix, 'python_executable': sys.executable,
                  'node_executable': str(node_exe), 'read_roots': [sys.base_prefix, str(site)]}
        for node in nodes:
            result = isolation.launch(dict(config, node=node, language='node' if node.endswith('.cjs') else 'python'))
            receipt['nodes'].append(result)
            receipt['guard_events'].extend(result['guard_events'])
            if result.get('version') and result['language'] == 'node':
                receipt['node_version'] = result['version']
            print(result['status'] + ' ' + node, flush=True)
        after = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in protected.iterdir()}
        receipt['synthetic_protected_unchanged'] = before == after
        receipt['counts'] = isolation.count_results(receipt['nodes'])
        receipt['by_language'] = {lang: isolation.count_results([r for r in receipt['nodes'] if r['language'] == lang])
                                  for lang in ('python', 'node')}
        good = all(r['status'] == 'passed' for r in receipt['nodes']) and before == after
        receipt['status'] = 'passed' if good else 'failed'
        receipt['module_complete'] = good and subset is None
        exit_code = 0 if good else 1
    except (Exception, KeyboardInterrupt) as exc:
        receipt['error'] = type(exc).__name__ + ':' + (str(exc) if isinstance(exc, ValueError) else 'configuration_or_interrupt')
        receipt['module_complete'] = False
        existing = {n['node'] for n in receipt['nodes']}
        receipt['nodes'].extend({'node': n, 'language': 'node' if n.endswith('.cjs') else 'python',
                                 'status': 'unverified', 'executed': False, 'reason': 'configuration_error',
                                 'guard_events': []} for n in receipt.get('selected', []) if n not in existing)
    receipt.setdefault('counts', isolation.count_results(receipt['nodes']))
    receipt['counts']['excluded'] = len(receipt['excluded'])
    receipt['exit_code'] = exit_code
    Path(receipt['receipt']).write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'status': receipt['status'], 'receipt': receipt['receipt'], 'counts': receipt['counts']}, ensure_ascii=False), flush=True)
    return exit_code


def main():
    if len(sys.argv) == 3 and sys.argv[1] == '--worker':
        return worker(sys.argv[2])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--module', required=True)
    parser.add_argument('--node', nargs='+')
    args = parser.parse_args()
    return run(args.module, args.node)


if __name__ == '__main__':
    raise SystemExit(main())
