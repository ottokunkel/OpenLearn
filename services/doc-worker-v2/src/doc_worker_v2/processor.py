import logging
from typing import Any

from .protocols import BlobStore, DocumentStore, Pipeline, Queue, QueueMessage

log = logging.getLogger(__name__)


class JobPayloadError(ValueError):
    """Raised when a queue message payload is missing required keys."""


def _validate(payload: dict[str, Any]) -> tuple[str, str, str, str]:
    try:
        return (
            payload["document_id"],
            payload["user_id"],
            payload["file_path"],
            payload["filename"],
        )
    except KeyError as e:
        raise JobPayloadError(f"missing key: {e}") from e


def process(
    msg: QueueMessage,
    queue: Queue,
    docs: DocumentStore,
    blobs: BlobStore,
    pipeline: Pipeline,
    *,
    max_retries: int,
) -> None:
    """Process one message end-to-end. Never raises."""
    try:
        document_id, user_id, file_path, filename = _validate(msg.payload)
    except JobPayloadError:
        log.exception("bad payload, archiving msg_id=%s", msg.msg_id)
        queue.archive(msg.msg_id)
        return

    attempt = max(1, msg.read_ct)
    log.info(
        "processing doc=%s attempt=%d/%d pipeline=%s",
        document_id,
        attempt,
        max_retries,
        pipeline.name,
    )

    try:
        docs.mark_processing(document_id)
        pdf_bytes = blobs.download(file_path)
        result = pipeline.convert(pdf_bytes)

        prefix = f"{user_id}/{document_id}"
        artifacts_map: dict[str, str] = {}
        for art in result.artifacts:
            path = f"{prefix}/{filename}.{art.extension}"
            blobs.upload(path, art.content, art.content_type, upsert=True)
            artifacts_map[art.name] = path

        docs.mark_completed(
            document_id,
            page_count=result.page_count,
            artifacts=artifacts_map,
            pipeline=pipeline.name,
        )
        queue.delete(msg.msg_id)
        log.info(
            "completed doc=%s pages=%d artifacts=%s",
            document_id,
            result.page_count,
            list(artifacts_map),
        )

    except Exception as e:
        err = str(e)[:1000]
        if attempt >= max_retries:
            log.exception("giving up doc=%s after %d attempts", document_id, attempt)
            try:
                docs.mark_failed(document_id, err)
            except Exception:
                log.exception("failed to mark failed doc=%s", document_id)
            queue.archive(msg.msg_id)
        else:
            log.exception(
                "retrying doc=%s attempt=%d/%d", document_id, attempt, max_retries
            )
            try:
                docs.mark_retrying(document_id, attempt, max_retries, err)
            except Exception:
                log.exception("failed to mark retrying doc=%s", document_id)
            # Do NOT delete/archive — pgmq re-presents when VT expires.
