import logging
import os
from datetime import datetime, timezone

from worker.config import Settings
from worker.converter import DoclingConverter
from worker.embedder import Embedder
from worker.s3 import S3Client

logger = logging.getLogger(__name__)


class JobProcessor:
    def __init__(
        self,
        settings: Settings,
        queue,
        s3: S3Client,
        converter: DoclingConverter,
        chunk_writer=None,
        doc_updater=None,
    ):
        self.settings = settings
        self.queue = queue
        self.s3 = s3
        self.converter = converter
        self.embedder = Embedder.from_settings(settings) if settings.embedding_api_key else None
        self.chunk_writer = chunk_writer
        self.doc_updater = doc_updater

    def process(self, job: dict, msg_id=None) -> None:
        document_id = job.get("document_id") or job.get("job_id")
        job_id = job.get("job_id", document_id)
        user_id = job.get("user_id")
        s3_bucket = job.get("s3_bucket", self.settings.s3_output_bucket)
        s3_key = job.get("s3_key") or job.get("file_path")
        callback_queue = job.get("callback_queue")
        metadata = job.get("metadata", {})
        local_path = None

        try:
            if self.doc_updater and document_id:
                self.doc_updater.mark_processing(document_id)

            # 1. Download PDF from S3 / Supabase Storage
            logger.info("Job %s: downloading s3://%s/%s", job_id, s3_bucket, s3_key)
            local_path = self.s3.download_file(s3_bucket, s3_key, job_id)

            # 2. Convert with Docling
            logger.info("Job %s: conversion started", job_id)
            result = self.converter.convert_pdf(local_path)

            # 3. Stream doc_dict to S3
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

            # 5. Stream chunks in batches: embed → write to PGVector / push to Redis
            total_chunks = 0
            batch_index = 0
            batch: list[dict] = []

            for chunk_data in result.chunk_iterator:
                batch.append(chunk_data)
                if len(batch) >= self.settings.chunk_batch_size:
                    self._process_batch(
                        batch, batch_index, total_chunks,
                        callback_queue, job_id, document_id, user_id,
                    )
                    total_chunks += len(batch)
                    batch_index += 1
                    batch = []

            if batch:
                self._process_batch(
                    batch, batch_index, total_chunks,
                    callback_queue, job_id, document_id, user_id,
                )
                total_chunks += len(batch)

            # 6. Mark completed
            if self.doc_updater and document_id:
                completion_metadata = {
                    **metadata,
                    "vlm_model": self.settings.vlm_model,
                    "embedding_model": self.settings.embedding_model if self.embedder else None,
                    "embedding_dimensions": self.settings.embedding_dimensions if self.embedder else None,
                    "processing_completed_at": datetime.now(timezone.utc).isoformat(),
                }
                self.doc_updater.mark_completed(
                    document_id,
                    page_count=result.page_count,
                    total_chunks=total_chunks,
                    doc_json_path=doc_dict_key,
                    markdown_path=markdown_key,
                    metadata=completion_metadata,
                )

            if callback_queue:
                self.queue.push_completion(
                    callback_queue, job_id,
                    doc_dict_s3_key=doc_dict_key,
                    markdown_s3_key=markdown_key,
                    page_count=result.page_count,
                    total_chunks=total_chunks,
                    metadata=metadata,
                )

            # Ack pgmq message
            if msg_id is not None and hasattr(self.queue, "ack_job"):
                self.queue.ack_job(msg_id)

            logger.info(
                "Job %s: completed — %d pages, %d chunks",
                job_id, result.page_count, total_chunks,
            )

        except Exception as e:
            logger.exception("Job %s: failed", job_id)

            if self.doc_updater and document_id:
                try:
                    self.doc_updater.mark_failed(document_id, str(e))
                except Exception:
                    logger.exception("Job %s: failed to update document status", job_id)

            if msg_id is not None and hasattr(self.queue, "nack_job"):
                try:
                    self.queue.nack_job(msg_id)
                except Exception:
                    logger.exception("Job %s: failed to nack pgmq message", job_id)

            if callback_queue:
                try:
                    self.queue.push_error(callback_queue, job_id, str(e), retryable=True)
                except Exception:
                    logger.exception("Job %s: failed to push error message", job_id)

        finally:
            if local_path and os.path.exists(local_path):
                os.remove(local_path)

    def _process_batch(
        self,
        batch: list[dict],
        batch_index: int,
        global_start_index: int,
        callback_queue: str | None,
        job_id: str,
        document_id: str | None,
        user_id: str | None,
    ) -> None:
        # Embed
        if self.embedder:
            texts = [c["text"] for c in batch]
            embeddings = self.embedder.embed_batch(texts)
            for chunk, emb in zip(batch, embeddings):
                chunk["embedding"] = emb

        # Write to PGVector
        if self.chunk_writer and document_id and user_id:
            chunk_metadata = {
                "embedding_model": self.settings.embedding_model if self.embedder else None,
                "embedding_dimensions": self.settings.embedding_dimensions if self.embedder else None,
                "vlm_model": self.settings.vlm_model,
                "chunk_method": "hybrid_chunker",
                "processed_at": datetime.now(timezone.utc).isoformat(),
            }
            self.chunk_writer.insert_chunks(
                document_id, user_id, batch,
                start_index=global_start_index,
                metadata=chunk_metadata,
            )

        # Push to Redis callback (backward compat)
        if callback_queue and hasattr(self.queue, "push_chunk_batch"):
            self.queue.push_chunk_batch(callback_queue, job_id, batch_index, batch)

        logger.debug("Job %s: processed batch %d (%d chunks)", job_id, batch_index, len(batch))
