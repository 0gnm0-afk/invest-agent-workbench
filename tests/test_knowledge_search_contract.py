"""M02 search/storage boundary contracts using only synthetic temporary data."""
from contextlib import closing
import hashlib
import sqlite3
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from knowledge import KnowledgeStore


class KnowledgeSearchContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'records.sqlite3'
        self.store = KnowledgeStore(self.path)

    def test_literal_filters_order_and_pages(self):
        # All timestamps tie, so source/note insertion order cannot mask ID ordering.
        with patch('knowledge.store._now', return_value='2026-01-01T00:00:00+00:00'):
            title = self.store.ingest('English title only', text='plain source',
                                     stock_keys=['US:AAA'], topics=['chips'])
            percent = self.store.ingest('Percent source', text='literal 20% result',
                                       stock_keys=['US:AAA'], topics=['chips'])
            underscore = self.store.append_note('observation', 'literal_under_score', 'source',
                                                stock_keys=['US:AAA'], topics=['chips'])
            slash = self.store.append_note('observation', r'folder\leaf', 'user',
                                           stock_keys=['US:AAA'], topics=['chips'])
            korean = self.store.append_note('interpretation', '한글 본문', 'ai',
                                            stock_keys=['US:AAA'], topics=['chips'])
            subject = self.store.ingest('Unrelated heading', text='no search term here',
                                       stock_keys=['US:AAA'], topics=['주제검색'])
            other_stock = self.store.append_note('observation', 'chips elsewhere', 'user',
                                                 stock_keys=['US:AAAB'], topics=['chips'])
            other_topic = self.store.append_note('observation', 'long topic name', 'user',
                                                 stock_keys=['US:AAA'], topics=['chipset'])
        fixtures = [title, percent, underscore, slash, korean, subject, other_stock, other_topic]
        ids = lambda records: sorted((record['id'] for record in records), reverse=True)
        cases = [
            ({'query': '%'}, [percent]),
            ({'query': '_'}, [underscore]),
            ({'query': '\\'}, [slash]),
            ({'query': '한글'}, [korean]),
            ({'query': 'english'}, [title]),
            ({'query': '주제검색'}, [subject]),
            ({'query': ''}, fixtures),
            ({'stock_key': 'US:AAA', 'topic': 'chips'}, fixtures[:5]),
            ({'stock_key': 'US:AAA', 'topic': 'chip'}, []),
            ({'stock_key': 'US:AA', 'topic': 'chips'}, []),
            ({'query': '%', 'stock_key': 'US:AAA', 'topic': 'chips'}, [percent]),
        ]
        for arguments, expected in cases:
            with self.subTest(arguments=arguments):
                expected_ids = ids(expected)
                seen, offset = [], 0
                while True:
                    page = self.store.search(**arguments, offset=offset, limit=2)
                    self.assertEqual(set(page), {'items', 'total', 'offset', 'next_offset'})
                    self.assertEqual(page['total'], len(expected_ids))
                    self.assertEqual(page['offset'], offset)
                    self.assertEqual([row['id'] for row in page['items']], expected_ids[offset:offset + 2])
                    seen.extend(row['id'] for row in page['items'])
                    expected_next = offset + 2 if offset + 2 < len(expected_ids) else None
                    self.assertEqual(page['next_offset'], expected_next)
                    if expected_next is None:
                        break
                    offset = expected_next
                self.assertEqual(seen, expected_ids)
                self.assertEqual(len(seen), len(set(seen)))
        beyond = self.store.search(offset=len(fixtures) + 1, limit=200)
        self.assertEqual(beyond, {'items': [], 'total': len(fixtures),
                                  'offset': len(fixtures) + 1, 'next_offset': None})

    def test_backend_injection_keeps_public_shape(self):
        long_text = '원문' * 2001
        long_body = 'A' * 4001
        with patch('knowledge.store._now', return_value='2026-01-02T00:00:00+00:00'):
            source = self.store.ingest('Fixture source', text=long_text,
                                       source_uri='fixture:report', as_of='2026-01-01',
                                       topics=['chips'], stock_keys=['US:AAA'], session_id='source-session')
            note = self.store.append_note('interpretation', long_body, 'ai',
                                          source_ids=[source['id']], topics=['chips'], stock_keys=['US:AAA'],
                                          session_id='note-session')
            boundary = self.store.append_note('observation', 'B' * 4000, 'source',
                                              topics=['chips'], stock_keys=['US:AAA'])
        expected = self.store.search(stock_key='US:AAA', topic='chips', limit=2)
        calls = []

        class FixtureSearch:
            def __bool__(self):
                # A supplied backend is selected by None, not by truthiness.
                return False

            def search_rows(self, connection, *, query, subjects):
                calls.append((connection, query, subjects))
                # Deliberately return the opposite order: Store owns public ordering.
                rows = [('note', row) for row in connection.execute('SELECT * FROM knowledge_notes')]
                rows += [('source', row) for row in connection.execute('SELECT * FROM knowledge_sources')]
                return sorted(rows, key=lambda item: item[1]['id'])

        backend = FixtureSearch()
        injected = KnowledgeStore(self.path, search_backend=backend)
        before = self.path.read_bytes()
        with patch.object(injected, '_write', side_effect=AssertionError('read attempted write')):
            actual = injected.search(stock_key='US:AAA', topic='chips', limit=2)
            all_items = injected.search(stock_key='US:AAA', topic='chips', limit=200)
        self.assertEqual(actual, expected)
        self.assertEqual(set(actual), {'items', 'total', 'offset', 'next_offset'})
        self.assertEqual(actual['next_offset'], 2)
        self.assertEqual(calls[0][1:], ('', ['topic:chips', 'US:AAA']))
        with self.assertRaises(sqlite3.ProgrammingError):
            calls[0][0].execute('SELECT 1')
        self.assertEqual(self.path.read_bytes(), before)
        by_id = {row['id']: row for row in all_items['items']}
        source_item, note_item, boundary_item = (by_id[item['id']] for item in (source, note, boundary))
        self.assertEqual(source_item['source_uri'], 'fixture:report')
        self.assertEqual(source_item['source_kind'], 'document')
        self.assertEqual(source_item['as_of'], '2026-01-01')
        self.assertEqual(source_item['text_preview'], long_text[:4000])
        self.assertEqual(source_item['text_length'], len(long_text))
        self.assertTrue(source_item['text_truncated'])
        self.assertNotIn('text', source_item)
        self.assertEqual(source_item['session_ids'], ['source-session'])
        self.assertEqual(source_item['subjects'], ['US:AAA', 'topic:chips'])
        self.assertEqual(source_item['original_paths'], [])
        self.assertEqual(note_item['author'], 'ai')
        self.assertEqual(note_item['source_ids'], [source['id']])
        self.assertEqual(note_item['session_ids'], ['note-session'])
        self.assertEqual(note_item['subjects'], ['US:AAA', 'topic:chips'])
        self.assertEqual(note_item['body'], long_body[:4000])
        self.assertEqual(note_item['body_length'], 4001)
        self.assertTrue(note_item['body_truncated'])
        self.assertEqual(boundary_item['author'], 'source')
        self.assertEqual(boundary_item['body_length'], 4000)
        self.assertFalse(boundary_item['body_truncated'])

    def test_reads_never_create_or_migrate(self):
        def assert_empty_reads(store):
            with patch.object(store, '_write', side_effect=AssertionError('read attempted write')):
                self.assertEqual(store.search()['total'], 0)
                self.assertFalse(store.context(stock_key='US:AAA')['found'])
                self.assertEqual(store.summary_history('US:AAA')['total'], 0)

        absent = self.root / 'absent' / 'records.sqlite3'
        assert_empty_reads(KnowledgeStore(absent))
        self.assertFalse(absent.parent.exists())
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute('CREATE TABLE unrelated(value TEXT)')
            connection.execute("INSERT INTO unrelated VALUES ('preserve')")
            connection.commit()
            schema_before = connection.execute('SELECT type,name,sql FROM sqlite_master ORDER BY name').fetchall()
        before = hashlib.sha256(self.path.read_bytes()).hexdigest()
        assert_empty_reads(self.store)
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(), before)
        with closing(sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True)) as connection:
            self.assertEqual(connection.execute('SELECT type,name,sql FROM sqlite_master ORDER BY name').fetchall(), schema_before)
            self.assertEqual(connection.execute('SELECT * FROM unrelated').fetchall(), [('preserve',)])

        note = self.store.append_note('question', 'Synthetic question', 'user', stock_keys=['US:AAA'])
        self.store.save_summary('US:AAA', 'Synthetic synthesis', 0, note_ids=[note['id']])
        populated_before = self.path.read_bytes()
        with patch.object(self.store, '_write', side_effect=AssertionError('read attempted write')):
            self.assertEqual(self.store.search()['total'], 1)
            self.assertTrue(self.store.context(stock_key='US:AAA')['found'])
            self.assertEqual(self.store.summary_history('US:AAA')['total'], 1)
        self.assertEqual(self.path.read_bytes(), populated_before)

    def test_full_history_resolves_paginated_question(self):
        clock = patch('knowledge.store._now', side_effect=[
            f'2026-01-03T00:00:{index:02d}+00:00' for index in range(40)])
        clock.start()
        self.addCleanup(clock.stop)
        def append(body, kind='observation', author='user', **kwargs):
            return self.store.append_note(kind, body, author, stock_keys=['US:AAA'], topics=['chips'], **kwargs)

        question = append('Old question remains readable.', kind='question')
        original = append('Old hypothesis.', kind='hypothesis')
        completed_step = append('Old next action.', kind='next_step')
        # Resolution can be recorded under another subject; state still reads all original notes.
        response = self.store.append_note('response', 'Supplied answer.', 'user',
                                          topics=['answer archive'], resolves=[question['id'], completed_step['id']])
        replacement = append('Replacement hypothesis.', kind='hypothesis', supersedes=original['id'])
        open_question = append('Still unanswered.', kind='question')
        open_step = append('Still pending action.', kind='next_step')
        for index in range(3):
            latest = append('Recent unrelated observation ' + str(index))
        count_before = self.store.search()['total']
        for link in ({'resolves': [open_question['id']]}, {'supersedes': replacement['id']}):
            with self.subTest(link=link):
                with self.assertRaisesRegex(ValueError, '^knowledge_cannot_close_another_authors_statement$'):
                    append('AI cannot close user text.', author='ai', **link)
        self.assertEqual(self.store.search()['total'], count_before)
        reopened = KnowledgeStore(self.path)
        context = reopened.context(stock_key='US:AAA', topic='chips', limit=1)
        self.assertEqual(context['history']['items'][0]['id'], latest['id'])
        self.assertNotIn(question['id'], [row['id'] for row in context['history']['items']])
        self.assertEqual(context['unresolved_page'], {'total': 2, 'offset': 0, 'next_offset': 1})
        self.assertEqual(context['unresolved'][0]['id'], replacement['id'])
        second = reopened.context(stock_key='US:AAA', topic='chips', limit=1, unresolved_offset=1)
        self.assertEqual(second['unresolved'][0]['id'], open_question['id'])
        self.assertIsNone(second['unresolved_page']['next_offset'])
        self.assertEqual(context['next_steps_page'], {'total': 1, 'offset': 0, 'next_offset': None})
        self.assertEqual([row['id'] for row in context['next_steps']], [open_step['id']])
        for prior in (question, original, completed_step, response):
            self.assertEqual(reopened.note(prior['id'])['body'], prior['body'])

    def test_saved_identity_and_revision_survive_refactor(self):
        raw = b'\xef\xbb\xbfFixture original\r\nbytes stay exact.\r\n'
        source_path = self.root / 'source.md'
        source_path.write_bytes(raw)
        first = self.store.ingest('Fixture', file_path=str(source_path),
                                  stock_keys=['US:AAA'], session_id='session-one')
        repeated = self.store.ingest('Fixture', file_path=str(source_path),
                                     stock_keys=['US:AAA'], session_id='session-two')
        self.assertEqual(first['id'], repeated['id'])
        self.assertEqual(repeated['sha256'], hashlib.sha256(raw).hexdigest())
        self.assertTrue(repeated['idempotent'])
        values = dict(kind='question', body='Original uncertainty.', author='user',
                      source_ids=[first['id']], stock_keys=['US:AAA'])
        note = self.store.append_note(**values, session_id='session-one')
        repeated_note = self.store.append_note(**values, session_id='session-two')
        self.assertEqual(note['id'], repeated_note['id'])
        self.assertTrue(repeated_note['idempotent'])
        old_body = '과거 요약을 보존한다.\n' * 600
        old = self.store.save_summary('US:AAA', old_body, 0, note_ids=[note['id']],
                                      source_ids=[first['id']], next_step='Verify evidence.')
        old_read = self.store.summary_read('US:AAA', old['revision'])
        self.store.save_summary('US:AAA', 'Later synthesis.', 1, note_ids=[note['id']])
        with self.assertRaisesRegex(ValueError, '^knowledge_revision_conflict$'):
            self.store.save_summary('US:AAA', 'Stale overwrite.', 1, note_ids=[note['id']])
        reopened = KnowledgeStore(self.path)
        self.assertEqual(reopened.summary_read('US:AAA', 1), old_read)
        self.assertEqual([row['revision'] for row in reopened.summary_history('US:AAA')['items']], [2, 1])
        pieces, offset = [], 0
        while True:
            page = reopened.summary_read('US:AAA', 1, offset=offset, limit=701)
            pieces.append(page['body'])
            self.assertEqual(page['sha256'], old_read['sha256'])
            self.assertEqual(page['status'], 'synthesis_not_user_adoption')
            if page['body_page']['next_offset'] is None:
                break
            offset = page['body_page']['next_offset']
        self.assertEqual(''.join(pieces), old_body)
        self.assertEqual(reopened.export_source_bytes(first['id']), raw)
        self.assertEqual(reopened.note(note['id'])['body'], values['body'])
        self.assertEqual(reopened.note(note['id'])['session_ids'], ['session-one', 'session-two'])
        with closing(sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True)) as connection:
            self.assertEqual(connection.execute('SELECT count(*) FROM knowledge_blobs').fetchone()[0], 1)
            self.assertEqual(connection.execute('SELECT count(*) FROM knowledge_sources').fetchone()[0], 1)
            self.assertEqual(connection.execute('SELECT count(*) FROM knowledge_notes').fetchone()[0], 1)

    def test_backend_failure_and_invalid_input_are_explicit(self):
        self.store.append_note('observation', 'Fixture', 'user', stock_keys=['US:AAA'])
        calls = []

        class RecordingSearch:
            def search_rows(self, connection, *, query, subjects):
                calls.append((query, subjects))
                return []

        store = KnowledgeStore(self.path, search_backend=RecordingSearch())
        invalid = [
            ({'offset': -1}, 'pagination'), ({'offset': True}, 'pagination'),
            ({'offset': 0.5}, 'pagination'), ({'limit': 0}, 'pagination'),
            ({'limit': 201}, 'pagination'), ({'limit': True}, 'pagination'),
            ({'query': 'x' * 501}, 'query'), ({'query': None}, 'query'),
            ({'query': 4}, 'query'), ({'stock_key': 'AAA'}, 'stock_key'),
            ({'topic': ' '}, 'topics'),
        ]
        for arguments, error in invalid:
            with self.subTest(arguments=arguments):
                with self.assertRaisesRegex(ValueError, '^knowledge_invalid_' + error + '$'):
                    store.search(**arguments)
        self.assertEqual(calls, [])
        literal = r'20%_\literal'
        self.assertEqual(store.search(literal)['total'], 0)
        self.assertEqual(store.search('x' * 500)['total'], 0)
        self.assertEqual(calls, [(literal, []), ('x' * 500, [])])
        failure = RuntimeError('synthetic search failure')

        class BrokenSearch:
            def search_rows(self, connection, *, query, subjects):
                raise failure

        broken = KnowledgeStore(self.path, search_backend=BrokenSearch())
        for call in (lambda: broken.search(), lambda: broken.context(stock_key='US:AAA')):
            with self.assertRaises(RuntimeError) as caught:
                call()
            self.assertIs(caught.exception, failure)
