"""API usage tracker that intercepts requests.Session.send to capture OpenRouter token usage."""

import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable, Optional

import requests


@dataclass
class ApiCallInfo:
    input_tokens: int
    output_tokens: int
    total_tokens: int
    duration: float


class ApiUsageTracker:
    """Tracks token usage and cost by intercepting HTTP responses from OpenRouter.

    Docling uses requests.post() to call the chat/completions endpoint.
    This tracker monkey-patches requests.Session.send inside a context manager
    to extract usage data from the JSON response body.
    """

    def __init__(
        self,
        pricing: dict | None = None,
        on_api_call: Callable[[ApiCallInfo], None] | None = None,
    ):
        self.pricing = pricing or {}
        self.on_api_call = on_api_call
        self.reset()

    def reset(self):
        self.calls: list[ApiCallInfo] = []
        self.total_input_tokens: int = 0
        self.total_output_tokens: int = 0
        self.total_cost: float = 0.0
        self._request_start_times: dict[int, float] = {}

    @contextmanager
    def track(self):
        """Patches requests.Session.send to intercept API responses."""
        original_send = requests.Session.send
        tracker = self

        def patched_send(session_self, request, **kwargs):
            req_id = id(request)
            tracker._request_start_times[req_id] = time.perf_counter()

            response = original_send(session_self, request, **kwargs)

            if request.method == "POST" and "chat/completions" in str(request.url):
                start_time = tracker._request_start_times.pop(req_id, None)
                duration = time.perf_counter() - start_time if start_time else 0.0
                tracker._extract_usage(response, duration)

            return response

        requests.Session.send = patched_send
        try:
            yield
        finally:
            requests.Session.send = original_send

    def _extract_usage(self, response: requests.Response, duration: float):
        """Extract usage from an OpenAI-compatible JSON response."""
        try:
            content_type = response.headers.get("content-type", "")
            if "text/event-stream" in content_type:
                return

            data = response.json()
            usage = data.get("usage", {})
            if not usage:
                return

            input_t = usage.get("prompt_tokens", 0)
            output_t = usage.get("completion_tokens", 0)
            total_t = usage.get("total_tokens", input_t + output_t)

            self.total_input_tokens += input_t
            self.total_output_tokens += output_t

            input_rate = self.pricing.get("input_cost_per_mtok", 0)
            output_rate = self.pricing.get("output_cost_per_mtok", 0)
            call_cost = (
                input_t * input_rate / 1_000_000
                + output_t * output_rate / 1_000_000
            )
            self.total_cost += call_cost

            call_info = ApiCallInfo(
                input_tokens=input_t,
                output_tokens=output_t,
                total_tokens=total_t,
                duration=duration,
            )
            self.calls.append(call_info)

            if self.on_api_call:
                self.on_api_call(call_info)

        except Exception:
            pass
