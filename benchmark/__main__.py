import argparse

from dotenv import load_dotenv


def main():
    load_dotenv()

    parser = argparse.ArgumentParser(description="Benchmark DoclingConverter")
    parser.add_argument(
        "--cli",
        action="store_true",
        help="Run in CLI mode (no TUI)",
    )
    parser.add_argument(
        "--config",
        default="benchmark/config.yaml",
        help="Config file path (default: benchmark/config.yaml)",
    )
    args = parser.parse_args()

    if args.cli:
        from benchmark.runner import run_cli

        run_cli(args.config)
    else:
        from benchmark.tui import BenchmarkTUI

        app = BenchmarkTUI()
        app.run()


if __name__ == "__main__":
    main()
