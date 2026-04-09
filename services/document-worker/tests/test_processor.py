"""Tests for worker/processor.py (JobProcessor)."""

from unittest.mock import MagicMock, patch

import pytest

from worker.config import Settings
from worker.processor import JobProcessor


@pytest.fixture
def job():
    return {
        "document_id": "doc-1",
        "user_id": "user-1",
        "s3_key": "user-1/doc-1/test.pdf",
        "s3_bucket": "documents",
        "callback_queue": "results:user-1",
        "metadata": {},
    }


def _make_processor(settings):
    queue, s3, converter = MagicMock(), MagicMock(), MagicMock()
    chunk_writer, doc_updater = MagicMock(), MagicMock()

    result = MagicMock()
    result.doc_dict = {"pages": []}
    result.markdown = "# Test"
    result.page_count = 1
    result.chunk_iterator = iter([
        {"text": "chunk 1", "headings": ["H1"], "label": "text", "page_no": 1},
    ])
    converter.convert_pdf.return_value = result
    s3.download_file.return_value = "/tmp/test.pdf"

    with patch("worker.processor.Embedder"):
        proc = JobProcessor(
            settings=settings, queue=queue, s3=s3, converter=converter,
            chunk_writer=chunk_writer, doc_updater=doc_updater,
        )
    return proc, queue, s3, converter, chunk_writer, doc_updater


@patch("worker.processor.os.path.exists", return_value=True)
@patch("worker.processor.os.remove")
class TestProcessor:
    def test_happy_path(self, mock_rm, mock_exists, settings, job):
        proc, queue, s3, conv, cw, du = _make_processor(settings)
        proc.process(job, msg_id=1)

        s3.download_file.assert_called_once()
        conv.convert_pdf.assert_called_once()
        du.mark_processing.assert_called_once_with("doc-1")
        du.mark_completed.assert_called_once()
        queue.ack_job.assert_called_once_with(1)
        mock_rm.assert_called_once()

    def test_marks_failed_on_error(self, mock_rm, mock_exists, settings, job):
        proc, queue, s3, conv, cw, du = _make_processor(settings)
        s3.download_file.side_effect = Exception("boom")
        proc.process(job, msg_id=1)

        du.mark_failed.assert_called_once()
        queue.nack_job.assert_called_once_with(1)
        queue.push_error.assert_called_once()

    def test_cleanup_on_error(self, mock_rm, mock_exists, settings, job):
        proc, *_ = _make_processor(settings)
        proc.s3.download_file.return_value = "/tmp/test.pdf"
        proc.converter.convert_pdf.side_effect = Exception("fail")
        proc.process(job)
        mock_rm.assert_called_once_with("/tmp/test.pdf")
