"""Redis queue consumer/producer using plain lists for cross-language compatibility."""

import json
import logging

import redis

logger = logging.getLogger(__name__)


class RedisQueue:
    def __init__(self, redis_url: str, input_queue: str = "docling:jobs"):
        self._redis = redis.Redis.from_url(redis_url, decode_responses=True)
        self._input_queue = input_queue

    def wait_for_job(self, timeout: int = 5) -> dict | None:
        """Block until a job is available on the input queue, or timeout."""
        result = self._redis.brpop(self._input_queue, timeout=timeout)
        if result is None:
            return None
        _, raw = result
        return json.loads(raw)

    def push_chunk_batch(
        self,
        callback_queue: str,
        job_id: str,
        batch_index: int,
        chunks: list[dict],
    ) -> None:
        msg = json.dumps({
            "job_id": job_id,
            "type": "chunks",
            "batch_index": batch_index,
            "chunks": chunks,
        })
        self._redis.lpush(callback_queue, msg)

    def push_completion(
        self,
        callback_queue: str,
        job_id: str,
        doc_dict_s3_key: str,
        markdown_s3_key: str,
        page_count: int,
        total_chunks: int,
        metadata: dict,
    ) -> None:
        msg = json.dumps({
            "job_id": job_id,
            "type": "complete",
            "doc_dict_s3_key": doc_dict_s3_key,
            "markdown_s3_key": markdown_s3_key,
            "page_count": page_count,
            "total_chunks": total_chunks,
            "metadata": metadata,
        })
        self._redis.lpush(callback_queue, msg)

    def push_error(
        self,
        callback_queue: str,
        job_id: str,
        error: str,
        retryable: bool,
    ) -> None:
        msg = json.dumps({
            "job_id": job_id,
            "type": "error",
            "error": error,
            "retryable": retryable,
        })
        self._redis.lpush(callback_queue, msg)

    def ping(self) -> bool:
        return self._redis.ping()

    def close(self) -> None:
        self._redis.close()
