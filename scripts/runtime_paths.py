"""Relocate only explicitly configured runtime roots without changing saved path keys."""
import json
from pathlib import Path

from workbench_paths import ROOT


def _aliases(root):
    config = root / 'config.json'
    settings = json.loads(config.read_text(encoding='utf-8-sig')) if config.is_file() else {}
    records = settings.get('records', {})
    for current, previous in ((root, records.get('previous_root')),
                              (settings.get('mother'), records.get('previous_mother_root'))):
        if previous:
            if not current or not Path(current).is_absolute() or not Path(previous).is_absolute():
                raise ValueError('record_previous_root_not_absolute')
            yield Path(current) / 'runtime', Path(previous) / 'runtime'


def local_runtime_path(path, *, root=None):
    path = Path(path)
    if not path.is_absolute():
        return path
    for current, previous in _aliases(Path(root) if root is not None else ROOT):
        if path.is_relative_to(previous):
            target = (current / path.relative_to(previous)).resolve()
            if not target.is_relative_to(current.resolve()):
                raise ValueError('record_relocated_path_outside_runtime')
            return target
    return path


def runtime_lookup_paths(path):
    """Use original database keys while accepting the new physical pathname."""
    path = Path(path).resolve()
    for current, previous in _aliases(ROOT):
        if path.is_relative_to(current):
            old = previous / path.relative_to(current)
            if old != path:
                return path, old
        elif path.is_relative_to(previous):
            current_path = local_runtime_path(path)
            if current_path != path:
                return path, current_path
    return (path,)
