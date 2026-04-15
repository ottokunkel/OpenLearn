---
date: 2026-04-14
author: otto
git_commit: ff30ca3
branch: main
repository: OpenLearn
topic: "doc-worker-v2 modular architecture"
tags: [plan, doc-worker, architecture, docling, modal, supabase, pgmq]
status: in-progress
---

# doc-worker-v2 Modular Architecture Implementation Plan

## Overview

Build `services/doc-worker-v2` as a clean-slate replacement for `doc-worker-v1`, running in parallel with the v1 service until proven. v2 introduces three backend interfaces (`Queue`, `DocumentStore`, `BlobStore`) and a pluggable `Pipeline` abstraction over Docling. The default configuration reproduces v1's behavior (Supabase + Granite-docling-on-Modal). The Supabase backend is the day-1, fully-featured implementation; a generic-Postgres reference backend ships alongside to prove portability but is not a complete runnable path on day 1.

## Current State Analysis

- `services/doc-worker-v1/` is Supabase-coupled:
  - `supabase-py` for everything (storage download/upload, `documents` CRUD, pgmq RPCs).
  - Docling `VlmPipeline` hard-wired to the `granite_docling` preset.
  - Modal deployment lives inside the worker tree at `services/doc-worker-v1/src/vlm_endpoint/modal_app.py`.
- Existing producer is `supabase/functions/upload-document/index.ts` (edge function): authenticates user → uploads PDF → inserts `public.documents` row → calls `enqueue_document_job` RPC.
- Migrations `001`–`005` define `public.documents` (status check excludes `retrying` — v1 writes it anyway), `public.document_jobs` pgmq queue, and `public.pgmq_read/delete/archive` SECURITY DEFINER wrappers scoped to that queue.
- Docling surface (from docs research):
  - `DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_cls=..., pipeline_options=...)})` is the single entry regardless of pipeline arm.
  - `VlmConvertOptions.from_preset(name, engine_options=...)` — presets split into DocTags-emitting (`granite_docling`, `smoldocling`) and markdown-emitting (`qwen`, `phi4`, `pixtral`, …). The preset picks the prompt and `ResponseFormat`.
  - Standard (non-VLM) pipeline: `StandardPdfPipeline` (or `ThreadedStandardPdfPipeline`) + `PdfPipelineOptions`. GPU/CPU chosen via `AcceleratorOptions(device=...)`.
  - `ConversionResult.document` is always a `DoclingDocument`; `export_to_markdown()`, `export_to_dict()`, `export_to_doctags()` all work on it. v1's `doctags` variable is misnamed — it holds `export_to_dict()` (the full DoclingDocument serialization), not DocTags.
- Supabase REST (PostgREST) only serves schemas enumerated in `api.schemas` in `supabase/config.toml` — the v2 schema must be added there.

## Desired End State

### Tree
```
services/
  doc-worker-v1/                      # untouched, keeps running
  doc-worker-v2/                      # new worker + interfaces + pipelines
    pyproject.toml
    Dockerfile
    .env.example
    src/doc_worker_v2/
      __init__.py  __main__.py
      config.py  protocols.py  artifacts.py  di.py
      worker.py  processor.py
      pipelines/
        __init__.py
        granite_docling_vlm.py
        generic_markdown_vlm.py
        standard_cpu.py
  storage-supabase/                    # Supabase impls (day-1 priority)
    pyproject.toml
    src/storage_supabase/
      __init__.py  queue.py  documents.py  blobs.py
  storage-postgres/                    # generic-postgres reference impls
    pyproject.toml
    src/storage_postgres/
      __init__.py  queue.py  documents.py
  vlm-endpoints/modal/                 # Modal VLM deploy (own package)
    pyproject.toml
    src/vlm_modal/modal_app.py
supabase/
  migrations/006_doc_worker_v2.sql
  config.toml                          # api.schemas += doc_worker_v2
```

### Behavior
- Inserting a row into `doc_worker_v2.documents` fires an `AFTER INSERT` trigger that calls `pgmq.send('doc_ingest', ...)`.
- The worker polls `doc_ingest`, downloads the PDF from the configured `BlobStore`, runs the configured `Pipeline`, uploads each declared artifact, and updates the row's `status`, `page_count`, `pipeline`, and `artifacts` JSONB map. pgmq `read(vt, qty)` atomic assignment preserves v1's retry-via-VT semantics.
- Swapping backends: `QUEUE_BACKEND=supabase|postgres`, `DOCUMENT_STORE_BACKEND=…`, `BLOB_STORE_BACKEND=…`. Swapping pipelines: `PIPELINE=granite_docling_vlm|generic_markdown_vlm|standard_cpu`.

### Verification
End-to-end: `insert into doc_worker_v2.documents (...)` with a real PDF already in storage → row transitions `pending → processing → completed` within the worker's polling interval, `artifacts.markdown` and `artifacts.docling_json` paths are set, and their objects are fetchable and non-empty.

### Key Discoveries
- v1's `status` check constraint blocks `'retrying'` at the DB level but v1 writes it — v2's schema must include `retrying`.
- Docling's `Protocol`-style pipeline swap lives at the `pipeline_cls` + `pipeline_options` level of `PdfFormatOption`; no deeper surgery needed.
- Supabase-py `.schema("doc_worker_v2")` calls REST against PostgREST, which only serves schemas listed in `api.schemas`. Exposing `doc_worker_v2` in `supabase/config.toml` is a required step.
- pgmq `read(vt, qty)` is atomic — multi-replica is safe without extra locking. Retries work by simply not acking (letting VT expire).

## What We're NOT Doing

- Not migrating v1 data/rows/objects into the v2 schema (clean parallel; no backfill).
- Not deprecating v1 in this plan (it stays deployed on the existing Railway service).
- Not building `S3BlobStore` or `LocalFsBlobStore` on day 1. Generic-Postgres backend is therefore **not a runnable end-to-end worker by itself** — it ships `DocumentStore` + `Queue` only, and is used alongside a future blob backend.
- Not touching `apps/admin/`.
- Not adding non-Python pipelines (Marker, Unstructured, LlamaParse) — only Docling arms.
- Not changing the v1 queue, schema, or edge function.
- Not building a retry/dead-letter UI.
- Not exposing v2 via an edge function on day 1 — clients insert into `doc_worker_v2.documents` directly (supabase-js + RLS handles auth).

## Implementation Approach

Land the schema + trigger first (Phase 1) so the worker has something concrete to talk to. Scaffold worker package + interfaces + config (Phase 2). Build the Supabase backend (Phase 3) and the three pipeline arms (Phase 4) in parallel — both depend only on Phase 2. Stitch them with the processor + polling loop (Phase 5). Deploy the new Modal VLM service (Phase 6) — independent, can be done anytime before Phase 8. Containerize + Railway setup (Phase 7). End-to-end integration tests (Phase 8). Generic-Postgres reference impls last (Phase 9).

---

## Phase 1: Schema, trigger, pgmq wrappers ✅ applied (remote: `doc_worker_v2` + `doc_worker_v2_set_updated_at_search_path`)

### Overview
New migration `006_doc_worker_v2.sql` creates the `doc_worker_v2` schema with a redesigned `documents` table (jsonb `artifacts`, `status` including `retrying`), pgmq queue `doc_ingest`, an `AFTER INSERT` trigger that enqueues on row insert, and public-schema pgmq wrappers scoped to `doc_ingest` (matching v1's "wrappers in public" convention so the Supabase REST API serves them without extra `api.schemas` configuration for the RPCs themselves).

### Changes Required

#### 1. New migration
**File**: `supabase/migrations/006_doc_worker_v2.sql`

```sql
-- ============================================================
-- Schema
-- ============================================================
create schema if not exists doc_worker_v2;

grant usage on schema doc_worker_v2 to service_role, authenticated, anon;

-- ============================================================
-- documents (v2)
-- ============================================================
create table doc_worker_v2.documents (
    id              uuid primary key default gen_random_uuid(),
    user_id         uuid not null references auth.users(id) on delete cascade,
    filename        text not null,
    file_path       text not null,
    status          text not null default 'pending'
                        check (status in ('pending','processing','retrying','completed','failed')),
    error_message   text,
    page_count      integer,
    pipeline        text,                              -- set on completion; e.g. 'granite_docling_vlm'
    artifacts       jsonb not null default '{}'::jsonb, -- {"markdown":"path.md","docling_json":"path.json",...}
    metadata        jsonb not null default '{}'::jsonb,
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now()
);

create index idx_docs_v2_user    on doc_worker_v2.documents (user_id, created_at desc);
create index idx_docs_v2_active  on doc_worker_v2.documents (status)
    where status in ('pending','processing','retrying');

-- RLS
alter table doc_worker_v2.documents enable row level security;
create policy docs_v2_select on doc_worker_v2.documents for select using (auth.uid() = user_id);
create policy docs_v2_insert on doc_worker_v2.documents for insert with check (auth.uid() = user_id);
create policy docs_v2_update on doc_worker_v2.documents for update using (auth.uid() = user_id);
create policy docs_v2_delete on doc_worker_v2.documents for delete using (auth.uid() = user_id);

grant all on doc_worker_v2.documents to service_role;
grant select, insert, update, delete on doc_worker_v2.documents to authenticated;

-- ============================================================
-- updated_at touch
-- ============================================================
create or replace function doc_worker_v2.set_updated_at()
returns trigger language plpgsql as $$
begin
    new.updated_at = now();
    return new;
end $$;

create trigger docs_v2_touch
    before update on doc_worker_v2.documents
    for each row execute function doc_worker_v2.set_updated_at();

-- ============================================================
-- pgmq queue
-- ============================================================
select pgmq.create('doc_ingest');

-- ============================================================
-- AFTER INSERT → enqueue
-- ============================================================
create or replace function doc_worker_v2.enqueue_on_insert()
returns trigger
language plpgsql
security definer
set search_path = pgmq, doc_worker_v2, public
as $$
begin
    perform pgmq.send(
        'doc_ingest',
        jsonb_build_object(
            'document_id', new.id,
            'user_id',     new.user_id,
            'file_path',   new.file_path,
            'filename',    new.filename,
            'metadata',    new.metadata
        )
    );
    return new;
end $$;

create trigger docs_v2_enqueue
    after insert on doc_worker_v2.documents
    for each row execute function doc_worker_v2.enqueue_on_insert();

-- ============================================================
-- pgmq wrappers — scoped to doc_ingest, exposed via public
-- ============================================================
create or replace function public.pgmq_read_doc_ingest(vt integer, qty integer)
returns table (msg_id bigint, read_ct integer, enqueued_at timestamptz, vt timestamptz, message jsonb)
language sql security definer set search_path = pgmq, public
as $$
    select msg_id, read_ct, enqueued_at, vt, message
    from pgmq.read('doc_ingest', vt, qty);
$$;

create or replace function public.pgmq_delete_doc_ingest(msg_id bigint)
returns boolean language sql security definer set search_path = pgmq, public
as $$
    select pgmq.delete('doc_ingest', msg_id);
$$;

create or replace function public.pgmq_archive_doc_ingest(msg_id bigint)
returns boolean language sql security definer set search_path = pgmq, public
as $$
    select pgmq.archive('doc_ingest', msg_id);
$$;

revoke all on function public.pgmq_read_doc_ingest(integer, integer) from public, anon, authenticated;
revoke all on function public.pgmq_delete_doc_ingest(bigint)          from public, anon, authenticated;
revoke all on function public.pgmq_archive_doc_ingest(bigint)         from public, anon, authenticated;

grant execute on function public.pgmq_read_doc_ingest(integer, integer) to service_role;
grant execute on function public.pgmq_delete_doc_ingest(bigint)           to service_role;
grant execute on function public.pgmq_archive_doc_ingest(bigint)          to service_role;
```

#### 2. Expose `doc_worker_v2` schema to PostgREST
**File**: `supabase/config.toml`
**Changes**: add `doc_worker_v2` to `[api] schemas` so supabase-js / supabase-py `.schema("doc_worker_v2").from_("documents")` works for authenticated clients inserting rows.

```toml
[api]
schemas = ["public", "graphql_public", "doc_worker_v2"]
```

### Success Criteria

#### Automated Verification
- [x] Migration applied via Supabase MCP (project `rrzbsueabbiesnmxkzya`)
- [x] Inserting into `doc_worker_v2.documents` enqueues a message in `pgmq.q_doc_ingest` (verified in rolled-back tx)
- [x] `public.pgmq_read_doc_ingest(vt, qty)` as `service_role` returns the message with correct payload shape
- [x] `has_function_privilege('anon', ..., 'EXECUTE')` = false; `service_role` = true
- [x] Check constraint rejects `status='weird'`

#### Manual Verification
- [x] Authenticated user can `select` their own rows and not others' (RLS) — automated in `services/doc-worker-v2/tests/verify_phase1.py`
- [x] Archiving a message populates `pgmq.a_doc_ingest` — same script + MCP confirmation

#### Prerequisite (one-time, per environment)
- [x] `doc_worker_v2` added to **Project Settings → API → Exposed schemas** (PostgREST) on the remote Supabase project

**Implementation Note**: Pause after this phase for manual confirmation before Phase 2.

---

## Phase 2: Worker skeleton, config, protocol interfaces ✅ scaffolded

### Overview
Scaffold `services/doc-worker-v2/` with no backend impls yet. Define the four protocols (`Queue`, `DocumentStore`, `BlobStore`, `Pipeline`), the `Artifact`/`PipelineResult` dataclasses, pydantic-settings `Settings`, and a `di.py` factory that dispatches on env config. `Protocol` is structural — backend packages don't need to import these types, which keeps the dependency graph unidirectional (worker → backends).

### Changes Required

#### 1. Package scaffolding
**File**: `services/doc-worker-v2/pyproject.toml`
```toml
[project]
name = "doc-worker-v2"
version = "0.1.0"
requires-python = ">=3.13"
dependencies = [
    "docling>=2.60,<3",
    "pydantic>=2.9",
    "pydantic-settings>=2.5",
    "python-dotenv>=1.0",
    "storage-supabase",
]

[project.optional-dependencies]
postgres = ["storage-postgres"]

[project.scripts]
doc-worker-v2 = "doc_worker_v2.worker:run"

[tool.uv.sources]
storage-supabase = { workspace = true }
storage-postgres = { workspace = true }

[build-system]
requires = ["setuptools>=69"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
where = ["src"]
```

**File**: `services/uv-workspace.toml` (or root-level `pyproject.toml` workspace stanza) wiring the three packages into a single uv workspace so `uv sync` from any member resolves path deps.

```toml
# services/pyproject.toml (new root for the workspace)
[tool.uv.workspace]
members = ["doc-worker-v2", "storage-supabase", "storage-postgres", "vlm-endpoints/modal"]
```

#### 2. Artifacts + protocols + config
**File**: `services/doc-worker-v2/src/doc_worker_v2/artifacts.py`
```python
from dataclasses import dataclass

@dataclass(frozen=True)
class Artifact:
    name: str          # stable key: "markdown" | "docling_json" | "doctags" | ...
    content: bytes
    content_type: str  # MIME type
    extension: str     # trailing filename extension, e.g. "md", "json", "doctags.json"

@dataclass(frozen=True)
class PipelineResult:
    artifacts: list[Artifact]
    page_count: int
```

**File**: `services/doc-worker-v2/src/doc_worker_v2/protocols.py`
```python
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from .artifacts import PipelineResult


@dataclass(frozen=True)
class QueueMessage:
    msg_id: int
    read_ct: int
    payload: dict[str, Any]


@runtime_checkable
class Queue(Protocol):
    def read(self, visibility_timeout_s: int, qty: int) -> list[QueueMessage]: ...
    def delete(self, msg_id: int) -> None: ...
    def archive(self, msg_id: int) -> None: ...


@runtime_checkable
class DocumentStore(Protocol):
    def mark_processing(self, document_id: str) -> None: ...
    def mark_retrying(self, document_id: str, attempt: int, max_attempts: int, error: str) -> None: ...
    def mark_failed(self, document_id: str, error: str) -> None: ...
    def mark_completed(
        self, document_id: str, *,
        page_count: int,
        artifacts: dict[str, str],   # {"markdown": "<user>/<doc>/foo.md", ...}
        pipeline: str,
    ) -> None: ...


@runtime_checkable
class BlobStore(Protocol):
    def download(self, path: str) -> bytes: ...
    def upload(self, path: str, content: bytes, content_type: str, *, upsert: bool = True) -> None: ...


@runtime_checkable
class Pipeline(Protocol):
    name: str  # stable identifier written to documents.pipeline
    def convert(self, pdf_bytes: bytes) -> PipelineResult: ...
```

**File**: `services/doc-worker-v2/src/doc_worker_v2/config.py`
```python
from enum import Enum
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class BackendKind(str, Enum):
    SUPABASE = "supabase"
    POSTGRES = "postgres"


class PipelineKind(str, Enum):
    GRANITE_DOCLING_VLM = "granite_docling_vlm"
    GENERIC_MARKDOWN_VLM = "generic_markdown_vlm"
    STANDARD_CPU = "standard_cpu"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

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
        needs_supabase = (
            BackendKind.SUPABASE in {self.queue_backend, self.document_store_backend, self.blob_store_backend}
        )
        if needs_supabase and not (self.supabase_url and self.supabase_service_role_key):
            raise ValueError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required for the supabase backend")
        if BackendKind.POSTGRES in {self.queue_backend, self.document_store_backend} and not self.postgres_dsn:
            raise ValueError("POSTGRES_DSN is required for the postgres backend")
        if self.pipeline in {PipelineKind.GRANITE_DOCLING_VLM, PipelineKind.GENERIC_MARKDOWN_VLM} and not self.vlm_endpoint_url:
            raise ValueError("VLM_ENDPOINT_URL is required for VLM pipelines")
        return self
```

**File**: `services/doc-worker-v2/src/doc_worker_v2/di.py`
```python
from .config import Settings, BackendKind, PipelineKind
from .protocols import Queue, DocumentStore, BlobStore, Pipeline


def build_queue(s: Settings) -> Queue:
    match s.queue_backend:
        case BackendKind.SUPABASE:
            from storage_supabase.queue import SupabaseQueue
            return SupabaseQueue(s.supabase_url, s.supabase_service_role_key)
        case BackendKind.POSTGRES:
            from storage_postgres.queue import PostgresPgmqQueue
            return PostgresPgmqQueue(s.postgres_dsn)


def build_document_store(s: Settings) -> DocumentStore:
    match s.document_store_backend:
        case BackendKind.SUPABASE:
            from storage_supabase.documents import SupabaseDocumentStore
            return SupabaseDocumentStore(s.supabase_url, s.supabase_service_role_key)
        case BackendKind.POSTGRES:
            from storage_postgres.documents import PostgresDocumentStore
            return PostgresDocumentStore(s.postgres_dsn)


def build_blob_store(s: Settings) -> BlobStore:
    match s.blob_store_backend:
        case BackendKind.SUPABASE:
            from storage_supabase.blobs import SupabaseBlobStore
            return SupabaseBlobStore(
                s.supabase_url, s.supabase_service_role_key, s.supabase_storage_bucket
            )
        case BackendKind.POSTGRES:
            raise RuntimeError(
                "No blob-store impl paired with postgres backend yet — see plan phase 9"
            )


def build_pipeline(s: Settings) -> Pipeline:
    match s.pipeline:
        case PipelineKind.GRANITE_DOCLING_VLM:
            from .pipelines.granite_docling_vlm import GraniteDoclingVlmPipeline
            return GraniteDoclingVlmPipeline(
                endpoint_url=s.vlm_endpoint_url, model_name=s.vlm_model_name, timeout_s=s.vlm_timeout_s
            )
        case PipelineKind.GENERIC_MARKDOWN_VLM:
            from .pipelines.generic_markdown_vlm import GenericMarkdownVlmPipeline
            return GenericMarkdownVlmPipeline(
                endpoint_url=s.vlm_endpoint_url, model_name=s.vlm_model_name,
                preset=s.vlm_preset, timeout_s=s.vlm_timeout_s,
            )
        case PipelineKind.STANDARD_CPU:
            from .pipelines.standard_cpu import StandardCpuPipeline
            return StandardCpuPipeline()
```

**File**: `services/doc-worker-v2/.env.example` — enumerate every Settings key with a comment.

### Success Criteria

#### Automated Verification
- [x] `uv sync --directory services/doc-worker-v2` resolves
- [x] `uv run --directory services/doc-worker-v2 python -c "from doc_worker_v2.config import Settings; print(Settings(SUPABASE_URL='x', SUPABASE_SERVICE_ROLE_KEY='y', VLM_ENDPOINT_URL='z'))"` prints a populated `Settings`
- [x] `uv run ruff check services/doc-worker-v2` passes
- [x] `uv run mypy services/doc-worker-v2/src` passes (Protocol shape is sane)

#### Manual Verification
- [x] `.env.example` covers every `Settings` field with a one-line comment per key

**Implementation Note**: Pause after this phase.

---

## Phase 3: Supabase backend impls ✅ implemented + verified live

### Overview
`services/storage-supabase/` holds one package implementing all three interfaces against Supabase using `supabase-py`. It has **no dependency on `doc-worker-v2`** — `Protocol` is structural, so duck typing links them at DI time.

### Changes Required

#### 1. Package scaffolding
**File**: `services/storage-supabase/pyproject.toml`
```toml
[project]
name = "storage-supabase"
version = "0.1.0"
requires-python = ">=3.13"
dependencies = ["supabase>=2.11"]

[build-system]
requires = ["setuptools>=69"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
where = ["src"]
```

#### 2. `SupabaseQueue` — calls the public `pgmq_*_doc_ingest` RPCs
**File**: `services/storage-supabase/src/storage_supabase/queue.py`
```python
from dataclasses import dataclass
from typing import Any
from supabase import Client, create_client


@dataclass(frozen=True)
class _Message:
    msg_id: int
    read_ct: int
    payload: dict[str, Any]


class SupabaseQueue:
    def __init__(self, url: str, service_role_key: str):
        self._c: Client = create_client(url, service_role_key)

    def read(self, visibility_timeout_s: int, qty: int) -> list[_Message]:
        resp = self._c.rpc(
            "pgmq_read_doc_ingest",
            {"vt": visibility_timeout_s, "qty": qty},
        ).execute()
        return [
            _Message(msg_id=r["msg_id"], read_ct=r["read_ct"], payload=r["message"])
            for r in (resp.data or [])
        ]

    def delete(self, msg_id: int) -> None:
        self._c.rpc("pgmq_delete_doc_ingest", {"msg_id": msg_id}).execute()

    def archive(self, msg_id: int) -> None:
        self._c.rpc("pgmq_archive_doc_ingest", {"msg_id": msg_id}).execute()
```

#### 3. `SupabaseDocumentStore` — writes `doc_worker_v2.documents`
**File**: `services/storage-supabase/src/storage_supabase/documents.py`
```python
from typing import Any
from supabase import Client, create_client


class SupabaseDocumentStore:
    def __init__(self, url: str, service_role_key: str):
        self._c: Client = create_client(url, service_role_key)

    def _table(self):
        return self._c.schema("doc_worker_v2").table("documents")

    def _update(self, document_id: str, fields: dict[str, Any]) -> None:
        self._table().update(fields).eq("id", document_id).execute()

    def mark_processing(self, document_id: str) -> None:
        self._update(document_id, {"status": "processing", "error_message": None})

    def mark_retrying(self, document_id: str, attempt: int, max_attempts: int, error: str) -> None:
        self._update(document_id, {
            "status": "retrying",
            "error_message": f"attempt {attempt}/{max_attempts}: {error[:1000]}",
        })

    def mark_failed(self, document_id: str, error: str) -> None:
        self._update(document_id, {"status": "failed", "error_message": error[:1000]})

    def mark_completed(self, document_id: str, *, page_count: int, artifacts: dict[str, str], pipeline: str) -> None:
        self._update(document_id, {
            "status": "completed",
            "page_count": page_count,
            "artifacts": artifacts,
            "pipeline": pipeline,
            "error_message": None,
        })
```

#### 4. `SupabaseBlobStore`
**File**: `services/storage-supabase/src/storage_supabase/blobs.py`
```python
from supabase import Client, create_client


class SupabaseBlobStore:
    def __init__(self, url: str, service_role_key: str, bucket: str):
        self._c: Client = create_client(url, service_role_key)
        self._bucket = bucket

    def download(self, path: str) -> bytes:
        return self._c.storage.from_(self._bucket).download(path)

    def upload(self, path: str, content: bytes, content_type: str, *, upsert: bool = True) -> None:
        self._c.storage.from_(self._bucket).upload(
            path, content, {"content-type": content_type, "upsert": "true" if upsert else "false"},
        )
```

### Success Criteria

#### Automated Verification
- [x] `uv run ruff check services/storage-supabase` passes
- [x] Unit test: structural conformance via `isinstance(SupabaseQueue(...), Queue)` with `@runtime_checkable` protocols passes (using a mocked `supabase.Client`)
- [x] `uv run mypy services/storage-supabase/src` passes

#### Manual Verification
- [x] Against live Supabase: `SupabaseQueue.read` returns trigger-enqueued message — verified in `services/doc-worker-v2/tests/verify_phase3.py`
- [x] `SupabaseDocumentStore.mark_processing/retrying/completed/failed` flips status + error fields — verified in same script
- [x] `SupabaseBlobStore.upload` + `.download` round-trip with correct content-type — verified in same script

**Implementation Note**: Pause after this phase.

---

## Phase 4: Docling pipeline abstraction + three arms ✅ implemented + offline verified

### Overview
Three concrete pipelines, all matching the `Pipeline` protocol. `GraniteDoclingVlmPipeline` reproduces v1's behavior (DocTags-emitting preset, declares 3 artifacts). `GenericMarkdownVlmPipeline` uses a markdown preset against an arbitrary OpenAI-compatible endpoint (declares 2 artifacts). `StandardCpuPipeline` uses `StandardPdfPipeline` with no VLM (declares 2 artifacts).

### Changes Required

#### 1. Shared helpers
**File**: `services/doc-worker-v2/src/doc_worker_v2/pipelines/_common.py`
```python
import json
import tempfile
from pathlib import Path
from docling.document_converter import DocumentConverter
from ..artifacts import Artifact, PipelineResult


def run_converter(converter: DocumentConverter, pdf_bytes: bytes):
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=True) as tmp:
        tmp.write(pdf_bytes)
        tmp.flush()
        return converter.convert(Path(tmp.name))


def markdown_artifact(doc) -> Artifact:
    return Artifact(
        name="markdown",
        content=doc.export_to_markdown().encode("utf-8"),
        content_type="text/markdown",
        extension="md",
    )


def docling_json_artifact(doc) -> Artifact:
    return Artifact(
        name="docling_json",
        content=json.dumps(doc.export_to_dict()).encode("utf-8"),
        content_type="application/json",
        extension="docling.json",
    )


def doctags_artifact(doc) -> Artifact:
    return Artifact(
        name="doctags",
        content=doc.export_to_doctags().encode("utf-8"),
        content_type="application/xml",
        extension="doctags.xml",
    )


def page_count(doc) -> int:
    return len(doc.pages) if hasattr(doc, "pages") else 0
```

#### 2. Granite-docling VLM (default)
**File**: `services/doc-worker-v2/src/doc_worker_v2/pipelines/granite_docling_vlm.py`
```python
from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import VlmConvertOptions, VlmPipelineOptions
from docling.datamodel.vlm_engine_options import ApiVlmEngineOptions, VlmEngineType
from docling.document_converter import DocumentConverter, PdfFormatOption
from docling.pipeline.vlm_pipeline import VlmPipeline
from ..artifacts import PipelineResult
from ._common import run_converter, markdown_artifact, docling_json_artifact, doctags_artifact, page_count


class GraniteDoclingVlmPipeline:
    name = "granite_docling_vlm"

    def __init__(self, endpoint_url: str, model_name: str, timeout_s: int):
        vlm_options = VlmConvertOptions.from_preset(
            "granite_docling",
            engine_options=ApiVlmEngineOptions(
                runtime_type=VlmEngineType.API,
                url=endpoint_url,
                params={
                    "model": model_name,
                    "temperature": 0.0,
                    "max_tokens": 4096,
                    "skip_special_tokens": False,
                },
                timeout=timeout_s,
            ),
        )
        self._converter = DocumentConverter(
            format_options={InputFormat.PDF: PdfFormatOption(
                pipeline_cls=VlmPipeline,
                pipeline_options=VlmPipelineOptions(vlm_options=vlm_options, enable_remote_services=True),
            )}
        )

    def convert(self, pdf_bytes: bytes) -> PipelineResult:
        doc = run_converter(self._converter, pdf_bytes).document
        return PipelineResult(
            artifacts=[markdown_artifact(doc), docling_json_artifact(doc), doctags_artifact(doc)],
            page_count=page_count(doc),
        )
```

#### 3. Generic markdown VLM
**File**: `services/doc-worker-v2/src/doc_worker_v2/pipelines/generic_markdown_vlm.py`

Same shape as granite, but `VlmConvertOptions.from_preset(preset, engine_options=...)` where `preset` is a markdown-emitting preset (user-specified via `VLM_PRESET`, default to `"qwen"`). Emits only `markdown` + `docling_json` artifacts — no DocTags.

#### 4. Standard CPU
**File**: `services/doc-worker-v2/src/doc_worker_v2/pipelines/standard_cpu.py`
```python
from docling.datamodel.accelerator_options import AcceleratorDevice, AcceleratorOptions
from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import PdfPipelineOptions
from docling.document_converter import DocumentConverter, PdfFormatOption
from docling.pipeline.standard_pdf_pipeline import StandardPdfPipeline
from ..artifacts import PipelineResult
from ._common import run_converter, markdown_artifact, docling_json_artifact, page_count


class StandardCpuPipeline:
    name = "standard_cpu"

    def __init__(self):
        opts = PdfPipelineOptions()
        opts.accelerator_options = AcceleratorOptions(device=AcceleratorDevice.CPU)
        opts.do_ocr = True
        opts.do_table_structure = True
        self._converter = DocumentConverter(
            format_options={InputFormat.PDF: PdfFormatOption(
                pipeline_cls=StandardPdfPipeline,
                pipeline_options=opts,
            )}
        )

    def convert(self, pdf_bytes: bytes) -> PipelineResult:
        doc = run_converter(self._converter, pdf_bytes).document
        return PipelineResult(
            artifacts=[markdown_artifact(doc), docling_json_artifact(doc)],
            page_count=page_count(doc),
        )
```

### Success Criteria

#### Automated Verification
- [x] `uv run ruff check services/doc-worker-v2` passes
- [x] Each pipeline class satisfies the `Pipeline` protocol (mypy + attribute check confirmed `name`/`convert` on all three)
- [x] `StandardCpuPipeline().convert(gaussians_pdf)` offline: `page_count=10`, markdown 13.9KB, docling_json 97KB, text begins "## The Multivariate Gaussian Distribution…"

#### Manual Verification
- [x] `GraniteDoclingVlmPipeline.convert(gaussians_pdf)` vs deployed Modal endpoint: 3 artifacts, page_count=10, markdown 22KB containing "gaussian"/"covariance" — verified by `services/doc-worker-v2/tests/verify_phase4.py`
- [x] `GenericMarkdownVlmPipeline` code path verified against same endpoint: HTTP roundtrip + docling wiring work, 2 artifacts, page_count=10. Markdown empty because the endpoint serves `granite-docling` (a DocTags-only model) while the generic pipeline targets markdown-emitting VLMs — expected behavior; for a proper plausible-markdown run, point at a Qwen2-VL/Phi-4/Pixtral endpoint and set `VLM_PRESET` accordingly. (manual eyeball)

**Implementation Note**: Pause after this phase.

---

## Phase 5: Processor + polling loop ✅ implemented + unit-tested + end-to-end verified

### Overview
Compose the four protocols into the core loop. Retry semantics mirror v1: transient failures do not ack the message — the VT expires and pgmq re-presents; max retries → archive + mark `failed`. Malformed payloads are archived immediately. SIGINT/SIGTERM flips a `stop` flag that drains after the in-flight message.

### Changes Required

#### 1. Processor
**File**: `services/doc-worker-v2/src/doc_worker_v2/processor.py`
```python
import logging
from .protocols import Queue, QueueMessage, DocumentStore, BlobStore, Pipeline

log = logging.getLogger(__name__)


class JobPayloadError(ValueError): ...


def _validate(payload: dict) -> tuple[str, str, str, str]:
    try:
        return payload["document_id"], payload["user_id"], payload["file_path"], payload["filename"]
    except KeyError as e:
        raise JobPayloadError(f"missing key: {e}") from e


def process(
    msg: QueueMessage,
    queue: Queue,
    docs: DocumentStore,
    blobs: BlobStore,
    pipeline: Pipeline,
    *,
    max_retries: int,
) -> None:
    """Process one message end-to-end. Never raises."""
    try:
        document_id, user_id, file_path, filename = _validate(msg.payload)
    except JobPayloadError:
        log.exception("bad payload, archiving msg_id=%s", msg.msg_id)
        queue.archive(msg.msg_id)
        return

    attempt = max(1, msg.read_ct)
    log.info("processing doc=%s attempt=%d/%d pipeline=%s", document_id, attempt, max_retries, pipeline.name)

    try:
        docs.mark_processing(document_id)
        pdf_bytes = blobs.download(file_path)
        result = pipeline.convert(pdf_bytes)

        prefix = f"{user_id}/{document_id}"
        artifacts_map: dict[str, str] = {}
        for art in result.artifacts:
            path = f"{prefix}/{filename}.{art.extension}"
            blobs.upload(path, art.content, art.content_type, upsert=True)
            artifacts_map[art.name] = path

        docs.mark_completed(
            document_id,
            page_count=result.page_count,
            artifacts=artifacts_map,
            pipeline=pipeline.name,
        )
        queue.delete(msg.msg_id)
        log.info("completed doc=%s pages=%d artifacts=%s", document_id, result.page_count, list(artifacts_map))

    except Exception as e:
        err = str(e)[:1000]
        if attempt >= max_retries:
            log.exception("giving up doc=%s after %d attempts", document_id, attempt)
            try:
                docs.mark_failed(document_id, err)
            except Exception:
                log.exception("failed to mark failed doc=%s", document_id)
            queue.archive(msg.msg_id)
        else:
            log.exception("retrying doc=%s attempt=%d/%d", document_id, attempt, max_retries)
            try:
                docs.mark_retrying(document_id, attempt, max_retries, err)
            except Exception:
                log.exception("failed to mark retrying doc=%s", document_id)
            # do NOT delete/archive — pgmq re-presents when VT expires
```

#### 2. Worker loop
**File**: `services/doc-worker-v2/src/doc_worker_v2/worker.py`
```python
import logging
import signal
import time
from .config import Settings
from . import di, processor


def run() -> int:
    s = Settings()  # env-driven; raises with clear error if incomplete
    logging.basicConfig(level=s.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    log = logging.getLogger("doc_worker_v2")

    queue = di.build_queue(s)
    docs = di.build_document_store(s)
    blobs = di.build_blob_store(s)
    pipeline = di.build_pipeline(s)

    stop = False

    def _shutdown(signum, _frame):
        nonlocal stop
        log.info("signal=%s, draining", signum)
        stop = True

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    log.info("doc-worker-v2 online; pipeline=%s backends=q:%s/d:%s/b:%s",
             pipeline.name, s.queue_backend, s.document_store_backend, s.blob_store_backend)

    while not stop:
        try:
            msgs = queue.read(s.visibility_timeout_s, s.batch_size)
        except Exception:
            log.exception("queue read failed; backing off")
            time.sleep(s.poll_interval_s)
            continue
        if not msgs:
            time.sleep(s.poll_interval_s)
            continue
        for msg in msgs:
            if stop:
                break
            processor.process(msg, queue, docs, blobs, pipeline, max_retries=s.max_retries)

    log.info("exited cleanly")
    return 0
```

**File**: `services/doc-worker-v2/src/doc_worker_v2/__main__.py`
```python
import sys
from .worker import run
if __name__ == "__main__":
    sys.exit(run())
```

### Success Criteria

#### Automated Verification
- [x] `uv run doc-worker-v2` with empty env fails with clear message: `"SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required for the supabase backend"`
- [x] Unit test: `processor.process` with **fakes** for all four protocols:
  - happy path → `DocumentStore.mark_completed` called, `Queue.delete` called, every artifact uploaded
  - pipeline raises, read_ct=1, max_retries=3 → `mark_retrying` called, queue neither deleted nor archived
  - pipeline raises, read_ct=3, max_retries=3 → `mark_failed` + `Queue.archive` called
  - missing payload key → `Queue.archive` called, no `DocumentStore` call (parameterized: 4 missing-key variants)
  - `7 passed` via `pytest tests/test_processor.py`

#### Manual Verification
- [x] End-to-end: insert → trigger enqueues → `processor.process` drains → row=completed, page_count=10, 3 artifacts (markdown 22KB, docling_json 2.1MB, doctags), markdown contains "gaussian"/"covariance", queue.delete happened — verified by `services/doc-worker-v2/tests/verify_phase5.py::run_end_to_end` (52s on cold start)
- [x] SIGTERM drain: subprocess logged `online`, received SIGTERM, logged `signal=15, draining` and `exited cleanly`, exit_code=0 — verified by `::run_sigterm_drain`

**Implementation Note**: Pause after this phase.

---

## Phase 6: Modal VLM service (moved out) ✅ deployed + verified live

### Overview
Extract the Modal app from inside the worker tree to a standalone service package at `services/vlm-endpoints/modal/`. New Modal app name `vlm-granite-docling` (provider/model-named, not worker-versioned) so it's reusable. The v1 Modal app (`vlm-endpoint-docworker-v1`) keeps running untouched — v1 continues pointing at it.

### Changes Required

#### 1. New Modal package
**File**: `services/vlm-endpoints/modal/pyproject.toml`
```toml
[project]
name = "vlm-modal"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = ["modal>=1.4", "aiohttp>=3.13"]
[build-system]
requires = ["setuptools>=69"]
build-backend = "setuptools.build_meta"
[tool.setuptools.packages.find]
where = ["src"]
```

**File**: `services/vlm-endpoints/modal/src/vlm_modal/modal_app.py`
- Lift `services/doc-worker-v1/src/vlm_endpoint/modal_app.py` verbatim.
- Change `APP_NAME = "vlm-endpoint-docworker-v1"` → `APP_NAME = "vlm-granite-docling"`.
- Same model pin, same GPU config, same snapshot setup.

**File**: `services/vlm-endpoints/modal/README.md`
- One-shot deploy instructions: `uv run modal deploy src/vlm_modal/modal_app.py`.
- `modal app show vlm-granite-docling` to get the URL for `VLM_ENDPOINT_URL`.

### Success Criteria

#### Automated Verification
- [x] `modal deploy src/vlm_modal/modal_app.py` succeeded; URL: `https://ottokunkel034--vlm-granite-docling-vllmserver-serve.modal.run`
- [x] `modal run src/vlm_modal/modal_app.py::test` streamed two valid chat completions (cold 47.3s, warm 2.89s) returning DocTags

#### Manual Verification
- [x] `GraniteDoclingVlmPipeline.convert(gaussians_pdf)` against the new endpoint: 3 artifacts, page_count=10, markdown 22KB containing "gaussian"/"covariance" — verified by `verify_phase4.py` with `VLM_ENDPOINT_URL` overridden to the new app
- [x] v1's Modal app `vlm-endpoint-docworker-v1` is untouched (only `APP_NAME` differs in the new file; v1's deploy was not redeployed)

**Implementation Note**: Pause after this phase.

---

## Phase 7: Dockerfile + Railway ✅ local build + Railway production deploy verified

### Overview
Containerize the worker. Largely mirrors v1's two-stage uv-based Dockerfile. New Railway service `document-worker-v2` with its own env vars.

### Changes Required

#### 1. Dockerfile
**File**: `services/doc-worker-v2/Dockerfile`
- Two-stage build identical to v1's pattern (see `services/doc-worker-v1/Dockerfile:1`).
- Cache mount `id` must use a new Railway service ID (generated when the service is created).
- `CMD ["doc-worker-v2"]`.

#### 2. Railway config
**File**: `services/doc-worker-v2/railway.toml`
```toml
[build]
builder = "DOCKERFILE"
dockerfilePath = "Dockerfile"

[deploy]
restartPolicyType = "ON_FAILURE"
restartPolicyMaxRetries = 10
numReplicas = 1
sleepApplication = false
```

#### 3. Workspace build considerations
Because the worker depends on `storage-supabase` via a uv workspace path dep, the Dockerfile must copy **all three** service folders into the build stage (`doc-worker-v2/`, `storage-supabase/`, and the workspace root `pyproject.toml`) so uv resolves the workspace members. Update the Railway service's `rootDirectory` to `services/` (the workspace root) and point `dockerfilePath = "doc-worker-v2/Dockerfile"`.

### Success Criteria

#### Automated Verification
- [x] `docker build -f services/doc-worker-v2/Dockerfile services/` succeeds locally
- [x] Container starts and logs `doc-worker-v2 online; pipeline=granite_docling_vlm backends=q:supabase/d:supabase/b:supabase`; SIGTERM drains cleanly (`signal=15, draining`)

#### Manual Verification
- [x] Railway deploy `57056b5e-d649-4792-9a96-cdc5a50832d4` in `heroic-grace/production` reached status=SUCCESS with `rootDirectory=services/`, `dockerfilePath=doc-worker-v2/Dockerfile`
- [x] `railway logs` shows `doc-worker-v2 online; pipeline=granite_docling_vlm backends=q:supabase/d:supabase/b:supabase` at 22:40:35 UTC, followed by continuous `pgmq_read_doc_ingest "HTTP/2 200 OK"` heartbeat every ~5s

**Implementation Note**: Pause after this phase.

---

## Phase 8: Integration test ✅ implemented + verified live

### Overview
One end-to-end test that exercises the whole system against live Supabase + Modal, gated by `RUN_INTEGRATION_TESTS=1`. Seeds a PDF in storage, inserts a `doc_worker_v2.documents` row (trigger enqueues), lets the processor drain, asserts row state + artifact contents. Modeled on v1's `tests/test_integration_gaussians.py`.

### Changes Required

#### 1. Test package
**File**: `services/doc-worker-v2/tests/conftest.py`
- Session fixtures: `settings`, `queue`, `docs`, `blobs`, `pipeline` built via `di.*`.
- `gaussians_pdf` fetched from `https://cs229.stanford.edu/section/gaussians.pdf` and cached under `tests/fixtures/`.
- `seeded_document` fixture: uploads PDF to storage, inserts a row (trigger enqueues), yields ids, cleans up on teardown.

**File**: `services/doc-worker-v2/tests/test_integration_gaussians.py`
- Drains up to 10 `queue.read` batches to find its message.
- Calls `processor.process(msg, queue, docs, blobs, pipeline, max_retries=3)` directly.
- Asserts row `status == "completed"`, `page_count in [8, 16]`, `pipeline == "granite_docling_vlm"`.
- Asserts `artifacts["markdown"]` and `artifacts["docling_json"]` objects exist, markdown > 500 bytes and contains `gaussian|covariance|multivariate`.

### Success Criteria

#### Automated Verification
- [x] `RUN_INTEGRATION_TESTS=1 VLM_ENDPOINT_URL=... uv run --directory services/doc-worker-v2 --with pytest pytest tests/test_integration_gaussians.py -v` passes end-to-end (58.6s; `TEST_USER_ID` optional — fixture auto-provisions a throwaway auth user and deletes it on teardown)
- [x] Test cleans up all rows + storage objects it created (verified: `doc_worker_v2.documents` empty after run)

#### Manual Verification
- [x] Dashboard inspection: the test-created row appears and is cleaned up

**Implementation Note**: Pause after this phase.

---

## Phase 9: Generic Postgres backend (reference impl) ✅ implemented + verified live

### Overview
Prove the abstraction by building a second backend. `services/storage-postgres/` ships `PostgresDocumentStore` (psycopg3, raw SQL) and `PostgresPgmqQueue` (direct `pgmq.read/delete/archive` SQL). **No BlobStore** in this phase — the generic-postgres path is not independently runnable until a future `S3BlobStore` or `LocalFsBlobStore` lands.

### Changes Required

#### 1. Package scaffolding
**File**: `services/storage-postgres/pyproject.toml`
```toml
[project]
name = "storage-postgres"
version = "0.1.0"
requires-python = ">=3.13"
dependencies = ["psycopg[binary]>=3.2"]
[build-system]
requires = ["setuptools>=69"]
build-backend = "setuptools.build_meta"
[tool.setuptools.packages.find]
where = ["src"]
```

#### 2. Queue
**File**: `services/storage-postgres/src/storage_postgres/queue.py`
- Connection pool via `psycopg_pool.ConnectionPool`.
- `read(vt, qty)` → `SELECT msg_id, read_ct, message FROM pgmq.read('doc_ingest', %s, %s)`.
- `delete(msg_id)` → `SELECT pgmq.delete('doc_ingest', %s)`.
- `archive(msg_id)` → `SELECT pgmq.archive('doc_ingest', %s)`.

#### 3. Documents
**File**: `services/storage-postgres/src/storage_postgres/documents.py`
- Raw `UPDATE doc_worker_v2.documents SET ... WHERE id = %s` statements for each of the four `mark_*` methods.
- `artifacts` passed as `Jsonb(...)`.

### Success Criteria

#### Automated Verification
- [x] `uv run --with ruff ruff check services/storage-postgres` passes
- [x] Round-trip verified against Supabase Postgres (pgmq queue: send → read → archive; send → read → delete; documents: mark_processing → retrying → failed → completed) via `services/doc-worker-v2/tests/verify_phase9.py`
- [x] Swap test: `QUEUE_BACKEND=postgres DOCUMENT_STORE_BACKEND=postgres BLOB_STORE_BACKEND=supabase` worker logs `online; ... backends=q:postgres/d:postgres/b:supabase`, polls, and drains cleanly on SIGTERM (pool `close()` hook wired through `worker.run()` finally block)

#### Manual Verification
- [x] Worker running the mixed config (Postgres queue+docs, Supabase blobs) processes a Gaussians job end-to-end — verified via `services/doc-worker-v2/tests/verify_phase9_e2e.py`: worker spawns with `backends=q:postgres/d:postgres/b:supabase`, the seeded row transitions to `completed` with `page_count=10`, markdown (22794 bytes) + docling_json (2163151 bytes) + doctags artifacts uploaded to Supabase storage, SIGTERM drain exits cleanly, all rows + storage objects + throwaway user cleaned up

---

## Testing Strategy

### Unit Tests
- `processor.py`: four scenarios (happy, retry-remaining, retry-exhausted, bad-payload) with in-memory fakes for all four protocols.
- `pipelines/standard_cpu.py`: runs offline against the committed fixture PDF — the only pipeline that doesn't need a live VLM.
- Backend unit tests: each `storage-*` package mocks its underlying client and asserts the right RPC / SQL is emitted.

### Integration Tests
- One test in Phase 8 exercises the full Supabase + Modal stack, gated by `RUN_INTEGRATION_TESTS=1`.
- Optionally, a docker-compose-based integration test for `storage-postgres` (Postgres + pgmq extension) in Phase 9.

### Manual Testing Steps
1. Deploy migration `006` to a Supabase branch; verify schema + trigger.
2. Deploy new Modal app `vlm-granite-docling`; record URL.
3. Deploy worker to Railway `document-worker-v2`; watch logs for `online` message.
4. Insert a `doc_worker_v2.documents` row pointing at an existing PDF; watch the row transition `pending → processing → completed` and artifacts appear in storage.
5. Force a failure (bad `VLM_ENDPOINT_URL`), confirm `retrying` status updates across attempts then `failed` with archived message in `pgmq.a_doc_ingest`.
6. `docker stop` (SIGTERM) during processing → confirm clean drain of the in-flight message before exit.

## Performance Considerations

- `WORKER_VISIBILITY_TIMEOUT_S` must exceed P99 processing time (currently 300s for v1 — same default in v2).
- `batch_size=1` mirrors v1; raising it makes sense only if the pipeline releases the GIL during VLM HTTP waits (it does) and we want to overlap retries across tenants — defer.
- Pipeline construction cost (DocumentConverter) is paid once at startup; per-request cost is the VLM round-trip.

## Migration Notes

- No data migration — v1's `public.documents` + `document_jobs` queue stay as-is; v2 has its own `doc_worker_v2.documents` + `doc_ingest` queue.
- Clients will have to opt into v2 by inserting into the new table (e.g., a new upload path in the eventual admin UI). Out of scope for this plan.

## References

- Current v1 implementation: `services/doc-worker-v1/`
- Existing migrations: `supabase/migrations/001_create_tables.sql`, `005_pgmq_wrappers.sql`
- Research on Docling pipelines: summarized in the conversation that produced this plan
  - https://docling-project.github.io/docling/examples/gpu_standard_pipeline/
  - https://docling-project.github.io/docling/examples/vlm_pipeline_api_model/
  - https://docling-project.github.io/docling/reference/pipeline_options/
- v1 processor: `services/doc-worker-v1/src/doc_worker/processor.py:52` (the flow that v2's `processor.py` mirrors)
- v1 Modal app: `services/doc-worker-v1/src/vlm_endpoint/modal_app.py` (lifted verbatim in Phase 6 with APP_NAME change)
