import logging
import os

from worker.config import Settings
from worker.converter import DoclingConverter
from worker.embedder import Embedder
from worker.queue import RedisQueue
from worker.s3 import S3Client

logger = logging.getLogger(__name__)


class JobProcessor:
    def __init__(
        self,
        settings: Settings,
        queue: RedisQueue,
        s3: S3Client,
        converter: DoclingConverter,
    ):
        self.settings = settings
        self.queue = queue
        self.s3 = s3
        self.converter = converter
        self.embedder = Embedder.from_settings(settings) if settings.embedding_api_key else None

    def process(self, job: dict) -> None:
        job_id = job["job_id"]
        s3_bucket = job["s3_bucket"]
        s3_key = job["s3_key"]
        callback_queue = job["callback_queue"]
        metadata = job.get("metadata", {})
        local_path = None

        try:
            # 1. Download PDF from S3
            logger.info("Job %s: downloading s3://%s/%s", job_id, s3_bucket, s3_key)
            local_path = self.s3.download_file(s3_bucket, s3_key, job_id)

            # 2. Convert with Docling
            logger.info("Job %s: conversion started", job_id)
            result = self.converter.convert_pdf(local_path)

            # 3. Stream doc_dict to S3 (via temp file — avoids double memory)
            doc_dict_key = f"output/{job_id}/document.json"
            logger.info("Job %s: uploading doc_dict to S3", job_id)
            self.s3.upload_json_streaming(
                self.settings.s3_output_bucket, doc_dict_key, result.doc_dict,
            )

            # 4. Upload markdown to S3
            markdown_key = f"output/{job_id}/markdown.md"
            self.s3.upload_text(
                self.settings.s3_output_bucket, markdown_key, result.markdown,
            )

            # 5. Stream chunks in batches: embed → push to Redis
            total_chunks = 0
            batch_index = 0
            batch: list[dict] = []

            for chunk_data in result.chunk_iterator:
                batch.append(chunk_data)
                if len(batch) >= self.settings.chunk_batch_size:
                    self._embed_and_push(callback_queue, job_id, batch_index, batch)
                    total_chunks += len(batch)
                    batch_index += 1
                    batch = []

            if batch:
                self._embed_and_push(callback_queue, job_id, batch_index, batch)
                total_chunks += len(batch)

            # 6. Push completion message
            self.queue.push_completion(
                callback_queue,
                job_id,
                doc_dict_s3_key=doc_dict_key,
                markdown_s3_key=markdown_key,
                page_count=result.page_count,
                total_chunks=total_chunks,
                metadata=metadata,
            )
            logger.info(
                "Job %s: completed — %d pages, %d chunks",
                job_id, result.page_count, total_chunks,
            )

        except Exception as e:
            logger.exception("Job %s: failed", job_id)
            try:
                self.queue.push_error(callback_queue, job_id, str(e), retryable=True)
            except Exception:
                logger.exception("Job %s: failed to push error message", job_id)

        finally:
            if local_path and os.path.exists(local_path):
                os.remove(local_path)

    def _embed_and_push(
        self,
        callback_queue: str,
        job_id: str,
        batch_index: int,
        batch: list[dict],
    ) -> None:
        if self.embedder:
            texts = [c["text"] for c in batch]
            embeddings = self.embedder.embed_batch(texts)
            for chunk, emb in zip(batch, embeddings):
                chunk["embedding"] = emb

        self.queue.push_chunk_batch(callback_queue, job_id, batch_index, batch)
        logger.debug("Job %s: pushed chunk batch %d (%d chunks)", job_id, batch_index, len(batch))
