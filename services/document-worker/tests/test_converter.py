"""Tests for worker/converter.py (RequestStats, _patch_retries, ConversionResult)."""

from threading import Event
from unittest.mock import MagicMock, patch

import pytest
import requests

from worker.converter import CancelledError, ConversionResult, RequestStats, _patch_retries


def _resp(status, body=None):
    import json
    r = requests.models.Response()
    r.status_code = status
    r._content = json.dumps(body or {}).encode()
    r.headers["Content-Type"] = "application/json"
    return r


class TestRequestStats:
    def test_lifecycle(self):
        s = RequestStats()
        s.start()
        assert s.in_flight == 1
        s.complete()
        assert (s.in_flight, s.completed, s.failed) == (0, 1, 0)
        s.fail()
        assert s.in_flight == 0  # floors at 0


class TestPatchRetries:
    @patch("worker.converter.RETRY_BACKOFF", 0)
    def test_retries_on_429(self):
        original = requests.Session.send
        calls = []

        def mock_send(self, req, **kw):
            calls.append(1)
            if len(calls) == 1:
                return _resp(429, {"error": {"message": "rate limit"}})
            return _resp(200)

        requests.Session.send = mock_send
        try:
            with _patch_retries():
                s = requests.Session()
                r = s.send(s.prepare_request(
                    requests.Request("POST", "https://x/chat/completions")
                ))
                assert r.status_code == 200
                assert len(calls) == 2
        finally:
            requests.Session.send = original

    def test_skips_non_post(self):
        original = requests.Session.send
        requests.Session.send = lambda self, req, **kw: _resp(429)
        try:
            with _patch_retries():
                s = requests.Session()
                r = s.send(s.prepare_request(
                    requests.Request("GET", "https://x/chat/completions")
                ))
                assert r.status_code == 429  # no retry
        finally:
            requests.Session.send = original

    @patch("worker.converter.RETRY_BACKOFF", 0)
    def test_cancel_event(self):
        original = requests.Session.send
        requests.Session.send = lambda self, req, **kw: _resp(200)
        cancel = Event()
        cancel.set()
        try:
            with pytest.raises(CancelledError):
                with _patch_retries(cancel_event=cancel):
                    s = requests.Session()
                    s.send(s.prepare_request(
                        requests.Request("POST", "https://x/chat/completions")
                    ))
        finally:
            requests.Session.send = original

    def test_restores_send(self):
        original = requests.Session.send
        with _patch_retries():
            assert requests.Session.send is not original
        assert requests.Session.send is original


class TestConversionResult:
    def test_to_dict(self):
        r = ConversionResult(
            doc_dict={"p": 1}, markdown="# Hi", page_count=1,
            chunk_iterator=iter([{"text": "a"}]),
        )
        d = r.to_dict()
        assert d["chunks"] == [{"text": "a"}]
