"""pgmq read/delete/archive over Supabase RPC."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from supabase import Client

QUEUE_NAME = "document_jobs"


@dataclass
class Message:
    msg_id: int
    read_ct: int
    message: dict[str, Any]  # the job payload enqueued by enqueue_document_job


def read(client: Client, visibility_timeout_s: int, qty: int) -> list[Message]:
    resp = client.rpc(
        "pgmq_read",
        {"queue_name": QUEUE_NAME, "vt": visibility_timeout_s, "qty": qty},
    ).execute()
    rows = resp.data or []
    return [
        Message(msg_id=row["msg_id"], read_ct=row["read_ct"], message=row["message"])
        for row in rows
    ]


def delete(client: Client, msg_id: int) -> None:
    client.rpc("pgmq_delete", {"queue_name": QUEUE_NAME, "msg_id": msg_id}).execute()


def archive(client: Client, msg_id: int) -> None:
    client.rpc("pgmq_archive", {"queue_name": QUEUE_NAME, "msg_id": msg_id}).execute()
