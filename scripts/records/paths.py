"""Resolve only the configured previous runtime of a relocated workbench."""
import json
from pathlib import Path

from workbench_paths import ROOT


def _runtimes():
    current = ROOT / 'runtime'
    config = ROOT / 'config.json'
    if not config.is_file():
        return current, None
    previous = json.loads(config.read_text(encoding='utf-8-sig')).get('records', {}).get('previous_root')
    if not previous:
        return current, None
    old = Path(previous)
    if not old.is_absolute():
        raise ValueError('record_previous_root_not_absolute')
    return current, old / 'runtime'


def local_runtime_path(path):
    path = Path(path)
    if not path.is_absolute():
        return path
    current, previous = _runtimes()
    if previous is not None and path.is_relative_to(previous):
        target = (current / path.relative_to(previous)).resolve()
        if not target.is_relative_to(current.resolve()):
            raise ValueError('record_relocated_path_outside_runtime')
        return target
    return path


def runtime_lookup_paths(path):
    """Use original database keys while accepting the new physical pathname."""
    path = Path(path).resolve()
    current, previous = _runtimes()
    if previous is not None and path.is_relative_to(current):
        old = previous / path.relative_to(current)
        if old != path:
            return path, old
    return (path,)
