# Deploy doc-worker-v1 to Railway + End-to-End Verification Plan

## Overview

Replace the stale `document-worker` service in the `heroic-grace` Railway project with a fresh deployment of `services/doc-worker-v1`. Wire it to the existing OpenLearn Supabase project (`rrzbsueabbiesnmxkzya`) and the deployed Modal VLM endpoint (`vlm-endpoint-docworker-v1`). Run three end-to-end scenarios (happy-path, malformed-payload archive, transient-failure retry) against the live deployment to prove the pipeline works before calling it done.

## Current State Analysis

### Supabase (`rrzbsueabbiesnmxkzya` — OpenLearn)
- `public.documents` exists with the columns the worker writes (`status`, `page_count`, `markdown_path`, `doc_json_path`, `error_message`), but its status check constraint is `status = ANY(ARRAY['pending','processing','completed','failed'])` — it does **not** include the `'retrying'` state our refactored `processor.py` tries to write.
- pgmq is set up: `pgmq.q_document_jobs` (1 row currently) + `pgmq.a_document_jobs` (6 archived rows from prior runs).
- RPCs `pgmq_read(text, int, int)`, `pgmq_delete(text, bigint)`, `pgmq_archive(text, bigint)`, `enqueue_document_job(uuid, uuid, text, text, jsonb)` all exist.
- Storage bucket `documents` (private) exists.
- Test users seeded: `aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa` (testuser1@openlearn.test) and siblings `bbbb…`, `cccc…`.

### Modal
- App `vlm-endpoint-docworker-v1` is `deployed` (app-id `ap-NQplUlvnMglLvHW9hFhZAH`, deployed 2026-04-12 14:53 EDT).
- Public URL: `https://ottokunkel034--vlm-endpoint-docworker-v1-vllmserver-serve.modal.run`
- Worker needs `VLM_ENDPOINT_URL=<base>/v1/chat/completions`.

### Railway (`heroic-grace`, project-id `ef5e687e-6baa-4ea4-80db-7ee3cd047f3b`)
- Single service `document-worker` exists. Last deployment was SUCCESS on 2026-04-10 — but built from `services/document-worker/Dockerfile`, which was deleted today in commit `9cceaa0 delete old files`. The service will fail any new build until reconfigured.
- Environment variables currently include leftovers from the previous Qwen-based worker (`VLM_MODEL=qwen/qwen-vl-plus`, `VLM_CONCURRENCY`, `EMBEDDING_*`, `CHUNK_*`, `DATABASE_URL` pooler URI, `OPENROUTER_*`, `DOWNLOAD_DIR`) — none are consumed by the new codebase.
- `SUPABASE_SERVICE_KEY=sb_secret_REDACTED` is present. `sb_secret_*` is the modern Supabase service-role secret format, which is what the worker's `SUPABASE_SERVICE_ROLE_KEY` expects.
- Deployment config: `rootDirectory=null`, `dockerfilePath="services/document-worker/Dockerfile"`, `numReplicas=1`, `region=us-west2`, `restartPolicyType=ON_FAILURE`.

### Code (`services/doc-worker-v1`)
- `Dockerfile` exists, builds cleanly locally (~3.2 GB image), fails fast with clear env error on `docker run` without credentials.
- Console script `doc-worker` resolves to `doc_worker.worker:run` (see `pyproject.toml:19`).
- `src/doc_worker/processor.py:110-115` writes `status='retrying'` on transient failure — this will raise inside the except block against the current DB constraint (caught by the inner try/except but produces a noisy log line; retry semantics still work because pgmq VT expiry does the retry).
- No `railway.toml` exists yet.

### Key Discoveries
- **Service reuse is possible**: the existing `document-worker` service already has the correct `SUPABASE_URL`, and its `SUPABASE_SERVICE_KEY` value is the right secret — we just need to mirror it as `SUPABASE_SERVICE_ROLE_KEY` (the old service read `SUPABASE_SERVICE_KEY`, the new code reads `SUPABASE_SERVICE_ROLE_KEY`). Keeping the service identity means logs, metrics, and the service-ID stay stable.
- **Build context choice matters**: if we run `railway up` from the monorepo root, the Dockerfile's top-level `COPY pyproject.toml uv.lock ./` fails (those files live in `services/doc-worker-v1/`, not at monorepo root). Either `railway.toml` sets `build.rootDirectory`, or we deploy from the service subdir directly. We'll deploy from the subdir — it's simpler and `railway.toml` in that same dir controls the builder.
- **pgmq retry semantics are already correct**: because we don't `delete` or `archive` on transient failure, pgmq re-presents the message after VT expiry. The bug is purely that the status-update inside the error handler is rejected — fixing the constraint unblocks the nicer observability of `retrying` state.
- **`processor.process(max_retries=3)` uses `msg.read_ct`**, which pgmq increments on every `read`. First read → `read_ct=1`, so attempt=1; after the third delivery `attempt >= 3` and we archive. The VT is 300s by default, so a retry loop takes ~10 min worst case to reach the dead-letter.

## Desired End State

After executing this plan:

- The Railway `document-worker` service in `heroic-grace` is running the code from `services/doc-worker-v1/`, polling `pgmq.q_document_jobs` on the OpenLearn Supabase project, and calling the `granite-docling` VLM on Modal.
- The `documents.status` check constraint includes `'retrying'` so transient-failure state transitions don't noise up the logs.
- All three of the following have been verified live:
  1. **Happy path**: enqueue → worker picks up → `documents.status='completed'` → `.md` + `.doctags.json` present in storage.
  2. **Malformed payload**: send a pgmq message missing required keys → worker archives it to `pgmq.a_document_jobs` with `read_ct=1` → no `documents` row is updated.
  3. **Transient failure retry**: enqueue a job whose `file_path` doesn't exist in storage → worker retries until `read_ct >= 3` → final state is archived message + `documents.status='failed'` + `error_message` includes the storage error.
- Test artifacts (seeded rows, storage objects, extra pgmq messages) are cleaned up.

### How to verify
- `mcp__Railway__list-deployments` for `document-worker` shows a SUCCESS deployment newer than any 2026-04-10 record.
- `mcp__Railway__get-logs` shows `worker online; polling document_jobs every 5.0s` and later `completed document_id=… pages=…` for the happy-path test.
- `mcp__supabase__execute_sql` confirms the expected final row states in `public.documents` and `pgmq.a_document_jobs`.

## What We're NOT Doing

- Not touching the `merry-youthfulness` or `your balls` Railway projects.
- Not renaming or recreating the Railway service — reusing `document-worker` per user decision.
- Not migrating any existing `public.documents` rows. Only altering the status check constraint.
- Not introducing GitHub auto-deploy / CI. Deploys are done via `mcp__Railway__deploy` (i.e. `railway up`) from this plan.
- Not increasing replicas. `numReplicas=1`.
- Not adjusting Modal's `SCALEDOWN_WINDOW` or replica config — only calling the deployed endpoint.
- Not cleaning up the 6 pre-existing rows in `pgmq.a_document_jobs` from prior worker runs. They're historical dead-letters, out of scope.
- Not adding any new test fixtures beyond the `tests/fixtures/gaussians.pdf` already in the repo.

## Implementation Approach

Six phases, each small and independently verifiable:

1. **Migration**: extend the `documents.status` check constraint to include `'retrying'`.
2. **Railway config file**: add `railway.toml` to `services/doc-worker-v1/` so Railway builds against the local Dockerfile.
3. **Reset Railway env vars**: clear the stale Qwen/embedding vars, set the four the new worker actually reads.
4. **Deploy**: `railway up` from the service workspace, watch the build to SUCCESS.
5. **Verify polling**: tail deploy logs until the worker prints "worker online".
6. **E2E tests**: run the three live scenarios, assert via MCP SQL + log queries, then clean up.

Each phase's success criteria are concrete MCP / CLI calls, so the whole plan is executable via tools.

---

## Phase 1: Migration — add `'retrying'` to the status check constraint

### Overview
The worker's retry path writes `status='retrying'` to `public.documents`. The current check constraint rejects that. Extend the constraint.

### Changes Required

#### 1. Apply migration via Supabase MCP

Call `mcp__supabase__apply_migration` with project_id `rrzbsueabbiesnmxkzya`, name `add_retrying_to_documents_status_check`, and query:

```sql
ALTER TABLE public.documents DROP CONSTRAINT IF EXISTS documents_status_check;
ALTER TABLE public.documents
  ADD CONSTRAINT documents_status_check
  CHECK (status = ANY (ARRAY['pending'::text, 'processing'::text, 'retrying'::text, 'completed'::text, 'failed'::text]));
```

### Success Criteria

#### Automated Verification
- [x] Migration applies without error (MCP returns success).
- [x] Verification query returns the new constraint definition:
  ```sql
  SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'documents_status_check';
  ```
  Expected: a definition containing `'retrying'`.
- [x] Sanity check that existing rows still pass the constraint:
  ```sql
  SELECT status, count(*) FROM public.documents GROUP BY status;
  ```
  (No rows in an invalid state; no error.)

#### Manual Verification
- [ ] No user-facing behavior to verify — a schema-only change. Proceeding directly to Phase 2.

---

## Phase 2: Railway config file (`railway.toml`)

### Overview
Add a `railway.toml` at `services/doc-worker-v1/railway.toml` so that when we run `railway up` from that directory, Railway builds against the local Dockerfile (not the now-deleted `services/document-worker/Dockerfile` path stored on the service).

### Changes Required

#### 1. Create `services/doc-worker-v1/railway.toml`

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

Notes:
- `builder = "DOCKERFILE"` overrides the service's inherited builder config.
- `dockerfilePath = "Dockerfile"` is relative to the uploaded build context (i.e. `services/doc-worker-v1/`).
- `sleepApplication = false` — the worker is a polling loop, not a request responder. It must not be slept.
- `numReplicas = 1` per user decision.
- Region is left at the service's existing `us-west2` (matches Supabase `us-west-2`).

### Success Criteria

#### Automated Verification
- [x] File exists: `ls services/doc-worker-v1/railway.toml` succeeds.
- [x] TOML parses (no tool needed — Railway will complain on deploy if invalid; we catch that in Phase 4).

#### Manual Verification
- [ ] Nothing deployed yet — proceeding to Phase 3.

---

## Phase 3: Reset Railway environment variables

### Overview
The `document-worker` service currently carries ~16 env vars from the deleted Qwen/embedding worker. We keep only the ones the new worker uses, and add the missing ones.

### Variables to keep (already correct)
- `SUPABASE_URL=https://rrzbsueabbiesnmxkzya.supabase.co`

### Variables to add
- `SUPABASE_SERVICE_ROLE_KEY` — copy from the existing `SUPABASE_SERVICE_KEY` value.
- `VLM_ENDPOINT_URL=https://ottokunkel034--vlm-endpoint-docworker-v1-vllmserver-serve.modal.run/v1/chat/completions`
- `VLM_MODEL_NAME=ibm-granite/granite-docling-258M`
- `VLM_TIMEOUT_S=600`
- `WORKER_VISIBILITY_TIMEOUT_S=300`
- `WORKER_BATCH_SIZE=1`
- `WORKER_POLL_INTERVAL_S=5`
- `WORKER_MAX_RETRIES=3`
- `LOG_LEVEL=INFO`

### Variables to remove (stale from old worker; worker doesn't read them — they're harmless but noise)
`SUPABASE_SERVICE_KEY`, `DATABASE_URL`, `DOCUMENT_TIMEOUT_SECONDS`, `DOWNLOAD_DIR`, `EMBEDDING_API_KEY`, `EMBEDDING_API_URL`, `EMBEDDING_BATCH_SIZE`, `EMBEDDING_DIMENSIONS`, `EMBEDDING_MODEL`, `OPENROUTER_API_KEY`, `OPENROUTER_BASE_URL`, `VLM_CONCURRENCY`, `VLM_MAX_TOKENS`, `VLM_MODEL`, `CHUNK_BATCH_SIZE`.

We'll leave the `RAILWAY_*` auto-managed vars alone (Railway owns those).

### Changes Required

#### 1. Set the new variables

Call `mcp__Railway__set-variables` with `workspacePath=/Users/ottokunkel/Documents/Development/OpenLearn/services/doc-worker-v1`, `service=document-worker`, `skipDeploys=true` (we'll deploy in Phase 4 explicitly), and `variables`:

```
SUPABASE_SERVICE_ROLE_KEY=sb_secret_REDACTED
VLM_ENDPOINT_URL=https://ottokunkel034--vlm-endpoint-docworker-v1-vllmserver-serve.modal.run/v1/chat/completions
VLM_MODEL_NAME=ibm-granite/granite-docling-258M
VLM_TIMEOUT_S=600
WORKER_VISIBILITY_TIMEOUT_S=300
WORKER_BATCH_SIZE=1
WORKER_POLL_INTERVAL_S=5
WORKER_MAX_RETRIES=3
LOG_LEVEL=INFO
```

#### 2. Remove stale variables

Via `railway variables --remove <NAME>` (Bash) or the dashboard. The MCP doesn't expose an unset tool directly, so we'll use `railway variables --remove` through Bash for each of the 15 vars listed above. Example:

```bash
cd services/doc-worker-v1 && \
  for v in SUPABASE_SERVICE_KEY DATABASE_URL DOCUMENT_TIMEOUT_SECONDS DOWNLOAD_DIR \
           EMBEDDING_API_KEY EMBEDDING_API_URL EMBEDDING_BATCH_SIZE \
           EMBEDDING_DIMENSIONS EMBEDDING_MODEL OPENROUTER_API_KEY \
           OPENROUTER_BASE_URL VLM_CONCURRENCY VLM_MAX_TOKENS VLM_MODEL CHUNK_BATCH_SIZE; do
    railway variables --remove "$v" --service document-worker --skip-deploys || true
  done
```

### Success Criteria

#### Automated Verification
- [x] `mcp__Railway__list-variables` (service=document-worker, json=true) shows every required variable listed under "Variables to add" above, each with a non-empty value.
- [x] None of the "Variables to remove" remain in the output (ignoring the `RAILWAY_*` auto-managed vars).
- [x] `SUPABASE_URL` is unchanged.

#### Manual Verification
- [ ] No live behaviour yet — proceeding to Phase 4 to deploy.

---

## Phase 4: Deploy the worker

### Overview
Trigger a build + deploy from the `services/doc-worker-v1/` workspace. Watch logs until build is SUCCESS.

### Changes Required

#### 1. Deploy via MCP

Call `mcp__Railway__deploy` with:
- `workspacePath=/Users/ottokunkel/Documents/Development/OpenLearn/services/doc-worker-v1`
- `service=document-worker`
- `environment=production`
- `ci=true` (stream build logs only, then exit — better than interactive for our purposes)

The MCP internally runs `railway up --service document-worker` from the workspace, which uploads the directory as the build context and triggers a Dockerfile build using the `railway.toml` created in Phase 2.

#### 2. If build fails

- Read the failure via `mcp__Railway__get-logs` with `logType=build` and the failing `deploymentId`.
- Likely failure modes to diagnose (most-to-least probable):
  - Missing `uv.lock` in context (`.dockerignore` is too aggressive) — inspect `.dockerignore`.
  - `uv sync --frozen --no-dev --no-editable` fails due to a lockfile drift. Verify lockfile is committed and matches `pyproject.toml`.
  - Layer cache for `ghcr.io/astral-sh/uv:0.5.11` missing — retry.
- Fix and re-deploy.

### Success Criteria

#### Automated Verification
- [x] `mcp__Railway__list-deployments` (service=document-worker, limit=1, json=true) shows a deployment with `status=SUCCESS` and a `createdAt` newer than any 2026-04-10 record.
- [x] `mcp__Railway__get-logs` (logType=build, lines=50) for the deployment contains `Successfully tagged` or the equivalent Railway "build complete" marker (no red error text).

#### Manual Verification
- [ ] Proceed to Phase 5 to confirm the process is actually running and polling.

---

## Phase 5: Verify the worker is online and polling

### Overview
A SUCCESS build only means the image was built. We must also confirm the container started, loaded config, and is polling pgmq.

### Changes Required

#### 1. Read deploy logs

Call `mcp__Railway__get-logs` with:
- `workspacePath=/Users/ottokunkel/Documents/Development/OpenLearn/services/doc-worker-v1`
- `service=document-worker`
- `logType=deploy`
- `lines=100`

Expected log lines (from `src/doc_worker/worker.py`):
```
INFO doc_worker worker online; polling document_jobs every 5.0s
```
…followed by periodic silence (polling; no messages).

#### 2. If logs show errors

- `RuntimeError: Missing required env vars: …` → env var not propagated; re-check Phase 3 output.
- `httpx.ConnectError` / `httpx.ReadTimeout` against Supabase → service role key wrong (401/403) or network egress blocked.
- `ConnectionError` to Modal → URL wrong, or Modal app deleted / sleeping and timing out.

Diagnose via `mcp__Railway__get-logs` with `filter=error` to narrow down.

### Success Criteria

#### Automated Verification
- [x] Deploy logs contain the exact string `worker online; polling document_jobs`.
- [x] No log line at `@level:error` or `@level:warn` in the first 60 seconds after boot (filter with `lines=100 filter=@level:error`).
- [x] The deployment status stays SUCCESS (no crash loop) for ≥ 60s.

#### Manual Verification
- [ ] Proceeding to Phase 6 to drive real jobs through the worker.

**Implementation Note**: After confirming the worker is online, pause for my confirmation before proceeding to Phase 6, since Phase 6 creates real DB rows and storage objects and makes live Modal calls (cost).

---

## Phase 6: End-to-end tests (live)

### Overview
Three scenarios against the deployed worker. Each seeds just enough to drive one message through and each has an explicit teardown. Assertions are made via `mcp__supabase__execute_sql` and `mcp__Railway__get-logs`.

Constants used below:
- `USER = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'`
- Fixture PDF: `services/doc-worker-v1/tests/fixtures/gaussians.pdf` (fetched by `conftest.py` on first run from `https://cs229.stanford.edu/section/gaussians.pdf`; copy to Supabase Storage via a signed upload — steps in each scenario).

### Scenario A — Happy path

#### Setup
1. Generate `DOC_ID_A = gen_random_uuid()` client-side (e.g., `python -c "import uuid; print(uuid.uuid4())"`).
2. Upload the fixture PDF to storage at `{USER}/{DOC_ID_A}/gaussians.pdf` via `supabase.storage.from_('documents').upload(...)` (scripted with a small Python one-liner using `supabase-py` — credentials from env or from Railway variables).
3. Insert the `documents` row:
   ```sql
   INSERT INTO public.documents (id, user_id, filename, file_path, status, metadata)
   VALUES (
     '<DOC_ID_A>',
     'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
     'gaussians.pdf',
     'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa/<DOC_ID_A>/gaussians.pdf',
     'pending',
     '{"source": "e2e-scenario-a"}'
   );
   ```
4. Enqueue the job:
   ```sql
   SELECT public.enqueue_document_job(
     '<DOC_ID_A>'::uuid,
     'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'::uuid,
     'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa/<DOC_ID_A>/gaussians.pdf',
     'gaussians.pdf',
     '{"source": "e2e-scenario-a"}'::jsonb
   );
   ```

#### Polling for completion

Poll (every 10s, up to 10 min — Modal cold start ~30-120s, gaussians.pdf is small so VLM ~1-3 min):
```sql
SELECT status, page_count, error_message, markdown_path, doc_json_path
FROM public.documents WHERE id = '<DOC_ID_A>';
```

#### Assertions
- `status = 'completed'`
- `page_count BETWEEN 8 AND 16` (same range the existing integration test uses)
- `markdown_path = 'aaaa…/<DOC_ID_A>/gaussians.pdf.md'`
- `doc_json_path = 'aaaa…/<DOC_ID_A>/gaussians.pdf.doctags.json'`
- Storage contains both files (verify via `storage.objects` query):
  ```sql
  SELECT name FROM storage.objects
  WHERE bucket_id = 'documents'
    AND name LIKE 'aaaaaaaa-%/<DOC_ID_A>/%';
  ```
  Expect three rows (original PDF, .md, .doctags.json).
- pgmq message is gone from `q_document_jobs` and NOT in `a_document_jobs`:
  ```sql
  SELECT msg_id FROM pgmq.q_document_jobs WHERE (message->>'document_id')::uuid = '<DOC_ID_A>';
  -- expect 0 rows
  SELECT msg_id FROM pgmq.a_document_jobs WHERE (message->>'document_id')::uuid = '<DOC_ID_A>';
  -- expect 0 rows
  ```
- Railway logs contain `processing document_id=<DOC_ID_A>` and later `completed document_id=<DOC_ID_A> pages=…`.

#### Teardown
```sql
DELETE FROM public.documents WHERE id = '<DOC_ID_A>';
```
Storage: remove all three objects under `{USER}/{DOC_ID_A}/` via `storage.from_('documents').remove([...])`.

---

### Scenario B — Malformed payload

#### Setup
Enqueue a message directly into pgmq bypassing the helper, with a payload missing `file_path`:
```sql
SELECT pgmq.send(
  queue_name := 'document_jobs',
  msg := jsonb_build_object(
    'document_id', gen_random_uuid(),
    'user_id', 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
    -- note: 'file_path' and 'filename' intentionally omitted
  )
);
```
Capture the returned `msg_id` as `MSG_ID_B`.

(No `documents` row is inserted — the worker should never touch the DB for a malformed payload.)

#### Polling

Wait up to 60s (poll interval is 5s, so worker will read + archive within that window):
```sql
SELECT msg_id, read_ct, message FROM pgmq.a_document_jobs WHERE msg_id = <MSG_ID_B>;
```

#### Assertions
- A row exists in `pgmq.a_document_jobs` with `msg_id = MSG_ID_B` and `read_ct = 1` (archived on the first delivery — malformed-payload handler does not retry).
- `pgmq.q_document_jobs` has no row with this `msg_id`.
- No row in `public.documents` matches the payload's `document_id` (no DB activity triggered).
- Railway logs contain `bad payload, archiving msg_id=<MSG_ID_B>`.

#### Teardown
```sql
DELETE FROM pgmq.a_document_jobs WHERE msg_id = <MSG_ID_B>;
```

---

### Scenario C — Transient failure retry then dead-letter

#### Setup
Create a documents row and enqueue a job whose `file_path` doesn't exist in storage. Each attempt will raise a storage-download error in `processor.process`, which is caught and treated as transient. Because `WORKER_MAX_RETRIES=3`, the message should be archived on the 3rd delivery.

1. Generate `DOC_ID_C = gen_random_uuid()`.
2. Insert row:
   ```sql
   INSERT INTO public.documents (id, user_id, filename, file_path, status, metadata)
   VALUES (
     '<DOC_ID_C>',
     'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
     'nonexistent.pdf',
     'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa/<DOC_ID_C>/nonexistent.pdf',
     'pending',
     '{"source": "e2e-scenario-c"}'
   );
   ```
3. Enqueue:
   ```sql
   SELECT public.enqueue_document_job(
     '<DOC_ID_C>'::uuid,
     'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'::uuid,
     'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa/<DOC_ID_C>/nonexistent.pdf',
     'nonexistent.pdf',
     '{"source": "e2e-scenario-c"}'::jsonb
   );
   ```

#### Polling (runs long — ~3× VT)

At `WORKER_VISIBILITY_TIMEOUT_S=300`, three attempts means the worst-case run time is ~15 minutes before the message lands in the dead letter. To keep this plan tractable, we'll **temporarily lower the visibility timeout** for this scenario: set `WORKER_VISIBILITY_TIMEOUT_S=30` via `mcp__Railway__set-variables` before kicking off the scenario (which triggers an auto-redeploy) — total runtime drops to ~100 seconds. Restore to `300` after the scenario.

Poll every 15s, up to 5 min:
```sql
-- Watch the row state progression: processing → retrying → failed
SELECT status, error_message FROM public.documents WHERE id = '<DOC_ID_C>';

-- Watch the queue: while retrying, row stays in q_document_jobs; at the end it moves to a_document_jobs
SELECT msg_id, read_ct FROM pgmq.q_document_jobs
  WHERE (message->>'document_id')::uuid = '<DOC_ID_C>';
SELECT msg_id, read_ct FROM pgmq.a_document_jobs
  WHERE (message->>'document_id')::uuid = '<DOC_ID_C>';
```

#### Assertions
- Final state: `public.documents.status = 'failed'` with `error_message` containing a storage-download failure signal (e.g. "Object not found", "404", or similar; the exact string depends on supabase-py's error message — we'll match on presence of `nonexistent.pdf` or keyword `404`/`not found`).
- Final pgmq state: 1 row in `pgmq.a_document_jobs` with `read_ct = 3`; 0 rows in `pgmq.q_document_jobs`.
- Intermediate state (caught during polling at least once): `documents.status = 'retrying'` with `error_message` starting with `attempt 1/3:` or `attempt 2/3:`.
- Railway logs contain three distinct `job failed on attempt N/3` lines and one `job failed after 3/3 attempts, archiving` line.

#### Teardown
```sql
DELETE FROM public.documents WHERE id = '<DOC_ID_C>';
DELETE FROM pgmq.a_document_jobs WHERE msg_id = <MSG_ID_C>;
```
Restore `WORKER_VISIBILITY_TIMEOUT_S=300` via `mcp__Railway__set-variables`.

### Success Criteria

#### Automated Verification
- [x] Scenario A: all assertions above pass. Final `documents.status='completed'`, storage has .md + .doctags.json, no pgmq residue.
- [x] Scenario B: `pgmq.a_document_jobs` has the malformed message with `read_ct=1`, no `documents` row created, logs show the archive line.
- [x] Scenario C: `documents.status='failed'` with error_message referencing the missing file; `pgmq.a_document_jobs` has the message with `read_ct=3`; logs show 3 retry attempts and the final archive.
- [x] After teardown in all three scenarios, the following return 0 rows:
  ```sql
  SELECT count(*) FROM public.documents WHERE metadata->>'source' LIKE 'e2e-scenario-%';
  SELECT count(*) FROM pgmq.q_document_jobs WHERE (message->>'metadata'->>'source') LIKE 'e2e-scenario-%';
  SELECT count(*) FROM pgmq.a_document_jobs WHERE (message->>'metadata'->>'source') LIKE 'e2e-scenario-%';
  ```
- [x] `WORKER_VISIBILITY_TIMEOUT_S` restored to `300` (confirm via `mcp__Railway__list-variables`).

#### Manual Verification
- [ ] (Optional) I download the produced markdown artifact from Scenario A and spot-check that it contains the string "multivariate" or "covariance" or "gaussian" — i.e. the worker actually ran the VLM, not just rubber-stamped a row.

**Implementation Note**: Scenario C lowers+restores `WORKER_VISIBILITY_TIMEOUT_S`. If Scenario C fails partway, the VT may be left at 30 seconds, which is too short for real workloads. The teardown restores it unconditionally, but the plan should double-check the final value before calling the phase done.

---

## Testing Strategy

### Automated (in-plan)
- Migration: SQL query to verify constraint definition.
- Deployment: Railway deployment status + log-tail for boot message.
- E2E: three black-box scenarios driven entirely by MCP calls (SQL + Railway API).

### What we're deliberately not doing
- Not re-running `tests/test_integration.py` against the deployed worker. That test uses `processor.process()` synchronously against a local Python process — it's the unit-of-code-level integration test. The three live scenarios in Phase 6 supersede it for the deployed-worker check.
- Not load-testing. One-at-a-time is the worker's design; any concurrency would require `WORKER_BATCH_SIZE>1` and multiple replicas, both explicitly out of scope.
- Not fault-injecting Modal (e.g. pause the Modal app mid-job). `granite-docling` cold starts naturally exercise the VLM call path; Scenario C's storage-404 covers the error path we most care about.

## Performance Considerations

- Cold-start budget: Modal is configured with `MIN_CONTAINERS=0`, so Scenario A's first call will trigger a cold boot (~30-120s). Scenario A's 10-min timeout comfortably covers this.
- The 3.2 GB image size means first deploy will spend a few minutes pushing to Railway's registry. This is a one-time cost per deploy.
- `WORKER_VISIBILITY_TIMEOUT_S=300` is comfortably above Modal cold-start + typical conversion time for documents up to ~30 pages. If we start seeing slow documents time out, bump this.

## Migration Notes

- Phase 1 schema migration is additive (only widens the allowed value set). No existing data is at risk, no rollback needed; to roll back, drop and re-add the constraint with the original value list.
- Railway service replacement is in place: the service ID `e3449a2d-58f7-48f3-a848-055059346fd1` is preserved, only its build source + env vars change. Downstream anything referencing that service URL/ID continues to work.

## References

- Current state exploration: live via Supabase MCP, Railway MCP, Modal SDK (this session).
- Worker code: `services/doc-worker-v1/src/doc_worker/`
  - Retry logic: `processor.py:55-130`
  - Boot/polling: `worker.py:11-52`
  - Env config: `config.py:23-47`
- Dockerfile: `services/doc-worker-v1/Dockerfile`
- Prior refactor plan (Phase 1 done): `thoughts/shared/plans/2026-04-12-simplify-doc-worker-v1.md`
- Modal app: `services/doc-worker-v1/src/vlm_endpoint/modal_app.py`
- pgmq queue + RPCs: already provisioned in Supabase project `rrzbsueabbiesnmxkzya`.
