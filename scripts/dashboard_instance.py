"""Small stdlib-only lease and discovery shared by desktop and MCP launches."""
import json
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path

from atomic_io import atomic_write


def acquire(root, name='server.lock'):
    path = Path(root) / 'runtime/dashboard' / name
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open('a+b')
    handle.seek(0, 2)
    if handle.tell() == 0:
        handle.write(b'0')
        handle.flush()
    handle.seek(0)
    try:
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def release(handle):
    if handle is not None:
        # Closing the descriptor also releases the OS lease after a crash.
        handle.close()


def request(url, path, body=None, token=None):
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or not parsed.port
            or parsed.path or parsed.query or parsed.fragment or parsed.username or parsed.password):
        raise ValueError('invalid_dashboard_url')
    headers = {'Origin': url}
    raw = None
    if body is not None:
        raw = json.dumps(body).encode()
        headers.update({'Content-Type': 'application/json', 'X-Workbench-Token': token or ''})
    with urllib.request.urlopen(urllib.request.Request(url + path, data=raw, headers=headers), timeout=3) as response:
        return json.load(response)


def publish(root, value):
    atomic_write(Path(root) / 'runtime/dashboard/server.json', json.dumps(value))


def existing(root):
    """Reject stale receipts and other loopback apps; never trust a PID alone."""
    value = json.loads((Path(root) / 'runtime/dashboard/server.json').read_text(encoding='utf-8'))
    status = request(value['url'], '/api/instance')
    if (status.get('workspace') != str(Path(root)) or status.get('instance_id') != value['instance_id']
            or status.get('protocol') != 1):
        raise ValueError('incompatible_dashboard_instance')
    return {**value, **status}


def reuse(root, value, arguments):
    offline = '--offline' in arguments
    if offline != value.get('offline', False):
        raise ValueError('dashboard_mode_conflict: close the existing server before changing offline mode')
    if value['state'] == 'failed':
        raise ValueError('dashboard_startup_failed: inspect the latest startup log and retry after it exits')
    daily = '--daily-refresh' in arguments
    retry = '--retry-market' in arguments
    # A saved-only reopen is read-only. A daily launch must still reach the daily gate.
    if daily or retry or (not offline and '--saved-accounts' not in arguments):
        for _ in range(480):
            if value['state'] == 'ready':
                break
            if value['state'] == 'failed':
                raise ValueError('dashboard_startup_failed')
            time.sleep(.5)
            try:
                value = existing(root)
            except (OSError, ValueError, KeyError):
                # A large JSON decode can briefly hold the GIL in the owner.
                # Keep its lease; never interpret a slow probe as a new owner.
                continue
        else:
            raise ValueError('dashboard_start_timeout')
        health = request(value['dashboard_url'], '/health')
        if health.get('workspace') != str(Path(root)):
            raise ValueError('incompatible_dashboard')
        request(value['dashboard_url'], '/api/launch',
                dict(daily_refresh=daily, retry_market=retry, saved_accounts='--saved-accounts' in arguments), health['token'])
    return dict(url=value.get('dashboard_url') or value['url'], state=value['state'],
                output=None, reused=True, pid=value['pid'])
