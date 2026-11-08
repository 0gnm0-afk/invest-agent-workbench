"""Bound new derived files without evicting historical or in-use cache entries."""
from contextlib import contextmanager, ExitStack
import os
from pathlib import Path
import re
import tempfile

from atomic_io import atomic_write
from invest_agent.trading.store import BusyError, instance_lock


MAX_ENTRIES = 8
MAX_BYTES = 128 * 1024 * 1024
OUTPUTS = {
    'sector-focus': {'focus.json', 'manifest.json'},
    'sector-projection': {'view.json', 'manifest.json'},
    'recent-screen': {'review.json'},
}
KEY = re.compile(r'[0-9a-f]{64}')


def _redirected(path):
    try:
        attributes = getattr(path.lstat(), 'st_file_attributes', 0)
    except FileNotFoundError:
        return False
    # Includes Windows junctions on Python versions before Path.is_junction.
    return path.is_symlink() or bool(attributes & 0x400)


@contextmanager
def read_lock(folder):
    """A busy cache is optional; never create a directory for an uncached key."""
    folder = Path(folder)
    try:
        available = folder.is_dir() and not _redirected(folder)
    except OSError:
        available = False
    if not available:
        yield False
        return
    with ExitStack() as stack:
        try:
            stack.enter_context(instance_lock(folder))
        except (BusyError, OSError):
            yield False
            return
        yield True


def _usage(folder, allowed):
    """Unknown files/links fail closed; they may be evidence we do not own."""
    entries = total = 0
    for item in folder.iterdir():
        if _redirected(item):
            return None
        if item.is_file() and item.name == '.writer.lock':
            total += item.stat().st_size
            continue
        if not item.is_dir() or not KEY.fullmatch(item.name):
            return None
        entries += 1
        lock_present = False
        for output in item.iterdir():
            if (_redirected(output) or not output.is_file()
                    or output.name not in allowed | {'.writer.lock'}):
                return None
            total += output.stat().st_size
            lock_present |= output.name == '.writer.lock'
        if not lock_present:
            total += 1  # A future verified reader may create the entry lock.
    return entries, total


def publish(root, family, key, files):
    """Admit at most 8 keys/128 MiB per family; otherwise keep the result in RAM.

    Existing entries, including incomplete/corrupt ones, are never replaced or
    removed. Only this call's unpublished temporary directory is cleaned up.
    The family lock makes admission atomic across concurrent new writers.
    """
    if family not in OUTPUTS or not KEY.fullmatch(key):
        raise ValueError('invalid_derived_cache_target')
    if set(files) != OUTPUTS[family] or not all(isinstance(v, str) for v in files.values()):
        raise ValueError('invalid_derived_cache_outputs')
    runtime = Path(root) / 'runtime'
    folder = runtime / family
    destination = folder / key
    # Include the entry lock; exFAT allocation overhead is also bounded by count.
    required = sum(len(text.encode('utf-8')) for text in files.values()) + 1
    if required > MAX_BYTES:
        return False
    try:
        if _redirected(runtime) or _redirected(folder) or destination.exists():
            return False
        with instance_lock(folder):
            if destination.exists():
                return False
            usage = _usage(folder, OUTPUTS[family])
            if usage is None:
                return False
            count, used = usage
            if count >= MAX_ENTRIES or used + required > MAX_BYTES:
                return False
            with tempfile.TemporaryDirectory(prefix='.pending-', dir=folder) as temporary:
                staging = Path(temporary)
                for name, text in files.items():
                    atomic_write(staging / name, text)
                (staging / '.writer.lock').write_bytes(b'0')
                # Never publish over a legacy writer's directory.
                if destination.exists():
                    return False
                os.rename(staging, destination)
            return True
    except (BusyError, OSError):
        return False
