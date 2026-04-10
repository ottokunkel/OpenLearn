"""Executes benchmark and integration test runs triggered from the admin panel.

Reads configuration from the test_runs table, runs the appropriate test,
and writes progress/logs/results back to the same table.
"""

import json
import logging
import os
import time
from datetime import datetime, timezone

from psycopg_pool import ConnectionPool

from worker.config import Settings

logger = logging.getLogger(__name__)

# Bundled test PDFs that ship with the worker image
BUILTIN_PDFS = {
    "gaussians.pdf": "benchmark/data/pdfs/gaussians.pdf",
    "quant_book.pdf": "benchmark/data/pdfs/quant_book.pdf",
}

# Minimum interval between progress DB writes (seconds)
PROGRESS_THROTTLE = 2.0


class TestRunner:
    """Handles benchmark and integration test execution for admin-triggered runs."""

    def __init__(self, pool: ConnectionPool, settings: Settings):
        self._pool = pool
        self._settings = settings

    def execute(self, test_run_id: str, test_type: str) -> None:
        logger.info("Test run %s: starting (%s)", test_run_id, test_type)
        self._update_status(test_run_id, "running")

        try:
            run = self._fetch_run(test_run_id)
            if run is None:
                raise RuntimeError(f"Test run {test_run_id} not found")

            pdf_path = self._resolve_pdf(run)

            if test_type == "benchmark":
                self._run_benchmark(test_run_id, run, pdf_path)
            elif test_type == "integration":
                self._run_integration(test_run_id, run, pdf_path)
            else:
                raise ValueError(f"Unknown test type: {test_type}")

        except Exception as e:
            logger.exception("Test run %s: failed", test_run_id)
            self._fail(test_run_id, str(e))

    # -- Benchmark execution --

    def _run_benchmark(self, run_id: str, run: dict, pdf_path: str) -> None:
        from benchmark.runner import build_settings, run_single
        from benchmark.tracker import ApiUsageTracker

        config = run["config"]
        pdf_label = run["pdf_label"]

        # Build settings from config, using worker's API key from env
        model_cfg = {
            "name": config.get("model", self._settings.vlm_model),
            "base_url": config.get("base_url", self._settings.openrouter_base_url),
            "api_key_env": config.get("api_key_env", "OPENROUTER_API_KEY"),
            "timeout": config.get("timeout", self._settings.document_timeout_seconds),
            "max_tokens": config.get("max_tokens", self._settings.max_tokens),
            "concurrency": config.get("concurrency", self._settings.concurrency),
        }

        settings = build_settings(model_cfg)
        pricing = config.get("pricing", {})
        tracker = ApiUsageTracker(pricing=pricing)

        last_progress_write = 0.0
        progress_state = {
            "api_calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "running_cost": 0.0,
            "in_flight": 0,
            "completed_requests": 0,
            "failed_requests": 0,
        }

        def on_api_call(call_info):
            progress_state["api_calls"] = len(tracker.calls)
            progress_state["input_tokens"] = tracker.total_input_tokens
            progress_state["output_tokens"] = tracker.total_output_tokens
            progress_state["running_cost"] = round(tracker.total_cost, 6)
            self._throttled_progress(run_id, progress_state, last_progress_write)

        def on_status(in_flight, completed, failed):
            nonlocal last_progress_write
            progress_state["in_flight"] = in_flight
            progress_state["completed_requests"] = completed
            progress_state["failed_requests"] = failed
            now = time.monotonic()
            if now - last_progress_write >= PROGRESS_THROTTLE:
                self._update_progress(run_id, progress_state)
                last_progress_write = now

        def on_retry(attempt, max_retries, msg, wait):
            self._append_log(
                run_id,
                f"Retry {attempt}/{max_retries}: {msg} (waiting {wait:.1f}s)",
            )

        def on_error(status_code, msg, will_retry):
            self._append_log(
                run_id,
                f"HTTP {status_code}: {msg} {'(will retry)' if will_retry else '(final)'}",
            )

        tracker = ApiUsageTracker(pricing=pricing, on_api_call=on_api_call)

        self._append_log(run_id, f"Starting benchmark: {pdf_label} with {settings.vlm_model}")

        result = run_single(
            pdf_path,
            pdf_label,
            settings,
            tracker,
            on_retry=on_retry,
            on_error=on_error,
            on_status=on_status,
        )

        # Final progress update
        progress_state["api_calls"] = len(tracker.calls)
        progress_state["input_tokens"] = tracker.total_input_tokens
        progress_state["output_tokens"] = tracker.total_output_tokens
        progress_state["running_cost"] = round(tracker.total_cost, 6)
        self._update_progress(run_id, progress_state)

        # Write results
        self._complete(run_id, result)
        self._append_log(
            run_id,
            f"Completed: {result['page_count']} pages, {result['api_calls']} API calls, "
            f"${result['estimated_cost_usd']:.4f} in {result['wall_time_seconds']:.1f}s",
        )

        # Also insert into benchmark_runs for the observability dashboard
        self._insert_benchmark_run(result)

        logger.info("Test run %s: benchmark completed", run_id)

    # -- Integration test execution --

    def _run_integration(self, run_id: str, run: dict, pdf_path: str) -> None:
        from integration.pipeline import PipelineRunner

        config = run["config"]

        # Build settings by overriding the worker's base settings
        settings = Settings(
            database_url=self._settings.database_url,
            supabase_url=self._settings.supabase_url,
            supabase_service_key=self._settings.supabase_service_key,
            openrouter_api_key=self._settings.openrouter_api_key,
            vlm_model=config.get("vlm_model", self._settings.vlm_model),
            openrouter_base_url=self._settings.openrouter_base_url,
            concurrency=config.get("concurrency", self._settings.concurrency),
            document_timeout_seconds=self._settings.document_timeout_seconds,
            max_tokens=self._settings.max_tokens,
            embedding_api_url=self._settings.embedding_api_url,
            embedding_api_key=self._settings.embedding_api_key,
            embedding_model=config.get("embedding_model", self._settings.embedding_model),
            embedding_dimensions=self._settings.embedding_dimensions,
            embedding_batch_size=self._settings.embedding_batch_size,
            s3_endpoint_url=self._settings.s3_endpoint_url,
            s3_region=self._settings.s3_region,
            s3_access_key=self._settings.s3_access_key,
            s3_secret_key=self._settings.s3_secret_key,
            s3_output_bucket=self._settings.s3_output_bucket,
        )

        stages_progress = run.get("progress", {}).get("stages", [])

        def on_log(text):
            # Strip Rich markup for plain-text storage
            import re
            clean = re.sub(r"\[/?[^\]]*\]", "", text).strip()
            if clean:
                self._append_log(run_id, clean)

        def on_stage_start(name):
            for s in stages_progress:
                if s["name"] == name:
                    s["status"] = "running"
                    break
            self._update_progress(run_id, {"stages": stages_progress})

        def on_stage_done(result):
            for s in stages_progress:
                if s["name"] == result.name:
                    s["status"] = "done" if result.ok else "failed"
                    s["elapsed"] = round(result.elapsed, 2)
                    s["detail"] = result.detail[:100] if result.detail else ""
                    break
            self._update_progress(run_id, {"stages": stages_progress})

        def on_vlm_status(in_flight, completed, failed):
            progress = {"stages": stages_progress, "vlm_status": {
                "in_flight": in_flight, "completed": completed, "failed": failed,
            }}
            self._update_progress(run_id, progress)

        def on_vlm_retry(attempt, max_retries, msg, wait):
            self._append_log(
                run_id, f"VLM retry {attempt}/{max_retries}: {msg} (waiting {wait:.1f}s)"
            )

        self._append_log(run_id, f"Starting integration test with {settings.vlm_model}")

        runner = PipelineRunner(
            settings=settings,
            pdf_path=pdf_path,
            on_log=on_log,
            on_stage_start=on_stage_start,
            on_stage_done=on_stage_done,
            on_vlm_status=on_vlm_status,
            on_vlm_retry=on_vlm_retry,
        )

        stage_results = runner.run()

        passed = sum(1 for r in stage_results if r.ok)
        total = len(stage_results)
        total_time = round(sum(r.elapsed for r in stage_results), 2)

        results = {
            "stages": [
                {
                    "name": r.name,
                    "ok": r.ok,
                    "elapsed": round(r.elapsed, 2),
                    "detail": r.detail,
                    "data": r.data,
                }
                for r in stage_results
            ],
            "summary": {"passed": passed, "total": total, "total_time": total_time},
        }

        self._complete(run_id, results)
        self._append_log(run_id, f"Completed: {passed}/{total} stages passed in {total_time}s")

        logger.info("Test run %s: integration completed (%d/%d)", run_id, passed, total)

    # -- PDF resolution --

    def _resolve_pdf(self, run: dict) -> str:
        pdf_source = run["pdf_source"]
        pdf_path = run.get("pdf_path") or ""

        if pdf_source == "builtin":
            local = BUILTIN_PDFS.get(pdf_path)
            if not local or not os.path.isfile(local):
                raise FileNotFoundError(f"Built-in PDF not found: {pdf_path}")
            return local

        if pdf_source == "storage":
            from worker.s3 import S3Client

            s3 = S3Client(
                endpoint_url=self._settings.s3_endpoint_url or None,
                region=self._settings.s3_region,
                access_key=self._settings.s3_access_key,
                secret_key=self._settings.s3_secret_key,
                download_dir=self._settings.download_dir,
            )
            return s3.download_file("documents", pdf_path, run["id"])

        raise ValueError(f"Unknown pdf_source: {pdf_source}")

    # -- DB helpers --

    def _fetch_run(self, run_id: str) -> dict | None:
        with self._pool.connection() as conn:
            return conn.execute(
                "SELECT * FROM test_runs WHERE id = %s", [run_id]
            ).fetchone()

    def _update_status(self, run_id: str, status: str) -> None:
        with self._pool.connection() as conn:
            if status == "running":
                conn.execute(
                    "UPDATE test_runs SET status = %s, started_at = now() WHERE id = %s",
                    [status, run_id],
                )
            else:
                conn.execute(
                    "UPDATE test_runs SET status = %s WHERE id = %s",
                    [status, run_id],
                )

    def _update_progress(self, run_id: str, progress: dict) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE test_runs SET progress = %s::jsonb WHERE id = %s",
                [json.dumps(progress), run_id],
            )

    def _append_log(self, run_id: str, message: str) -> None:
        timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
        line = f"[{timestamp}] {message}"
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE test_runs SET logs = array_append(logs, %s) WHERE id = %s",
                [line, run_id],
            )

    def _complete(self, run_id: str, results: dict) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE test_runs SET status = 'completed', results = %s::jsonb, "
                "completed_at = now() WHERE id = %s",
                [json.dumps(results), run_id],
            )

    def _fail(self, run_id: str, error_message: str) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE test_runs SET status = 'failed', error_message = %s, "
                "completed_at = now() WHERE id = %s",
                [error_message, run_id],
            )

    def _insert_benchmark_run(self, result: dict) -> None:
        """Also write to benchmark_runs for the observability dashboard."""
        try:
            with self._pool.connection() as conn:
                conn.execute(
                    """
                    INSERT INTO benchmark_runs
                        (pdf_label, model, wall_time_seconds, page_count, api_calls,
                         input_tokens, output_tokens, total_tokens, estimated_cost_usd,
                         markdown_length, error)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    [
                        result.get("pdf", ""),
                        result.get("model", ""),
                        result.get("wall_time_seconds", 0),
                        result.get("page_count", 0),
                        result.get("api_calls", 0),
                        result.get("input_tokens", 0),
                        result.get("output_tokens", 0),
                        result.get("total_tokens", 0),
                        result.get("estimated_cost_usd", 0),
                        result.get("markdown_length", 0),
                        result.get("error"),
                    ],
                )
        except Exception:
            logger.warning("Failed to insert benchmark_runs record", exc_info=True)

    def _throttled_progress(self, run_id: str, progress: dict, last_write: float) -> None:
        now = time.monotonic()
        if now - last_write >= PROGRESS_THROTTLE:
            self._update_progress(run_id, progress)
