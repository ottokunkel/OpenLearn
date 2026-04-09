"""Core benchmark execution: runs DoclingConverter on test PDFs and collects metrics."""

import json
import os
import time
from pathlib import Path
from threading import Event
from typing import Callable, Optional

import yaml

from worker.config import Settings
from worker.converter import DoclingConverter
from benchmark.tracker import ApiUsageTracker


def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def resolve_api_key(model_cfg: dict) -> str:
    """Resolve API key from direct value or env var."""
    if "api_key" in model_cfg and model_cfg["api_key"]:
        return model_cfg["api_key"]
    env_var = model_cfg.get("api_key_env", "")
    if env_var:
        return os.environ.get(env_var, "")
    return ""


def build_settings(model_cfg: dict) -> Settings:
    """Build a Settings from a single model config entry. Redis/S3 fields are unused."""
    return Settings(
        openrouter_api_key=resolve_api_key(model_cfg),
        vlm_model=model_cfg.get("name", "qwen/qwen3.5-flash-02-23"),
        openrouter_base_url=model_cfg.get("base_url", "https://openrouter.ai/api/v1"),
        document_timeout_seconds=model_cfg.get("timeout", 120),
        max_tokens=model_cfg.get("max_tokens", 8192),
        concurrency=model_cfg.get("concurrency", 50),
        extra_api_params=model_cfg.get("extra_params", {}),
    )


def _sanitize_filename(name: str) -> str:
    return name.replace("/", "_").replace(" ", "_")


def run_single(
    pdf_path: str,
    label: str,
    settings: Settings,
    tracker: ApiUsageTracker,
    output_dir: str | None = None,
    on_retry: Optional[Callable[[int, int, str, float], None]] = None,
    on_error: Optional[Callable[[int, str, bool], None]] = None,
    on_status: Optional[Callable[[int, int, int], None]] = None,
    cancel_event: Optional[Event] = None,
) -> dict:
    """Run DoclingConverter on a single PDF and return metrics + save doc JSON."""
    tracker.reset()
    converter = DoclingConverter(settings)

    start = time.perf_counter()
    with tracker.track():
        conversion = converter.convert_pdf(
            pdf_path,
            on_retry=on_retry,
            on_error=on_error,
            on_status=on_status,
            cancel_event=cancel_event,
        )
    elapsed = time.perf_counter() - start

    result = conversion.to_dict()
    doc_dict = result["doc_dict"]
    markdown = result["markdown"]
    chunks = result.get("chunks", [])
    page_count = result["page_count"]

    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
        doc_path = os.path.join(
            output_dir,
            f"{_sanitize_filename(label)}_{_sanitize_filename(settings.vlm_model)}.json",
        )
        with open(doc_path, "w") as f:
            json.dump(
                {
                    "doc_dict": doc_dict,
                    "markdown": markdown,
                    "chunks": chunks,
                    "page_count": page_count,
                },
                f,
                indent=2,
                ensure_ascii=False,
            )

    return {
        "pdf": label,
        "model": settings.vlm_model,
        "wall_time_seconds": round(elapsed, 2),
        "page_count": page_count,
        "api_calls": len(tracker.calls),
        "input_tokens": tracker.total_input_tokens,
        "output_tokens": tracker.total_output_tokens,
        "total_tokens": tracker.total_input_tokens + tracker.total_output_tokens,
        "estimated_cost_usd": round(tracker.total_cost, 6),
        "markdown_length": len(markdown),
    }


def run_cli(config_path: str):
    """Run benchmark in CLI mode (no TUI)."""
    cfg = load_config(config_path)
    models = cfg.get("models", [])
    output_cfg = cfg.get("output", {})

    results = []

    for model_cfg in models:
        model_name = model_cfg.get("name", "unknown")
        api_key = resolve_api_key(model_cfg)

        if not api_key:
            key_source = model_cfg.get("api_key_env", "api_key")
            print(f"Skipping {model_name}: no API key ({key_source})")
            continue

        settings = build_settings(model_cfg)
        pricing = model_cfg.get("pricing", {})
        tracker = ApiUsageTracker(pricing=pricing)

        for pdf_entry in cfg.get("pdfs", []):
            pdf_path = pdf_entry["path"]
            label = pdf_entry.get("label", Path(pdf_path).stem)

            if not os.path.isfile(pdf_path):
                print(f"Error: PDF not found: {pdf_path}")
                continue

            print(f"Converting {label} with {model_name}...")
            try:
                result = run_single(
                    pdf_path,
                    label,
                    settings,
                    tracker,
                    output_dir=output_cfg.get("documents_dir"),
                )
                results.append(result)
                print(
                    f"  Done in {result['wall_time_seconds']}s "
                    f"— ${result['estimated_cost_usd']:.4f} "
                    f"({result['page_count']} pages, "
                    f"{result['api_calls']} API calls)"
                )
            except Exception as e:
                print(f"  FAILED: {e}")
                results.append({
                    "pdf": label,
                    "model": model_name,
                    "error": str(e),
                })

    json_path = output_cfg.get("results_json")
    if json_path and results:
        os.makedirs(os.path.dirname(json_path), exist_ok=True)
        with open(json_path, "w") as f:
            json.dump({"runs": results}, f, indent=2)
        print(f"\nResults saved to {json_path}")
