"""Registered MCP tools use the existing stores without dashboard HTTP or launch."""
import asyncio
from contextlib import closing
from datetime import timedelta
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest

os.environ.setdefault('PYDANTIC_DISABLE_PLUGINS', '__all__')
SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
from analysis.service import AnalysisService
from drawings.store import WorkspaceStore
from market.history.chart import aggregate
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


class DirectSessionMcpTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.db = self.root / 'runtime/chart-workspaces.sqlite3'
        bars = [dict(date=day, open=10, high=12, low=9, close=11, volume=100)
                for day in ('2026-10-12', '2026-10-13', '2026-10-14', '2026-10-15')]
        stock = dict(symbol='SYNTH', market='US', currency='USD', bars=bars, price_basis='adjusted')
        history = SimpleNamespace(saved=SimpleNamespace(stocks={}),
            view=lambda value, frame: {**value, 'timeframe': frame,
                'bars': aggregate(value['bars'], frame), 'history': {'state': 'complete'}})
        financials = SimpleNamespace(read=lambda key: {'state': 'missing'})
        view = lambda: ({'accounts': {'accounts': [], 'limitations': ['fixture_only']},
                         'market': {'state': 'saved_fixture'}}, {'US:SYNTH': stock})
        service = AnalysisService(WorkspaceStore(self.db), history, financials, view, self.root)
        detail = service.dispatch('create', {'stock_key': 'US:SYNTH', 'test_record': True},
                                  'http://127.0.0.1:1')
        self.case = detail['case']['id']
        self.snapshot = detail['snapshots'][0]['id']
        self.wrapper = self.root / 'isolated_mcp.py'
        self.wrapper.write_text(
            'import sys, subprocess, urllib.request, http.client, webbrowser\n'
            + 'from pathlib import Path\n'
            + 'sys.path.insert(0, ' + repr(str(SCRIPTS)) + ')\n'
            + 'import workbench_paths\nworkbench_paths.ROOT = Path(' + repr(str(self.root)) + ')\n'
            + 'from session_mcp import mcp\n'
            + 'def forbidden(*args, **kwargs):\n    raise RuntimeError("unexpected_external_or_dashboard_dependency")\n'
            + 'subprocess.Popen = forbidden\nurllib.request.urlopen = forbidden\n'
            + 'http.client.HTTPConnection.connect = forbidden\nwebbrowser.open = forbidden\n'
            + '@mcp.tool()\ndef fixture_modules() -> dict:\n'
            + '    return {"loaded": [x for x in ("web.server", "web.data", "launch.startup") if x in sys.modules]}\n'
            + 'mcp.run(transport="stdio")\n', encoding='utf-8')

    async def run_session(self, role, action):
        parameters = StdioServerParameters(command=sys.executable, args=[str(self.wrapper)],
            env={'PYTHONUTF8': '1', 'INVEST_ANALYSIS_ROLE': role})
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=120)) as client:
                await client.initialize()
                result = await action(client)
                state = await self.tool(client, 'fixture_modules', {})
                self.assertEqual(state['loaded'], [])
                return result

    async def tool(self, client, name, arguments, *, error=False):
        response = await client.call_tool(name, arguments)
        self.assertEqual(bool(response.isError), error, str(response.content))
        if error:
            return response
        return json.loads(next(item.text for item in response.content if item.type == 'text'))

    def test_existing_snapshot_and_record_roundtrip_without_dashboard(self):
        original = self.db.read_bytes()

        async def read_history(client):
            names = {tool.name for tool in (await client.list_tools()).tools}
            self.assertFalse({'analysis_adopt', 'analysis_record_fill', 'analysis_create'} & names)
            history = await self.tool(client, 'analysis_history', {'analysis_key': self.case})
            self.assertEqual(history['case']['id'], self.case)
            self.assertIn(self.snapshot, [row['id'] for row in history['snapshots']])
            chart = await self.tool(client, 'analysis_read', {
                'analysis_key': self.case, 'snapshot_id': self.snapshot,
                'section': 'chart', 'offset': 0, 'limit': 2})
            self.assertEqual(chart['snapshot_id'], self.snapshot)
            self.assertEqual(chart['next_offset'], 2)
            return history

        asyncio.run(self.run_session('collaboration', read_history))
        self.assertEqual(self.db.read_bytes(), original)

        async def append_record(client):
            result = await self.tool(client, 'analysis_save_record', {
                'analysis_key': self.case, 'snapshot_id': self.snapshot, 'kind': 'review',
                'title': 'Isolated contract fixture', 'body': 'Synthetic evidence only; no user investment view.',
                'author': 'llm'})
            return result['id']

        record_id = asyncio.run(self.run_session('collaboration', append_record))

        async def reopen_as_risk(client):
            history = await self.tool(client, 'analysis_history', {'analysis_key': self.case})
            record = next(row for row in history['items'] if row['id'] == record_id)
            self.assertEqual(record['snapshot_id'], self.snapshot)
            self.assertEqual(record['body'], 'Synthetic evidence only; no user investment view.')
            await self.tool(client, 'analysis_save_record', {
                'analysis_key': self.case, 'snapshot_id': self.snapshot, 'kind': 'review',
                'title': 'Forbidden write', 'body': 'Must not persist.', 'author': 'llm'}, error=True)

        asyncio.run(self.run_session('risk', reopen_as_risk))
        with closing(sqlite3.connect(self.db)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM analysis_items WHERE kind='review'").fetchone()[0], 1)
        self.assertFalse((self.root / 'runtime/dashboard').exists())


if __name__ == '__main__':
    unittest.main()
