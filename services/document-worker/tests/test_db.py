"""Tests for worker/db.py (PgmqQueue, ChunkWriter, DocumentUpdater)."""

import json
from unittest.mock import patch

from worker.db import ChunkWriter, DocumentUpdater, PgmqQueue


class TestPgmqQueue:
    def test_wait_for_job(self, mock_pool):
        pool, conn, _ = mock_pool
        conn.execute.return_value.fetchone.return_value = {
            "msg_id": 42,
            "message": {"document_id": "doc-1"},
        }
        assert PgmqQueue(pool).wait_for_job() == (42, {"document_id": "doc-1"})

    @patch("worker.db.time.sleep")
    def test_wait_for_job_none(self, mock_sleep, mock_pool):
        pool, conn, _ = mock_pool
        conn.execute.return_value.fetchone.return_value = None
        assert PgmqQueue(pool).wait_for_job() is None

    def test_ack_job(self, mock_pool):
        pool, conn, _ = mock_pool
        PgmqQueue(pool, "q").ack_job(42)
        assert "pgmq.archive" in conn.execute.call_args[0][0]


class TestChunkWriter:
    def test_insert_chunks(self, mock_pool):
        pool, conn, cursor = mock_pool
        chunks = [{"text": "hello", "headings": ["H1"], "embedding": [0.1]}]
        ChunkWriter(pool).insert_chunks("doc-1", "user-1", chunks, metadata={"k": "v"})
        rows = cursor.executemany.call_args[0][1]
        assert rows[0][3] == "hello"
        assert json.loads(rows[0][7]) == [0.1]
        conn.commit.assert_called_once()

    def test_insert_empty(self, mock_pool):
        pool, conn, cursor = mock_pool
        ChunkWriter(pool).insert_chunks("d", "u", [])
        cursor.executemany.assert_not_called()


class TestDocumentUpdater:
    def test_mark_processing(self, mock_pool):
        pool, conn, _ = mock_pool
        DocumentUpdater(pool).mark_processing("doc-1")
        assert "processing" in conn.execute.call_args[0][0]

    def test_mark_failed(self, mock_pool):
        pool, conn, _ = mock_pool
        DocumentUpdater(pool).mark_failed("doc-1", "error")
        assert "failed" in conn.execute.call_args[0][0]
