"""Resolve configured runtime/journal moves without changing saved path keys."""
import json
from pathlib import Path

from workbench_paths import ROOT


LEGACY_JOURNAL_DIR = Path('vault', '20_매매일지', '일지')


def previous_roots(records, singular, plural):
    values = records.get(plural, [])
    if not isinstance(values, list):
        raise ValueError('record_previous_roots_not_list')
    # Additional moves are listed newest first; the legacy singular root is oldest.
    values = values + ([records[singular]] if records.get(singular) else [])
    result = []
    for value in values:
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise ValueError('record_previous_root_not_absolute')
        path = Path(value)
        if path not in result:
            result.append(path)
    return result


def journal_relative_path(settings):
    value = settings.get('records', {}).get('journal_relative_path', str(LEGACY_JOURNAL_DIR))
    path = Path(value)
    if path.is_absolute() or path.drive or '..' in path.parts or not path.parts:
        raise ValueError('trade_journal_relative_path_invalid')
    return path


def _aliases(root):
    config = root / 'config.json'
    settings = json.loads(config.read_text(encoding='utf-8-sig')) if config.is_file() else {}
    records = settings.get('records', {})
    for current, old_roots in (
        (root, previous_roots(records, 'previous_root', 'previous_roots')),
        (settings.get('mother'), previous_roots(records, 'previous_mother_root', 'previous_mother_roots')),
    ):
        for previous in old_roots:
            if not current or not Path(current).is_absolute():
                raise ValueError('record_previous_root_not_absolute')
            yield Path(current) / 'runtime', Path(previous) / 'runtime'
    for previous in previous_roots(records, 'previous_journal_folder', 'previous_journal_folders'):
        mother = settings.get('mother')
        if not mother or not Path(mother).is_absolute():
            raise ValueError('trade_journal_mother_must_be_absolute')
        yield Path(mother) / journal_relative_path(settings), previous


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


def runtime_lookup_paths(path, *, root=None):
    """Use original database keys while accepting the new physical pathname."""
    path = Path(path).resolve()
    root = Path(root) if root is not None else ROOT
    physical = local_runtime_path(path, root=root).resolve()
    candidates = [path]
    if physical not in candidates:
        candidates.append(physical)
    for current, previous in _aliases(root):
        if physical.is_relative_to(current):
            old = previous / physical.relative_to(current)
            if old not in candidates:
                candidates.append(old)
    return tuple(candidates)
