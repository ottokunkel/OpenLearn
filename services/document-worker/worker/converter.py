import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event, Lock
from typing import Callable, Optional

import requests

from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import VlmConvertOptions, VlmPipelineOptions
from docling.datamodel.vlm_engine_options import ApiVlmEngineOptions, VlmEngineType
from docling.document_converter import DocumentConverter, PdfFormatOption
from docling.pipeline.vlm_pipeline import VlmPipeline
from docling_core.transforms.chunker import HybridChunker

from worker.config import Settings

logger = logging.getLogger(__name__)

MAX_RETRIES = 5
RETRY_BACKOFF = 1.0  # seconds, doubles each retry


class CancelledError(Exception):
    """Raised when conversion is cancelled via the cancel event."""


@dataclass
class RequestStats:
    """Thread-safe counters for in-flight request tracking."""

    in_flight: int = 0
    completed: int = 0
    failed: int = 0
    _lock: Lock = field(default_factory=Lock)

    def start(self):
        with self._lock:
            self.in_flight += 1
            return self._snapshot()

    def complete(self):
        with self._lock:
            self.in_flight = max(0, self.in_flight - 1)
            self.completed += 1
            return self._snapshot()

    def fail(self):
        with self._lock:
            self.in_flight = max(0, self.in_flight - 1)
            self.failed += 1
            return self._snapshot()

    def _snapshot(self) -> tuple[int, int, int]:
        return (self.in_flight, self.completed, self.failed)


@contextmanager
def _patch_retries(
    on_retry: Optional[Callable[[int, int, str, float], None]] = None,
    on_error: Optional[Callable[[int, str, bool], None]] = None,
    on_status: Optional[Callable[[int, int, int], None]] = None,
    cancel_event: Optional[Event] = None,
):
    """Monkey-patch requests.Session.send to retry on transient API errors.

    Args:
        on_retry: callback(attempt, max_retries, error_msg, wait_seconds)
        on_error: callback(status_code, error_msg, will_retry) — fires on every failed response
        on_status: callback(in_flight, completed, failed) — fires on every request start/end
        cancel_event: if set, checked before each retry; raises CancelledError
    """
    original_send = requests.Session.send
    stats = RequestStats()

    def _emit_status(snapshot):
        if on_status:
            on_status(*snapshot)

    def retrying_send(session_self, request, **kwargs):
        if request.method != "POST" or "chat/completions" not in str(request.url):
            return original_send(session_self, request, **kwargs)

        if cancel_event and cancel_event.is_set():
            raise CancelledError("Conversion cancelled")

        _emit_status(stats.start())

        last_response = None
        for attempt in range(MAX_RETRIES + 1):
            response = original_send(session_self, request, **kwargs)
            if response.ok:
                _emit_status(stats.complete())
                return response

            try:
                body = response.json()
                msg = body.get("error", {}).get("message", "")
            except Exception:
                msg = response.text[:200] if response.text else f"HTTP {response.status_code}"

            retryable = any(s in msg.lower() for s in [
                "model busy", "retry later", "overloaded",
                "rate limit", "too many requests",
            ]) or response.status_code in (429, 503, 502)

            will_retry = retryable and attempt < MAX_RETRIES

            if on_error:
                on_error(response.status_code, msg, will_retry)

            if not will_retry:
                _emit_status(stats.fail())
                return response

            wait = RETRY_BACKOFF * (2 ** attempt)
            logger.warning(
                "API returned %s (%s), retrying in %.1fs (%d/%d)",
                response.status_code, msg, wait, attempt + 1, MAX_RETRIES,
            )

            if on_retry:
                on_retry(attempt + 1, MAX_RETRIES, msg, wait)

            deadline = time.monotonic() + wait
            while time.monotonic() < deadline:
                if cancel_event and cancel_event.is_set():
                    _emit_status(stats.fail())
                    raise CancelledError("Conversion cancelled")
                time.sleep(min(0.25, deadline - time.monotonic()))

            last_response = response

        _emit_status(stats.fail())
        return last_response

    requests.Session.send = retrying_send
    try:
        yield
    finally:
        requests.Session.send = original_send


class DoclingConverter:
    def __init__(self, settings: Settings):
        self.settings = settings
        # Set page_batch_size to a large number so all pages enter the pipeline
        # at once. The actual in-flight request limit is controlled by
        # ApiVlmEngineOptions.concurrency — this gives true sliding-window
        # concurrency instead of Docling's default batch-and-wait behavior.
        from docling.datamodel.settings import settings as docling_settings
        docling_settings.perf.page_batch_size = settings.concurrency
        self.converter = self._build_converter(settings)

    @staticmethod
    def _build_converter(settings: Settings) -> DocumentConverter:
        engine_options = ApiVlmEngineOptions(
            engine_type=VlmEngineType.API,
            url=f"{settings.openrouter_base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {settings.openrouter_api_key}",
                "HTTP-Referer": "https://openlearn.app",
                "X-Title": "OpenLearn DoclingConverter",
            },
            params={
                "model": settings.vlm_model,
                "temperature": 0.0,
                "max_tokens": settings.max_tokens,
                **settings.extra_api_params,
            },
            timeout=settings.document_timeout_seconds,
            concurrency=settings.concurrency,
        )

        vlm_options = VlmConvertOptions.from_preset(
            "qwen",
            engine_options=engine_options,
        )

        pipeline_options = VlmPipelineOptions(
            vlm_options=vlm_options,
            enable_remote_services=True,
        )

        return DocumentConverter(
            allowed_formats=[InputFormat.PDF],
            format_options={
                InputFormat.PDF: PdfFormatOption(
                    pipeline_cls=VlmPipeline,
                    pipeline_options=pipeline_options,
                ),
            },
        )

    def convert_pdf(
        self,
        file_path: str,
        on_retry: Optional[Callable[[int, int, str, float], None]] = None,
        on_error: Optional[Callable[[int, str, bool], None]] = None,
        on_status: Optional[Callable[[int, int, int], None]] = None,
        cancel_event: Optional[Event] = None,
    ) -> "ConversionResult":
        logger.info("Converting PDF: %s", file_path)

        with _patch_retries(
            on_retry=on_retry,
            on_error=on_error,
            on_status=on_status,
            cancel_event=cancel_event,
        ):
            result = self.converter.convert(source=Path(file_path))

        doc = result.document

        doc_dict = doc.export_to_dict()
        markdown = doc.export_to_markdown()
        page_count = len(doc.pages) if hasattr(doc, "pages") else 0

        logger.info(
            "Conversion complete: %d pages, %d bytes markdown",
            page_count,
            len(markdown),
        )

        def _chunk_iter() -> Iterator[dict]:
            chunker = HybridChunker(merge_peers=True)
            for chunk in chunker.chunk(doc):
                chunk_data = {
                    "text": chunk.text,
                    "headings": chunk.meta.headings,
                }
                if chunk.meta.doc_items:
                    item = chunk.meta.doc_items[0]
                    chunk_data["label"] = item.label if isinstance(item.label, str) else item.label.value
                    if item.prov:
                        chunk_data["page_no"] = item.prov[0].page_no
                yield chunk_data

        return ConversionResult(
            doc_dict=doc_dict,
            markdown=markdown,
            page_count=page_count,
            chunk_iterator=_chunk_iter(),
        )


@dataclass
class ConversionResult:
    """Result of PDF conversion with lazy chunk iteration."""

    doc_dict: dict
    markdown: str
    page_count: int
    chunk_iterator: Iterator[dict]

    def to_dict(self) -> dict:
        """Materialize all chunks into a dict. For benchmark/testing use."""
        chunks = list(self.chunk_iterator)
        return {
            "doc_dict": self.doc_dict,
            "markdown": self.markdown,
            "chunks": chunks,
            "page_count": self.page_count,
        }
