import argparse
import sys

from dotenv import load_dotenv


def main():
    load_dotenv()

    parser = argparse.ArgumentParser(description="Integration pipeline TUI")
    parser.add_argument(
        "--pdf",
        default="benchmark/data/pdfs/gaussians.pdf",
        help="PDF file to process (default: benchmark/data/pdfs/gaussians.pdf)",
    )
    args = parser.parse_args()

    from worker.config import load_settings

    settings = load_settings()

    if not settings.database_url:
        print("ERROR: DATABASE_URL must be set in .env or environment")
        sys.exit(1)

    from integration.tui import PipelineTUI

    app = PipelineTUI(settings=settings, pdf_path=args.pdf)
    app.run()


if __name__ == "__main__":
    main()
