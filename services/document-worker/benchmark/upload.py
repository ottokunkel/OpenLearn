"""Upload benchmark results from results.json to the Supabase benchmark_runs table.

Usage:
    python -m benchmark.upload                           # default: benchmark/data/results.json
    python -m benchmark.upload --file path/to/results.json

Requires DATABASE_URL in .env or environment.
"""

import argparse
import json
import os
import sys

from dotenv import load_dotenv
import psycopg


def upload_results(database_url: str, results_path: str) -> int:
    with open(results_path) as f:
        data = json.load(f)

    runs = data.get("runs", [])
    if not runs:
        print("No runs found in results file.")
        return 0

    inserted = 0
    with psycopg.connect(database_url, autocommit=True) as conn:
        for run in runs:
            if "error" in run and run.get("wall_time_seconds") is None:
                # Skip entries that only have an error and no metrics
                continue

            conn.execute(
                """
                INSERT INTO benchmark_runs
                    (pdf_label, model, wall_time_seconds, page_count, api_calls,
                     input_tokens, output_tokens, total_tokens, estimated_cost_usd,
                     markdown_length, error)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                [
                    run["pdf"],
                    run["model"],
                    run.get("wall_time_seconds", 0),
                    run.get("page_count", 0),
                    run.get("api_calls", 0),
                    run.get("input_tokens", 0),
                    run.get("output_tokens", 0),
                    run.get("total_tokens", 0),
                    run.get("estimated_cost_usd", 0),
                    run.get("markdown_length", 0),
                    run.get("error"),
                ],
            )
            inserted += 1

    return inserted


def main():
    load_dotenv()

    parser = argparse.ArgumentParser(description="Upload benchmark results to Supabase")
    parser.add_argument(
        "--file",
        default="benchmark/data/results.json",
        help="Path to results.json (default: benchmark/data/results.json)",
    )
    args = parser.parse_args()

    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        print("Error: DATABASE_URL is not set.", file=sys.stderr)
        sys.exit(1)

    if not os.path.isfile(args.file):
        print(f"Error: file not found: {args.file}", file=sys.stderr)
        sys.exit(1)

    count = upload_results(database_url, args.file)
    print(f"Uploaded {count} benchmark run(s) to Supabase.")


if __name__ == "__main__":
    main()
