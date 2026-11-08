from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import timedelta
import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from knowledge import KnowledgeStore


class KnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'records.sqlite3'
        self.store = KnowledgeStore(self.path)

    def source(self, text='Original research, uncertainty retained.'):
        return self.store.ingest('Fixture research', text=text, topics=['chips'], stock_keys=['US:AAA'])

    def note(self, body='Margins may improve; input costs unverified.', **kwargs):
        values = dict(kind='hypothesis', body=body, author='user', stock_keys=['US:AAA'])
        values.update(kwargs)
        return self.store.append_note(**values)

    def test_empty_reads_do_not_create_db(self):
        self.assertFalse(self.store.context(stock_key='US:AAA')['found'])
        self.assertEqual(self.store.search('AAA')['total'], 0)
        self.assertEqual(self.store.summaries('US:AAA'), [])
        self.assertFalse(self.path.exists())

    def test_coexists_with_existing_records_and_read_does_not_migrate(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('CREATE TABLE existing(value TEXT)')
            db.execute("INSERT INTO existing VALUES ('keep')")
            db.commit()
        self.assertFalse(self.store.context(stock_key='US:AAA')['found'])
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT name FROM sqlite_master WHERE name LIKE 'knowledge_%'").fetchall(), [])
        self.source()
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute('SELECT * FROM existing').fetchall(), [('keep',)])

    def test_raw_file_survives_deletion_dedup_and_new_session(self):
        file = self.root / 'source.md'
        raw = b'\xef\xbb\xbfOriginal\r\nwith exact bytes.\r\n'
        file.write_bytes(raw)
        one = self.store.ingest('Report', file_path=str(file), session_id='chat-one', stock_keys=['US:AAA'])
        two = self.store.ingest('Report', file_path=str(file), session_id='chat-two', topics=['chips'])
        self.assertEqual(one['id'], two['id'])
        self.assertTrue(two['idempotent'])
        self.assertEqual(two['sha256'], hashlib.sha256(raw).hexdigest())
        file.unlink()
        reopened = KnowledgeStore(self.path)
        stored = reopened.source(one['id'])
        self.assertEqual(stored['session_ids'], ['chat-one', 'chat-two'])
        self.assertEqual(stored['subjects'], ['US:AAA', 'topic:chips'])
        self.assertEqual(stored['original_paths'], [str(file.resolve())])
        self.assertEqual(reopened.export_source_bytes(one['id']), raw)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM knowledge_blobs').fetchone()[0], 1)

    def test_author_uncertainty_and_pagination_survive_reopen(self):
        source = self.source()
        thought = self.note(source_ids=[source['id']], session_id='first', topics=['chips'])
        self.note('Analyst expects margin improvement.', author='source', kind='observation')
        self.note('Possible demand recovery.', author='ai')
        self.note('Check raw material costs.', kind='question')
        reopened = KnowledgeStore(self.path)
        context = reopened.context(stock_key='US:AAA', limit=1)
        self.assertTrue(context['found'])
        self.assertEqual(context['history']['total'], 5)
        self.assertEqual(context['history']['next_offset'], 1)
        self.assertEqual(len(context['unresolved']), 1)
        self.assertEqual(context['unresolved_page']['total'], 3)
        self.assertEqual(context['unresolved_page']['next_offset'], 1)
        self.assertEqual(reopened.note(thought['id'])['author'], 'user')
        self.assertEqual(reopened.note(thought['id'])['body'], thought['body'])
        ids, offset = [], 0
        while True:
            page = reopened.search(stock_key='US:AAA', offset=offset, limit=2)
            ids.extend(r['id'] for r in page['items'])
            if page['next_offset'] is None:
                break
            offset = page['next_offset']
        self.assertEqual(len(set(ids)), 5)

    def test_summary_revisions_conflict_and_history(self):
        one = self.note()
        first = self.store.save_summary('US:AAA', 'Unverified user thesis.', 0, note_ids=[one['id']], next_step='Check costs.')
        retry = self.store.save_summary('US:AAA', 'Unverified user thesis.', 0, note_ids=[one['id']], next_step='Check costs.')
        self.assertTrue(retry['idempotent'])
        self.assertEqual(first['revision'], retry['revision'])
        self.store.save_summary('US:AAA', 'New evidence still needed.', 1, note_ids=[one['id']])
        with self.assertRaisesRegex(ValueError, 'revision_conflict'):
            self.store.save_summary('US:AAA', 'Stale overwrite.', 1, note_ids=[one['id']])
        versions = KnowledgeStore(self.path).summaries('US:AAA')
        self.assertEqual([r['revision'] for r in versions], [2, 1])
        self.assertEqual(versions[1]['body'], first['body'])
        self.assertEqual(self.store.context(stock_key='US:AAA')['summaries']['US:AAA']['revision'], 2)
        self.assertEqual(versions[0]['status'], 'synthesis_not_user_adoption')

    def test_resolution_and_supersession_preserve_history(self):
        question = self.note(kind='question', body='What is the raw material cost?')
        old = self.note()
        self.note('Answered based on supplied report.', kind='response', resolves=[question['id']])
        latest = self.note('Margins uncertain after cost increase.', supersedes=old['id'])
        context = self.store.context(stock_key='US:AAA', limit=1)
        self.assertEqual([r['id'] for r in context['unresolved']], [latest['id']])
        self.assertEqual(self.store.note(old['id'])['body'], old['body'])
        self.assertEqual(context['history']['total'], 4)
        with self.assertRaisesRegex(ValueError, 'another_authors'):
            self.note('AI marks user thesis settled.', author='ai', resolves=[latest['id']])

    def test_missing_evidence_rolls_back(self):
        self.source()
        with self.assertRaisesRegex(ValueError, 'missing_reference'):
            self.note(source_ids=['ks_missing'])
        with self.assertRaisesRegex(ValueError, 'requires_evidence'):
            self.store.save_summary('US:AAA', 'Unsupported summary.', 0)
        self.assertEqual(self.store.search(stock_key='US:AAA')['total'], 1)

    def test_concurrent_idempotent_notes_and_summary_revision(self):
        self.source()
        def append(_):
            return KnowledgeStore(self.path).append_note('question', 'Same question', 'user', stock_keys=['US:AAA'])
        with ThreadPoolExecutor(max_workers=4) as pool:
            values = list(pool.map(append, range(4)))
        self.assertEqual(len({v['id'] for v in values}), 1)
        self.assertEqual(sum(not v['idempotent'] for v in values), 1)
        note_id = values[0]['id']
        def summarize(i):
            try:
                return KnowledgeStore(self.path).save_summary('US:AAA', 'Different summary ' + str(i), 0, note_ids=[note_id])['revision']
            except ValueError as error:
                return str(error)
        with ThreadPoolExecutor(max_workers=2) as pool:
            versions = list(pool.map(summarize, range(2)))
        self.assertCountEqual(versions, [1, 'knowledge_revision_conflict'])

    def test_immutable_tables_reject_update_delete(self):
        source = self.source()
        note = self.note()
        self.store.save_summary('US:AAA', 'Synthesis.', 0, note_ids=[note['id']])
        with closing(sqlite3.connect(self.path)) as db:
            for table in ('knowledge_sources', 'knowledge_notes', 'knowledge_summaries', 'knowledge_blobs'):
                with self.assertRaisesRegex(sqlite3.IntegrityError, 'knowledge_immutable'):
                    db.execute('DELETE FROM ' + table)
        self.assertTrue(self.store.source(source['id'])['verified'])

    def test_file_and_input_guards(self):
        file = self.root / 'secrets' / 'token.txt'
        file.parent.mkdir()
        file.write_text('not a source')
        with self.assertRaisesRegex(ValueError, 'disallowed_source_file'):
            self.store.ingest('bad', file_path=str(file))
        with self.assertRaisesRegex(ValueError, 'disallowed_source_file'):
            self.store.ingest('bad', file_path=str(self.root / 'records.sqlite3'))
        with self.assertRaisesRegex(ValueError, 'stock_key'):
            self.note(stock_keys=['AAPL'])
        with self.assertRaisesRegex(ValueError, 'pagination'):
            self.store.search(offset=-1)
        with self.assertRaisesRegex(ValueError, 'kind_or_author'):
            self.note(author='adopted')
        self.assertFalse(self.path.exists())

    def test_literal_search_and_topic_intersection(self):
        self.source('Profit margin 20% and literal_under_score.')
        self.source('Another source, no percent.')
        self.assertEqual(self.store.search('%')['total'], 1)
        self.assertEqual(self.store.search('_')['total'], 1)
        self.assertEqual(self.store.search(stock_key='US:AAA', topic='chips')['total'], 2)
        self.assertEqual(self.store.search(stock_key='US:AAA', topic='other')['total'], 0)

    def test_one_stock_study_signal_defer_and_new_session(self):
        source = self.source()
        hypothesis = self.note(source_ids=[source['id']], session_id='study')
        question = self.note('Verify input costs.', kind='question', session_id='study')
        self.store.save_summary('US:AAA', 'User thesis remains provisional.', 0,
                                note_ids=[hypothesis['id'], question['id']], source_ids=[source['id']], next_step='Review cost evidence.')
        signal = self.note('Saved fixture signal requires review.', author='ai', kind='observation',
                           references=[{'kind': 'monitor_event', 'id': 'fixture-event-only'}], session_id='review')
        decision = self.note('Defer until input costs are checked.', kind='defer',
                             references=[{'kind': 'note', 'id': signal['id']}], session_id='review')
        self.store.save_summary('US:AAA', 'User deferred; the input cost question remains open.', 1,
                                note_ids=[decision['id'], question['id']], next_step='Obtain cost evidence.')
        resumed = KnowledgeStore(self.path).context(stock_key='US:AAA')
        self.assertEqual(resumed['summaries']['US:AAA']['revision'], 2)
        self.assertIn(question['id'], {r['id'] for r in resumed['unresolved']})
        self.assertEqual(resumed['history']['total'], 5)
        self.assertEqual(KnowledgeStore(self.path).export_source_bytes(source['id']), source['text'].encode())

    def test_bounded_previews_and_independent_open_pages(self):
        body = '가' * 25000
        source = self.source(body)
        first = self.note(body=body, kind='question')
        second = self.note(body='Second question.', kind='question')
        self.note(body=body, kind='next_step')
        packet = self.store.context(stock_key='US:AAA', limit=1, unresolved_offset=1)
        self.assertEqual(packet['unresolved_page'], {'total': 2, 'offset': 1, 'next_offset': None})
        self.assertEqual(packet['unresolved'][0]['id'], second['id'])
        self.assertEqual(packet['next_steps_page']['total'], 1)
        self.assertEqual(len(packet['next_steps'][0]['body']), 4000)
        self.assertTrue(packet['next_steps'][0]['body_truncated'])
        for reader, item_id, field in ((self.store.note, first['id'], 'body'), (self.store.source, source['id'], 'text')):
            chunks, offset = [], 0
            while True:
                page = reader(item_id, offset=offset, limit=10000)
                chunks.append(page[field])
                offset = page[field + '_page']['next_offset']
                if offset is None:
                    break
            self.assertEqual(''.join(chunks), body)

    def test_original_export_preserves_bytes_mime_names_and_deduplicates(self):
        raw = b'%PDF-1.4\nexact original binary\x00\xff\n'
        original = self.root / '산업 보고서.pdf'
        original.write_bytes(raw)
        source = self.store.ingest('Report', file_path=str(original), source_uri='https://example.invalid/report', as_of='2026-01-01', stock_keys=['US:AAA'])
        original.unlink()
        before = self.path.read_bytes()
        exported = self.store.export_source(source['id'])
        self.assertEqual(Path(exported['path']).read_bytes(), raw)
        self.assertEqual(Path(exported['path']).parent, self.path.parent / '01.원본내보내기')
        self.assertEqual(exported['original_name'], '산업 보고서.pdf')
        self.assertEqual(exported['mime_type'], 'application/pdf')
        self.assertEqual(exported['mime_basis'], 'original_filename_extension')
        self.assertEqual(exported['source']['source_uri'], 'https://example.invalid/report')
        self.assertEqual(exported['source']['as_of'], '2026-01-01')
        self.assertTrue(exported['verified'])
        self.assertFalse(exported['database_modified'])
        self.assertEqual(exported['sha256'], hashlib.sha256(raw).hexdigest())
        again = KnowledgeStore(self.path).export_source(source['id'])
        self.assertTrue(again['idempotent'])
        self.assertEqual(again['path'], exported['path'])
        self.assertEqual(self.path.read_bytes(), before)
        renamed = self.root / 'different-name.pdf'
        renamed.write_bytes(raw)
        other = self.store.ingest('Renamed report', file_path=str(renamed))
        third = self.store.export_source(other['id'])
        self.assertEqual(third['path'], exported['path'])
        self.assertEqual(third['original_name'], 'different-name.pdf')
        self.assertEqual(len(list(Path(exported['path']).parent.iterdir())), 1)

    def test_text_export_conflict_never_overwrites_and_partial_write_cleans_up(self):
        raw = '  원래 발언\n확인할 질문.\n'.encode('utf-8')
        source = self.source(raw.decode('utf-8'))
        directory = self.path.parent / '01.원본내보내기'
        directory.mkdir()
        target = directory / (source['sha256'] + '.txt')
        conflicting = b'keep user-owned data'
        target.write_bytes(conflicting)
        with self.assertRaisesRegex(ValueError, 'export_conflict'):
            self.store.export_source(source['id'])
        self.assertEqual(target.read_bytes(), conflicting)
        target.unlink()
        with patch('knowledge.store.os.fsync', side_effect=OSError('fixture disk failure')):
            with self.assertRaisesRegex(OSError, 'fixture disk failure'):
                self.store.export_source(source['id'])
        self.assertFalse(target.exists())
        result = self.store.export_source(source['id'])
        self.assertIsNone(result['original_name'])
        self.assertEqual(result['mime_type'], 'text/plain')
        self.assertEqual(Path(result['path']).read_bytes(), raw)

    def test_export_rejects_linked_directory_and_target(self):
        source = self.source()
        outside = self.root / 'auth'
        outside.mkdir()
        directory = self.path.parent / '01.원본내보내기'
        junction = False
        try:
            directory.symlink_to(outside, target_is_directory=True)
        except OSError as error:
            if os.name != 'nt':
                self.skipTest('Host cannot create test symlink: ' + type(error).__name__)
            # Windows junctions do not require the symlink privilege. Verify the
            # real filesystem redirection guard on restricted Windows hosts too.
            import _winapi
            _winapi.CreateJunction(str(outside), str(directory))
            junction = True
        with self.assertRaisesRegex(ValueError, 'unsafe_export_directory'):
            self.store.export_source(source['id'])
        self.assertEqual(list(outside.iterdir()), [])
        directory.rmdir() if junction else directory.unlink()
        directory.mkdir()
        private = outside / 'private.txt'
        private.write_text('keep this file', encoding='utf-8')
        target = directory / (source['sha256'] + '.txt')
        if junction:
            _winapi.CreateJunction(str(outside), str(target))
        else:
            target.symlink_to(private)
        with self.assertRaisesRegex(ValueError, 'unsafe_export_target'):
            self.store.export_source(source['id'])
        self.assertEqual(private.read_text(encoding='utf-8'), 'keep this file')

    def test_export_refuses_sensitive_storage_directory(self):
        private_store = KnowledgeStore(self.root / 'secrets' / 'records.sqlite3')
        source = private_store.ingest('Fixture only', text='Original fixture text.')
        before = private_store.path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'unsafe_export_directory'):
            private_store.export_source(source['id'])
        self.assertEqual(private_store.path.read_bytes(), before)
        self.assertFalse((private_store.path.parent / '01.원본내보내기').exists())

    def test_corrupt_original_fails_before_export_file_is_created(self):
        source = self.source()
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('DROP TRIGGER knowledge_blobs_update_guard')
            db.execute('UPDATE knowledge_blobs SET raw=?', (b'fixture corruption',))
            db.commit()
        with self.assertRaisesRegex(ValueError, 'source_integrity_failure'):
            self.store.export_source(source['id'])
        self.assertFalse((self.path.parent / '01.원본내보내기').exists())

    def test_summary_history_and_exact_version_paging_leave_database_unchanged(self):
        note = self.note()
        body = '첫 판단은 아직 가설이다.\n' * 400
        first = self.store.save_summary('US:AAA', body, 0, note_ids=[note['id']], next_step='근거 확인')
        self.store.save_summary('US:AAA', 'New provisional synthesis.', 1, note_ids=[note['id']])
        self.store.save_summary('US:AAA', 'Latest synthesis remains AI-authored.', 2, note_ids=[note['id']])
        before = self.path.read_bytes()
        pages = [self.store.summary_history('US:AAA', offset=i, limit=1) for i in range(3)]
        self.assertEqual([p['items'][0]['revision'] for p in pages], [3, 2, 1])
        self.assertEqual([p['next_offset'] for p in pages], [1, 2, None])
        self.assertEqual(pages[0]['total'], 3)
        old = pages[-1]['items'][0]
        self.assertEqual(len(old['body_preview']), 2000)
        self.assertTrue(old['body_truncated'])
        self.assertEqual(old['note_count'], 1)
        reconstructed, offset = '', 0
        while True:
            packet = self.store.summary_read('US:AAA', 1, offset=offset, limit=1000)
            reconstructed += packet['body']
            self.assertEqual(packet['revision'], 1)
            self.assertEqual(packet['created_at'], first['created_at'])
            self.assertEqual(packet['sha256'], old['sha256'])
            self.assertEqual(packet['note_ids'], [note['id']])
            self.assertEqual(packet['mode'], 'exact_saved_revision')
            offset = packet['body_page']['next_offset']
            if offset is None:
                break
        self.assertEqual(reconstructed, body)
        self.assertEqual(self.path.read_bytes(), before)
        with self.assertRaisesRegex(ValueError, 'summary_not_found'):
            self.store.summary_read('US:AAA', 99)
        with self.assertRaisesRegex(ValueError, 'invalid_revision'):
            self.store.summary_read('US:AAA', True)

    def test_summary_history_reads_missing_db_without_creating_it(self):
        self.assertEqual(self.store.summary_history('US:AAA')['total'], 0)
        with self.assertRaisesRegex(ValueError, 'summary_not_found'):
            self.store.summary_read('US:AAA', 1)
        self.assertFalse(self.path.exists())

    def test_mcp_persistence_across_processes_without_dashboard(self):
        with patch.dict(os.environ, {'PYDANTIC_DISABLE_PLUGINS': '__all__'}):
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
        scripts = Path(__file__).resolve().parents[1] / 'scripts'
        root = self.root / 'isolated-workspace'
        root.mkdir()
        wrapper = self.root / 'mcp_fixture.py'
        wrapper.write_text('import sys\nfrom pathlib import Path\n'
                           + 'sys.path.insert(0, ' + repr(str(scripts)) + ')\n'
                           + 'import workbench_paths\nworkbench_paths.ROOT = Path(' + repr(str(root)) + ')\n'
                           + 'from session_mcp import mcp\nmcp.run(transport="stdio")\n', encoding='utf-8')
        raw = b'%PDF-1.4\nfixture original only; no real company claims\n'
        document = self.root / 'fixture.pdf'
        document.write_bytes(raw)

        async def call(client, name, arguments):
            result = await client.call_tool(name, arguments)
            self.assertFalse(result.isError, str(result.content))
            return json.loads(next(item.text for item in result.content if item.type == 'text'))

        async def first_session():
            params = StdioServerParameters(command=sys.executable, args=[str(wrapper)], env={'PYTHONUTF8': '1'})
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=120)) as client:
                    initialized = await client.initialize()
                    self.assertIn('Session-first investment workspace', initialized.instructions)
                    names = {t.name for t in (await client.list_tools()).tools}
                    self.assertTrue({'knowledge_ingest', 'knowledge_note', 'investment_context'} <= names)
                    source = await call(client, 'knowledge_ingest', dict(title='Isolated fixture report', file_path=str(document),
                                                                       text='Fixture extracted research, not a real opinion.', stock_keys=['US:AAA'], topics=['chips'], session_id='one'))
                    note = await call(client, 'knowledge_note', dict(kind='question', body='Fixture question still unresolved.', author='user',
                                                                    stock_keys=['US:AAA'], source_ids=[source['id']], session_id='one'))
                    summary = await call(client, 'knowledge_summary', dict(subject='US:AAA', body='Fixture question remains open.',
                                                                            expected_revision=0, note_ids=[note['id']], source_ids=[source['id']], next_step='Fixture follow-up.'))
                    self.assertEqual(summary['revision'], 1)
                    return source, note

        source, note = asyncio.run(first_session())

        async def second_session():
            params = StdioServerParameters(command=sys.executable, args=[str(wrapper)], env={'PYTHONUTF8': '1'})
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=120)) as client:
                    await client.initialize()
                    context = await call(client, 'investment_context', {'stock_key': 'US:AAA'})
                    self.assertEqual(context['knowledge']['summaries']['US:AAA']['revision'], 1)
                    self.assertEqual(context['knowledge']['unresolved'][0]['id'], note['id'])
                    self.assertEqual(context['knowledge']['related_topics'], ['topic:chips'])
                    duplicate = await call(client, 'knowledge_note', dict(kind='question', body='Fixture question still unresolved.', author='user',
                                                                         stock_keys=['US:AAA'], source_ids=[source['id']], session_id='two'))
                    self.assertTrue(duplicate['idempotent'])
                    self.assertEqual(duplicate['id'], note['id'])
                    self.assertEqual(duplicate['session_ids'], ['one', 'two'])
                    original = await call(client, 'knowledge_source', {'source_id': source['id']})
                    self.assertEqual(original['sha256'], hashlib.sha256(raw).hexdigest())
                    self.assertTrue(original['verified'])
                    history = await call(client, 'knowledge_summary_history', {'subject': 'US:AAA', 'limit': 1})
                    self.assertEqual(history['items'][0]['revision'], 1)
                    previous = await call(client, 'knowledge_summary_read', {'subject': 'US:AAA', 'revision': 1, 'limit': 10})
                    self.assertEqual(previous['body'], 'Fixture qu')
                    self.assertEqual(previous['body_page']['next_offset'], 10)
                    exported = await call(client, 'knowledge_source_export', {'source_id': source['id']})
                    self.assertEqual(Path(exported['path']).read_bytes(), raw)
                    self.assertEqual(exported['original_name'], document.name)
                    self.assertEqual(exported['mime_type'], 'application/pdf')
                    self.assertTrue(exported['verified'])
                    repeat_export = await call(client, 'knowledge_source_export', {'source_id': source['id']})
                    self.assertTrue(repeat_export['idempotent'])

        asyncio.run(second_session())
        document.unlink()
        export = subprocess.run([sys.executable, '-c',
            'import sys,base64; sys.path.insert(0, ' + repr(str(scripts)) + '); '
            'from knowledge import KnowledgeStore; '
            'print(base64.b64encode(KnowledgeStore(' + repr(str(root / 'runtime/records.sqlite3')) + ').export_source_bytes(' + repr(source['id']) + ')).decode())'],
            check=True, capture_output=True, text=True, timeout=30)
        self.assertEqual(base64.b64decode(export.stdout.strip()), raw)
        self.assertFalse((root / 'runtime/dashboard').exists())
        self.assertFalse((root / 'runtime/chart-workspaces.sqlite3').exists())


if __name__ == '__main__':
    unittest.main()
