"""M09 wrapper contracts; executed only inside the M00 synthetic workspace."""
import ast
import asyncio
from contextlib import closing
from copy import deepcopy
import hashlib
import importlib
import json
from pathlib import Path
import sqlite3
import sys
import unittest
from unittest.mock import Mock, patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import session_mcp
from analysis import Forbidden
from drawings.store import Conflict, WorkspaceStore, defaults
from test_analysis_direct import DirectAnalysisTests, FIXTURE
from test_drawing_interpretation import DrawingInterpretation
analysis_mcp = importlib.import_module('analysis.mcp')

EXPECTED_TOOLS = frozenset('''
knowledge_ingest knowledge_note knowledge_search investment_context knowledge_source
knowledge_source_export knowledge_note_read knowledge_summary knowledge_summary_history
knowledge_summary_read investment_signal_status investment_signal_watch investment_signal_rules
analysis_list analysis_monitors analysis_read analysis_history analysis_report_document
analysis_propose analysis_save_record analysis_holding_links analysis_refresh
analysis_collaboration_read analysis_save_discussion analysis_save_plan analysis_plan_input
telegram_news_status telegram_news_dialogs telegram_news_search investment_drawings
'''.split())
# Arguments and complete news functions from the fdc4449 M09 base, excluding the new tool.
EXISTING_ARGUMENTS_SHA256 = '70fa7600def41a33e4fba292d6317e09ca231e80c3fa2785139eba810455f4da'
NEWS_AST_SHA256 = {
    'telegram_news_status': 'c30a8de83da27d88752eaa3f127fa2f8d6f64ff3df671f47a716807640c7534f',
    'telegram_news_dialogs': '601148569f647a0e0895415e05662413984760112e0a29acd9c1c2c3bab45bcf',
    'telegram_news_search': '2f8fafb5fd0ddf9f3f54c537321702cbd2960f08172d09310f444b07adf93a3a',
}


def run_async(coroutine):
    # M00 allows IPv4 socketpairs; Windows Proactor native IO is intentionally denied.
    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        return runner.run(coroutine)


def file_hashes(root):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob('*') if p.is_file()}


class SessionMcpContractTests(unittest.TestCase):
    def packet(self):
        coords = [{'time': '2026-09-01', 'price': 100}, {'time': '2026-09-10', 'price': 110}]
        drawing = dict(drawing_id='line', workspace_revision=2, workspace_saved_at='2026-09-11T10:00:00Z',
                       coordinates=coords, drawing={'points': coords}, levels=[{'distance': -2, 'distance_pct': -2}],
                       summary='저장 완료 일봉 종가 98 (2026-09-11) · 거리 -2 (-2%)')
        return dict(stock_key='US:FIX', read_only=True, summary='최신 저장 작도: 합성 해석\n채택 계획 참조 작도: 합성 해석',
                    price={'date': '2026-09-11', 'close': 98, 'status': 'ok'},
                    latest=dict(status='ok', workspace_revision=2, workspace_saved_at='2026-09-11T10:00:00Z',
                                drawings=[drawing], payload={'drawings': [drawing]}),
                    adopted=dict(status='ok', references=[{'snapshot_drawing': drawing}], drawings=[dict(drawing, workspace_revision=1)]),
                    automatic_l1=dict(status='ok', kind='automatic_l1_reference',
                        window={'start': '2026-03-11', 'end': '2026-09-11', 'timeframe': '1d'},
                        calculated_window={'start': '2026-03-12', 'end': '2026-09-11', 'timeframe': '1d', 'bar_count': 100},
                        lines=[{'direction': 'up', 'points': coords}], stored=False, signal=False))

    def drawing_fixture(self):
        fixture = DrawingInterpretation('test_saved_drawings_packet_and_summary')
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        fixture.store = WorkspaceStore(fixture.root / 'runtime/chart-workspaces.sqlite3')
        fixture.store.shared('US:FIX', revision=0, state=fixture.state)
        return fixture

    def reader(self, fixture):
        actual = session_mcp.read_drawings
        def read(key, **kwargs):
            return actual(key, **kwargs, now=fixture.now,
                          price_reader=lambda *a, **k: deepcopy(fixture.packet),
                          price_context_reader=lambda *a, **k: deepcopy(fixture.contexts),
                          sessions_reader=lambda *a: ['2026-09-23'])
        return read

    def analysis_fixture(self):
        fixture = DirectAnalysisTests('test_discussion_and_plan_use_existing_revision_and_never_adopt')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        return fixture

    def test_registered_names_schemas_and_readonly_annotations(self):
        tools = run_async(session_mcp.mcp.list_tools())
        names = [tool.name for tool in tools]
        self.assertEqual(len(names), 30)
        self.assertEqual(set(names), EXPECTED_TOOLS)
        self.assertEqual(len(names), len(set(names)))
        self.assertFalse(any(name.startswith('analysis_cycle') for name in names))
        tool = next(tool for tool in tools if tool.name == 'investment_drawings')
        self.assertEqual(set(tool.inputSchema['properties']), {'stock_key', 'start', 'end', 'timeframe'})
        self.assertEqual(tool.inputSchema['required'], ['stock_key'])
        self.assertEqual(tool.inputSchema['properties']['timeframe']['default'], '1d')
        for field in ('start', 'end'):
            self.assertIsNone(tool.inputSchema['properties'][field]['default'])
        self.assertTrue(tool.annotations.readOnlyHint)
        self.assertFalse(tool.annotations.destructiveHint)
        self.assertFalse(tool.annotations.openWorldHint)
        # Listing metadata must not start the deferred news bridge.
        self.assertNotIn('news.bridge', sys.modules)

    def test_existing_arguments_and_news_functions_unchanged(self):
        arguments, news = {}, {}
        for name in ('session_mcp.py', 'analysis/mcp.py'):
            tree = ast.parse((SCRIPTS / name).read_text(encoding='utf-8-sig'))
            for node in tree.body:
                if isinstance(node, ast.FunctionDef) and node.decorator_list and node.name != 'investment_drawings':
                    arguments[node.name] = ast.dump(node.args, include_attributes=False)
                    if node.name.startswith('telegram_news_'):
                        news[node.name] = hashlib.sha256(ast.dump(node, include_attributes=False).encode()).hexdigest()
        self.assertEqual(set(arguments), EXPECTED_TOOLS - {'investment_drawings'})
        self.assertEqual(hashlib.sha256(json.dumps(arguments, sort_keys=True).encode()).hexdigest(), EXISTING_ARGUMENTS_SHA256)
        self.assertEqual(news, NEWS_AST_SHA256)

    def test_descriptions_name_research_copy_and_hypothesis_contract(self):
        tools = {tool.name: tool for tool in run_async(session_mcp.mcp.list_tools())}
        descriptions = '\n'.join(tool.description or '' for tool in tools.values())
        instructions = session_mcp.mcp.instructions
        for text in (descriptions, instructions):
            self.assertNotIn('stock-qualitative-analysis', text)
            self.assertNotIn('codex', text.lower())
        self.assertIn('eps-research', instructions)
        self.assertIn('deep-research', instructions)
        self.assertIn('세션에 붙여 넣을 문맥 복사', instructions)
        description = tools['analysis_save_plan'].description
        for field in ('hypothesis', 'verification_date', 'invalidation_statement', 'pre_verification_allocation'):
            self.assertIn(field, description)
        self.assertIn('null', description)
        self.assertEqual(tools['analysis_save_plan'].inputSchema['properties']['plan']['type'], 'object')
        self.assertNotIn('author', tools['analysis_save_plan'].inputSchema['properties'])

    def test_drawings_pass_through_default_and_explicit_windows(self):
        packet = self.packet()
        original = deepcopy(packet)
        for args in ({}, {'start': '2026-03-01', 'end': '2026-09-11', 'timeframe': '1w'},
                     {'timeframe': '1mo'}):
            with self.subTest(args=args), patch.object(session_mcp, 'read_drawings', return_value=packet) as read:
                result = session_mcp.investment_drawings('US:FIX', **args)
            self.assertIs(result, packet)
            self.assertEqual(packet, original)
            key_args, kwargs = read.call_args
            self.assertEqual(key_args, ('US:FIX',))
            self.assertEqual(kwargs['root'], session_mcp.ROOT)
            self.assertIsInstance(kwargs['workspace_store'], WorkspaceStore)
            self.assertEqual(kwargs['workspace_store'].path, session_mcp.ROOT / 'runtime/chart-workspaces.sqlite3')
            self.assertEqual({k: v for k, v in kwargs.items() if k not in ('root', 'workspace_store')},
                             dict(start=args.get('start'), end=args.get('end'), timeframe=args.get('timeframe', '1d'), include_l1=True))
            read.assert_called_once()

    def test_context_preserves_pages_drawings_and_complete_c3_result(self):
        packet = self.packet()
        knowledge = {'history': {'next_offset': 4}, 'unresolved_page': {'next_offset': 7}, 'next_steps_page': {'next_offset': 9}}
        operations = {'drawings': [{'payload': {'points': ['raw']}}], 'records_page': {'next_offset': 4}, 'errors': []}
        originals = deepcopy((packet, knowledge, operations))
        with patch.object(session_mcp, 'store') as store, patch.object(session_mcp, 'saved_context', return_value=operations) as saved, \
                patch.object(session_mcp, 'investment_drawings', return_value=packet) as read:
            store.return_value.context.return_value = knowledge
            result = session_mcp.investment_context('US:FIX', 'topic', offset=2, limit=2, unresolved_offset=5, next_steps_offset=7)
        store.return_value.context.assert_called_once_with(stock_key='US:FIX', topic='topic', offset=2, limit=2, unresolved_offset=5, next_steps_offset=7)
        saved.assert_called_once_with('US:FIX', root=session_mcp.ROOT, offset=2, limit=2)
        read.assert_called_once_with('US:FIX')
        self.assertEqual(result['knowledge'], knowledge)
        self.assertEqual(result['operations']['records_page'], operations['records_page'])
        self.assertEqual(result['operations']['drawings'], operations['drawings'])
        self.assertEqual({k: v for k, v in result['operations'].items() if k != 'drawing_context'}, operations)
        context = result['operations']['drawing_context']
        self.assertEqual(context, packet)
        self.assertEqual(context['latest']['drawings'][0]['workspace_revision'], 2)
        self.assertEqual(context['adopted']['drawings'][0]['workspace_revision'], 1)
        self.assertEqual(context['latest']['drawings'][0]['levels'][0]['distance'], -2)
        self.assertEqual(context['automatic_l1']['lines'], packet['automatic_l1']['lines'])
        self.assertEqual((packet, knowledge, operations), originals)

    def test_topic_only_and_errors_do_not_guess_or_retry(self):
        with patch.object(session_mcp, 'store') as store, patch.object(session_mcp, 'saved_context') as saved, \
                patch.object(session_mcp, 'investment_drawings') as read:
            store.return_value.context.return_value = {'history': {'next_offset': None}}
            result = session_mcp.investment_context(topic='semiconductors')
            self.assertEqual(set(result), {'knowledge'})
            saved.assert_not_called()
            read.assert_not_called()
            store.return_value.context.side_effect = ValueError('invalid_page')
            with self.assertRaisesRegex(ValueError, 'invalid_page'):
                session_mcp.investment_context('US:FIX', offset=-1)
            saved.assert_not_called()
            read.assert_not_called()
        for exc in (ValueError('invalid_timeframe'), Conflict('revision'), Forbidden('role')):
            with patch.object(session_mcp, 'read_drawings', side_effect=exc) as read:
                with self.assertRaises(type(exc)):
                    session_mcp.investment_drawings('US:FIX')
                read.assert_called_once()

    def test_real_c3_preserves_revisions_l1_and_all_fixture_files(self):
        fixture = self.drawing_fixture()
        fixture.adopt(timeframe='1d')
        fixture.store.shared('US:FIX', revision=1, state=defaults())
        with closing(sqlite3.connect(fixture.store.path)) as keeper:
            keeper.execute('PRAGMA journal_mode=WAL')
            keeper.execute('UPDATE shared_chart_workspaces SET payload=payload')
            keeper.commit()
            before = file_hashes(fixture.root)
            self.assertTrue(any(name.endswith('-wal') for name in before))
            self.assertTrue(any(name.endswith('-shm') for name in before))
            with patch.object(session_mcp, 'ROOT', fixture.root), patch.object(session_mcp, 'read_drawings', side_effect=self.reader(fixture)), \
                    patch.object(WorkspaceStore, 'connect', side_effect=AssertionError('writer called')), \
                    patch.object(WorkspaceStore, 'shared', side_effect=AssertionError('workspace mutation')):
                result = session_mcp.investment_drawings('US:FIX', start='2026-09-01', end='2026-09-23')
                expected = fixture.read(include_l1=True, start='2026-09-01', end='2026-09-23')
            self.assertEqual(result, expected)
            self.assertEqual(result['latest']['workspace_revision'], 2)
            self.assertEqual(result['adopted']['drawings'][0]['workspace_revision'], 1)
            self.assertEqual(result['price']['date'], '2026-09-23')
            self.assertEqual(result['automatic_l1']['window'], {'start': '2026-09-01', 'end': '2026-09-23', 'timeframe': '1d'})
            self.assertEqual(file_hashes(fixture.root), before)

    def test_real_c3_missing_prices_store_and_invalid_evidence_stay_explicit(self):
        fixture = self.drawing_fixture()
        with patch.object(session_mcp, 'ROOT', fixture.root), patch.object(session_mcp, 'read_drawings', side_effect=self.reader(fixture)):
            before = file_hashes(fixture.root)
            with self.assertRaises(ValueError):
                session_mcp.investment_drawings('not an exact key')
            self.assertEqual(before, file_hashes(fixture.root))
            fixture.packet['bars'] = []
            missing = session_mcp.investment_drawings('US:FIX')
            self.assertEqual(missing['price']['status'], 'price_missing')
            self.assertIsNone(missing['price']['close'])
            self.assertEqual(missing['automatic_l1']['status'], 'unavailable')
            self.assertTrue(all(v['distance'] is None for d in missing['latest']['drawings'] for v in d['levels']))
            self.assertEqual(before, file_hashes(fixture.root))
            absent_root = fixture.root / 'absent'
            with patch.object(session_mcp, 'ROOT', absent_root):
                missing_store = session_mcp.investment_drawings('US:FIX')
            self.assertEqual(missing_store['latest']['status'], 'missing_database')
            self.assertFalse(absent_root.exists())
            fixture.packet['bars'] = deepcopy(fixture.bars)
            outside = deepcopy(fixture.state)
            outside['drawings'][0]['points'][0]['time'] = '1990-01-01'
            fixture.store.shared('US:FIX', revision=1, state=outside)
            outside_before = file_hashes(fixture.root)
            invalid = session_mcp.investment_drawings('US:FIX')
            self.assertEqual(invalid['price']['status'], 'ok')
            line = invalid['latest']['drawings'][0]
            self.assertEqual(line['reason'], 'anchor_outside_supported_timeline')
            self.assertEqual(line['status'], 'unavailable')
            self.assertTrue(all(level['distance'] is None and level['distance_pct'] is None
                                for level in line['levels']))
            self.assertEqual(file_hashes(fixture.root), outside_before)
            with closing(sqlite3.connect(fixture.store.path)) as db, db:
                db.execute("UPDATE shared_chart_workspaces SET payload='{}'")
            corrupt_before = file_hashes(fixture.root)
            corrupt = session_mcp.investment_drawings('US:FIX')
            self.assertNotEqual(corrupt['latest']['status'], 'ok')
            self.assertNotEqual(corrupt['latest']['status'], 'empty')
            self.assertEqual(corrupt_before, file_hashes(fixture.root))

    def test_plan_hypothesis_roundtrip_nulls_versions_and_no_adoption(self):
        fixture = self.analysis_fixture()
        plan = deepcopy(FIXTURE['plan'])
        hypothesis = dict(verification_date='2028-02-29', invalidation_statement='  반복 매출이 늘어도\n재구매 이유가 사라지면 재검토  ',
                          pre_verification_allocation='  전체 투자자산(현금 포함)의 5% 이내\n실적 전에는 추가하지 않음  ')
        plan['hypothesis'] = hypothesis
        with patch.object(analysis_mcp, 'call', side_effect=fixture.invoke):
            first = analysis_mcp.analysis_save_plan(fixture.cid, fixture.sid, 0, plan, [])
            self.assertTrue(first['verified'])
            record = first['record']
            self.assertEqual(record['snapshot_id'], fixture.sid)
            self.assertEqual(record['plan']['hypothesis'], hypothesis)
            self.assertEqual(record['plan_version'], 1)
            reopened = analysis_mcp.analysis_collaboration_read(fixture.cid)
            self.assertEqual(reopened['active_plans'], [])
            plan['hypothesis'] = {field: None for field in hypothesis}
            second = analysis_mcp.analysis_save_plan(fixture.cid, fixture.sid, reopened['revision'], plan, [],
                                                    plan_id=record['plan_id'], expected_plan_version=1)
            self.assertTrue(second['verified'])
            self.assertEqual(second['record']['plan']['hypothesis'], plan['hypothesis'])
            self.assertEqual(second['record']['plan_id'], record['plan_id'])
            self.assertEqual(second['record']['plan_version'], 2)
            self.assertNotEqual(second['record']['id'], record['id'])
            packet = analysis_mcp.analysis_plan_input(fixture.cid, record['id'], mode='hypothetical')
            self.assertEqual(packet['plan_record']['plan']['hypothesis'], hypothesis)
            before = file_hashes(fixture.root)
            with self.assertRaises(Conflict):
                analysis_mcp.analysis_save_plan(fixture.cid, fixture.sid, 0, plan, [])
            self.assertEqual(file_hashes(fixture.root), before)
            self.assertEqual(analysis_mcp.analysis_collaboration_read(fixture.cid)['active_plans'], [])

    def test_plan_permissions_and_read_page_failures_propagate_once(self):
        fixture = self.analysis_fixture()
        # Establish the historical reader's SQLite WAL sidecars before measuring
        # error calls; the C3 test separately checks a cold read and every file.
        fixture.invoke('read', dict(analysis_key=fixture.cid, snapshot_id=fixture.sid, section='chart'))
        before = file_hashes(fixture.root)
        with patch.object(analysis_mcp, 'call', side_effect=lambda op, args: fixture.invoke(op, args, role='risk')) as bridge:
            with self.assertRaises(Forbidden):
                analysis_mcp.analysis_save_plan(fixture.cid, fixture.sid, 0, deepcopy(FIXTURE['plan']), [])
            bridge.assert_called_once()
        with patch.object(analysis_mcp, 'call', side_effect=fixture.invoke) as bridge:
            with self.assertRaises(ValueError):
                analysis_mcp.analysis_read(fixture.cid, fixture.sid, section='chart', offset=-1)
            bridge.assert_called_once()
        self.assertEqual(file_hashes(fixture.root), before)
