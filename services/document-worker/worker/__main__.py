import logging

from dotenv import load_dotenv

from worker.config import load_settings
from worker.loop import WorkerLoop


def main():
    load_dotenv()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    settings = load_settings()
    loop = WorkerLoop(settings)
    loop.run()


if __name__ == "__main__":
    main()
