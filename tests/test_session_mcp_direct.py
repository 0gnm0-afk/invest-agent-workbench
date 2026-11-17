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


def run_selector(coroutine):
    """Use guarded socket IO instead of Windows native overlapped operations."""
    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        return runner.run(coroutine)

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

        run_selector(self.run_session('collaboration', read_history))
        self.assertEqual(self.db.read_bytes(), original)

        async def append_record(client):
            result = await self.tool(client, 'analysis_save_record', {
                'analysis_key': self.case, 'snapshot_id': self.snapshot, 'kind': 'review',
                'title': 'Isolated contract fixture', 'body': 'Synthetic evidence only; no user investment view.',
                'author': 'llm'})
            return result['id']

        record_id = run_selector(self.run_session('collaboration', append_record))

        async def reopen_as_risk(client):
            history = await self.tool(client, 'analysis_history', {'analysis_key': self.case})
            record = next(row for row in history['items'] if row['id'] == record_id)
            self.assertEqual(record['snapshot_id'], self.snapshot)
            self.assertEqual(record['body'], 'Synthetic evidence only; no user investment view.')
            await self.tool(client, 'analysis_save_record', {
                'analysis_key': self.case, 'snapshot_id': self.snapshot, 'kind': 'review',
                'title': 'Forbidden write', 'body': 'Must not persist.', 'author': 'llm'}, error=True)

        run_selector(self.run_session('risk', reopen_as_risk))
        with closing(sqlite3.connect(self.db)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM analysis_items WHERE kind='review'").fetchone()[0], 1)
        self.assertFalse((self.root / 'runtime/dashboard').exists())


    async def run_wire_session(self, action):
        """Exercise SDK JSONL transport in this guarded worker, without spawning."""
        import anyio
        from mcp import types as mcp_types
        from mcp.server.stdio import stdio_server
        from mcp.shared.message import SessionMessage
        from session_mcp import mcp

        # These streams replace only stdin/stdout IO. SDK parsing, serialization,
        # request dispatch, initialization and tool execution remain real.
        input_send, input_receive = anyio.create_memory_object_stream(0)
        output_send, output_receive = anyio.create_memory_object_stream(0)
        client_send, client_receive = anyio.create_memory_object_stream(0)
        request_send, request_receive = anyio.create_memory_object_stream(0)
        wire = {'requests': [], 'responses': []}

        class TextInput:
            def __aiter__(self):
                return self

            async def __anext__(self):
                try:
                    return await input_receive.receive()
                except anyio.EndOfStream:
                    raise StopAsyncIteration

        class TextOutput:
            async def write(self, line):
                wire['responses'].append(line)
                await output_send.send(line)
                return len(line)

            async def flush(self):
                pass

        async def server():
            async with stdio_server(stdin=TextInput(), stdout=TextOutput()) as (read, write):
                await mcp._mcp_server.run(read, write, mcp._mcp_server.create_initialization_options())

        async def send_requests():
            async with request_receive, input_send:
                async for message in request_receive:
                    line = message.message.model_dump_json(by_alias=True, exclude_none=True) + '\n'
                    wire['requests'].append(line)
                    await input_send.send(line)

        async def read_responses():
            async with output_receive, client_send:
                async for line in output_receive:
                    await client_send.send(SessionMessage(mcp_types.JSONRPCMessage.model_validate_json(line)))

        with anyio.fail_after(20):
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(server)
                tasks.start_soon(send_requests)
                tasks.start_soon(read_responses)
                try:
                    async with ClientSession(client_receive, request_send,
                                             read_timeout_seconds=timedelta(seconds=10)) as client:
                        initialized = await client.initialize()
                        self.assertIn('eps-research', initialized.instructions)
                        result = await action(client)
                finally:
                    # No OS child exists here: cancel only the in-worker IO tasks.
                    tasks.cancel_scope.cancel()
        for stream in (input_send, input_receive, output_send, output_receive,
                       client_send, client_receive, request_send, request_receive):
            await stream.aclose()
        self.assertTrue(all(line.endswith('\n') for line in wire['requests'] + wire['responses']))
        self.assertEqual(json.loads(wire['requests'][0])['method'], 'initialize')
        self.assertTrue(wire['responses'])
        self.assertFalse({'web.server', 'web.data', 'launch.startup'} & set(sys.modules))
        return result

    def test_stdio_wire_roundtrip_in_guarded_worker(self):
        """Real SDK stdio JSONL in M00's guarded worker, not child spawn/inheritance.

        This supplements the subprocess test; it does not satisfy or replace the
        independent server child's lifecycle and inherited-guard acceptance check.
        """
        from contextlib import ExitStack
        import io
        import logging
        from unittest.mock import patch
        import isolation
        import workbench_paths

        self.assertIsNotNone(isolation.ACTIVE, 'Run this fixture through tests/run_isolated.py')
        self.assertTrue(self.root.is_relative_to(Path(isolation.ACTIVE.config['temp'])))
        with ExitStack() as patches:
            patches.enter_context(patch.object(workbench_paths, 'ROOT', self.root))
            import analysis.bridge
            import session_context
            import session_mcp
            # Capture SDK diagnostics in a noninteractive fixture stream. Rich's
            # Windows console renderer is unrelated to stdio protocol behavior.
            log_output = io.StringIO()
            handler = logging.StreamHandler(log_output)
            patches.enter_context(patch.object(logging.getLogger(), 'handlers', [handler]))
            self.addCleanup(handler.close)
            patches.enter_context(patch.object(analysis.bridge, 'ROOT', self.root))
            patches.enter_context(patch.object(session_context, 'ROOT', self.root))
            patches.enter_context(patch.object(session_mcp, 'ROOT', self.root))

            def run(role, action):
                with patch.dict(os.environ, {'INVEST_ANALYSIS_ROLE': role}):
                    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
                        return runner.run(self.run_wire_session(action))

            async def read_history(client):
                tools = (await client.list_tools()).tools
                names = [tool.name for tool in tools]
                self.assertEqual(len(names), 30)
                self.assertEqual(len(set(names)), 30)
                self.assertIn('investment_drawings', names)
                self.assertFalse({'analysis_adopt', 'analysis_record_fill', 'analysis_create',
                                  'analysis_cycles', 'analysis_cycle_read', 'analysis_cycle_input',
                                  'analysis_cycle_calculate', 'analysis_cycle_save_draft'} & set(names))
                history = await self.tool(client, 'analysis_history', {'analysis_key': self.case})
                self.assertEqual(history['case']['id'], self.case)
                self.assertIn(self.snapshot, [row['id'] for row in history['snapshots']])
                chart = await self.tool(client, 'analysis_read', {
                    'analysis_key': self.case, 'snapshot_id': self.snapshot,
                    'section': 'chart', 'offset': 0, 'limit': 2})
                self.assertEqual(chart['snapshot_id'], self.snapshot)
                self.assertEqual(chart['next_offset'], 2)

            original = self.db.read_bytes()
            run('collaboration', read_history)
            self.assertEqual(self.db.read_bytes(), original)

            async def append_record(client):
                result = await self.tool(client, 'analysis_save_record', {
                    'analysis_key': self.case, 'snapshot_id': self.snapshot, 'kind': 'review',
                    'title': 'Guarded wire fixture', 'body': 'Synthetic evidence only; no user investment view.',
                    'author': 'llm'})
                return result['id']

            record_id = run('collaboration', append_record)

            async def reopen_as_risk(client):
                history = await self.tool(client, 'analysis_history', {'analysis_key': self.case})
                record = next(row for row in history['items'] if row['id'] == record_id)
                self.assertEqual(record['snapshot_id'], self.snapshot)
                self.assertEqual(record['body'], 'Synthetic evidence only; no user investment view.')
                await self.tool(client, 'analysis_save_record', {
                    'analysis_key': self.case, 'snapshot_id': self.snapshot, 'kind': 'review',
                    'title': 'Forbidden wire write', 'body': 'Must not persist.', 'author': 'llm'}, error=True)

            run('risk', reopen_as_risk)
            with closing(sqlite3.connect(self.db)) as db:
                self.assertEqual(db.execute("SELECT count(*) FROM analysis_items WHERE kind='review'").fetchone()[0], 1)
            self.assertFalse((self.root / 'runtime/dashboard').exists())

if __name__ == '__main__':
    unittest.main()
