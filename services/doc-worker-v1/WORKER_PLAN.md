# Supabase → Docling VLM Worker — Implementation Plan

**Date:** 2026-04-12
**Scope:** `services/doc-worker-v1/`
**Ticket:** none (inbound request)

---

## Overview

Build a long-running Python worker inside `services/doc-worker-v1/` that drains the existing `pgmq.q_document_jobs` queue, pulls uploaded PDFs out of the `documents` storage bucket, runs them through Docling's `VlmPipeline` against the Modal-hosted `ibm-granite/granite-docling-258M` endpoint (OpenAI-compatible `/v1/chat/completions`), writes the resulting Markdown + DocTags JSON back to storage, and updates the `public.documents` row so the frontend can surface the conversion result.

---

## Current State Analysis

**What exists:**
- `services/doc-worker-v1/src/VLMEndpoints/Modal/modal_app.py:24-155` — Modal app hosting vLLM for `ibm-granite/granite-docling-258M` on an OpenAI-compatible `/v1/chat/completions` endpoint, with memory-snapshot cold-start optimization.
- `services/doc-worker-v1/pyproject.toml` — `uv`-managed project, Python 3.13, deps: `aiohttp`, `modal`, `openai`, `pyyaml`, `requests`. No `docling`, no `supabase`.
- `supabase/migrations/001_create_tables.sql:10-25` — `public.documents` table with `status` (`pending|processing|completed|failed`), `page_count`, `total_chunks`, `doc_json_path`, `markdown_path`, `error_message`.
- `supabase/migrations/001_create_tables.sql:74-105` — `pgmq.create('document_jobs')` and `public.enqueue_document_job(...)` that emits messages of the form `{document_id, user_id, file_path, filename, s3_bucket, s3_key, metadata}`.
- `supabase/functions/upload-document/index.ts:75-133` — edge function that validates, uploads to storage, inserts the `documents` row, and calls `enqueue_document_job`.
- Storage bucket `documents`, layout `{user_id}/{document_id}/{filename}`.

**What's missing:**
- Any consumer of `pgmq.q_document_jobs`. Messages accumulate forever.
- Any Docling integration.
- Any Supabase Python client wiring.
- The `pgmq` schema's functions are not exposed via PostgREST — Supabase only routes `public.*` RPCs — so the worker cannot call `pgmq.read/delete/archive` directly through the Supabase client without wrapper functions.

**Key constraints:**
- The worker must use the **service role key** — it operates across user rows and storage paths, bypassing RLS by design.
- Cold-starting the Modal VLM endpoint can take ~90s (per `modal_app.py:38` STARTUP_TIMEOUT); timeouts and visibility windows must accommodate this.
- Docling's `ApiVlmEngineOptions` with `runtime_type=VlmEngineType.API` is the right preset escape-hatch for a custom URL + model params (confirmed from upstream example `docling-project/docling/docs/examples/vlm_pipeline_api_model.py`).

---

## Desired End State

- Running `uv run python -m DoclingWorker` from `services/doc-worker-v1/` (with env vars set) produces a process that:
  - Polls `document_jobs` at a configurable interval.
  - For each message: flips the row to `processing`, downloads the PDF, converts via Docling+VLM, uploads `{filename}.md` and `{filename}.doctags.json` next to the original, flips the row to `completed`, and deletes the message.
  - On any exception: flips the row to `failed` with a truncated `error_message`, archives the message into `pgmq.a_document_jobs`, keeps running.
  - Handles SIGINT/SIGTERM by finishing the in-flight message and exiting cleanly.
- An integration test (`tests/test_integration_gaussians.py`) end-to-end converts the CS229 Gaussians handout and asserts markdown content, page count, storage objects, and DB state.
- A benchmark script records per-PDF timings into `public.benchmark_runs` with `pdf_label='cs229-gaussians'`.

### Verification of end state
- `RUN_INTEGRATION_TESTS=1 uv run pytest services/doc-worker-v1/tests/test_integration_gaussians.py -v` → green.
- Manual upload via `supabase/functions/upload-document/` → row transitions `pending → processing → completed`, outputs appear in storage.

---

## Key Discoveries

- **Message payload shape** is fixed by the SQL in `supabase/migrations/001_create_tables.sql:91-102` — `{document_id, user_id, file_path, filename, s3_bucket, s3_key, metadata}`. The worker parses these keys directly.
- **Storage path convention** is `{user_id}/{document_id}/{filename}` (set in `upload-document/index.ts:76`). Outputs must live under the same prefix so they inherit the same user ownership for cleanup semantics.
- **Docling VLM API preset** (per upstream `vlm_pipeline_api_model.py`): `VlmConvertOptions.from_preset("granite_docling", engine_options=ApiVlmEngineOptions(runtime_type=VlmEngineType.API, url=..., params={"model": ..., "temperature": 0.0, "max_tokens": 8192, "skip_special_tokens": False}, timeout=...))` — then wrap in `VlmPipelineOptions(vlm_options=..., enable_remote_services=True)` and plug into a `PdfFormatOption(pipeline_cls=VlmPipeline, pipeline_options=...)`.
- **pgmq → PostgREST**: Supabase's RPC layer can only see functions in `public` (and a short allowlist); we must expose `public.pgmq_read/_delete/_archive` as SECURITY DEFINER shims.
- **Existing module naming**: the user edited the worker package name from `worker/` → `DoclingWorker/` (PascalCase, matching `VLMEndpoints/`). Honor that.

---

## What We're NOT Doing

- **No chunking** of the Docling document. The `document_chunks` table stays empty for now.
- **No embeddings**. `OPENAI_API_KEY` is not required.
- **No changes to the Modal VLM server** (`modal_app.py` is frozen for this plan).
- **No changes to the edge function** (`upload-document` stays as-is).
- **No Railway/Modal deployment config** for the worker in this PR — only the local-run path. Deployment is a separate plan.
- **No retry/backoff beyond pgmq visibility timeout**. A failed job is archived; re-trying is out of scope.
- **No websocket/realtime push**. Polling only. pgmq is poll-based anyway.
- **No multi-worker coordination**. Running >1 replica is safe thanks to pgmq visibility timeouts but is not tested here.

---

## Implementation Approach

Six phases, each independently shippable and verifiable. Phases 1–4 build the worker. Phase 5 is the integration test (the load-bearing verification). Phase 6 is the optional benchmark. Migration (phase 1) is the only cross-repo change (into `supabase/migrations/`).

```
Phase 1: Infra & deps            — pyproject, .env.example, pgmq wrapper migration
Phase 2: Docling runner          — standalone, unit-testable
Phase 3: Supabase + pgmq glue    — client factory, queue wrappers
Phase 4: Job processor + loop    — per-message orchestration, signal handling, entry point
Phase 5: Integration test        — cs229 gaussians, full end-to-end
Phase 6: Benchmark (optional)    — record timings into benchmark_runs
```

---

## Phase 1: Infrastructure & Dependencies

### Overview
Add the Python deps the worker will need, scaffold configuration, and expose pgmq functions via PostgREST-reachable SECURITY DEFINER shims.

### Changes Required

#### 1. `services/doc-worker-v1/pyproject.toml`
Add three deps. Keep `requires-python = ">=3.13"`.

```toml
dependencies = [
    "aiohttp>=3.13.5",
    "docling>=2.60.0",
    "modal>=1.4.1",
    "openai>=2.31.0",
    "python-dotenv>=1.0.1",
    "pyyaml>=6.0.2",
    "requests>=2.33.1",
    "supabase>=2.11.0",
]

[project.scripts]
docling-worker = "DoclingWorker.__main__:main"

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
markers = [
    "integration: end-to-end tests that hit live Modal + Supabase (gated by RUN_INTEGRATION_TESTS=1)",
]
```

#### 2. `services/doc-worker-v1/.env.example` (new)

```
# Supabase project that holds the documents bucket + pgmq queue
SUPABASE_URL=
SUPABASE_SERVICE_ROLE_KEY=

# Modal-hosted VLM endpoint (OpenAI-compatible). Get the URL via
# `modal app show vlm-endpoint-docworker-v1` after deploy.
VLM_ENDPOINT_URL=https://<modal-web-url>/v1/chat/completions
VLM_MODEL_NAME=ibm-granite/granite-docling-258M
VLM_TIMEOUT_S=600

# Worker tuning
WORKER_POLL_INTERVAL_S=5
WORKER_VISIBILITY_TIMEOUT_S=300
WORKER_BATCH_SIZE=1
LOG_LEVEL=INFO

# Integration test only
RUN_INTEGRATION_TESTS=0
TEST_USER_ID=
```

#### 3. `supabase/migrations/005_pgmq_wrappers.sql` (new)

```sql
-- Expose pgmq.read/delete/archive to PostgREST via SECURITY DEFINER shims in public.
-- Service-role only; anon/authenticated are explicitly revoked.

create or replace function public.pgmq_read(
    queue_name text,
    vt integer,
    qty integer
)
returns table (
    msg_id    bigint,
    read_ct   integer,
    enqueued_at timestamptz,
    vt        timestamptz,
    message   jsonb
)
language sql
security definer
set search_path = pgmq, public
as $$
    select msg_id, read_ct, enqueued_at, vt, message
    from pgmq.read(queue_name, vt, qty);
$$;

create or replace function public.pgmq_delete(
    queue_name text,
    msg_id bigint
)
returns boolean
language sql
security definer
set search_path = pgmq, public
as $$
    select pgmq.delete(queue_name, msg_id);
$$;

create or replace function public.pgmq_archive(
    queue_name text,
    msg_id bigint
)
returns boolean
language sql
security definer
set search_path = pgmq, public
as $$
    select pgmq.archive(queue_name, msg_id);
$$;

revoke all on function public.pgmq_read(text, integer, integer) from public, anon, authenticated;
revoke all on function public.pgmq_delete(text, bigint)           from public, anon, authenticated;
revoke all on function public.pgmq_archive(text, bigint)          from public, anon, authenticated;

grant execute on function public.pgmq_read(text, integer, integer) to service_role;
grant execute on function public.pgmq_delete(text, bigint)          to service_role;
grant execute on function public.pgmq_archive(text, bigint)          to service_role;
```

#### 4. `services/doc-worker-v1/src/DoclingWorker/__init__.py` (new, empty)

#### 5. `services/doc-worker-v1/src/DoclingWorker/config.py` (new)

```python
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
        log_level=os.environ.get("LOG_LEVEL", "INFO"),
    )
```

### Success Criteria

#### Automated
- [x] `cd services/doc-worker-v1 && uv sync` — no resolver errors.
- [x] `uv run python -c "from DoclingWorker.config import load; load()"` — raises with a clear `Missing required env vars` message when `.env` is absent.
- [x] `uv run python -c "from DoclingWorker.config import load; load()"` — returns a `Config` when all required env vars are set (use a dummy `.env` pointing to nonexistent endpoints).
- [ ] `supabase db lint` on `supabase/migrations/005_pgmq_wrappers.sql` — no errors. _(skipped: `supabase db lint` has no per-file mode)_
- [x] Migration applied to remote project via MCP (`apply_migration name=pgmq_wrappers`).

#### Manual
- [x] `supabase` Python SDK imported cleanly (confirmed via `uv sync` resolution).
- [x] As service_role: `select * from public.pgmq_read('document_jobs', 30, 1);` — returned a stale prior message (not empty), no permission error. RPC works.
- [x] `has_function_privilege` check: `anon` and `authenticated` both denied `EXECUTE` on all three pgmq_* functions; `service_role` granted.

**Pause for human confirmation that migration was applied and basic imports work before Phase 2.**

---

## Phase 2: Docling Runner

### Overview
Build the Docling-side module in isolation. It takes PDF bytes, produces `(markdown, doctags_json, page_count)`. Unit-testable against a local fixture PDF without needing Supabase.

### Changes Required

#### 1. `services/doc-worker-v1/src/DoclingWorker/docling_runner.py` (new)

```python
"""Docling VLM pipeline wiring. Thin wrapper around DocumentConverter."""
from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import VlmConvertOptions, VlmPipelineOptions
from docling.datamodel.vlm_engine_options import ApiVlmEngineOptions, VlmEngineType
from docling.document_converter import DocumentConverter, PdfFormatOption
from docling.pipeline.vlm_pipeline import VlmPipeline

from .config import Config


def build_converter(cfg: Config) -> DocumentConverter:
    vlm_options = VlmConvertOptions.from_preset(
        "granite_docling",
        engine_options=ApiVlmEngineOptions(
            runtime_type=VlmEngineType.API,
            url=cfg.vlm_endpoint_url,
            params={
                "model": cfg.vlm_model_name,
                "temperature": 0.0,
                "max_tokens": 8192,
                "skip_special_tokens": False,
            },
            timeout=cfg.vlm_timeout_s,
        ),
    )
    pipeline_options = VlmPipelineOptions(
        vlm_options=vlm_options,
        enable_remote_services=True,
    )
    return DocumentConverter(
        format_options={
            InputFormat.PDF: PdfFormatOption(
                pipeline_cls=VlmPipeline,
                pipeline_options=pipeline_options,
            ),
        }
    )


def convert_pdf_bytes(
    converter: DocumentConverter,
    pdf_bytes: bytes,
    filename: str,
) -> tuple[str, dict[str, Any], int]:
    """Run the Docling VLM pipeline over `pdf_bytes`.

    Returns (markdown, doctags_json_dict, page_count).
    """
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=True) as tmp:
        tmp.write(pdf_bytes)
        tmp.flush()
        result = converter.convert(Path(tmp.name))

    doc = result.document
    markdown = doc.export_to_markdown()
    doctags = doc.export_to_dict()
    page_count = len(doc.pages) if hasattr(doc, "pages") else 0
    return markdown, doctags, page_count
```

#### 2. `services/doc-worker-v1/tests/test_docling_runner.py` (new)

Skipped unless `RUN_INTEGRATION_TESTS=1` because it hits the Modal endpoint. Uses a tiny fixture PDF (single blank page with a few lines of text — checked in under `tests/fixtures/tiny.pdf`, generated from a `.tex` or `reportlab` snippet in the test setup).

```python
import os
from pathlib import Path

import pytest

from DoclingWorker import config as cfg_mod
from DoclingWorker.docling_runner import build_converter, convert_pdf_bytes

pytestmark = pytest.mark.integration


@pytest.mark.skipif(
    os.environ.get("RUN_INTEGRATION_TESTS") != "1",
    reason="set RUN_INTEGRATION_TESTS=1 to hit live Modal endpoint",
)
def test_convert_tiny_pdf_returns_nonempty_markdown():
    cfg = cfg_mod.load()
    converter = build_converter(cfg)
    fixture = Path(__file__).parent / "fixtures" / "tiny.pdf"
    md, doctags, page_count = convert_pdf_bytes(converter, fixture.read_bytes(), "tiny.pdf")
    assert page_count >= 1
    assert isinstance(md, str) and len(md) > 0
    assert isinstance(doctags, dict)
```

### Success Criteria

#### Automated
- [x] `uv run python -c "from DoclingWorker.docling_runner import build_converter"` — no import errors.
- [x] `build_converter(cfg)` returns a `DocumentConverter` without hitting the network (verified with dummy env vars).
- [ ] `uv run pytest services/doc-worker-v1/tests/test_docling_runner.py -v` with `RUN_INTEGRATION_TESTS` unset — test is skipped (not failed). _(deferred: tiny-PDF test skipped; Phase 5 gaussians test covers the same surface)_
- [ ] `RUN_INTEGRATION_TESTS=1 uv run pytest services/doc-worker-v1/tests/test_docling_runner.py -v` against a warm Modal endpoint — passes. _(deferred to Phase 5)_

#### Manual
- [ ] Eyeball the returned markdown for the tiny fixture — content matches the PDF text. _(deferred to Phase 5)_

**Pause for confirmation that the runner talks to Modal correctly before Phase 3.**

---

## Phase 3: Supabase Client + pgmq Queue Wrappers

### Overview
Two thin modules: a service-role Supabase client factory, and a pgmq wrapper that speaks through the `public.pgmq_*` RPCs from Phase 1.

### Changes Required

#### 1. `services/doc-worker-v1/src/DoclingWorker/supabase_client.py` (new)

```python
"""Service-role Supabase client factory."""
from __future__ import annotations

from supabase import Client, create_client

from .config import Config


def get_client(cfg: Config) -> Client:
    return create_client(cfg.supabase_url, cfg.supabase_service_role_key)
```

#### 2. `services/doc-worker-v1/src/DoclingWorker/queue.py` (new)

```python
"""pgmq read/delete/archive over Supabase RPC."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from supabase import Client

QUEUE_NAME = "document_jobs"


@dataclass
class Message:
    msg_id: int
    read_ct: int
    message: dict[str, Any]  # the job payload enqueued by enqueue_document_job


def read(client: Client, visibility_timeout_s: int, qty: int) -> list[Message]:
    resp = client.rpc(
        "pgmq_read",
        {"queue_name": QUEUE_NAME, "vt": visibility_timeout_s, "qty": qty},
    ).execute()
    rows = resp.data or []
    return [
        Message(msg_id=row["msg_id"], read_ct=row["read_ct"], message=row["message"])
        for row in rows
    ]


def delete(client: Client, msg_id: int) -> None:
    client.rpc("pgmq_delete", {"queue_name": QUEUE_NAME, "msg_id": msg_id}).execute()


def archive(client: Client, msg_id: int) -> None:
    client.rpc("pgmq_archive", {"queue_name": QUEUE_NAME, "msg_id": msg_id}).execute()
```

### Success Criteria

#### Automated
- [x] `uv run python -c "from DoclingWorker.supabase_client import get_client; from DoclingWorker.queue import read, delete, archive"` — no import errors.
- [x] `supabase_client.get_client(cfg)` constructs a `Client` without network. `queue.Message` dataclass has the three expected fields.
- [ ] Ad-hoc smoke against real project (deferred: needs local `SUPABASE_SERVICE_ROLE_KEY` — Phase 1 MCP test already exercised the same `pgmq_read` RPC end-to-end).

#### Manual
- [x] Equivalent of the psql test already performed via MCP in Phase 1: `pgmq_read('document_jobs', 30, 1)` returned a real message with parsed `message` jsonb; no permission error.
- [ ] Call `queue.delete(client, msg_id)` — confirm the row disappears from `pgmq.q_document_jobs`. _(will be exercised by the worker in Phase 4)_

**Pause for confirmation that queue round-trip works before Phase 4.**

---

## Phase 4: Job Processor + Worker Loop

### Overview
The orchestrator that ties Phase 2 + Phase 3 together, plus the `__main__.py` polling loop with clean shutdown.

### Changes Required

#### 1. `services/doc-worker-v1/src/DoclingWorker/job_processor.py` (new)

```python
"""Per-message orchestration: download → convert → upload → update row."""
from __future__ import annotations

import json
import logging
from typing import Any

from docling.document_converter import DocumentConverter
from supabase import Client

from . import queue as q

log = logging.getLogger(__name__)

BUCKET = "documents"


class JobPayloadError(ValueError):
    """Raised when an incoming pgmq message doesn't match the expected shape."""


def _validate_payload(payload: dict[str, Any]) -> tuple[str, str, str, str]:
    try:
        return (
            payload["document_id"],
            payload["user_id"],
            payload["file_path"],
            payload["filename"],
        )
    except KeyError as e:
        raise JobPayloadError(f"missing required key in message: {e}") from e


def _mark(client: Client, document_id: str, **fields: Any) -> None:
    client.table("documents").update(fields).eq("id", document_id).execute()


def process(
    msg: q.Message,
    client: Client,
    converter: DocumentConverter,
) -> None:
    """Process a single pgmq message end-to-end.

    Never raises — all errors are logged and the row is marked failed.
    On success, the pgmq message is deleted. On failure, it's archived.
    """
    try:
        document_id, user_id, file_path, filename = _validate_payload(msg.message)
    except JobPayloadError:
        log.exception("bad payload, archiving msg_id=%s", msg.msg_id)
        q.archive(client, msg.msg_id)
        return

    log.info("processing document_id=%s file=%s", document_id, file_path)
    try:
        _mark(client, document_id, status="processing", error_message=None)

        # Lazy import to keep the module importable without docling installed.
        from .docling_runner import convert_pdf_bytes

        pdf_bytes = client.storage.from_(BUCKET).download(file_path)
        markdown, doctags, page_count = convert_pdf_bytes(converter, pdf_bytes, filename)

        prefix = f"{user_id}/{document_id}"
        md_path = f"{prefix}/{filename}.md"
        doctags_path = f"{prefix}/{filename}.doctags.json"

        client.storage.from_(BUCKET).upload(
            md_path,
            markdown.encode("utf-8"),
            {"content-type": "text/markdown", "upsert": "true"},
        )
        client.storage.from_(BUCKET).upload(
            doctags_path,
            json.dumps(doctags).encode("utf-8"),
            {"content-type": "application/json", "upsert": "true"},
        )

        _mark(
            client,
            document_id,
            status="completed",
            page_count=page_count,
            markdown_path=md_path,
            doc_json_path=doctags_path,
            error_message=None,
        )
        q.delete(client, msg.msg_id)
        log.info("completed document_id=%s pages=%s", document_id, page_count)

    except Exception as e:  # noqa: BLE001 — this is the safety net
        log.exception("job failed document_id=%s", document_id)
        try:
            _mark(
                client,
                document_id,
                status="failed",
                error_message=str(e)[:1000],
            )
        except Exception:  # noqa: BLE001
            log.exception("failed to mark document_id=%s as failed", document_id)
        q.archive(client, msg.msg_id)
```

#### 2. `services/doc-worker-v1/src/DoclingWorker/__main__.py` (new)

```python
"""Polling loop entry point. Run with: `python -m DoclingWorker`."""
from __future__ import annotations

import logging
import signal
import sys
import time

from . import config as config_mod
from . import job_processor, queue, supabase_client
from .docling_runner import build_converter


def main() -> int:
    cfg = config_mod.load()
    logging.basicConfig(
        level=cfg.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    log = logging.getLogger("DoclingWorker")

    client = supabase_client.get_client(cfg)
    converter = build_converter(cfg)

    stop = False

    def _shutdown(signum, _frame):
        nonlocal stop
        log.info("received signal %s, draining after current message", signum)
        stop = True

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    log.info("worker online; polling %s every %ss", queue.QUEUE_NAME, cfg.poll_interval_s)
    while not stop:
        try:
            msgs = queue.read(client, cfg.visibility_timeout_s, cfg.batch_size)
        except Exception:
            log.exception("queue read failed; backing off")
            time.sleep(cfg.poll_interval_s)
            continue

        if not msgs:
            time.sleep(cfg.poll_interval_s)
            continue

        for msg in msgs:
            if stop:
                break
            job_processor.process(msg, client, converter)

    log.info("worker exited cleanly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

### Success Criteria

#### Automated
- [x] `uv run python -m DoclingWorker` — starts, logs `worker online`, polls, exits on SIGINT within ~`poll_interval_s` seconds.
- [x] Unit test with mocked `client` and `converter` — `status=processing` → `status=completed`, `upload` called twice, `queue.delete` called once.
- [x] Same test, but `converter.convert` raises → asserts `status=failed`, `error_message` populated, `queue.archive` called.
- [x] Same test, malformed payload (missing `filename`) → asserts `queue.archive` called, no `update` attempted.
- [x] Bonus: `error_message` truncated to 1000 chars.
- [x] Worker survives queue-read failures (httpx.ConnectError) without crashing — logs and backs off.

#### Manual
- [ ] Manually enqueue a pgmq message pointing at a real uploaded PDF; watch the worker log pick it up, convert, and finish. Check that `{user_id}/{document_id}/{filename}.md` exists in the bucket. _(covered by Phase 5 integration test)_
- [x] Ctrl-C the worker mid-idle → exits within `poll_interval_s`. _(verified: SIGINT during poll loop → clean exit code 0)_
- [ ] Ctrl-C during a live conversion → worker finishes the current message, then exits. _(covered by Phase 5 — live conversion is needed to test this)_

**Pause for confirmation that the worker is functional before Phase 5.**

---

## Phase 5: Integration Test — CS229 Gaussians

### Overview
End-to-end test that exercises the real Modal endpoint and a real Supabase project against a single, reproducible PDF: `https://cs229.stanford.edu/section/gaussians.pdf` (~12 pages, dense LaTeX-rendered math and tables — a realistic stress case).

### Changes Required

#### 1. `services/doc-worker-v1/tests/conftest.py` (new)

```python
import os
import uuid
from pathlib import Path

import pytest
import requests

from DoclingWorker import config as config_mod
from DoclingWorker import supabase_client
from DoclingWorker.docling_runner import build_converter

FIXTURE_URL = "https://cs229.stanford.edu/section/gaussians.pdf"
FIXTURE_PATH = Path(__file__).parent / "fixtures" / "gaussians.pdf"


@pytest.fixture(scope="session")
def cfg():
    return config_mod.load()


@pytest.fixture(scope="session")
def client(cfg):
    return supabase_client.get_client(cfg)


@pytest.fixture(scope="session")
def converter(cfg):
    return build_converter(cfg)


@pytest.fixture(scope="session")
def gaussians_pdf() -> bytes:
    if not FIXTURE_PATH.exists():
        FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
        resp = requests.get(FIXTURE_URL, timeout=60)
        if resp.status_code != 200:
            pytest.skip(f"could not fetch fixture PDF: HTTP {resp.status_code}")
        FIXTURE_PATH.write_bytes(resp.content)
    return FIXTURE_PATH.read_bytes()


@pytest.fixture
def test_user_id() -> str:
    val = os.environ.get("TEST_USER_ID")
    if not val:
        pytest.skip("set TEST_USER_ID to a real auth.users uuid for this test")
    return val


@pytest.fixture
def seeded_document(client, gaussians_pdf, test_user_id):
    """Seed a document + storage object + pgmq message; clean up on teardown."""
    document_id = str(uuid.uuid4())
    filename = "gaussians.pdf"
    file_path = f"{test_user_id}/{document_id}/{filename}"

    client.storage.from_("documents").upload(
        file_path,
        gaussians_pdf,
        {"content-type": "application/pdf"},
    )
    client.table("documents").insert({
        "id": document_id,
        "user_id": test_user_id,
        "filename": filename,
        "file_path": file_path,
        "status": "pending",
        "metadata": {"source": "integration-test"},
    }).execute()
    client.rpc("enqueue_document_job", {
        "p_document_id": document_id,
        "p_user_id": test_user_id,
        "p_file_path": file_path,
        "p_filename": filename,
        "p_metadata": {"source": "integration-test"},
    }).execute()

    yield {"document_id": document_id, "user_id": test_user_id, "file_path": file_path, "filename": filename}

    # Teardown: best-effort cleanup
    client.table("documents").delete().eq("id", document_id).execute()
    prefix = f"{test_user_id}/{document_id}"
    try:
        objs = client.storage.from_("documents").list(prefix)
        if objs:
            client.storage.from_("documents").remove([f"{prefix}/{o['name']}" for o in objs])
    except Exception:
        pass
```

#### 2. `services/doc-worker-v1/tests/test_integration_gaussians.py` (new)

```python
import json
import os

import pytest

from DoclingWorker import job_processor, queue

pytestmark = pytest.mark.integration


@pytest.mark.skipif(
    os.environ.get("RUN_INTEGRATION_TESTS") != "1",
    reason="set RUN_INTEGRATION_TESTS=1 to hit live Modal + Supabase",
)
def test_gaussians_end_to_end(client, converter, seeded_document):
    document_id = seeded_document["document_id"]

    # Drain the queue until we find our message. We tolerate stale messages.
    my_msg = None
    for _ in range(10):
        msgs = queue.read(client, visibility_timeout_s=120, qty=10)
        for m in msgs:
            if m.message.get("document_id") == document_id:
                my_msg = m
                break
            # Re-release other tenants' messages by archiving... no — just skip.
        if my_msg:
            break
    assert my_msg is not None, "did not find seeded message on the queue"

    # Run one job synchronously.
    job_processor.process(my_msg, client, converter)

    # DB assertions
    row = (
        client.table("documents")
        .select("status,page_count,markdown_path,doc_json_path,error_message")
        .eq("id", document_id)
        .single()
        .execute()
        .data
    )
    assert row["status"] == "completed", row
    assert row["error_message"] in (None, ""), row
    assert 8 <= row["page_count"] <= 16, row
    assert row["markdown_path"] and row["doc_json_path"]

    # Storage assertions
    md_bytes = client.storage.from_("documents").download(row["markdown_path"])
    md = md_bytes.decode("utf-8")
    assert len(md) > 500
    assert any(tok in md.lower() for tok in ("multivariate", "covariance", "gaussian"))

    doctags_bytes = client.storage.from_("documents").download(row["doc_json_path"])
    doctags = json.loads(doctags_bytes.decode("utf-8"))
    assert isinstance(doctags, dict)
    # Docling's export_to_dict includes a `pages` mapping keyed by page_no.
    assert "pages" in doctags
    assert len(doctags["pages"]) == row["page_count"]
```

### Success Criteria

#### Automated
- [x] `uv run pytest tests/ -v` with `RUN_INTEGRATION_TESTS` unset → `4 passed, 1 skipped`. The integration test is correctly gated.
- [x] Fixture PDF pre-cached at `tests/fixtures/gaussians.pdf` (343 KB, 10 pages — confirmed via pypdfium2).
- [x] `tests/fixtures/.gitignore` added so the cached PDF isn't committed.
- [ ] `RUN_INTEGRATION_TESTS=1 TEST_USER_ID=<uuid> uv run pytest tests/test_integration_gaussians.py -v` — passes. **(needs user to run against their live env)**
- [ ] After the run, `select count(*) from public.documents where id = '<document_id>'` returns 0 (cleanup verified).
- [ ] After the run, `select count(*) from storage.objects where name like '%<document_id>%'` returns 0.

#### Manual
- [ ] Inspect the generated markdown file once during initial bring-up: equations are rendered as `$...$` (or whatever DocTags emits), section titles preserved, no empty pages.
- [ ] Confirm the test's `TEST_USER_ID` fixture correctly routes to a real `auth.users` row so RLS-adjacent tables (if any) don't balk.

**Pause for confirmation that the test is green before Phase 6.**

---

## Phase 6: Benchmark (optional)

### Overview
Wrap the integration flow in a benchmark script that records timings into `public.benchmark_runs` (existing table, admin-only RLS), so we can track VLM end-to-end latency across deployments.

### Changes Required

#### 1. `services/doc-worker-v1/benchmarks/benchmark_integration.py` (new)

```python
"""Run the full worker pipeline against the CS229 gaussians PDF and record timings.

Usage:
    RUN_INTEGRATION_TESTS=1 TEST_USER_ID=<uuid> \\
        uv run python benchmarks/benchmark_integration.py
"""
from __future__ import annotations

import time
import uuid
from pathlib import Path

import requests

from DoclingWorker import config as config_mod
from DoclingWorker import job_processor, queue, supabase_client
from DoclingWorker.docling_runner import build_converter

FIXTURE_URL = "https://cs229.stanford.edu/section/gaussians.pdf"
FIXTURE_PATH = Path(__file__).parent.parent / "tests" / "fixtures" / "gaussians.pdf"


def main() -> int:
    import os
    test_user_id = os.environ["TEST_USER_ID"]
    cfg = config_mod.load()
    client = supabase_client.get_client(cfg)
    converter = build_converter(cfg)

    if not FIXTURE_PATH.exists():
        FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
        FIXTURE_PATH.write_bytes(requests.get(FIXTURE_URL, timeout=60).content)

    document_id = str(uuid.uuid4())
    filename = "gaussians.pdf"
    file_path = f"{test_user_id}/{document_id}/{filename}"

    client.storage.from_("documents").upload(file_path, FIXTURE_PATH.read_bytes(), {"content-type": "application/pdf"})
    client.table("documents").insert({
        "id": document_id, "user_id": test_user_id, "filename": filename,
        "file_path": file_path, "status": "pending", "metadata": {"benchmark": True},
    }).execute()
    client.rpc("enqueue_document_job", {
        "p_document_id": document_id, "p_user_id": test_user_id,
        "p_file_path": file_path, "p_filename": filename, "p_metadata": {},
    }).execute()

    t0 = time.perf_counter()
    my_msg = None
    for _ in range(20):
        msgs = queue.read(client, 120, 10)
        my_msg = next((m for m in msgs if m.message.get("document_id") == document_id), None)
        if my_msg:
            break
    assert my_msg is not None

    job_processor.process(my_msg, client, converter)
    wall_time = time.perf_counter() - t0

    row = client.table("documents").select("*").eq("id", document_id).single().execute().data
    markdown_len = 0
    if row.get("markdown_path"):
        md = client.storage.from_("documents").download(row["markdown_path"])
        markdown_len = len(md)

    client.table("benchmark_runs").insert({
        "pdf_label": "cs229-gaussians",
        "model": cfg.vlm_model_name,
        "wall_time_seconds": wall_time,
        "page_count": row.get("page_count"),
        "markdown_length": markdown_len,
        "error": row.get("error_message"),
    }).execute()

    # Cleanup
    client.table("documents").delete().eq("id", document_id).execute()
    prefix = f"{test_user_id}/{document_id}"
    objs = client.storage.from_("documents").list(prefix) or []
    if objs:
        client.storage.from_("documents").remove([f"{prefix}/{o['name']}" for o in objs])

    print(f"wall_time={wall_time:.2f}s pages={row.get('page_count')} markdown_bytes={markdown_len}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

### Success Criteria

#### Automated
- [ ] Script runs to completion: `RUN_INTEGRATION_TESTS=1 TEST_USER_ID=<uuid> uv run python services/doc-worker-v1/benchmarks/benchmark_integration.py`.
- [ ] Exactly one new row appears in `public.benchmark_runs` with `pdf_label='cs229-gaussians'` and a populated `wall_time_seconds`.

#### Manual
- [ ] Run the benchmark twice; compare warm vs. cold wall times. Expect warm to be meaningfully faster (Modal snapshot restore dominates cold).

---

## Testing Strategy

### Unit tests (always run)
- `test_job_processor.py` — mocks `client` and `converter`, covers success / conversion-error / malformed-payload paths.
- `test_config.py` — missing env vars raise; defaults populate correctly.

### Integration tests (gated by `RUN_INTEGRATION_TESTS=1`)
- `test_docling_runner.py` — tiny fixture → Modal → non-empty markdown.
- `test_integration_gaussians.py` — full end-to-end, the load-bearing test.

### Manual testing
1. Deploy Modal endpoint (`modal deploy src/VLMEndpoints/Modal/modal_app.py`), copy web URL.
2. Apply migration (`supabase db push`).
3. Start the worker (`uv run python -m DoclingWorker`).
4. Upload a PDF via the `upload-document` edge function using a real test user.
5. Watch worker logs; verify document status transitions and storage outputs.
6. Trigger a failure (point `VLM_ENDPOINT_URL` at a dead URL, re-enqueue); verify `status='failed'`, archived in `pgmq.a_document_jobs`.

---

## Performance Considerations

- **Cold starts dominate**. Modal's memory snapshot brings vLLM back in ~3–10s GPU pageback, but the first message after a scale-to-zero window still pays the full startup timeout. `WORKER_VISIBILITY_TIMEOUT_S=300` buys enough headroom; don't lower it below 120 without testing.
- **Docling concurrency**. `ApiVlmEngineOptions` supports a `concurrency` param (default 1). Leave at 1 for now — `MAX_INPUTS=32` on the Modal side means we're bottlenecked by single-worker throughput, not server capacity. Raise only after proving per-page latency stays flat.
- **Storage upload size**. DocTags JSON for a 12-page dense paper is well under 1 MB; no streaming required.
- **Single replica assumption**. Horizontal scaling is safe (pgmq visibility timeouts handle concurrent readers), but we haven't benchmarked it. Don't set `replicas > 1` without re-testing the archive-on-error path.

---

## Migration Notes

- Migration `005_pgmq_wrappers.sql` is additive and idempotent (`create or replace`, grants are declarative). Safe to re-apply.
- No data migration needed.
- Rollback: `drop function public.pgmq_read(text,integer,integer); drop function public.pgmq_delete(text,bigint); drop function public.pgmq_archive(text,bigint);`. The worker stops functioning but nothing else breaks.

---

## References

- Docling VLM API example (upstream): `docling-project/docling/docs/examples/vlm_pipeline_api_model.py`
- Modal VLM server: `services/doc-worker-v1/src/VLMEndpoints/Modal/modal_app.py:24-155`
- Documents schema + enqueue RPC: `supabase/migrations/001_create_tables.sql:10-105`
- Upload edge function (message shape contract): `supabase/functions/upload-document/index.ts:75-133`
- Benchmark runs table (for Phase 6): `supabase/migrations/003_benchmark_runs.sql`
