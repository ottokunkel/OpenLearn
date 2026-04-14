# doc-worker-v1

Document ingestion worker. Pulls PDF jobs off a Supabase pgmq queue, runs them
through Docling's VLM pipeline (granite-docling, hosted on Modal), and uploads
markdown + Docling JSON back to Supabase storage.

```
enqueue_document_job(...)  ──►  pgmq "document_jobs"
                                       │
                                       ▼
                      ┌─────────────────────────────┐
                      │  doc-worker (this service)  │
                      │  ┌───────────────────────┐  │
                      │  │ 1. read (atomic VT)   │  │
                      │  │ 2. download PDF       │──┼──► Supabase Storage
                      │  │ 3. convert via VLM    │──┼──► Modal: vllm + granite-docling
                      │  │ 4. upload .md + .json │──┼──► Supabase Storage
                      │  │ 5. update documents   │──┼──► Supabase Postgres
                      │  │ 6. delete / retry /   │  │
                      │  │    archive message    │  │
                      │  └───────────────────────┘  │
                      └─────────────────────────────┘
```

---

## Setup

```bash
cd services/doc-worker-v1
uv sync
cp .env.example .env
# then fill in the Supabase + VLM values (see "Configuration" below)
```

Python 3.13. Dependencies pinned in `uv.lock`.

---

## Deploy the Modal VLM endpoint

The worker calls an OpenAI-compatible chat-completions endpoint hosted on
Modal. It serves `ibm-granite/granite-docling-258M` on an L40S.

```bash
# One-time deploy (or after changing src/vlm_endpoint/modal_app.py)
uv run modal deploy src/vlm_endpoint/modal_app.py

# Get the public URL and copy it into .env as VLM_ENDPOINT_URL
uv run modal app show vlm-endpoint-docworker-v1
```

`VLM_ENDPOINT_URL` should end with `/v1/chat/completions`.

Quick sanity check against the deployed app (streams a transcription of a
test image):

```bash
uv run modal run src/vlm_endpoint/modal_app.py::test
```

---

## Run the worker

Locally:

```bash
uv run doc-worker
```

In Docker:

```bash
docker build -t doc-worker-v1 .
docker run --rm --env-file .env doc-worker-v1
```

The worker polls the `document_jobs` queue every `WORKER_POLL_INTERVAL_S`
seconds. `SIGINT` / `SIGTERM` triggers a clean drain: the in-flight message
finishes before the process exits. Docker stops the container with `SIGTERM`
by default, so `docker stop` / orchestrator shutdown is graceful.

---

## Deploy to Railway

The service is deployed as `document-worker` in the `heroic-grace` Railway
project. `railway.toml` lives in this directory and Railway reads it because
the service's `rootDirectory` is set to `services/doc-worker-v1`. Both the
Dockerfile and `railway.toml` are picked up relative to that subdirectory.

### First-time service setup (done once)

If you're wiring up a brand-new Railway service, do this from the monorepo
root:

```bash
railway link                  # pick project heroic-grace → production
railway service document-worker
```

Then set the service's `rootDirectory` to `services/doc-worker-v1` — either
via the Railway dashboard (**Settings → Source → Root directory**) or via
the GraphQL API:

```bash
curl -X POST https://backboard.railway.app/graphql/v2 \
  -H "Authorization: Bearer $(jq -r .user.accessToken ~/.railway/config.json)" \
  -H "Content-Type: application/json" \
  -d '{"query":"mutation { serviceInstanceUpdate(serviceId: \"<SERVICE_ID>\", environmentId: \"<ENV_ID>\", input: { rootDirectory: \"services/doc-worker-v1\" }) }"}'
```

### Environment variables

Populate the service with the same vars listed in the [Configuration](#configuration)
table. The `sb_secret_*`-format Supabase service-role key goes into
`SUPABASE_SERVICE_ROLE_KEY`. All `VLM_*` and `WORKER_*` vars follow the
defaults shown there.

```bash
railway variable set SUPABASE_URL='https://<project>.supabase.co' --service document-worker
railway variable set SUPABASE_SERVICE_ROLE_KEY='sb_secret_...' --service document-worker
railway variable set VLM_ENDPOINT_URL='https://<modal-app>/v1/chat/completions' --service document-worker
# ...and the rest
```

### Deploy

From `services/doc-worker-v1/`:

```bash
railway up --ci --service document-worker --environment production
```

`--ci` streams the build log and exits. A successful build ends with
`Build time: <N>s` and `Deploy complete`. Confirm it's polling with:

```bash
railway logs --service document-worker --environment production
# expect: "worker online; polling document_jobs every 5.0s"
```

### Dockerfile caveats on Railway

Railway rejects anonymous cache mounts — each `RUN --mount=type=cache` must
include an `id` prefixed with `s/<SERVICE_ID>-`. The mount IDs in this
Dockerfile are hardcoded to the production `document-worker` service ID. If
you fork this into a new service, update them or Railway will refuse the
build with *"Cache mount ID is not prefixed with cache key"*.

---

## Configuration

All config is env-driven. Required vars fail fast at startup with a clear
error listing what's missing.

| Var                            | Required | Default                              | Purpose                                                     |
| ------------------------------ | -------- | ------------------------------------ | ----------------------------------------------------------- |
| `SUPABASE_URL`                 | yes      | —                                    | Supabase project URL.                                       |
| `SUPABASE_SERVICE_ROLE_KEY`    | yes      | —                                    | Service-role key (bypasses RLS; the worker is trusted).     |
| `VLM_ENDPOINT_URL`             | yes      | —                                    | `https://…/v1/chat/completions` from the Modal deploy.      |
| `VLM_MODEL_NAME`               | no       | `ibm-granite/granite-docling-258M`   | Must match the deployed model.                              |
| `VLM_TIMEOUT_S`                | no       | `600`                                | Per-request HTTP timeout to the VLM.                        |
| `WORKER_POLL_INTERVAL_S`       | no       | `5`                                  | Sleep between empty-queue polls.                            |
| `WORKER_VISIBILITY_TIMEOUT_S`  | no       | `300`                                | See "Atomic assignment" below.                              |
| `WORKER_BATCH_SIZE`            | no       | `1`                                  | Messages read per poll. Processed sequentially.             |
| `WORKER_MAX_RETRIES`           | no       | `3`                                  | Attempts before archiving to dead-letter.                   |
| `LOG_LEVEL`                    | no       | `INFO`                               | Standard Python log level.                                  |

---

## Job lifecycle

### Atomic assignment

pgmq's `read(vt, qty)` returns messages and sets a visibility timeout (VT)
atomically. Once a worker has read a message, pgmq hides it from every other
reader until either the worker deletes it, archives it, or the VT expires.
**You can safely run N worker replicas against the same queue.**

`WORKER_VISIBILITY_TIMEOUT_S` must comfortably exceed worst-case processing
time. If the VT expires while a job is still running, a second worker will
grab it — work is duplicated but uploads are idempotent (upserts to the same
`{user_id}/{document_id}/filename.md` paths), so state stays consistent.
Still, tune this for your P99 document.

### Success path

1. Read one message, set `documents.status = 'processing'`.
2. Download the PDF from Supabase Storage.
3. Convert via Docling + Modal VLM.
4. Upload markdown + Docling JSON to `{user_id}/{document_id}/…`.
5. Update the row: `status = 'completed'`, `page_count`, `markdown_path`,
   `doc_json_path`.
6. Delete the pgmq message.

### Retry on transient failure

On any exception during steps 2–5 (download, VLM, upload, DB update), the
worker does **not** delete or archive the message. It returns, the VT
expires, pgmq re-presents the message to the next worker, and `msg.read_ct`
increments. The `documents` row is marked `retrying` with the failing error.

After `WORKER_MAX_RETRIES` attempts, the message is archived to the
dead-letter table `pgmq.a_document_jobs` for inspection, and the row is
marked `failed`.

### Permanent failure

A malformed payload (missing `document_id`, `user_id`, `file_path`, or
`filename`) can never succeed on retry, so the message is archived
immediately and no row is updated.

### Dead-letter inspection

```sql
select msg_id, message, read_ct, enqueued_at, archived_at
from pgmq.a_document_jobs
order by archived_at desc
limit 20;
```

---

## Layout

```
src/doc_worker/          worker library
  ├── __main__.py         python -m doc_worker
  ├── worker.py           polling loop + signal handling (doc-worker entry)
  ├── processor.py        per-job orchestration + retry logic
  ├── docling.py          VLM pipeline wrapper (build_converter, convert_pdf)
  ├── queue.py            pgmq read/delete/archive RPC wrappers
  ├── supabase.py         service-role client factory
  └── config.py           env-backed Config dataclass

src/vlm_endpoint/
  └── modal_app.py        Modal deployment (vLLM + granite-docling)

benchmarks/               VLM cold-start + throughput benchmarks
tests/                    one end-to-end integration test
Dockerfile                two-stage build → /opt/venv, non-root worker user
```

---

## Library use

The worker is importable, so you can embed it in another service without
running its polling loop:

```python
from doc_worker.config import load
from doc_worker.supabase import get_client
from doc_worker.docling import build_converter
from doc_worker.processor import process
from doc_worker.queue import read, Message

cfg = load()
client = get_client(cfg)
converter = build_converter(cfg)

for msg in read(client, visibility_timeout_s=300, qty=1):
    process(msg, client, converter, max_retries=3)
```

---

## Tests

One end-to-end integration test hits live Modal + Supabase, seeds a job
through the real queue, and asserts the resulting rows + storage objects.
Skipped by default.

```bash
# One-off run
export RUN_INTEGRATION_TESTS=1
export TEST_USER_ID=<uuid from auth.users>   # row FK on public.documents
uv run pytest -v
```

---

## Benchmarks

Measure cold-start, warm-request latency, and estimated Modal cost for the
deployed VLM:

```bash
uv run python benchmarks/benchmark_vlm_modal.py
BENCH_WARM_REQUESTS=20 BENCH_CONCURRENCY=4 uv run python benchmarks/benchmark_vlm_modal.py
```

See `benchmarks/benchmark_config.yaml` for all knobs.
