import argparse
import os

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
    parser.add_argument(
        "--upload",
        action="store_true",
        help="Upload results to Supabase after running (requires DATABASE_URL)",
    )
    args = parser.parse_args()

    if args.cli:
        from benchmark.runner import run_cli

        run_cli(args.config)
    else:
        from benchmark.tui import BenchmarkTUI

        app = BenchmarkTUI()
        app.run()

    if args.upload:
        database_url = os.environ.get("DATABASE_URL", "")
        if not database_url:
            print("Warning: --upload specified but DATABASE_URL is not set. Skipping upload.")
        else:
            import yaml

            with open(args.config) as f:
                cfg = yaml.safe_load(f)
            results_path = cfg.get("output", {}).get("results_json", "benchmark/data/results.json")

            if os.path.isfile(results_path):
                from benchmark.upload import upload_results

                count = upload_results(database_url, results_path)
                print(f"Uploaded {count} benchmark run(s) to Supabase.")
            else:
                print(f"Warning: results file not found at {results_path}. Skipping upload.")


if __name__ == "__main__":
    main()
