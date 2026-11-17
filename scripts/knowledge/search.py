"""Read-only keyword selection, independent of storage and public result paging."""


class KeywordSearch:
    def search_rows(self, connection, *, query, subjects):
        """Return (source|note, Row) pairs using a caller-owned read connection.

        The store validates the raw query/subjects and owns formatting, ordering
        and paging. Escape SQL LIKE literals exactly once at this boundary; do
        not open, close or write the supplied connection.
        """
        query = query.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
        rows = []
        for kind, table in (('source', 'knowledge_sources'), ('note', 'knowledge_notes')):
            searchable = "coalesce(json_extract(payload, '$.title'), '') || char(10) || coalesce(json_extract(payload, '$.text'), '') || char(10) || coalesce(json_extract(payload, '$.body'), '')"
            clauses = [f"(({searchable}) LIKE ? ESCAPE '\\' OR EXISTS (SELECT 1 FROM knowledge_links q WHERE q.item_type=? AND q.item_id=t.id AND q.subject LIKE ? ESCAPE '\\'))"]
            params = ['%' + query + '%', kind, '%' + query + '%']
            for subject in subjects:
                clauses.append('EXISTS (SELECT 1 FROM knowledge_links l WHERE l.item_type=? AND l.item_id=t.id AND l.subject=?)')
                params.extend([kind, subject])
            rows.extend((kind, row) for row in connection.execute(
                'SELECT * FROM ' + table + ' t WHERE ' + ' AND '.join(clauses), params))
        return rows
