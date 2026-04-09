import logging
import signal
import time

from worker.config import Settings
from worker.converter import DoclingConverter
from worker.processor import JobProcessor
from worker.queue import RedisQueue
from worker.s3 import S3Client

logger = logging.getLogger(__name__)


class WorkerLoop:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.running = True
        self.queue = RedisQueue(settings.redis_url, settings.input_queue)
        self.s3 = S3Client(
            endpoint_url=settings.s3_endpoint_url or None,
            region=settings.s3_region,
            access_key=settings.s3_access_key,
            secret_key=settings.s3_secret_key,
            download_dir=settings.download_dir,
        )
        self.converter = DoclingConverter(settings)
        self.processor = JobProcessor(settings, self.queue, self.s3, self.converter)

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
                job = self.queue.wait_for_job(timeout=5)
                if job:
                    logger.info("Received job %s", job.get("job_id"))
                    self.processor.process(job)
            except Exception:
                logger.exception("Unexpected error in worker loop")
                time.sleep(2)

        self.queue.close()
        logger.info("Worker %s shut down", self.settings.worker_id)
