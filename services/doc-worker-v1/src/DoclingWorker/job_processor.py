"""Per-message orchestration: download → convert → upload → update row."""
from __future__ import annotations

import json
import logging
from typing import Any

from docling.document_converter import DocumentConverter
from supabase import Client

from . import queue as q

log = logging.getLogger(__name__)

BUCKET = "documents"


class JobPayloadError(ValueError):
    """Raised when an incoming pgmq message doesn't match the expected shape."""


def _validate_payload(payload: dict[str, Any]) -> tuple[str, str, str, str]:
    try:
        return (
            payload["document_id"],
            payload["user_id"],
            payload["file_path"],
            payload["filename"],
        )
    except KeyError as e:
        raise JobPayloadError(f"missing required key in message: {e}") from e


def _mark(client: Client, document_id: str, **fields: Any) -> None:
    client.table("documents").update(fields).eq("id", document_id).execute()


def process(
    msg: q.Message,
    client: Client,
    converter: DocumentConverter,
) -> None:
    """Process a single pgmq message end-to-end.

    Never raises. On success, the pgmq message is deleted and the documents row
    is marked completed. On failure, the row is marked failed and the message
    is archived (moved to pgmq.a_document_jobs for inspection).
    """
    try:
        document_id, user_id, file_path, filename = _validate_payload(msg.message)
    except JobPayloadError:
        log.exception("bad payload, archiving msg_id=%s", msg.msg_id)
        q.archive(client, msg.msg_id)
        return

    log.info("processing document_id=%s file=%s", document_id, file_path)
    try:
        _mark(client, document_id, status="processing", error_message=None)

        # Lazy import so the module stays importable for unit tests that don't
        # install docling / hit the network.
        from .docling_runner import convert_pdf_bytes

        pdf_bytes = client.storage.from_(BUCKET).download(file_path)
        markdown, doctags, page_count = convert_pdf_bytes(converter, pdf_bytes)

        prefix = f"{user_id}/{document_id}"
        md_path = f"{prefix}/{filename}.md"
        doctags_path = f"{prefix}/{filename}.doctags.json"

        client.storage.from_(BUCKET).upload(
            md_path,
            markdown.encode("utf-8"),
            {"content-type": "text/markdown", "upsert": "true"},
        )
        client.storage.from_(BUCKET).upload(
            doctags_path,
            json.dumps(doctags).encode("utf-8"),
            {"content-type": "application/json", "upsert": "true"},
        )

        _mark(
            client,
            document_id,
            status="completed",
            page_count=page_count,
            markdown_path=md_path,
            doc_json_path=doctags_path,
            error_message=None,
        )
        q.delete(client, msg.msg_id)
        log.info("completed document_id=%s pages=%s", document_id, page_count)

    except Exception as e:  # noqa: BLE001 — safety net; keep worker alive
        log.exception("job failed document_id=%s", document_id)
        try:
            _mark(
                client,
                document_id,
                status="failed",
                error_message=str(e)[:1000],
            )
        except Exception:  # noqa: BLE001
            log.exception("failed to mark document_id=%s as failed", document_id)
        q.archive(client, msg.msg_id)
