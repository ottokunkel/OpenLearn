import os
import uuid
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Settings:
    supabase_url: str
    supabase_service_key: str
    source_bucket: str
    output_bucket: str
    openrouter_api_key: str
    vlm_model: str
    openrouter_base_url: str
    poll_interval_seconds: int = 5
    worker_id: str = field(default_factory=lambda: f"worker-{uuid.uuid4().hex[:8]}")
    download_dir: str = "/tmp/pdf_downloads"
    document_timeout_seconds: int = 120
    max_tokens: int = 8192
    concurrency: int = 50
    extra_api_params: dict = field(default_factory=dict)


def load_settings() -> Settings:
    return Settings(
        supabase_url=os.environ["SUPABASE_URL"],
        supabase_service_key=os.environ["SUPABASE_SERVICE_KEY"],
        source_bucket=os.environ.get("SOURCE_BUCKET", "pdfs"),
        output_bucket=os.environ.get("OUTPUT_BUCKET", "docling-documents"),
        openrouter_api_key=os.environ["OPENROUTER_API_KEY"],
        vlm_model=os.environ.get("VLM_MODEL", "qwen/qwen3.5-flash-02-23"),
        openrouter_base_url=os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
        poll_interval_seconds=int(os.environ.get("POLL_INTERVAL_SECONDS", "5")),
        download_dir=os.environ.get("DOWNLOAD_DIR", "/tmp/pdf_downloads"),
        document_timeout_seconds=int(os.environ.get("DOCUMENT_TIMEOUT_SECONDS", "120")),
        max_tokens=int(os.environ.get("VLM_MAX_TOKENS", "8192")),
        concurrency=int(os.environ.get("VLM_CONCURRENCY", "50")),
    )
