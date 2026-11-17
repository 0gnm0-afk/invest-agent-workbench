"""Saved account observations; official session MCPs are the only read transport."""
from pathlib import Path

from atomic_io import sha, write_new_json as write
from workbench_paths import ROOT

from accounts.saved import saved_accounts


def account_view(config, output, refresh=True, saved_dashboard_root=None, request_id=None, *, persist_receipt=True):
    """Compatibility for M07/M10/M12: all options now read saved observations only.

    ``refresh`` and ``request_id`` cannot reserve a query or advance its time.
    The dashboard anchor locates the existing DB; no config/auth/export is read.
    """
    dashboard = Path(saved_dashboard_root or Path(output).parent)
    return saved_accounts(dashboard.parent.parent)


def publish_receipt(output, accounts, *, view=None, **metadata):
    """Preserve the explicit dashboard export until M10 removes the account UI."""
    receipt = {'account_view_ref': {'metadata': {k: v for k, v in accounts.items() if k != 'accounts'},
                                    'snapshot_ref': accounts['snapshot_ref']}} if accounts.get('snapshot_ref') else accounts
    write(output / 'account-view.json', receipt)
    if view is not None:
        write(output / 'dashboard.json', view)
    write(output / 'manifest.json', {'files': {p.name: sha(p) for p in output.iterdir() if p.is_file()},
                                    'authority': 'read_only_no_orders', **metadata})


def refresh_account_snapshot(root=ROOT, request_id=None):
    """Legacy name for a saved read, with the original observation/attempt times.

    M12 removes this caller and the account-attempt status in the launcher.
    New observations are accepted only by record.py begin/complete.
    """
    return saved_accounts(root)
