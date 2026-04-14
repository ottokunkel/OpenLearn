"""Per-message orchestration: download → convert → upload → update row.

Retry semantics
---------------
pgmq guarantees atomic assignment: once `queue.read` returns a message with a
visibility timeout (VT), the message is invisible to every other reader until
either (a) we delete it, (b) we archive it, or (c) the VT expires.

We exploit (c) for retries: on a transient failure we do *not* delete or
archive — we simply let the VT expire, and pgmq re-presents the message to
the next worker that calls `read`. `msg.read_ct` tells us how many times the
message has been read; once it reaches ``max_retries`` we give up and archive
the message to the dead-letter table for inspection.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from docling.document_converter import DocumentConverter
from supabase import Client

from . import queue as q
from .docling import convert_pdf

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
    max_retries: int = 3,
) -> None:
    """Process a single pgmq message end-to-end.

    Never raises. Outcomes:
      * success → pgmq message deleted, row marked ``completed``.
      * malformed payload → archived immediately, no row to update.
      * transient failure with attempts remaining → message left on queue
        (pgmq re-presents after VT expires); row marked ``retrying``.
      * attempts exhausted → archived to dead-letter; row marked ``failed``.
    """
    try:
        document_id, user_id, file_path, filename = _validate_payload(msg.message)
    except JobPayloadError:
        log.exception("bad payload, archiving msg_id=%s", msg.msg_id)
        q.archive(client, msg.msg_id)
        return

    attempt = max(1, msg.read_ct)
    log.info(
        "processing document_id=%s file=%s attempt=%d/%d",
        document_id, file_path, attempt, max_retries,
    )
    try:
        _mark(client, document_id, status="processing", error_message=None)

        pdf_bytes = client.storage.from_(BUCKET).download(file_path)
        markdown, doctags, page_count = convert_pdf(converter, pdf_bytes)

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
        err = str(e)[:1000]
        if attempt >= max_retries:
            log.exception(
                "job failed after %d/%d attempts, archiving document_id=%s",
                attempt, max_retries, document_id,
            )
            try:
                _mark(client, document_id, status="failed", error_message=err)
            except Exception:  # noqa: BLE001
                log.exception("failed to mark document_id=%s as failed", document_id)
            q.archive(client, msg.msg_id)
        else:
            log.exception(
                "job failed on attempt %d/%d, will retry document_id=%s",
                attempt, max_retries, document_id,
            )
            try:
                _mark(
                    client,
                    document_id,
                    status="retrying",
                    error_message=f"attempt {attempt}/{max_retries}: {err}",
                )
            except Exception:  # noqa: BLE001
                log.exception("failed to mark document_id=%s as retrying", document_id)
            # Intentionally do NOT delete or archive: pgmq re-presents when VT expires.
