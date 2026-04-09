import logging
import signal
import time

from worker.config import Settings
from worker.converter import DoclingConverter
from worker.db import Database
from worker.processor import JobProcessor

logger = logging.getLogger(__name__)


class WorkerLoop:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.running = True
        self.db = Database(settings)
        self.converter = DoclingConverter(settings)
        self.processor = JobProcessor(settings, self.db, self.converter)

    def _handle_signal(self, signum, frame):
        logger.info("Received signal %s, shutting down gracefully...", signum)
        self.running = False

    def run(self):
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

        logger.info(
            "Worker %s started, polling every %ds",
            self.settings.worker_id,
            self.settings.poll_interval_seconds,
        )

        while self.running:
            try:
                job = self.db.claim_job(self.settings.worker_id)
                if job:
                    logger.info("Claimed job %s", job["id"])
                    self.processor.process(job)
                else:
                    time.sleep(self.settings.poll_interval_seconds)
            except Exception:
                logger.exception("Unexpected error in polling loop")
                time.sleep(self.settings.poll_interval_seconds)

        logger.info("Worker %s shut down", self.settings.worker_id)
