"""Generic Postgres document-store backend.

Writes `doc_worker_v2.documents` rows via raw SQL over psycopg3. Mirrors
`storage_supabase.documents.SupabaseDocumentStore` behavior.
"""
from __future__ import annotations

from typing import Any

from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool


class PostgresDocumentStore:
    def __init__(self, dsn: str, *, min_size: int = 1, max_size: int = 4):
        self._pool = ConnectionPool(dsn, min_size=min_size, max_size=max_size, open=True)

    def _update(self, document_id: str, fields: dict[str, Any]) -> None:
        cols = list(fields)
        set_clause = ", ".join(f"{c} = %s" for c in cols)
        values = [fields[c] for c in cols]
        sql = f"UPDATE doc_worker_v2.documents SET {set_clause} WHERE id = %s"
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, (*values, document_id))

    def mark_processing(self, document_id: str) -> None:
        self._update(document_id, {"status": "processing", "error_message": None})

    def mark_retrying(
        self,
        document_id: str,
        attempt: int,
        max_attempts: int,
        error: str,
    ) -> None:
        self._update(
            document_id,
            {
                "status": "retrying",
                "error_message": f"attempt {attempt}/{max_attempts}: {error[:1000]}",
            },
        )

    def mark_failed(self, document_id: str, error: str) -> None:
        self._update(
            document_id,
            {"status": "failed", "error_message": error[:1000]},
        )

    def mark_completed(
        self,
        document_id: str,
        *,
        page_count: int,
        artifacts: dict[str, str],
        pipeline: str,
    ) -> None:
        self._update(
            document_id,
            {
                "status": "completed",
                "page_count": page_count,
                "artifacts": Jsonb(artifacts),
                "pipeline": pipeline,
                "error_message": None,
            },
        )

    def close(self) -> None:
        self._pool.close()
