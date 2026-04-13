"""Polling loop entry point. Run with: ``python -m DoclingWorker``."""
from __future__ import annotations

import logging
import signal
import sys
import time

from . import config as config_mod
from . import job_processor, queue, supabase_client
from .docling_runner import build_converter


def main() -> int:
    cfg = config_mod.load()
    logging.basicConfig(
        level=cfg.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    log = logging.getLogger("DoclingWorker")

    client = supabase_client.get_client(cfg)
    converter = build_converter(cfg)

    stop = False

    def _shutdown(signum, _frame):
        nonlocal stop
        log.info("received signal %s, draining after current message", signum)
        stop = True

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    log.info("worker online; polling %s every %ss", queue.QUEUE_NAME, cfg.poll_interval_s)
    while not stop:
        try:
            msgs = queue.read(client, cfg.visibility_timeout_s, cfg.batch_size)
        except Exception:
            log.exception("queue read failed; backing off")
            time.sleep(cfg.poll_interval_s)
            continue

        if not msgs:
            time.sleep(cfg.poll_interval_s)
            continue

        for msg in msgs:
            if stop:
                break
            job_processor.process(msg, client, converter)

    log.info("worker exited cleanly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
