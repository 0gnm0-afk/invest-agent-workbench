"""Explicit, offline M00 installation. Never modifies an existing environment.

Run with the independent interpreter recorded in dev-environment.json:
  python -I scripts/setup_dev_environment.py --wheelhouse <local wheels> --target <OS temp>/env
The wheelhouse can be prepared separately from official PyPI using the two locks.
This installer deliberately accepts only a new target below the OS temp directory.
An operating .venv transition belongs to the separately approved operations step.
"""
import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import venv


ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def lock_digest(path):
    # Git on Windows may check out CRLF; package hashes and logical lock input
    # must stay reproducible across that formatting-only transformation.
    return hashlib.sha256(Path(path).read_bytes().replace(b'\r\n', b'\n')).hexdigest()


def canonical(name):
    return name.lower().replace('_', '-').replace('.', '-')


def check_manifest(root=ROOT):
    spec = json.loads((root / 'dev-environment.json').read_text(encoding='utf-8'))
    base = Path(sys.base_prefix) / 'python.exe'
    expected = spec['python']
    if (list(sys.version_info[:3]) != list(map(int, expected['version'].split('.')))
            or struct.calcsize('P') * 8 != expected['bits']
            or base.resolve() != Path(expected['executable']).resolve()
            or digest(base) != expected['sha256']
            or any(part.lower() in ('.cache', '.codex', 'codex-runtimes') for part in base.parts)):
        raise ValueError('independent_python_mismatch')
    for filename, sha in spec['locks'].items():
        if lock_digest(root / filename) != sha:
            raise ValueError('lock_hash_mismatch')
    return spec


def installed_packages(site):
    return {canonical(d.metadata['Name']): d.version
            for d in importlib.metadata.distributions(path=[str(site)])}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wheelhouse', type=Path, required=True)
    parser.add_argument('--target', type=Path, required=True)
    args = parser.parse_args()
    spec = check_manifest()
    target = args.target.resolve()
    temp = Path(tempfile.gettempdir()).resolve()
    if not target.is_relative_to(temp) or target == temp or target.exists():
        raise ValueError('target_must_be_new_directory_under_os_temp')
    wheelhouse = args.wheelhouse.resolve()
    for row in spec['packages']:
        if digest(wheelhouse / row['file']) != row['sha256']:
            raise ValueError('wheel_hash_mismatch:' + row['name'])
    # No inherited credentials, user PATH, PYTHONPATH, pip configuration or tokens.
    env = {'SystemRoot': 'C:\\Windows', 'WINDIR': 'C:\\Windows',
           'TEMP': str(temp), 'TMP': str(temp), 'PYTHONUTF8': '1',
           'PYTHONNOUSERSITE': '1', 'PIP_CONFIG_FILE': os.devnull,
           'PIP_DISABLE_PIP_VERSION_CHECK': '1'}
    venv.EnvBuilder(with_pip=False).create(target)
    python = target / 'Scripts/python.exe'
    subprocess.run([str(python), '-I', '-X', 'utf8', '-m', 'ensurepip', '--default-pip'],
                   env=env, check=True)
    subprocess.run([str(python), '-I', '-X', 'utf8', '-m', 'pip', '--isolated', 'install',
                    '--no-index', '--no-cache-dir', '--disable-pip-version-check',
                    '--find-links', str(wheelhouse), '--require-hashes',
                    '-r', str(ROOT / 'requirements-dev.lock.txt')], env=env, check=True)
    site = target / 'Lib/site-packages'
    actual = installed_packages(site)
    expected = {canonical(r['name']): r['version'] for r in spec['packages']}
    if actual != expected:
        raise ValueError('installed_packages_mismatch')
    (site / 'invest_agent_local.pth').write_text(
        ''.join(str(ROOT / name) + '\n' for name in spec['import_paths']), encoding='utf-8')
    discovery = subprocess.run([str(python), '-I', '-X', 'utf8', '-c',
        "import importlib.util,json; print(json.dumps({n:importlib.util.find_spec(n).origin for n in ('invest_agent','workbench_paths')}))"],
        env=env, check=True, capture_output=True, text=True, encoding='utf-8')
    origins = json.loads(discovery.stdout)
    if (Path(origins['invest_agent']).resolve() != ROOT / 'src/invest_agent/__init__.py'
            or Path(origins['workbench_paths']).resolve() != ROOT / 'scripts/workbench_paths.py'):
        raise ValueError('installed_import_paths_mismatch')
    receipt = {'schema': 1, 'status': 'passed', 'target': str(target),
               'python': spec['python'], 'locks': spec['locks'], 'packages': actual,
               'wheelhouse': str(wheelhouse), 'offline': True,
               'import_discovery': origins,
               'import_paths': [str(ROOT / p) for p in spec['import_paths']]}
    (target / 'setup-receipt.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'python': str(python), 'receipt': str(target / 'setup-receipt.json')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
