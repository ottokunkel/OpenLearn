import logging
import signal
import time

from . import di, processor
from .config import Settings


def run() -> int:
    s = Settings()  # env-driven; raises with clear error if incomplete
    logging.basicConfig(
        level=s.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    log = logging.getLogger("doc_worker_v2")

    queue = di.build_queue(s)
    docs = di.build_document_store(s)
    blobs = di.build_blob_store(s)
    pipeline = di.build_pipeline(s)

    stop = False

    def _shutdown(signum, _frame):
        nonlocal stop
        log.info("signal=%s, draining", signum)
        stop = True

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    log.info(
        "doc-worker-v2 online; pipeline=%s backends=q:%s/d:%s/b:%s",
        pipeline.name,
        s.queue_backend.value,
        s.document_store_backend.value,
        s.blob_store_backend.value,
    )

    try:
        while not stop:
            try:
                msgs = queue.read(s.visibility_timeout_s, s.batch_size)
            except Exception:
                log.exception("queue read failed; backing off")
                time.sleep(s.poll_interval_s)
                continue
            if not msgs:
                time.sleep(s.poll_interval_s)
                continue
            for msg in msgs:
                if stop:
                    break
                processor.process(
                    msg, queue, docs, blobs, pipeline, max_retries=s.max_retries
                )
    finally:
        for backend in (queue, docs, blobs):
            close = getattr(backend, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    log.exception("error closing %s", type(backend).__name__)

    log.info("exited cleanly")
    return 0
