"""Small CLI entrypoint for deterministic engine operations."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path

DEFAULT_MCP_URL = "https://korea-stock-analyzer-mcp-production.up.railway.app/mcp"


def _configure_utf8_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="invest-agent")
    subcommands = parser.add_subparsers(dest="command", required=True)

    fetch = subcommands.add_parser("fetch-data", help="Fetch one normalized Korean stock DataBundle.")
    fetch.add_argument("ticker", help="Six-digit KOSPI/KOSDAQ ticker")
    fetch.add_argument("--years", type=int, default=5, choices=range(2, 11))
    fetch.add_argument("--output", type=Path, help="Write JSON to this path instead of stdout")
    fetch.add_argument(
        "--mcp-url",
        default=os.getenv("KOREA_STOCK_MCP_URL", DEFAULT_MCP_URL),
        help="Streamable HTTP endpoint for Korea Stock MCP",
    )

    calculate = subcommands.add_parser(
        "calculate", help="Calculate C02 DCF and C08 warnings from a JSON file."
    )
    calculate.add_argument("input", type=Path, help="Path to the calculation input JSON")
    calculate.add_argument("--output", type=Path, help="Write JSON to this path instead of stdout")

    analyze = subcommands.add_parser(
        "analyze", help="Fetch provider data and calculate C02/C08 from user assumptions."
    )
    analyze.add_argument("ticker", help="Six-digit KOSPI/KOSDAQ ticker")
    analyze.add_argument("assumptions", type=Path, help="Path to assumptions JSON")
    analyze.add_argument("--years", type=int, default=5, choices=range(3, 6))
    analyze.add_argument(
        "--format",
        choices=("json", "markdown"),
        default="json",
        help="Output format (default: json)",
    )
    analyze.add_argument("--output", type=Path, help="Write output to this path instead of stdout")
    analyze.add_argument(
        "--mcp-url",
        default=os.getenv("KOREA_STOCK_MCP_URL", DEFAULT_MCP_URL),
        help="Streamable HTTP endpoint for Korea Stock MCP",
    )
    backup=subcommands.add_parser('backup',help='Archive and verify the instance DB and referenced artifacts')
    backup.add_argument('--instance',type=Path,required=True)
    restore=subcommands.add_parser('restore',help='Verify an archive and restore to a new directory only')
    restore.add_argument('--archive',type=Path,required=True)
    restore.add_argument('--destination',type=Path,required=True)
    return parser


async def _fetch_data(args: argparse.Namespace) -> str:
    from .korea_stock_provider import KoreaStockMcpProvider
    bundle = await KoreaStockMcpProvider(args.mcp_url).fetch(args.ticker, args.years)
    return json.dumps(bundle.to_dict(), ensure_ascii=False, indent=2)


async def _analyze(args: argparse.Namespace) -> dict:
    from .analysis_pipeline import calculate_from_data_bundles, requested_peer_tickers
    from .korea_stock_provider import KoreaStockMcpProvider
    assumptions = json.loads(args.assumptions.read_text(encoding="utf-8"))
    peer_tickers = requested_peer_tickers(assumptions)
    if args.ticker in peer_tickers:
        raise ValueError("The target ticker cannot also be listed as a peer.")
    provider = KoreaStockMcpProvider(args.mcp_url)
    target = await provider.fetch(args.ticker, args.years)
    # OpenDartReader performs process-global setup; keep DART-backed fetches serial.
    peers = [await provider.fetch(ticker, args.years) for ticker in peer_tickers]
    return calculate_from_data_bundles(target, assumptions, peers)


def main(argv: Sequence[str] | None = None) -> int:
    _configure_utf8_console()
    args = _parser().parse_args(argv)
    if args.command in ('backup','restore'):
        from .trading.backup import create, restore
        try:
            result=create(args.instance) if args.command=='backup' else restore(args.archive,args.destination)
            print(json.dumps(result,ensure_ascii=False,indent=2))
            return 0
        except Exception as exc:  # noqa: BLE001 -- Boundary records failures and isolates optional provider/component errors.
            print(json.dumps({'error':str(exc),'type':type(exc).__name__},ensure_ascii=False),file=sys.stderr)
            return 1
    if args.command == "fetch-data":
        payload = asyncio.run(_fetch_data(args))
    elif args.command == "calculate":
        from .calculators import calculate_analysis
        raw = json.loads(args.input.read_text(encoding="utf-8"))
        payload = json.dumps(calculate_analysis(raw), ensure_ascii=False, indent=2)
    elif args.command == "analyze":
        from .reporting import render_markdown_report
        result = asyncio.run(_analyze(args))
        payload = (
            render_markdown_report(result)
            if args.format == "markdown"
            else json.dumps(result, ensure_ascii=False, indent=2)
        )
    else:
        raise AssertionError(f"Unsupported command: {args.command}")
    if args.output:
        args.output.write_text(payload + "\n", encoding="utf-8")
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
