import os
import uuid
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Settings:
    # Redis
    redis_url: str = "redis://localhost:6379/0"
    input_queue: str = "docling:jobs"
    # S3-compatible storage
    s3_endpoint_url: str = ""
    s3_region: str = "us-east-1"
    s3_access_key: str = ""
    s3_secret_key: str = ""
    s3_output_bucket: str = "docling-output"
    # VLM
    openrouter_api_key: str = ""
    vlm_model: str = "qwen/qwen3.5-flash-02-23"
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    document_timeout_seconds: int = 120
    max_tokens: int = 8192
    concurrency: int = 50
    extra_api_params: dict = field(default_factory=dict)
    # Worker
    worker_id: str = field(default_factory=lambda: f"worker-{uuid.uuid4().hex[:8]}")
    download_dir: str = "/tmp/pdf_downloads"
    chunk_batch_size: int = 100
    # Embedding (optional — empty api_key disables)
    embedding_api_url: str = "https://api.openai.com/v1"
    embedding_api_key: str = ""
    embedding_model: str = "text-embedding-3-small"
    embedding_dimensions: int = 1536
    embedding_batch_size: int = 100


def load_settings() -> Settings:
    return Settings(
        redis_url=os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
        input_queue=os.environ.get("INPUT_QUEUE", "docling:jobs"),
        s3_endpoint_url=os.environ.get("S3_ENDPOINT_URL", ""),
        s3_region=os.environ.get("S3_REGION", "us-east-1"),
        s3_access_key=os.environ.get("S3_ACCESS_KEY", ""),
        s3_secret_key=os.environ.get("S3_SECRET_KEY", ""),
        s3_output_bucket=os.environ.get("S3_OUTPUT_BUCKET", "docling-output"),
        openrouter_api_key=os.environ.get("OPENROUTER_API_KEY", ""),
        vlm_model=os.environ.get("VLM_MODEL", "qwen/qwen3.5-flash-02-23"),
        openrouter_base_url=os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
        document_timeout_seconds=int(os.environ.get("DOCUMENT_TIMEOUT_SECONDS", "120")),
        max_tokens=int(os.environ.get("VLM_MAX_TOKENS", "8192")),
        concurrency=int(os.environ.get("VLM_CONCURRENCY", "50")),
        download_dir=os.environ.get("DOWNLOAD_DIR", "/tmp/pdf_downloads"),
        chunk_batch_size=int(os.environ.get("CHUNK_BATCH_SIZE", "100")),
        embedding_api_url=os.environ.get("EMBEDDING_API_URL", "https://api.openai.com/v1"),
        embedding_api_key=os.environ.get("EMBEDDING_API_KEY", ""),
        embedding_model=os.environ.get("EMBEDDING_MODEL", "text-embedding-3-small"),
        embedding_dimensions=int(os.environ.get("EMBEDDING_DIMENSIONS", "1536")),
        embedding_batch_size=int(os.environ.get("EMBEDDING_BATCH_SIZE", "100")),
    )
