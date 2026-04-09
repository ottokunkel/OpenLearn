import json
import logging
import os

from worker.config import Settings
from worker.converter import DoclingConverter
from worker.db import Database
from worker.storage import StorageClient

logger = logging.getLogger(__name__)

MARKDOWN_PREVIEW_LIMIT = 10_000


class JobProcessor:
    def __init__(self, settings: Settings, db: Database, converter: DoclingConverter):
        self.settings = settings
        self.db = db
        self.storage = StorageClient(db.client, settings)
        self.converter = converter

    def process(self, job: dict) -> None:
        job_id = job["id"]
        storage_path = job["storage_path"]
        local_path = None

        try:
            logger.info("Job %s: downloading %s", job_id, storage_path)
            local_path = self.storage.download_pdf(job_id, storage_path)

            logger.info("Job %s: conversion started", job_id)
            result = self.converter.convert_pdf(local_path)

            output_storage_path = f"{job_id}/document.json"
            doc_json_bytes = json.dumps(result["doc_dict"], ensure_ascii=False).encode("utf-8")
            logger.info("Job %s: uploading result (%d bytes)", job_id, len(doc_json_bytes))
            self.storage.upload_json(output_storage_path, doc_json_bytes)

            markdown_preview = (result["markdown"] or "")[:MARKDOWN_PREVIEW_LIMIT]

            doc_name = os.path.basename(storage_path)
            doc_id = self.db.insert_document(
                job_id=job_id,
                doc_name=doc_name,
                page_count=result["page_count"],
                storage_path=output_storage_path,
                markdown_preview=markdown_preview,
            )
            logger.info("Job %s: document %s created", job_id, doc_id)

            self.db.complete_job(job_id)
            logger.info("Job %s: completed", job_id)

        except Exception as e:
            logger.exception("Job %s: failed", job_id)
            self.db.fail_job(job_id, str(e), retryable=True)

        finally:
            if local_path and os.path.exists(local_path):
                os.remove(local_path)
