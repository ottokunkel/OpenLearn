"""Postgres-backed queue (pgmq), chunk writer (pgvector), and document updater.

All classes share a single connection pool initialised once per worker process.
"""

import json
import logging
import time

import psycopg
import psycopg.rows
from psycopg_pool import ConnectionPool

logger = logging.getLogger(__name__)


def create_pool(database_url: str, min_size: int = 2, max_size: int = 5) -> ConnectionPool:
    return ConnectionPool(
        database_url,
        min_size=min_size,
        max_size=max_size,
        kwargs={"autocommit": True, "row_factory": psycopg.rows.dict_row},
    )


# ---------------------------------------------------------------------------
# pgmq-based job queue
# ---------------------------------------------------------------------------

class PgmqQueue:
    """Reads jobs from a pgmq queue backed by Postgres."""

    def __init__(self, pool: ConnectionPool, queue_name: str = "document_jobs"):
        self._pool = pool
        self._queue = queue_name

    def wait_for_job(self, timeout: int = 5) -> tuple[int, dict] | None:
        """Poll pgmq for one message. Returns (msg_id, job_dict) or None.

        If timeout is 0, returns immediately without sleeping on empty queue.
        """
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT * FROM pgmq.read(%s, %s, %s)",
                [self._queue, 300, 1],  # 300s visibility timeout, 1 message
            ).fetchone()

        if row is None:
            if timeout > 0:
                time.sleep(min(timeout, 2))
            return None

        msg = row["message"]
        if isinstance(msg, str):
            msg = json.loads(msg)
        return row["msg_id"], msg

    def ack_job(self, msg_id: int) -> None:
        with self._pool.connection() as conn:
            conn.execute("SELECT pgmq.archive(%s, %s)", [self._queue, msg_id])

    def nack_job(self, msg_id: int) -> None:
        """Make message immediately visible again (for retries)."""
        with self._pool.connection() as conn:
            conn.execute(
                "SELECT pgmq.set_vt(%s, %s, %s)", [self._queue, msg_id, 0],
            )

    def ping(self) -> bool:
        with self._pool.connection() as conn:
            conn.execute("SELECT 1")
        return True

    def close(self) -> None:
        pass  # pool is closed separately


# ---------------------------------------------------------------------------
# Bulk chunk writer (pgvector)
# ---------------------------------------------------------------------------

class ChunkWriter:
    """Inserts document chunks with embeddings into the document_chunks table."""

    def __init__(self, pool: ConnectionPool):
        self._pool = pool

    def insert_chunks(
        self,
        document_id: str,
        user_id: str,
        chunks: list[dict],
        start_index: int = 0,
        metadata: dict | None = None,
    ) -> None:
        if not chunks:
            return

        rows = []
        for i, chunk in enumerate(chunks):
            embedding = chunk.get("embedding")
            rows.append((
                document_id,
                user_id,
                start_index + i,
                chunk["text"],
                chunk.get("headings", []),
                chunk.get("label"),
                chunk.get("page_no"),
                json.dumps(embedding) if embedding else None,
                json.dumps(metadata or {}),
            ))

        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO document_chunks
                        (document_id, user_id, chunk_index, content, headings,
                         label, page_no, embedding, metadata)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s::vector, %s::jsonb)
                    """,
                    rows,
                )
            conn.commit()

        logger.debug(
            "Inserted %d chunks for document %s (start_index=%d)",
            len(chunks), document_id, start_index,
        )


# ---------------------------------------------------------------------------
# Document status updater
# ---------------------------------------------------------------------------

class DocumentUpdater:
    """Updates document status and metadata in the documents table."""

    def __init__(self, pool: ConnectionPool):
        self._pool = pool

    def mark_processing(self, document_id: str) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE documents SET status = 'processing' WHERE id = %s",
                [document_id],
            )

    def mark_completed(
        self,
        document_id: str,
        page_count: int,
        total_chunks: int,
        doc_json_path: str | None = None,
        markdown_path: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                """
                UPDATE documents
                SET status = 'completed',
                    page_count = %s,
                    total_chunks = %s,
                    doc_json_path = %s,
                    markdown_path = %s,
                    metadata = metadata || %s::jsonb
                WHERE id = %s
                """,
                [
                    page_count,
                    total_chunks,
                    doc_json_path,
                    markdown_path,
                    json.dumps(metadata or {}),
                    document_id,
                ],
            )

    def mark_failed(self, document_id: str, error_message: str) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE documents SET status = 'failed', error_message = %s WHERE id = %s",
                [error_message, document_id],
            )
