import logging
import signal
import time

from worker.config import Settings
from worker.converter import DoclingConverter
from worker.processor import JobProcessor
from worker.s3 import S3Client

logger = logging.getLogger(__name__)


class WorkerLoop:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.running = True
        self._pool = None

        self.s3 = S3Client(
            endpoint_url=settings.s3_endpoint_url or None,
            region=settings.s3_region,
            access_key=settings.s3_access_key,
            secret_key=settings.s3_secret_key,
            download_dir=settings.download_dir,
        )
        self.converter = DoclingConverter(settings)

        if settings.database_url:
            from worker.db import ChunkWriter, DocumentUpdater, PgmqQueue, create_pool

            self._pool = create_pool(settings.database_url)
            self.queue = PgmqQueue(self._pool)
            chunk_writer = ChunkWriter(self._pool)
            doc_updater = DocumentUpdater(self._pool)
            logger.info("Using pgmq queue (Postgres-backed)")
        else:
            from worker.queue import RedisQueue

            self.queue = RedisQueue(settings.redis_url, settings.input_queue)
            chunk_writer = None
            doc_updater = None
            logger.info("Using Redis queue")

        self.processor = JobProcessor(
            settings, self.queue, self.s3, self.converter,
            chunk_writer=chunk_writer,
            doc_updater=doc_updater,
        )

    def _handle_signal(self, signum, frame):
        logger.info("Received signal %s, shutting down gracefully...", signum)
        self.running = False

    def run(self):
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

        logger.info(
            "Worker %s started, listening on queue '%s'",
            self.settings.worker_id,
            self.settings.input_queue,
        )

        while self.running:
            try:
                result = self.queue.wait_for_job(timeout=5)
                if result is None:
                    continue

                if isinstance(result, tuple):
                    msg_id, job = result
                else:
                    msg_id, job = None, result

                logger.info("Received job %s", job.get("job_id") or job.get("document_id"))
                self.processor.process(job, msg_id=msg_id)
            except Exception:
                logger.exception("Unexpected error in worker loop")
                time.sleep(2)

        self.queue.close()
        if self._pool:
            self._pool.close()
        logger.info("Worker %s shut down", self.settings.worker_id)
