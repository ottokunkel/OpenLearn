from .config import BackendKind, PipelineKind, Settings
from .protocols import BlobStore, DocumentStore, Pipeline, Queue


def build_queue(s: Settings) -> Queue:
    match s.queue_backend:
        case BackendKind.SUPABASE:
            from storage_supabase.queue import SupabaseQueue

            return SupabaseQueue(s.supabase_url, s.supabase_service_role_key)
        case BackendKind.POSTGRES:
            from storage_postgres.queue import PostgresPgmqQueue

            assert s.postgres_dsn is not None  # guaranteed by Settings validator
            return PostgresPgmqQueue(s.postgres_dsn)


def build_document_store(s: Settings) -> DocumentStore:
    match s.document_store_backend:
        case BackendKind.SUPABASE:
            from storage_supabase.documents import SupabaseDocumentStore

            return SupabaseDocumentStore(
                s.supabase_url, s.supabase_service_role_key
            )
        case BackendKind.POSTGRES:
            from storage_postgres.documents import PostgresDocumentStore

            assert s.postgres_dsn is not None  # guaranteed by Settings validator
            return PostgresDocumentStore(s.postgres_dsn)


def build_blob_store(s: Settings) -> BlobStore:
    match s.blob_store_backend:
        case BackendKind.SUPABASE:
            from storage_supabase.blobs import SupabaseBlobStore

            return SupabaseBlobStore(
                s.supabase_url,
                s.supabase_service_role_key,
                s.supabase_storage_bucket,
            )
        case BackendKind.POSTGRES:
            raise RuntimeError(
                "No blob-store impl paired with postgres backend yet — see plan phase 9"
            )


def build_pipeline(s: Settings) -> Pipeline:
    match s.pipeline:
        case PipelineKind.GRANITE_DOCLING_VLM:
            from .pipelines.granite_docling_vlm import GraniteDoclingVlmPipeline

            assert s.vlm_endpoint_url is not None  # guaranteed by Settings validator
            return GraniteDoclingVlmPipeline(
                endpoint_url=s.vlm_endpoint_url,
                model_name=s.vlm_model_name,
                timeout_s=s.vlm_timeout_s,
            )
        case PipelineKind.GENERIC_MARKDOWN_VLM:
            from .pipelines.generic_markdown_vlm import GenericMarkdownVlmPipeline

            assert s.vlm_endpoint_url is not None  # guaranteed by Settings validator
            return GenericMarkdownVlmPipeline(
                endpoint_url=s.vlm_endpoint_url,
                model_name=s.vlm_model_name,
                preset=s.vlm_preset,
                timeout_s=s.vlm_timeout_s,
            )
        case PipelineKind.STANDARD_CPU:
            from .pipelines.standard_cpu import StandardCpuPipeline

            return StandardCpuPipeline()
