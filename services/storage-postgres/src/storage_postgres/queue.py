"""Generic Postgres pgmq queue backend.

Hits the `pgmq` extension directly via SQL. The queue name is hard-coded to
`doc_ingest` to match `doc_worker_v2.documents` and the supabase-backed
counterpart in `storage_supabase.queue`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from psycopg_pool import ConnectionPool

QUEUE_NAME = "doc_ingest"


@dataclass(frozen=True)
class _Message:
    msg_id: int
    read_ct: int
    payload: dict[str, Any]


class PostgresPgmqQueue:
    def __init__(self, dsn: str, *, min_size: int = 1, max_size: int = 4):
        self._pool = ConnectionPool(dsn, min_size=min_size, max_size=max_size, open=True)

    def read(self, visibility_timeout_s: int, qty: int) -> list[_Message]:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT msg_id, read_ct, message FROM pgmq.read(%s, %s, %s)",
                (QUEUE_NAME, visibility_timeout_s, qty),
            )
            rows = cur.fetchall()
        return [_Message(msg_id=r[0], read_ct=r[1], payload=r[2]) for r in rows]

    def delete(self, msg_id: int) -> None:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT pgmq.delete(%s, %s)", (QUEUE_NAME, msg_id))

    def archive(self, msg_id: int) -> None:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT pgmq.archive(%s, %s)", (QUEUE_NAME, msg_id))

    def close(self) -> None:
        self._pool.close()
