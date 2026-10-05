"""Synthetic production-path checks: offline boundary and risk MCP roundtrip."""
import asyncio
import http.client
import json
import sys
import threading
import unittest
import test_risk_roundtrip as fixture
from dashboard import handler_for, ThreadingHTTPServer


class Operations(unittest.TestCase):
    def setUp(self):
        self.f = fixture.Roundtrip(); self.f.setUp()
        f = self.f.f
        self.cap = {'collaboration': 'fixture-collab', 'risk': 'fixture-risk'}
        handler = handler_for(f.data, {'US:SYNTH': f.stock}, fixture.ROOT/'dashboard', 'browser', f.store.path,
            analysis_dependencies={'histories': f.hist, 'financials': f.fin}, analysis_capabilities=self.cap,
            risk_options={'clock': self.f.clock, 'allow_test_records': True}, offline=True)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True); self.worker.start()
        self.url = 'http://127.0.0.1:' + str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.worker.join(); self.f.tearDown()

    def test_offline_blocks_refresh_but_chart_and_records_remain_available(self):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port)
        headers = {'Origin': self.url, 'X-Workbench-Token': 'browser', 'Content-Type': 'application/json'}
        try:
            conn.request('GET', '/health'); r = conn.getresponse(); self.assertTrue(json.loads(r.read())['offline'])
            for path, body, expected in [
                ('/api/chart-history/US%3ASYNTH', {'retry': True}, 200),
                ('/api/financials/US%3ASYNTH', {'refresh': True}, 403),
                ('/api/analysis-rpc', {'op': 'refresh', 'arguments': {'analysis_key': self.f.f.cid, 'mode': 'price_history'}}, 403),
            ]:
                conn.request('POST', path, json.dumps(body), headers); r = conn.getresponse()
                self.assertEqual(r.status, expected, r.read())
        finally: conn.close()

    def test_real_risk_mcp_roundtrip_and_collaboration_denial(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        root = self.f.f.root
        endpoint = root/'endpoint.json'
        endpoint.write_text(json.dumps({'url': self.url, 'analysis_capabilities': self.cap}), encoding='utf-8')
        wrapper = root/'mcp_fixture.py'
        wrapper.write_text('import sys,runpy\nfrom pathlib import Path\nsys.path.insert(0,' + repr(str(fixture.ROOT/'scripts')) + ')\nimport analysis_bridge\nanalysis_bridge.ENDPOINT=Path(' + repr(str(endpoint)) + ')\nrunpy.run_module("analysis_mcp",run_name="__main__")\n', encoding='utf-8')
        async def run(role):
            params = StdioServerParameters(command=sys.executable, args=[str(wrapper)], env={'INVEST_ANALYSIS_ROLE': role, 'PYTHONUTF8': '1'})
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as client:
                    await client.initialize()
                    response = await client.call_tool('risk_overview', {})
                    self.assertEqual(bool(response.isError), role != 'risk')
                    if role != 'risk': return
                    response = await client.call_tool('risk_calculate', self.f.request())
                    self.assertFalse(response.isError, response)
                    result = json.loads(response.content[0].text)
                    self.assertEqual(result['result']['response']['total'], '1062500.0')
                    response = await client.call_tool('risk_save', {'ticket': result['ticket'], 'analysis_key': self.f.f.cid, 'title': 'MCP fixture', 'body': 'synthetic only'})
                    self.assertFalse(response.isError, response)
                    self.assertTrue(json.loads(response.content[0].text)['verified'])
        asyncio.run(run('collaboration')); asyncio.run(run('risk'))


if __name__ == '__main__': unittest.main()
