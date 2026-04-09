"""Tests for worker/queue.py (RedisQueue)."""

import json
from unittest.mock import MagicMock, patch


@patch("worker.queue.redis.Redis.from_url")
class TestRedisQueue:
    def _make(self, mock_from_url):
        mock_redis = MagicMock()
        mock_from_url.return_value = mock_redis
        from worker.queue import RedisQueue
        return RedisQueue("redis://localhost/0", "test:jobs"), mock_redis

    def test_wait_for_job(self, mock_from_url):
        q, r = self._make(mock_from_url)
        r.brpop.return_value = ("test:jobs", '{"id": "1"}')
        assert q.wait_for_job() == {"id": "1"}

    def test_wait_for_job_timeout(self, mock_from_url):
        q, r = self._make(mock_from_url)
        r.brpop.return_value = None
        assert q.wait_for_job() is None

    def test_push_error(self, mock_from_url):
        q, r = self._make(mock_from_url)
        q.push_error("cb:q", "j1", "boom", retryable=True)
        payload = json.loads(r.lpush.call_args[0][1])
        assert payload["error"] == "boom"
        assert payload["retryable"] is True
