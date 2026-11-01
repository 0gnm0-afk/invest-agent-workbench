"""Account coordination companion; official broker MCPs remain the actual read transport."""
if not __package__:
    import sys
    from pathlib import Path
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from fastmcp import FastMCP
from accounts.store import AccountStore

mcp = FastMCP('invest_accounts', instructions='Read-only account coordination. Before an explicit NH/Meritz '
    'refresh, call accounts_query_begin. Only execute the official nhplug/meritz balance read when execute=true. '
    'Always complete the lease with received/partial/failed. Use the same request_id on retry. '
    'Do not automatically repeat pending/failed queries. Normalize actual returned balances, preserve their as_of '
    'and completeness; never invent positions or use code examples as account data. No orders or credentials. '
    'For ordinary dashboard/analysis reads use accounts_saved. Direct uncoordinated official MCP calls bypass this ledger.')


@mcp.tool()
def accounts_saved(provider: str) -> dict:
    """Read saved normalized account data with its original timestamp and provenance; never calls a broker."""
    if provider not in ('nh','meritz'): raise ValueError('account_provider_invalid')
    store=AccountStore(); receipt=store.latest(provider)
    return {'state':'saved' if receipt else 'missing','receipt':receipt,'latest_attempt':store.latest_attempt(provider),
            'snapshot':store.read(receipt['snapshot_ref']) if receipt else None}


@mcp.tool()
def accounts_query_begin(provider: str, request_id: str) -> dict:
    """Reserve one explicit full balance refresh for NH or Meritz; execute=false forbids another read."""
    return AccountStore().begin(provider,request_id,'mcp')


@mcp.tool()
def accounts_query_complete(provider: str, request_id: str, lease: str, metadata: dict,
                            snapshot: dict | None = None) -> dict:
    """Record the actual read result once. metadata.query_state=received/partial/failed, reason/time as observed.
    snapshot is normalized actual data: provider (NH PLUG/Meritz), as_of with timezone,
    accounts[{alias,positions:[...],...}].
    It must use the existing dashboard account schema for holdings, currency, quote_symbol and completeness;
    missing data stays missing, never zero. Official broker tools supply balances; this tool cannot query or order.
    """
    return AccountStore().complete(provider,request_id,lease,metadata=metadata,snapshot=snapshot)


if __name__ == '__main__': mcp.run()
