"""Unit tests for job_processor with mocked Supabase client + converter."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from DoclingWorker import job_processor, queue


def _valid_payload() -> dict:
    return {
        "document_id": "00000000-0000-0000-0000-000000000001",
        "user_id": "00000000-0000-0000-0000-0000000000aa",
        "file_path": "00000000-0000-0000-0000-0000000000aa/00000000-0000-0000-0000-000000000001/test.pdf",
        "filename": "test.pdf",
        "s3_bucket": "documents",
        "s3_key": "…",
        "metadata": {},
    }


def _mock_client_with_storage(pdf_bytes: bytes = b"%PDF-1.4 fake") -> MagicMock:
    """Build a MagicMock that mimics the chained Supabase client calls used by
    job_processor: table().update().eq().execute(), storage.from_().download/upload,
    and rpc().execute() for pgmq ops (routed through DoclingWorker.queue)."""
    client = MagicMock()
    # storage chain
    bucket = MagicMock()
    bucket.download.return_value = pdf_bytes
    bucket.upload.return_value = MagicMock()
    client.storage.from_.return_value = bucket
    # rpc chain used by queue.delete / queue.archive
    client.rpc.return_value.execute.return_value = MagicMock(data=None)
    # table().update().eq().execute()
    table_chain = client.table.return_value
    table_chain.update.return_value.eq.return_value.execute.return_value = MagicMock(data=None)
    return client


def _status_updates(client: MagicMock) -> list[dict]:
    """Extract ordered list of update(...) payloads against the documents table."""
    return [call.args[0] for call in client.table.return_value.update.call_args_list]


def _rpc_calls(client: MagicMock) -> list[tuple]:
    """Return (rpc_name, params) for every client.rpc(...) call."""
    return [(c.args[0], c.args[1]) for c in client.rpc.call_args_list]


def test_process_success_deletes_message_and_marks_completed():
    client = _mock_client_with_storage()
    converter = MagicMock()
    msg = queue.Message(msg_id=42, read_ct=1, message=_valid_payload())

    with patch(
        "DoclingWorker.docling_runner.convert_pdf_bytes",
        return_value=("# Hello\n", {"pages": {1: {}}}, 1),
    ):
        job_processor.process(msg, client, converter)

    updates = _status_updates(client)
    assert any(u.get("status") == "processing" for u in updates)
    completed = next((u for u in updates if u.get("status") == "completed"), None)
    assert completed is not None, updates
    assert completed["page_count"] == 1
    assert completed["markdown_path"].endswith("/test.pdf.md")
    assert completed["doc_json_path"].endswith("/test.pdf.doctags.json")

    # Two uploads: .md and .doctags.json
    bucket = client.storage.from_.return_value
    assert bucket.upload.call_count == 2

    # Verify doctags upload was valid JSON
    doctags_upload = bucket.upload.call_args_list[1]
    doctags_bytes = doctags_upload.args[1]
    assert json.loads(doctags_bytes.decode("utf-8")) == {"pages": {"1": {}}}

    rpcs = _rpc_calls(client)
    assert ("pgmq_delete", {"queue_name": "document_jobs", "msg_id": 42}) in rpcs
    assert not any(r[0] == "pgmq_archive" for r in rpcs)


def test_process_conversion_failure_marks_failed_and_archives():
    client = _mock_client_with_storage()
    converter = MagicMock()
    msg = queue.Message(msg_id=77, read_ct=1, message=_valid_payload())

    with patch(
        "DoclingWorker.docling_runner.convert_pdf_bytes",
        side_effect=RuntimeError("vlm boom"),
    ):
        job_processor.process(msg, client, converter)

    updates = _status_updates(client)
    failed = next((u for u in updates if u.get("status") == "failed"), None)
    assert failed is not None, updates
    assert "vlm boom" in failed["error_message"]

    rpcs = _rpc_calls(client)
    assert ("pgmq_archive", {"queue_name": "document_jobs", "msg_id": 77}) in rpcs
    assert not any(r[0] == "pgmq_delete" for r in rpcs)


def test_process_malformed_payload_archives_without_status_updates():
    client = _mock_client_with_storage()
    converter = MagicMock()
    bad_payload = {"document_id": "x"}  # missing user_id, file_path, filename
    msg = queue.Message(msg_id=99, read_ct=1, message=bad_payload)

    job_processor.process(msg, client, converter)

    # No updates attempted — we never identified a row to touch
    assert client.table.call_count == 0

    rpcs = _rpc_calls(client)
    assert ("pgmq_archive", {"queue_name": "document_jobs", "msg_id": 99}) in rpcs


def test_process_error_message_truncated_to_1000_chars():
    client = _mock_client_with_storage()
    converter = MagicMock()
    msg = queue.Message(msg_id=1, read_ct=1, message=_valid_payload())
    huge = "x" * 5000

    with patch(
        "DoclingWorker.docling_runner.convert_pdf_bytes",
        side_effect=RuntimeError(huge),
    ):
        job_processor.process(msg, client, converter)

    updates = _status_updates(client)
    failed = next(u for u in updates if u.get("status") == "failed")
    assert len(failed["error_message"]) == 1000


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
