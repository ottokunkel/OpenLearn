"""Unit tests for processor.process covering four scenarios:

    1. happy path → mark_completed, queue.delete, each artifact uploaded
    2. pipeline raises, read_ct=1, max_retries=3 → mark_retrying, no ack
    3. pipeline raises, read_ct=3, max_retries=3 → mark_failed, queue.archive
    4. missing payload key → queue.archive, no DocumentStore call

All four protocols are stubbed with in-memory fakes. Run from repo root:

    uv run --directory services/doc-worker-v2 --with pytest pytest tests/test_processor.py -v
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from doc_worker_v2.artifacts import Artifact, PipelineResult
from doc_worker_v2.processor import process
from doc_worker_v2.protocols import QueueMessage


# ---------- Fakes ----------

@dataclass
class FakeQueue:
    deleted: list[int] = field(default_factory=list)
    archived: list[int] = field(default_factory=list)
    read_responses: list[list[QueueMessage]] = field(default_factory=list)

    def read(self, visibility_timeout_s: int, qty: int) -> list[QueueMessage]:
        return self.read_responses.pop(0) if self.read_responses else []

    def delete(self, msg_id: int) -> None:
        self.deleted.append(msg_id)

    def archive(self, msg_id: int) -> None:
        self.archived.append(msg_id)


@dataclass
class FakeDocs:
    processing: list[str] = field(default_factory=list)
    retrying: list[tuple[str, int, int, str]] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    completed: list[tuple[str, int, dict[str, str], str]] = field(default_factory=list)

    def mark_processing(self, document_id: str) -> None:
        self.processing.append(document_id)

    def mark_retrying(self, document_id, attempt, max_attempts, error):
        self.retrying.append((document_id, attempt, max_attempts, error))

    def mark_failed(self, document_id, error):
        self.failed.append((document_id, error))

    def mark_completed(self, document_id, *, page_count, artifacts, pipeline):
        self.completed.append((document_id, page_count, artifacts, pipeline))


@dataclass
class FakeBlobs:
    # map path → bytes (for downloads)
    store: dict[str, bytes] = field(default_factory=dict)
    uploads: list[tuple[str, bytes, str, bool]] = field(default_factory=list)
    download_error: Exception | None = None

    def download(self, path: str) -> bytes:
        if self.download_error is not None:
            raise self.download_error
        return self.store.get(path, b"fake-pdf")

    def upload(self, path: str, content: bytes, content_type: str, *, upsert: bool = True) -> None:
        self.uploads.append((path, content, content_type, upsert))


class FakeHappyPipeline:
    name = "fake_pipeline"

    def convert(self, pdf_bytes: bytes) -> PipelineResult:
        return PipelineResult(
            artifacts=[
                Artifact(name="markdown", content=b"# hi", content_type="text/markdown", extension="md"),
                Artifact(name="docling_json", content=b"{}", content_type="application/json", extension="docling.json"),
            ],
            page_count=7,
        )


class FakeBoomPipeline:
    name = "fake_pipeline"

    def convert(self, pdf_bytes: bytes) -> PipelineResult:
        raise RuntimeError("boom")


def _msg(read_ct: int = 0, payload: dict[str, Any] | None = None, msg_id: int = 1) -> QueueMessage:
    return QueueMessage(
        msg_id=msg_id,
        read_ct=read_ct,
        payload=payload if payload is not None else {
            "document_id": "doc-123",
            "user_id": "user-abc",
            "file_path": "user-abc/doc-123/in.pdf",
            "filename": "in.pdf",
        },
    )


# ---------- Tests ----------

def test_happy_path_marks_completed_deletes_and_uploads_every_artifact():
    q, docs, blobs, pipe = FakeQueue(), FakeDocs(), FakeBlobs(), FakeHappyPipeline()

    process(_msg(), q, docs, blobs, pipe, max_retries=3)

    assert docs.processing == ["doc-123"]
    assert len(docs.completed) == 1
    doc_id, pages, artifacts, pipeline_name = docs.completed[0]
    assert doc_id == "doc-123"
    assert pages == 7
    assert artifacts == {
        "markdown": "user-abc/doc-123/in.pdf.md",
        "docling_json": "user-abc/doc-123/in.pdf.docling.json",
    }
    assert pipeline_name == "fake_pipeline"

    assert len(blobs.uploads) == 2
    paths = {u[0] for u in blobs.uploads}
    assert paths == set(artifacts.values())

    assert q.deleted == [1]
    assert q.archived == []
    assert docs.retrying == []
    assert docs.failed == []


def test_retry_remaining_marks_retrying_and_does_not_ack():
    q, docs, blobs, pipe = FakeQueue(), FakeDocs(), FakeBlobs(), FakeBoomPipeline()

    # read_ct=1 means this is the first attempt; max_retries=3 means two more tries.
    process(_msg(read_ct=1), q, docs, blobs, pipe, max_retries=3)

    assert docs.processing == ["doc-123"]
    assert len(docs.retrying) == 1
    doc_id, attempt, max_attempts, err = docs.retrying[0]
    assert (doc_id, attempt, max_attempts) == ("doc-123", 1, 3)
    assert "boom" in err

    # Critical: the message is NOT deleted or archived — pgmq re-presents it.
    assert q.deleted == []
    assert q.archived == []
    assert docs.failed == []
    assert docs.completed == []


def test_retry_exhausted_marks_failed_and_archives():
    q, docs, blobs, pipe = FakeQueue(), FakeDocs(), FakeBlobs(), FakeBoomPipeline()

    process(_msg(read_ct=3), q, docs, blobs, pipe, max_retries=3)

    assert docs.processing == ["doc-123"]
    assert len(docs.failed) == 1
    doc_id, err = docs.failed[0]
    assert doc_id == "doc-123"
    assert "boom" in err

    assert q.archived == [1]
    assert q.deleted == []
    assert docs.retrying == []
    assert docs.completed == []


@pytest.mark.parametrize(
    "payload",
    [
        {"user_id": "u", "file_path": "p", "filename": "f"},           # no document_id
        {"document_id": "d", "file_path": "p", "filename": "f"},       # no user_id
        {"document_id": "d", "user_id": "u", "filename": "f"},         # no file_path
        {"document_id": "d", "user_id": "u", "file_path": "p"},        # no filename
    ],
)
def test_bad_payload_archives_without_touching_docstore(payload):
    q, docs, blobs, pipe = FakeQueue(), FakeDocs(), FakeBlobs(), FakeHappyPipeline()

    process(_msg(payload=payload, msg_id=42), q, docs, blobs, pipe, max_retries=3)

    assert q.archived == [42]
    assert q.deleted == []
    assert docs.processing == []
    assert docs.retrying == []
    assert docs.failed == []
    assert docs.completed == []
    assert blobs.uploads == []
