"""Read-only STDIO MCP fallback when an already-open chat has an old tool catalog.

Prints transient results to the caller, never to a news file/cache. Uses the same
analysis_mcp.py server registered in Codex, not a parallel Telegram data path.
"""
import argparse
import asyncio
import json
from pathlib import Path
import sys
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT=Path(__file__).resolve().parents[1]
TOOLS={'status':'telegram_news_status','dialogs':'telegram_news_dialogs','search':'telegram_news_search'}


async def call(op,args):
    if op not in TOOLS:
        return {'error':'read_only_operation'}
    # Start the persistent connector before creating the temporary MCP job.
    # The Windows MCP SDK kills its entire child job when this one-shot call ends.
    # A connector created inside that job would lose its in-memory room lookup.
    from news.bridge import ensure_health
    from urllib.request import build_opener, ProxyHandler
    try:
        await asyncio.to_thread(ensure_health, build_opener(ProxyHandler({})))
    except (OSError, ValueError):
        pass  # The MCP tool returns the normal unavailable/identity error.
    params=StdioServerParameters(command=sys.executable,args=['-B','-X','utf8',str(ROOT/'scripts/analysis_mcp.py')],cwd=str(ROOT))
    async with stdio_client(params) as (read,write):
        async with ClientSession(read,write) as session:
            await session.initialize()
            result=await session.call_tool(TOOLS[op],args)
            if result.isError:
                return {'error':'mcp_tool_failed'}
            return json.loads(result.content[0].text)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='op',required=True)
    sub.add_parser('status')
    rooms=sub.add_parser('dialogs');rooms.add_argument('--offset',type=int,default=0);rooms.add_argument('--limit',type=int,default=100);rooms.add_argument('--refresh',action='store_true')
    search=sub.add_parser('search')
    for key in ('room-id','query','start','end'):search.add_argument('--'+key,required=True)
    search.add_argument('--offset-id',type=int,default=0);search.add_argument('--limit',type=int,default=30)
    args=vars(parser.parse_args());op=args.pop('op')
    try:
        result=asyncio.run(call(op,args))
    except Exception:
        result={'error':'mcp_connection_failed'}
    print(json.dumps(result,ensure_ascii=False))
    return 1 if result.get('error') else 0


if __name__=='__main__':
    if sys.platform=='win32':sys.stdout.reconfigure(encoding='utf-8')
    raise SystemExit(main())
