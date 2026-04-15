from dataclasses import dataclass
from typing import Any, cast

from supabase import Client, create_client


@dataclass(frozen=True)
class _Message:
    msg_id: int
    read_ct: int
    payload: dict[str, Any]


class SupabaseQueue:
    def __init__(self, url: str, service_role_key: str):
        self._c: Client = create_client(url, service_role_key)

    def read(self, visibility_timeout_s: int, qty: int) -> list[_Message]:
        resp = self._c.rpc(
            "pgmq_read_doc_ingest",
            {"vt": visibility_timeout_s, "qty": qty},
        ).execute()
        rows = cast(list[dict[str, Any]], resp.data or [])
        return [
            _Message(
                msg_id=r["msg_id"],
                read_ct=r["read_ct"],
                payload=r["message"],
            )
            for r in rows
        ]

    def delete(self, msg_id: int) -> None:
        self._c.rpc("pgmq_delete_doc_ingest", {"msg_id": msg_id}).execute()

    def archive(self, msg_id: int) -> None:
        self._c.rpc("pgmq_archive_doc_ingest", {"msg_id": msg_id}).execute()
