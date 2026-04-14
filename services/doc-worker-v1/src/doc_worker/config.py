"""Env-backed worker config. Fails fast on missing required vars."""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    supabase_url: str
    supabase_service_role_key: str
    vlm_endpoint_url: str
    vlm_model_name: str
    vlm_timeout_s: int
    poll_interval_s: float
    visibility_timeout_s: int
    batch_size: int
    max_retries: int
    log_level: str


def load() -> Config:
    load_dotenv()
    required = {
        "SUPABASE_URL": os.environ.get("SUPABASE_URL"),
        "SUPABASE_SERVICE_ROLE_KEY": os.environ.get("SUPABASE_SERVICE_ROLE_KEY"),
        "VLM_ENDPOINT_URL": os.environ.get("VLM_ENDPOINT_URL"),
    }
    missing = [k for k, v in required.items() if not v]
    if missing:
        raise RuntimeError(f"Missing required env vars: {', '.join(missing)}")

    return Config(
        supabase_url=required["SUPABASE_URL"],
        supabase_service_role_key=required["SUPABASE_SERVICE_ROLE_KEY"],
        vlm_endpoint_url=required["VLM_ENDPOINT_URL"],
        vlm_model_name=os.environ.get("VLM_MODEL_NAME", "ibm-granite/granite-docling-258M"),
        vlm_timeout_s=int(os.environ.get("VLM_TIMEOUT_S", "600")),
        poll_interval_s=float(os.environ.get("WORKER_POLL_INTERVAL_S", "5")),
        visibility_timeout_s=int(os.environ.get("WORKER_VISIBILITY_TIMEOUT_S", "300")),
        batch_size=int(os.environ.get("WORKER_BATCH_SIZE", "1")),
        max_retries=int(os.environ.get("WORKER_MAX_RETRIES", "3")),
        log_level=os.environ.get("LOG_LEVEL", "INFO"),
    )
