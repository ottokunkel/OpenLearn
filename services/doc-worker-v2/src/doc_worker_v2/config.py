from enum import Enum

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class BackendKind(str, Enum):
    SUPABASE = "supabase"
    POSTGRES = "postgres"


class PipelineKind(str, Enum):
    GRANITE_DOCLING_VLM = "granite_docling_vlm"
    GENERIC_MARKDOWN_VLM = "generic_markdown_vlm"
    STANDARD_CPU = "standard_cpu"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", extra="ignore", case_sensitive=False
    )

    # --- Backend selection ---
    queue_backend: BackendKind = BackendKind.SUPABASE
    document_store_backend: BackendKind = BackendKind.SUPABASE
    blob_store_backend: BackendKind = BackendKind.SUPABASE

    # --- Pipeline selection ---
    pipeline: PipelineKind = PipelineKind.GRANITE_DOCLING_VLM

    # --- Supabase (used by supabase backends) ---
    supabase_url: str | None = None
    supabase_service_role_key: str | None = None
    supabase_storage_bucket: str = "documents"

    # --- Generic Postgres (used by postgres backends) ---
    postgres_dsn: str | None = None

    # --- VLM (used by VLM pipelines) ---
    vlm_endpoint_url: str | None = None
    vlm_model_name: str = "ibm-granite/granite-docling-258M"
    vlm_preset: str = "granite_docling"
    vlm_timeout_s: int = 600

    # --- Worker tuning ---
    poll_interval_s: float = 5.0
    visibility_timeout_s: int = 300
    batch_size: int = 1
    max_retries: int = 3
    log_level: str = "INFO"

    @model_validator(mode="after")
    def _require_backend_config(self):
        needs_supabase = BackendKind.SUPABASE in {
            self.queue_backend,
            self.document_store_backend,
            self.blob_store_backend,
        }
        if needs_supabase and not (
            self.supabase_url and self.supabase_service_role_key
        ):
            raise ValueError(
                "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required for the supabase backend"
            )
        if (
            BackendKind.POSTGRES
            in {self.queue_backend, self.document_store_backend}
            and not self.postgres_dsn
        ):
            raise ValueError(
                "POSTGRES_DSN is required for the postgres backend"
            )
        if (
            self.pipeline
            in {PipelineKind.GRANITE_DOCLING_VLM, PipelineKind.GENERIC_MARKDOWN_VLM}
            and not self.vlm_endpoint_url
        ):
            raise ValueError(
                "VLM_ENDPOINT_URL is required for VLM pipelines"
            )
        return self
