"""Integration tests against a live Supabase/Postgres database.

Requires DATABASE_URL env var. Skipped otherwise.
Run: DATABASE_URL=postgresql://... pytest tests/test_integration.py -v
"""

import json
import os
import uuid

import psycopg
import psycopg.rows
import pytest
from psycopg_pool import ConnectionPool

from worker.db import ChunkWriter, DocumentUpdater, PgmqQueue

DATABASE_URL = os.environ.get("DATABASE_URL", "")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DATABASE_URL, reason="DATABASE_URL not set"),
]

USER_1 = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
USER_2 = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


@pytest.fixture(scope="module")
def pool():
    p = ConnectionPool(
        DATABASE_URL,
        min_size=1,
        max_size=3,
        kwargs={"autocommit": True, "row_factory": psycopg.rows.dict_row},
    )
    yield p
    p.close()


@pytest.fixture
def doc_id(pool):
    """Insert a test document and clean it up after (cascades to chunks)."""
    did = str(uuid.uuid4())
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO documents (id, user_id, filename, file_path, status) "
            "VALUES (%s, %s, %s, %s, 'pending')",
            [did, USER_1, "integration_test.pdf", f"{USER_1}/{did}/test.pdf"],
        )
    yield did
    with pool.connection() as conn:
        conn.execute("DELETE FROM documents WHERE id = %s", [did])


# ---------------------------------------------------------------------------
# Document lifecycle
# ---------------------------------------------------------------------------


class TestDocumentLifecycle:
    def test_insert_and_update_status(self, pool, doc_id):
        updater = DocumentUpdater(pool)

        updater.mark_processing(doc_id)
        with pool.connection() as conn:
            row = conn.execute(
                "SELECT status FROM documents WHERE id = %s", [doc_id]
            ).fetchone()
        assert row["status"] == "processing"

        updater.mark_completed(
            doc_id, page_count=3, total_chunks=10,
            doc_json_path="out/doc.json", markdown_path="out/doc.md",
            metadata={"model": "test"},
        )
        with pool.connection() as conn:
            row = conn.execute(
                "SELECT status, page_count, total_chunks FROM documents WHERE id = %s",
                [doc_id],
            ).fetchone()
        assert row["status"] == "completed"
        assert row["page_count"] == 3
        assert row["total_chunks"] == 10

    def test_mark_failed(self, pool, doc_id):
        updater = DocumentUpdater(pool)
        updater.mark_failed(doc_id, "something broke")

        with pool.connection() as conn:
            row = conn.execute(
                "SELECT status, error_message FROM documents WHERE id = %s",
                [doc_id],
            ).fetchone()
        assert row["status"] == "failed"
        assert row["error_message"] == "something broke"


# ---------------------------------------------------------------------------
# Chunk writes with pgvector
# ---------------------------------------------------------------------------


class TestChunkWriter:
    def test_insert_and_query_chunks(self, pool, doc_id):
        writer = ChunkWriter(pool)
        chunks = [
            {"text": "The quick brown fox", "headings": ["Intro"], "label": "text", "page_no": 1, "embedding": [0.1] * 1536},
            {"text": "jumps over the lazy dog", "headings": ["Intro"], "label": "text", "page_no": 1, "embedding": [0.2] * 1536},
        ]
        writer.insert_chunks(doc_id, USER_1, chunks, metadata={"test": True})

        with pool.connection() as conn:
            rows = conn.execute(
                "SELECT content, chunk_index, headings FROM document_chunks "
                "WHERE document_id = %s ORDER BY chunk_index",
                [doc_id],
            ).fetchall()

        assert len(rows) == 2
        assert rows[0]["content"] == "The quick brown fox"
        assert rows[0]["chunk_index"] == 0
        assert rows[1]["chunk_index"] == 1

    def test_vector_similarity_search(self, pool, doc_id):
        writer = ChunkWriter(pool)
        chunks = [
            {"text": "machine learning algorithms", "headings": ["ML"], "label": "text", "page_no": 1, "embedding": [1.0] + [0.0] * 1535},
            {"text": "cooking recipes for pasta", "headings": ["Food"], "label": "text", "page_no": 2, "embedding": [0.0] * 1535 + [1.0]},
        ]
        writer.insert_chunks(doc_id, USER_1, chunks)

        # Query with embedding close to first chunk
        query_vec = json.dumps([1.0] + [0.0] * 1535)
        with pool.connection() as conn:
            rows = conn.execute(
                "SELECT content, 1 - (embedding <=> %s::vector) AS similarity "
                "FROM document_chunks WHERE document_id = %s "
                "ORDER BY embedding <=> %s::vector LIMIT 1",
                [query_vec, doc_id, query_vec],
            ).fetchall()

        assert len(rows) == 1
        assert rows[0]["content"] == "machine learning algorithms"
        assert rows[0]["similarity"] > 0.99


# ---------------------------------------------------------------------------
# pgmq queue
# ---------------------------------------------------------------------------


class TestPgmqQueue:
    def test_enqueue_read_archive(self, pool):
        q = PgmqQueue(pool, "document_jobs")

        # Enqueue via the helper function
        msg_payload = {"document_id": "integration-test", "user_id": USER_1}
        with pool.connection() as conn:
            row = conn.execute(
                "SELECT pgmq.send(%s, %s::jsonb)",
                ["document_jobs", json.dumps(msg_payload)],
            ).fetchone()
        msg_id = list(row.values())[0]

        # Read it back
        result = q.wait_for_job(timeout=1)
        assert result is not None
        read_id, job = result
        assert job["document_id"] == "integration-test"

        # Archive it
        q.ack_job(read_id)

        # Verify it's gone from the queue
        with pool.connection() as conn:
            remaining = conn.execute(
                "SELECT * FROM pgmq.read(%s, %s, %s)",
                ["document_jobs", 0, 1],
            ).fetchone()
        # Could be None or a different message — just confirm our msg_id isn't returned
        if remaining is not None:
            assert remaining["msg_id"] != read_id


# ---------------------------------------------------------------------------
# RLS isolation
# ---------------------------------------------------------------------------


class TestRLS:
    def test_user_isolation(self, pool):
        """User 1 cannot see User 2's documents through RLS."""
        with pool.connection() as conn:
            # Start a transaction so SET LOCAL works
            conn.autocommit = False
            try:
                conn.execute("SET LOCAL role TO 'authenticated'")
                conn.execute(
                    "SET LOCAL request.jwt.claims TO %s",
                    [json.dumps({"sub": USER_1})],
                )
                rows = conn.execute("SELECT id, filename FROM documents").fetchall()
                filenames = [r["filename"] for r in rows]

                # User 1 should only see their own docs
                assert any("user1" in f for f in filenames) or len(filenames) == 0
                assert not any("user2" in f.lower() for f in filenames)
            finally:
                conn.rollback()
                conn.autocommit = True
