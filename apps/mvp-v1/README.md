# mvp-v1

Small React client for OpenLearn that lets a signed-in user upload PDFs, watch
processing status update live, and download the resulting artifacts
(markdown / docling_json / doctags). Targets the `doc-worker-v2` stack — inserts
directly into `doc_worker_v2.documents`; the `AFTER INSERT` trigger enqueues
into `doc_ingest` and the deployed Railway worker processes the job.

## Setup

Vite reads env from the **repo root** `.env`. Add two `VITE_`-prefixed mirrors
alongside the existing unprefixed keys (copy the same values):

```bash
VITE_SUPABASE_URL=<same value as SUPABASE_URL>
VITE_SUPABASE_ANON_KEY=<same value as SUPABASE_ANON_KEY>
```

> Never add `VITE_` mirrors of `SUPABASE_SERVICE_ROLE_KEY` or the S3 keys —
> Vite's `VITE_` prefix is the security boundary keeping them out of the
> browser bundle.

Install and run:

```bash
cd apps/mvp-v1
npm install
npm run dev   # http://localhost:5173
```

## Credentials

Sign in with any Supabase auth user on this project. Test users provisioned per
the repo README: `testuser1@openlearn.test` / `TestPassword123!` (also 2 and 3).

## Related

- Implementation plan: `thoughts/shared/plans/2026-04-14-mvp-v1-upload-progress-download.md`
- Backend: `services/doc-worker-v2/`
- Admin app (v1 reader): `apps/admin/`
