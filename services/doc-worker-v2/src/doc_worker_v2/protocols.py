from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from .artifacts import PipelineResult


@dataclass(frozen=True)
class QueueMessage:
    msg_id: int
    read_ct: int
    payload: dict[str, Any]


@runtime_checkable
class Queue(Protocol):
    def read(self, visibility_timeout_s: int, qty: int) -> list[QueueMessage]: ...
    def delete(self, msg_id: int) -> None: ...
    def archive(self, msg_id: int) -> None: ...


@runtime_checkable
class DocumentStore(Protocol):
    def mark_processing(self, document_id: str) -> None: ...
    def mark_retrying(
        self, document_id: str, attempt: int, max_attempts: int, error: str
    ) -> None: ...
    def mark_failed(self, document_id: str, error: str) -> None: ...
    def mark_completed(
        self,
        document_id: str,
        *,
        page_count: int,
        artifacts: dict[str, str],
        pipeline: str,
    ) -> None: ...


@runtime_checkable
class BlobStore(Protocol):
    def download(self, path: str) -> bytes: ...
    def upload(
        self,
        path: str,
        content: bytes,
        content_type: str,
        *,
        upsert: bool = True,
    ) -> None: ...


@runtime_checkable
class Pipeline(Protocol):
    name: str

    def convert(self, pdf_bytes: bytes) -> PipelineResult: ...
