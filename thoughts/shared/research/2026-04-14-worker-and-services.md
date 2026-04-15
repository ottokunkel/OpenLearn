---
date: 2026-04-14T21:06:02-0400
researcher: otto
git_commit: ff30ca32b6040ab5714c2ca757015e0d961a4c5e
branch: main
repository: OpenLearn
topic: "Current worker implementation and services"
tags: [research, codebase, doc-worker-v1, doc-worker-v2, storage-supabase, storage-postgres, vlm-endpoints, modal, docling, pgmq, supabase]
status: complete
last_updated: 2026-04-14
last_updated_by: otto
---

# Research: Current Worker Implementation and Services

**Date**: 2026-04-14T21:06:02-04:00
**Researcher**: otto
**Git Commit**: ff30ca32b6040ab5714c2ca757015e0d961a4c5e
**Branch**: main
**Repository**: OpenLearn

## Research Question

Research the current worker implementation and the services — document the state of `services/` as it exists today: what runs where, how the pieces talk to each other, and what each service package contains.

## Summary

OpenLearn's `services/` tree currently houses **two parallel document-processing workers** plus three support packages:

- **`doc-worker-v1/`** — the production worker. Supabase-coupled (supabase-py for everything), Docling `VlmPipeline` hard-wired to the `granite_docling` preset, Modal VLM deploy inside the worker tree. Deployed on Railway service `document-worker` in `heroic-grace/production`. Driven by the `public.documents` / `public.document_jobs` pgmq stack and the `upload-document` edge function.
- **`doc-worker-v2/`** — a clean-slate rewrite building out a modular architecture (Phase 1–9 of `thoughts/shared/plans/2026-04-14-doc-worker-v2-modular-architecture.md` are all marked `✅`). Four `Protocol` interfaces (`Queue`, `DocumentStore`, `BlobStore`, `Pipeline`) + a DI factory + three pipeline arms (`granite_docling_vlm`, `generic_markdown_vlm`, `standard_cpu`). Deployed on Railway as `document-worker-v2`. Driven by the new `doc_worker_v2.documents` table whose `AFTER INSERT` trigger enqueues into `doc_ingest`.
- **`storage-supabase/`** — Supabase-backed `Queue`, `DocumentStore`, `BlobStore` impls (workspace member).
- **`storage-postgres/`** — Generic Postgres reference impls of `Queue` + `DocumentStore` only (no blob store). Workspace member.
- **`vlm-endpoints/modal/`** — Standalone Modal deploy for the shared `vlm-granite-docling` VLM app (lifted verbatim from v1 with a new `APP_NAME`). Not in the uv workspace.

Both workers target the same Supabase project. v1 is untouched and remains deployed; v2 runs alongside it on its own schema and queue. Migration `006_doc_worker_v2.sql` provides the v2 schema + trigger; `config.toml` exposes `doc_worker_v2` to PostgREST. There is one uncommitted change on `services/doc-worker-v1/src/vlm_endpoint/modal_app.py` (`MAX_INPUTS: 32 → 4`).

## Detailed Findings

### 1. `services/doc-worker-v1/` — Production Worker

**Purpose**: Poll `public.document_jobs` pgmq queue, convert PDFs via a Modal-hosted granite-docling VLM, upload markdown + DocTags JSON back to Supabase storage, update `public.documents` row.

#### Entry point & runtime flow
- CLI: `uv run doc-worker` (console script in `services/doc-worker-v1/pyproject.toml:19`, dispatches to `doc_worker.worker:run`).
- Also invokable via `python -m doc_worker` (`services/doc-worker-v1/src/doc_worker/__main__.py:1`).
- Container entry: `CMD ["doc-worker"]` (`services/doc-worker-v1/Dockerfile:45`).

#### Polling loop (`services/doc-worker-v1/src/doc_worker/worker.py`)
1. Load config via `config.load()` (line 14) — validates required env vars, defaults the rest.
2. Build Supabase client + Docling converter (lines 21–22).
3. Register `SIGINT` and `SIGTERM` handlers (lines 26–32) that set `stop=True` and log `"draining after current message"`.
4. Logs `"worker online; polling document_jobs every {poll_interval_s}s"` (line 34).
5. Main loop (line 35): `q.read(client, vt, qty)` → sleep on empty → process each msg sequentially → check stop flag between messages (line 48).
6. On exit: log `"worker exited cleanly"` and return 0 (lines 52–53).

#### Processor (`services/doc-worker-v1/src/doc_worker/processor.py`)
Single function `process(msg, client, converter, max_retries=3)` (lines 52–139). Flow:
1. `_validate_payload` extracts `document_id`, `user_id`, `file_path`, `filename` (lines 67–72). On `JobPayloadError`: `q.archive(client, msg.msg_id)` (line 71), return — malformed payloads dead-letter immediately.
2. `attempt = max(1, msg.read_ct)` (line 74).
3. Try block:
   - `_mark(client, document_id, status="processing", error_message=None)` (line 80).
   - `pdf_bytes = client.storage.from_(BUCKET).download(file_path)` where `BUCKET = "documents"` (line 29, 82).
   - `markdown, doctags, page_count = convert_pdf(converter, pdf_bytes)` (line 83).
   - Upload artifacts to `{user_id}/{document_id}/{filename}.md` and `.doctags.json` with `upsert: "true"` (lines 89–98).
   - `_mark(...status="completed", page_count, markdown_path, doc_json_path, error_message=None)` (lines 100–108).
   - `q.delete(client, msg.msg_id)` (line 109).
4. Except block (lines 112–139):
   - If `attempt >= max_retries`: `_mark(...status="failed")` + `q.archive(client, msg.msg_id)` (lines 114–123).
   - Else: `_mark(...status="retrying", error_message="attempt {n}/{max}: {err}")` and **do not delete/archive** — VT expires, pgmq re-presents (retry semantics docstring at lines 3–13).
5. Idempotency: storage uploads use `upsert: "true"` so reprocessing overwrites.

#### Queue wrappers (`services/doc-worker-v1/src/doc_worker/queue.py`)
- `read()` (lines 20–28) → `client.rpc("pgmq_read", {"queue_name": "document_jobs", "vt": ..., "qty": ...})`.
- `delete()` (lines 31–32) → `client.rpc("pgmq_delete", {"queue_name": "document_jobs", "msg_id": ...})`.
- `archive()` (lines 35–36) → `client.rpc("pgmq_archive", {...})`.
- `Message` dataclass (lines 10–15): `msg_id`, `read_ct`, `message` (payload dict).

#### Supabase client (`services/doc-worker-v1/src/doc_worker/supabase.py`)
One-liner: `create_client(cfg.supabase_url, cfg.supabase_service_role_key)` (lines 9–10). Service-role bypasses RLS.

#### Docling pipeline (`services/doc-worker-v1/src/doc_worker/docling.py`)
`build_converter(cfg)` (lines 17–45):
- `VlmConvertOptions.from_preset("granite_docling", engine_options=ApiVlmEngineOptions(runtime_type=API, url=cfg.vlm_endpoint_url, params={"model": cfg.vlm_model_name, "temperature": 0.0, "max_tokens": 4096, "skip_special_tokens": False}, timeout=cfg.vlm_timeout_s))`.
- `VlmPipelineOptions(vlm_options, enable_remote_services=True)`.
- `DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_cls=VlmPipeline, pipeline_options=...)})`.

`convert_pdf(converter, pdf_bytes)` (lines 48–65) — writes PDF to a tempfile, returns `(markdown, doctags_dict, page_count)`. Note: the variable named `doctags` in v1 is actually `doc.export_to_dict()` (full DoclingDocument dict), not DocTags XML — called out in the v2 plan as a misnomer.

#### Config (`services/doc-worker-v1/src/doc_worker/config.py`)
Frozen dataclass with 11 fields (lines 10–21). `load()` (lines 24–46) requires `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `VLM_ENDPOINT_URL`; defaults: `VLM_MODEL_NAME=ibm-granite/granite-docling-258M`, `VLM_TIMEOUT_S=600`, `WORKER_POLL_INTERVAL_S=5`, `WORKER_VISIBILITY_TIMEOUT_S=300`, `WORKER_BATCH_SIZE=1`, `WORKER_MAX_RETRIES=3`, `LOG_LEVEL=INFO`.

#### Modal VLM endpoint (`services/doc-worker-v1/src/vlm_endpoint/modal_app.py`)
- `APP_NAME = "vlm-endpoint-docworker-v1"` (line 24).
- `MODEL_NAME = "ibm-granite/granite-docling-258M"` pinned at revision `55d41aa...` (lines 26–27).
- GPU `L40S:1`, region `us-east`, `MIN_CONTAINERS=0`, `MAX_INPUTS=4` (line 34 — **uncommitted change from 32**), `FAST_BOOT=False`, `SCALEDOWN_WINDOW=2`, `TIMEOUT=120s`, `STARTUP_TIMEOUT=180s` (lines 29–38).
- vLLM settings: `MAX_NUM_SEQS=32`, `MAX_MODEL_LEN=8192`, `MAX_NUM_BATCHED_TOKENS=8192`, `GPU_MEMORY_UTILIZATION=0.9` (lines 41–45).
- Image: `nvidia/cuda:12.9.0-devel-ubuntu22.04` + Python 3.12 + `vllm==0.19.0` + envs `VLLM_SERVER_DEV_MODE=1`, `HF_XET_HIGH_PERFORMANCE=1`, `TORCHINDUCTOR_COMPILE_THREADS=1` (lines 56–65).
- `VllmServer` class (lines 115–159) uses `enable_memory_snapshot=True` and experimental `enable_gpu_snapshot=True`. Lifecycle: `@modal.enter(snap=True) start()` spawns `vllm serve`, calls `wait_ready → warmup → sleep`. `@modal.enter(snap=False) wake_up()` on cold start. `@modal.web_server(port=8000) serve()` exposes HTTP. `@modal.exit() stop()` terminates subprocess.
- `@app.local_entrypoint() test()` (lines 168–189) — sends two chat completions against a test image (arxiv PNG URL) for smoke-testing.

#### Config files
- `pyproject.toml`: deps include `docling>=2.60.0`, `modal>=1.4.1`, `openai>=2.31.0`, `supabase>=2.11.0`, `aiohttp`, `python-dotenv`, `pyyaml`, `requests`. Console script `doc-worker = "doc_worker.worker:run"`.
- `.env.example`: 24 lines covering Supabase, VLM, worker tuning, and integration-test keys.
- `Dockerfile`: two-stage Python 3.13-slim build using `uv` (`ghcr.io/astral-sh/uv:0.5.11`). Cache mount IDs hard-pinned to Railway service `s/e3449a2d-58f7-48f3-a848-055059346fd1-uv`. Non-root `worker` user (uid 1000). `STOPSIGNAL SIGTERM`, `CMD ["doc-worker"]`.
- `railway.toml`: Dockerfile builder, `ON_FAILURE` restart, `numReplicas=1`, `sleepApplication=false`.
- `.python-version`: `3.13`.

#### Tests (`services/doc-worker-v1/tests/`)
- `conftest.py` (106 lines): session-scoped fixtures for config, client, converter, Gaussians PDF download; function-scoped `test_user_id` and `seeded_document` (inserts row + uploads PDF + enqueues job; teardown cleans all).
- `test_integration_gaussians.py` (70 lines): `@pytest.mark.integration`, gated by `RUN_INTEGRATION_TESTS=1`. Drains queue to find the seeded msg, calls `processor.process` synchronously, asserts row status completed, page_count 8–16, markdown > 500 bytes containing `gaussian|covariance|multivariate`.

#### README
11 sections: overview with ASCII flow diagram, setup, deploy Modal endpoint, run worker locally/Docker, Railway deploy (Service `document-worker` in project `heroic-grace`), env vars table, job lifecycle, source layout, library-use example, tests, benchmarks.

#### Uncommitted change
`git status` shows `services/doc-worker-v1/src/vlm_endpoint/modal_app.py` modified — the single line change is `MAX_INPUTS = 32 → 4` (line 34).

---

### 2. `services/doc-worker-v2/` — Modular Rewrite

**Purpose**: Clean-slate worker with pluggable `Queue` / `DocumentStore` / `BlobStore` / `Pipeline` interfaces so backends (Supabase / Postgres) and conversion pipelines (Granite VLM / generic VLM / CPU-only) can be swapped via env config.

#### Package layout (`services/doc-worker-v2/src/doc_worker_v2/`)
- `__init__.py`, `__main__.py` — module markers / CLI wrapper (`sys.exit(run())`).
- `config.py` — pydantic-settings `Settings` + `BackendKind` / `PipelineKind` enums.
- `protocols.py` — four `@runtime_checkable Protocol`s + `QueueMessage` dataclass.
- `artifacts.py` — `Artifact` + `PipelineResult` dataclasses.
- `di.py` — four `build_*` factory functions.
- `processor.py` — `process()` single-message handler.
- `worker.py` — `run()` loop with SIGINT/SIGTERM draining.
- `pipelines/` — `_common.py`, `granite_docling_vlm.py`, `generic_markdown_vlm.py`, `standard_cpu.py`.

#### Protocols (`services/doc-worker-v2/src/doc_worker_v2/protocols.py`)
All `@runtime_checkable`:
- `QueueMessage(msg_id: int, read_ct: int, payload: dict)` — lines 7–11.
- `Queue`: `read(visibility_timeout_s, qty) -> list[QueueMessage]`, `delete(msg_id)`, `archive(msg_id)` (lines 14–18).
- `DocumentStore`: `mark_processing`, `mark_retrying(id, attempt, max_attempts, error)`, `mark_failed(id, error)`, `mark_completed(id, *, page_count, artifacts: dict[str,str], pipeline: str)` (lines 21–35).
- `BlobStore`: `download(path) -> bytes`, `upload(path, content, content_type, *, upsert=True)` (lines 38–48).
- `Pipeline`: `name: str` attribute + `convert(pdf_bytes) -> PipelineResult` (lines 51–55).

Structural typing — backend packages don't import these; they duck-type match.

#### Artifacts (`services/doc-worker-v2/src/doc_worker_v2/artifacts.py`)
- `Artifact(name: str, content: bytes, content_type: str, extension: str)` (lines 4–9).
- `PipelineResult(artifacts: list[Artifact], page_count: int)` (lines 12–15).

#### Config (`services/doc-worker-v2/src/doc_worker_v2/config.py`)
`Settings(BaseSettings)` with `SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)`. Fields:
- **Backend selection**: `queue_backend`, `document_store_backend`, `blob_store_backend` — all default `BackendKind.SUPABASE`.
- **Pipeline**: `pipeline` default `PipelineKind.GRANITE_DOCLING_VLM`.
- **Supabase**: `supabase_url`, `supabase_service_role_key` (both `str | None`), `supabase_storage_bucket="documents"`.
- **Postgres**: `postgres_dsn: str | None = None`.
- **VLM**: `vlm_endpoint_url`, `vlm_model_name="ibm-granite/granite-docling-258M"`, `vlm_preset="granite_docling"`, `vlm_timeout_s=600`.
- **Worker tuning**: `poll_interval_s=5.0`, `visibility_timeout_s=300`, `batch_size=1`, `max_retries=3`, `log_level="INFO"`.
- **Validator** (`@model_validator(mode="after") _require_backend_config`, lines 52–81): requires Supabase creds if any `*_BACKEND=supabase`; requires `POSTGRES_DSN` if queue/docs=postgres; requires `VLM_ENDPOINT_URL` for VLM pipelines.

#### DI factory (`services/doc-worker-v2/src/doc_worker_v2/di.py`)
Four `match`-based dispatchers:
- `build_queue(s)` → `SupabaseQueue` or `PostgresPgmqQueue`.
- `build_document_store(s)` → `SupabaseDocumentStore` or `PostgresDocumentStore`.
- `build_blob_store(s)` → `SupabaseBlobStore` or raises `RuntimeError("No blob-store impl paired with postgres backend yet — see plan phase 9")` (lines 44–46).
- `build_pipeline(s)` → `GraniteDoclingVlmPipeline`, `GenericMarkdownVlmPipeline`, or `StandardCpuPipeline`.

#### Processor (`services/doc-worker-v2/src/doc_worker_v2/processor.py`)
`_validate(payload)` (lines 13–22) raises `JobPayloadError` if any of document_id/user_id/file_path/filename missing.

`process(msg, queue, docs, blobs, pipeline, *, max_retries)` (lines 25–94) — never raises:
1. Validate → on failure, `queue.archive(msg_id)` and return.
2. `attempt = max(1, msg.read_ct)`; log.
3. Try: `docs.mark_processing` → `blobs.download(file_path)` → `pipeline.convert(pdf_bytes)` → for each artifact upload to `{user_id}/{document_id}/{filename}.{extension}` and collect `{name: path}` into `artifacts_map` → `docs.mark_completed(id, page_count, artifacts_map, pipeline.name)` → `queue.delete(msg_id)` → log completion.
4. Except: truncate error to 1000 chars; if `attempt >= max_retries` → `docs.mark_failed` + `queue.archive`; else → `docs.mark_retrying(id, attempt, max_retries, err)` and **do not delete/archive** (VT-based retry).

#### Worker loop (`services/doc-worker-v2/src/doc_worker_v2/worker.py`)
`run()` (lines 9–67):
1. `Settings()` env-load (raises on missing required).
2. `basicConfig` logging.
3. `di.build_queue/docs/blobs/pipeline`.
4. Install SIGINT/SIGTERM handlers that flip `stop=True` and log `"signal=<n>, draining"`.
5. Log `"doc-worker-v2 online; pipeline={name} backends=q:{q}/d:{d}/b:{b}"`.
6. Loop: `queue.read(vt, qty)` (back off on exception), sleep on empty, `processor.process(...)` for each, check `stop` between messages.
7. `finally` calls `close()` on any backend that defines it (Postgres pool cleanup); log `"exited cleanly"`; return 0.

#### Pipelines (`services/doc-worker-v2/src/doc_worker_v2/pipelines/`)
**`_common.py`**: `run_converter(converter, pdf_bytes)` writes to NamedTemporaryFile and calls `converter.convert(Path(tmp.name))`; `markdown_artifact(doc)` emits `(name="markdown", "text/markdown", "md")` via `doc.export_to_markdown().encode()`; `docling_json_artifact(doc)` emits `("docling_json", "application/json", "docling.json")` via `json.dumps(doc.export_to_dict())`; `doctags_artifact(doc)` emits `("doctags", "application/xml", "doctags.xml")` via `doc.export_to_doctags()`; `page_count(doc)` returns `len(doc.pages)` or 0.

**`granite_docling_vlm.py`** (`name="granite_docling_vlm"`): hard-wires the `"granite_docling"` preset with `ApiVlmEngineOptions` — params `temperature=0.0, max_tokens=4096, skip_special_tokens=False`. Uses `VlmPipeline` + `VlmPipelineOptions(enable_remote_services=True)`. Emits **3 artifacts** (markdown, docling_json, doctags). Constructor: `endpoint_url, model_name, timeout_s`.

**`generic_markdown_vlm.py`** (`name="generic_markdown_vlm"`): configurable `preset` constructor arg (pulled from `VLM_PRESET` via config, default `"granite_docling"` — plan notes this should be set to a markdown-emitting preset like `qwen`/`phi4`/`pixtral` for real use). Omits `skip_special_tokens`. Emits **2 artifacts** (markdown, docling_json).

**`standard_cpu.py`** (`name="standard_cpu"`): no VLM. `PdfPipelineOptions(accelerator_options=AcceleratorOptions(device=AcceleratorDevice.CPU), do_ocr=True, do_table_structure=True)` + `StandardPdfPipeline`. Emits **2 artifacts**. No endpoint required.

#### Config files
- `pyproject.toml` (29 lines): deps `docling>=2.60,<3`, `pydantic>=2.9`, `pydantic-settings>=2.5`, `python-dotenv>=1.0`, `storage-supabase`. Optional `postgres = ["storage-postgres"]`. Console script `doc-worker-v2 = "doc_worker_v2.worker:run"`. `[tool.uv.sources]` marks `storage-supabase` and `storage-postgres` as `workspace = true`.
- `.env.example` (36 lines): every Settings field with a one-line comment, grouped by backend/pipeline/VLM/worker tuning.
- `Dockerfile` (53 lines): two-stage Python 3.13-slim + uv 0.11.1. Build stage copies workspace root + all three service folders, runs `uv sync --frozen --no-dev --no-editable` in `/app/doc-worker-v2`. Cache mount ID `s/3f705c5c-aa88-4f53-ae0e-95af43eb4d7a-uv` (Railway service ID for `document-worker-v2`). Runtime stage copies `/opt/venv`, non-root `worker`, `STOPSIGNAL SIGTERM`, `CMD ["doc-worker-v2"]`.

#### Tests (`services/doc-worker-v2/tests/`)
- `conftest.py` (156 lines): session fixtures (`settings`, `queue`, `docs`, `blobs`, `pipeline`, `admin_client`, `gaussians_pdf`), function fixtures (`test_user_id` — auto-provisions throwaway auth user if `TEST_USER_ID` env not set; `seeded_document` — uploads PDF + inserts row).
- `test_processor.py` (196 lines): in-memory `FakeQueue`/`FakeDocs`/`FakeBlobs`/`FakeHappyPipeline`/`FakeBoomPipeline`; 4 test cases covering happy path, retry-remaining, retry-exhausted, and 4 parametrized bad-payload variants (7 total assertions passing).
- `test_integration_gaussians.py` (81 lines): `RUN_INTEGRATION_TESTS=1`-gated E2E against live Supabase + Modal, asserts row completion, page count, artifact paths, markdown content.
- `verify_phase1.py` (170 lines): RLS + archive trigger verification (two users, cross-user read denied).
- `verify_phase3.py` (175 lines): Supabase-backend round-trip (all three backends).
- `verify_phase4.py` (159 lines): live VLM pipeline verification (granite + generic).
- `verify_phase5.py` (316 lines): E2E (`run_end_to_end`) + SIGTERM drain subprocess test (`run_sigterm_drain`).
- `verify_phase9.py` (207 lines): Postgres-backend round-trip (queue + docs).
- `verify_phase9_e2e.py` (206 lines): mixed-backend worker subprocess (postgres queue+docs, supabase blobs), full E2E with the Gaussians PDF.

---

### 3. `services/storage-supabase/` — Supabase Backend Package

**pyproject.toml** (13 lines): name `storage-supabase`, `requires-python>=3.13`, dep `supabase>=2.11`, setuptools build.

**`src/storage_supabase/queue.py`** (38 lines) — `SupabaseQueue`:
- Constructor: `create_client(url, service_role_key)`.
- `read(vt, qty)` (lines 18–31): `self._c.rpc("pgmq_read_doc_ingest", {"vt": vt, "qty": qty}).execute()` → returns list of `_Message(msg_id, read_ct, payload)`.
- `delete(msg_id)` (lines 33–34): `rpc("pgmq_delete_doc_ingest", {"msg_id": ...})`.
- `archive(msg_id)` (lines 36–37): `rpc("pgmq_archive_doc_ingest", {"msg_id": ...})`.

**`src/storage_supabase/documents.py`** (58 lines) — `SupabaseDocumentStore`:
- `_table()` (lines 10–11): `self._c.schema("doc_worker_v2").table("documents")` — uses `.schema()` which requires `doc_worker_v2` exposed in `api.schemas`.
- `_update(id, fields)`: `.update(fields).eq("id", document_id).execute()`.
- `mark_processing`: `{"status": "processing", "error_message": None}`.
- `mark_retrying(id, attempt, max, error)`: `{"status": "retrying", "error_message": f"attempt {n}/{m}: {err[:1000]}"}`.
- `mark_failed(id, error)`: `{"status": "failed", "error_message": error[:1000]}`.
- `mark_completed(id, *, page_count, artifacts, pipeline)`: `{"status": "completed", "page_count", "artifacts", "pipeline", "error_message": None}`.

**`src/storage_supabase/blobs.py`** (28 lines) — `SupabaseBlobStore`:
- Constructor: `create_client(url, key)` + stores bucket name.
- `download(path)`: `self._c.storage.from_(bucket).download(path)`.
- `upload(path, content, content_type, *, upsert=True)`: `.upload(path, content, {"content-type": ct, "upsert": "true"|"false"})`.

---

### 4. `services/storage-postgres/` — Generic Postgres Backend Package

**pyproject.toml** (13 lines): name `storage-postgres`, deps `psycopg[binary]>=3.2` and `psycopg-pool>=3.2`. **No blob store** — only Queue + DocumentStore.

**`src/storage_postgres/queue.py`** (47 lines) — `PostgresPgmqQueue`:
- Constructor `(dsn, *, min_size=1, max_size=4)`: `ConnectionPool(dsn, min_size, max_size, open=True)`.
- Hard-coded `QUEUE_NAME = "doc_ingest"` (line 14).
- `read(vt, qty)`: raw SQL `SELECT msg_id, read_ct, message FROM pgmq.read(%s, %s, %s)` bound to `(QUEUE_NAME, vt, qty)`.
- `delete(msg_id)`: `SELECT pgmq.delete(%s, %s)`.
- `archive(msg_id)`: `SELECT pgmq.archive(%s, %s)`.
- `close()`: `self._pool.close()` — called by worker `finally` block.

**`src/storage_postgres/documents.py`** (71 lines) — `PostgresDocumentStore`:
- Constructor also uses `ConnectionPool`.
- `_update(id, fields)` (lines 18–24): builds SET clause dynamically as `", ".join(f"{c} = %s" for c in cols)` and runs `UPDATE doc_worker_v2.documents SET ... WHERE id = %s`.
- `mark_*` methods mirror Supabase semantics.
- `mark_completed` wraps `artifacts` in `Jsonb(artifacts)` (from `psycopg.types.json`) so psycopg serializes it as JSONB.
- `close()` closes the pool.

---

### 5. `services/vlm-endpoints/modal/` — Standalone VLM Service

**pyproject.toml** (13 lines): name `vlm-modal`, `requires-python>=3.12`, deps `modal>=1.4` + `aiohttp>=3.13`. **Not a workspace member** (not in `services/pyproject.toml` `[tool.uv.workspace] members`).

**README.md** (58 lines): deploy with `uv run modal deploy src/vlm_modal/modal_app.py`, copy the public URL to v2's `.env` as `VLM_ENDPOINT_URL` (ending `/v1/chat/completions`). Smoke test: `uv run modal run src/vlm_modal/modal_app.py::test` — runs twice (cold + warm). States this is a verbatim lift of v1's modal_app.py with `APP_NAME` changed to `vlm-granite-docling`.

**`src/vlm_modal/modal_app.py`** (225 lines) — identical to v1's modal_app.py except:
- `APP_NAME = "vlm-granite-docling"` (v1 uses `"vlm-endpoint-docworker-v1"`).
- All other constants (model, GPU, snapshot setup, helpers `sleep`/`wake_up`/`wait_ready`/`warmup`, `VllmServer` class, `test` entrypoint, `_probe`/`_stream` helpers) are unchanged.

Deployed URL recorded in Phase 6 of the plan: `https://ottokunkel034--vlm-granite-docling-vllmserver-serve.modal.run`.

---

### 6. Workspace & shared config

**`services/pyproject.toml`** (6 lines): uv workspace root listing `members = ["doc-worker-v2", "storage-supabase", "storage-postgres"]` (note `vlm-endpoints/modal` is NOT a member — plan called for it but final config omits).

**`services/railway.toml`** (10 lines): builds `doc-worker-v2/Dockerfile` from the workspace root. `ON_FAILURE`, 10 retries, 1 replica.

**`services/.dockerignore`** (19 lines): excludes `doc-worker-v1/`, `vlm-endpoints/`, caches, venvs, test fixtures, `.env` files; keeps `.env.example`.

---

### 7. Supabase side

#### Migrations (`supabase/migrations/`)

**001_create_tables.sql** — v1 core:
- Extensions: `vector`, `pgmq`.
- `public.documents` (id, user_id, filename, file_path, status check 'pending'|'processing'|'completed'|'failed' — **no 'retrying'**, page_count, total_chunks, doc_json_path, markdown_path, metadata, timestamps) with RLS `auth.uid() = user_id`.
- `public.document_chunks` (chunk_index, content, headings, label, page_no, embedding `vector(1536)` with HNSW cosine index).
- pgmq queue `document_jobs`.
- `public.enqueue_document_job(document_id, user_id, file_path, filename, metadata) returns bigint` SECURITY DEFINER calling `pgmq.send('document_jobs', ...)`.
- `public.match_document_chunks(...)` SECURITY INVOKER for vector similarity.
- `public.update_updated_at()` trigger function.
- Storage bucket `documents` (private) with per-user folder policies.

**002_add_admin_role.sql** — admin role:
- `public.is_admin()` SECURITY DEFINER STABLE — reads `auth.jwt() -> 'app_metadata' ->> 'role'`.
- Admin RLS policies on documents, chunks, storage.

**003_benchmark_runs.sql** — `public.benchmark_runs` table (pdf_label, model, wall_time, page_count, api_calls, tokens, cost, markdown_length, error, timestamps), admin-only RLS.

**004_test_runs.sql** — `public.test_runs` table (type, status, config, pdf_source, progress jsonb, logs, results), admin-only RLS. pgmq queue `test_jobs`. `enqueue_test_run(...) returns uuid` — checks `is_admin()`, builds 10-stage initial progress for integration tests.

**005_pgmq_wrappers.sql** — v1 queue wrappers in public schema, SECURITY DEFINER, service_role-only grants:
- `public.pgmq_read(queue_name, vt, qty)` returns `(msg_id, read_ct, enqueued_at, vt, message)`.
- `public.pgmq_delete(queue_name, msg_id)` returns boolean.
- `public.pgmq_archive(queue_name, msg_id)` returns boolean.

**006_doc_worker_v2.sql** — v2 stack:
- `create schema doc_worker_v2` with usage grants to service_role, authenticated, anon.
- `doc_worker_v2.documents` — id, user_id, filename, file_path, **status check includes 'retrying'**, error_message, page_count, pipeline (nullable, set on completion), **artifacts jsonb** (default `{}`, consolidates markdown/docling_json/doctags paths), metadata, timestamps.
- Indexes: `idx_docs_v2_user` (user_id, created_at desc), `idx_docs_v2_active` partial on status in (pending, processing, retrying).
- RLS `auth.uid() = user_id`; grants `all` to service_role, `select/insert/update/delete` to authenticated.
- `doc_worker_v2.set_updated_at()` + `docs_v2_touch` BEFORE UPDATE trigger.
- pgmq queue `doc_ingest`.
- **Auto-enqueue trigger chain**:
  - `doc_worker_v2.enqueue_on_insert()` SECURITY DEFINER set search_path=pgmq,doc_worker_v2,public — calls `pgmq.send('doc_ingest', jsonb_build_object('document_id', new.id, 'user_id', new.user_id, 'file_path', new.file_path, 'filename', new.filename, 'metadata', new.metadata))`.
  - `docs_v2_enqueue` AFTER INSERT trigger.
- v2 queue wrappers in public schema (service_role only):
  - `public.pgmq_read_doc_ingest(vt, qty)` returns table of pgmq rows.
  - `public.pgmq_delete_doc_ingest(msg_id)` returns boolean.
  - `public.pgmq_archive_doc_ingest(msg_id)` returns boolean.

#### `supabase/config.toml`
`[api] enabled = true, schemas = ["public", "graphql_public", "doc_worker_v2"]` — exposes v2 schema so supabase-py `.schema("doc_worker_v2")` works via PostgREST.

#### Edge function `supabase/functions/upload-document/index.ts` (146 lines)
**v1-only producer**. POST `/functions/v1/upload-document`:
1. Verify JWT with anon-client-based `userClient.auth.getUser()`.
2. Parse multipart form (`file` field, PDF, ≤50MB).
3. Optional `metadata` query parameter as JSON.
4. Generate `documentId = crypto.randomUUID()`.
5. Upload PDF to `documents/{user.id}/{documentId}/{file.name}` via service-role client, `upsert: false`.
6. Insert `public.documents` row via user-client (RLS enforced). On failure, service-role deletes the uploaded file.
7. Service-role calls `enqueue_document_job(...)` RPC. On failure, updates status to `failed`.
8. Returns `{document_id, status: "pending"}` with 201.

v2 has no equivalent edge function — clients insert into `doc_worker_v2.documents` directly, and the AFTER INSERT trigger does the enqueue.

---

### 8. Adjacent context

**`apps/admin/`** (untracked) — Vite + React 19.2 + TS + Tailwind 4 + shadcn/ui + React Router 7.14 + supabase-js. Pages: login, documents, document-detail (with PDF + bbox overlay), jobs, users, benchmarks. Components split across `ui/`, `layout/` (AppLayout, AuthGuard, Sidebar), `benchmarks/`, `observability/`. Hooks for auth, documents, chunks, benchmark runs, job stats, observability metrics. Reads from backend; does not trigger uploads.

**Repo-root configs**:
- `railway.toml`: builds v1 from `services/doc-worker-v1/Dockerfile`.
- `docker-compose.yml`: single `document-worker` service, scalable `--scale document-worker=3`.
- `.mcp.json`: one MCP server configured (`Supabase` at `https://mcp.supabase.com/mcp`).
- `.env`: keys `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `SUPABASE_ANON_KEY`, `SUPABASE_S3_URL`, `SUPABASE_S3_ACCESS_KEY_ID`, `SUPABASE_S3_SECRET_ACCESS_KEY`, `MODAL_WEB_URL`.

**`thoughts/shared/plans/`**:
- `2026-04-12-simplify-doc-worker-v1.md`
- `2026-04-13-deploy-doc-worker-to-railway.md`
- `2026-04-14-doc-worker-v2-modular-architecture.md` (the plan this work is tracking — all 9 phases marked ✅)

---

## Code References

### doc-worker-v1
- `services/doc-worker-v1/src/doc_worker/worker.py:13-53` — polling loop + signal handlers.
- `services/doc-worker-v1/src/doc_worker/processor.py:52-139` — per-message flow, retry semantics.
- `services/doc-worker-v1/src/doc_worker/processor.py:82` — storage download; `:89-98` — artifact uploads; `:109` — queue delete; `:123` — queue archive on exhaustion.
- `services/doc-worker-v1/src/doc_worker/queue.py:19-36` — `pgmq_read`/`pgmq_delete`/`pgmq_archive` RPC wrappers.
- `services/doc-worker-v1/src/doc_worker/docling.py:17-45` — `build_converter`; `:48-65` — `convert_pdf`.
- `services/doc-worker-v1/src/doc_worker/config.py:10-46` — Config dataclass + `load()`.
- `services/doc-worker-v1/src/vlm_endpoint/modal_app.py:24` — APP_NAME; `:34` — MAX_INPUTS (pending 32→4); `:112-159` — `VllmServer`; `:168-189` — `test` entrypoint.
- `services/doc-worker-v1/Dockerfile:1-45` — two-stage uv build.
- `services/doc-worker-v1/tests/test_integration_gaussians.py:22-70` — integration test.

### doc-worker-v2
- `services/doc-worker-v2/src/doc_worker_v2/protocols.py:7-55` — all four protocols + QueueMessage.
- `services/doc-worker-v2/src/doc_worker_v2/config.py:18-82` — Settings + validators.
- `services/doc-worker-v2/src/doc_worker_v2/di.py:5-73` — factory dispatchers.
- `services/doc-worker-v2/src/doc_worker_v2/processor.py:25-94` — `process()`.
- `services/doc-worker-v2/src/doc_worker_v2/worker.py:9-67` — `run()` loop.
- `services/doc-worker-v2/src/doc_worker_v2/pipelines/granite_docling_vlm.py:18-56` — Granite pipeline (3 artifacts).
- `services/doc-worker-v2/src/doc_worker_v2/pipelines/generic_markdown_vlm.py:17-56` — generic VLM (2 artifacts, preset-configurable).
- `services/doc-worker-v2/src/doc_worker_v2/pipelines/standard_cpu.py:17-38` — CPU pipeline.
- `services/doc-worker-v2/src/doc_worker_v2/pipelines/_common.py:11-46` — shared converter + artifact helpers.
- `services/doc-worker-v2/tests/test_processor.py:113-195` — 4 unit tests with fakes.
- `services/doc-worker-v2/tests/verify_phase5.py:57-290` — E2E + SIGTERM drain.
- `services/doc-worker-v2/tests/verify_phase9_e2e.py:1-206` — mixed-backend worker E2E.
- `services/doc-worker-v2/Dockerfile:1-53` — workspace-aware two-stage build.

### Backend packages
- `services/storage-supabase/src/storage_supabase/queue.py:14-37` — `SupabaseQueue`.
- `services/storage-supabase/src/storage_supabase/documents.py:6-57` — `SupabaseDocumentStore` (uses `.schema("doc_worker_v2")`).
- `services/storage-supabase/src/storage_supabase/blobs.py:4-27` — `SupabaseBlobStore`.
- `services/storage-postgres/src/storage_postgres/queue.py:24-46` — `PostgresPgmqQueue` with psycopg pool + `close()`.
- `services/storage-postgres/src/storage_postgres/documents.py:14-70` — `PostgresDocumentStore` with `Jsonb` wrapping.

### VLM service
- `services/vlm-endpoints/modal/src/vlm_modal/modal_app.py:24` — `APP_NAME = "vlm-granite-docling"`.
- `services/vlm-endpoints/modal/README.md` — deploy instructions, smoke test, config knob reference.

### Supabase
- `supabase/migrations/001_create_tables.sql:10-25` — v1 documents; `:74` — document_jobs queue; `:79-105` — enqueue_document_job.
- `supabase/migrations/005_pgmq_wrappers.sql:1-54` — v1 public wrappers.
- `supabase/migrations/006_doc_worker_v2.sql:12-14` — schema; `:19-33` — v2 documents with artifacts jsonb; `:77` — doc_ingest queue; `:82-105` — enqueue_on_insert trigger; `:113-153` — v2 public wrappers.
- `supabase/config.toml` — `[api] schemas` includes `doc_worker_v2`.
- `supabase/functions/upload-document/index.ts:1-146` — v1 producer edge function.

### Workspace / infra
- `services/pyproject.toml` — uv workspace (3 members, modal not listed).
- `services/railway.toml` — v2 Railway deploy config.
- `services/.dockerignore` — excludes v1 + vlm-endpoints from v2 build.
- `railway.toml` (repo root) — v1 Railway deploy config.

## Architecture Documentation

### Two parallel processing stacks

```
v1 stack                                v2 stack
────────────────────────────────────    ─────────────────────────────────────────
client POST /functions/v1/upload-doc    client INSERT doc_worker_v2.documents
         │                                       │
         ▼                                       ▼ (AFTER INSERT trigger)
 upload-document edge function           doc_worker_v2.enqueue_on_insert()
 ├─ auth (JWT)                                   │
 ├─ upload PDF to storage                        ▼
 ├─ insert public.documents              pgmq.send('doc_ingest', ...)
 └─ RPC enqueue_document_job                     │
         │                                       ▼
         ▼                               doc-worker-v2 (Railway)
 pgmq.send('document_jobs', ...)         ├─ SupabaseQueue (RPC pgmq_*_doc_ingest)
         │                               ├─ SupabaseDocumentStore (schema('doc_worker_v2'))
         ▼                               ├─ SupabaseBlobStore (bucket 'documents')
 doc-worker-v1 (Railway)                 └─ Pipeline (granite/generic/cpu)
 ├─ supabase-py (everything)                     │
 ├─ Docling VlmPipeline (granite)                ▼
 └─ Modal: vlm-endpoint-docworker-v1     Modal: vlm-granite-docling
         │                                       │
         ▼                                       ▼
 public.documents + storage              doc_worker_v2.documents + storage
   (markdown_path, doc_json_path            (artifacts jsonb:
    as separate columns)                     {"markdown":..., "docling_json":..., "doctags":...})
```

### Shared patterns
- **VT-based retry**: both workers leave messages on the pgmq queue on transient failure; visibility timeout expires and pgmq re-presents. Max-retries crossing triggers `archive` (dead letter `pgmq.a_<queue>` table).
- **Service-role pgmq access**: both versions expose pgmq operations as `SECURITY DEFINER` functions in the `public` schema, revoked from anon/authenticated and granted only to service_role.
- **Per-user storage folders**: both stacks store artifacts at `{user_id}/{document_id}/{filename}.<ext>`; RLS policies check `storage.foldername(name)[1] = auth.uid()::text`.
- **Upsert uploads**: artifact uploads use `upsert=true` so retried processing overwrites prior output.
- **SIGTERM drain**: `Dockerfile` sets `STOPSIGNAL SIGTERM`; handler flips a `stop` flag that the loop checks between messages; current message completes before exit.

### What's new in v2
- **Schema separation**: `doc_worker_v2.documents` instead of `public.documents`. Requires schema listed in `config.toml` `[api] schemas` for `supabase-py .schema("doc_worker_v2")` to work.
- **Status value `retrying`**: v1's check constraint disallows it but v1 writes it anyway; v2's constraint includes it.
- **`artifacts` jsonb** consolidates arbitrary artifact paths (keyed by stable name) instead of v1's hardcoded `markdown_path` + `doc_json_path` columns.
- **`pipeline` column** records which pipeline produced the row (`granite_docling_vlm`, `generic_markdown_vlm`, `standard_cpu`, ...).
- **Trigger-based enqueue**: atomic (row + queue message either both exist or neither does) — no separate RPC from the client.
- **Protocol-based DI**: swap queue/docs/blobs/pipeline via env vars without code changes. Backend packages don't import the worker.
- **Three Docling arms**: VLM with granite preset (3 artifacts including doctags), VLM with configurable markdown preset (2 artifacts), CPU-only Standard pipeline (2 artifacts, no endpoint required).

### Dependency graph
```
doc-worker-v2  ─depends on→  storage-supabase  (workspace path dep)
               ─optional  →  storage-postgres  (extra "postgres")
               ─runtime   →  Docling + Modal-hosted VLM
storage-supabase          →  supabase-py
storage-postgres          →  psycopg[binary] + psycopg-pool
vlm-endpoints/modal       →  modal + aiohttp  (NOT in uv workspace)
```

## Historical Context (from thoughts/)

- `thoughts/shared/plans/2026-04-14-doc-worker-v2-modular-architecture.md` — the 1168-line plan driving v2. All 9 phases marked `✅`: schema/trigger, worker skeleton + protocols, Supabase impls, Docling pipelines, processor/loop, Modal extract, Dockerfile/Railway, integration test, generic-Postgres ref impl. Records deployed URLs and Railway service IDs used in current code.
- `thoughts/shared/plans/2026-04-13-deploy-doc-worker-to-railway.md` — earlier plan for v1's Railway deploy (service `document-worker` in `heroic-grace/production`, cache mount IDs baked into v1's Dockerfile).
- `thoughts/shared/plans/2026-04-12-simplify-doc-worker-v1.md` — preceding refactor of v1 (commit `ff30ca3` "Refactor doc-worker-v1 and deploy to Railway").

## Related Research

None prior — this is the first research doc under `thoughts/shared/research/`.

## Open Questions

- `services/doc-worker-v1/src/vlm_endpoint/modal_app.py:34` has uncommitted `MAX_INPUTS: 32 → 4` — pending change, unclear whether it will be applied or reverted.
- `services/vlm-endpoints/modal/pyproject.toml` is not in the uv workspace (`services/pyproject.toml` lists only 3 members), though the v2 plan Phase 2 called for it to be included.
- v1 stack `public.documents.status` check constraint still does not include `'retrying'` (v1 writes it anyway); v2's does. No migration has been made to reconcile v1.
- No client code yet inserts into `doc_worker_v2.documents` — `apps/admin/` reads from the v1 tables. Producing into v2 is listed in the plan as out of scope.
