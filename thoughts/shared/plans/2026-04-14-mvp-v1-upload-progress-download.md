---
date: 2026-04-14
author: otto
git_commit: ff30ca32b6040ab5714c2ca757015e0d961a4c5e
branch: main
repository: OpenLearn
topic: "mvp-v1: upload → progress → download frontend"
tags: [plan, mvp-v1, react, vite, supabase, doc-worker-v2, realtime]
status: complete
---

# mvp-v1 — Upload, Track, Download Frontend

## Overview

Build `apps/mvp-v1/` — a small React (Vite + TS + Tailwind + shadcn) client that lets a single authenticated user (1) upload local PDFs, (2) watch each document's processing status update in real-time, and (3) download the produced artifacts (markdown, docling_json, doctags) once processing completes. Targets the **doc-worker-v2 stack** directly (inserts into `doc_worker_v2.documents`; the AFTER INSERT trigger enqueues into `doc_ingest` and the already-deployed Railway worker processes it). Reads config from the repo-root `.env`.

## Current State Analysis

- **v2 stack is ready for a client but has none.** `doc_worker_v2.documents` + `AFTER INSERT` trigger + `doc_ingest` queue + Railway worker + Modal VLM are all deployed. The plan `thoughts/shared/plans/2026-04-14-doc-worker-v2-modular-architecture.md` explicitly marks "client insert into v2" as out of scope — this MVP provides it.
- **Storage bucket `documents`** (private) exists with per-user folder RLS: `bucket_id = 'documents' AND auth.uid()::text = (storage.foldername(name))[1]` (`supabase/migrations/001_create_tables.sql:173-189`). That policy was created alongside v1 but applies to any user — v2 uploads into the same bucket under `{user.id}/{doc_id}/...`.
- **`doc_worker_v2.documents` RLS** requires `auth.uid() = user_id` for `select/insert/update/delete` (`supabase/migrations/006_doc_worker_v2.sql:44-54`). Authenticated role has `select, insert, update, delete` grants. The AFTER INSERT trigger fires as the inserting user, then the SECURITY DEFINER `enqueue_on_insert` function calls `pgmq.send`.
- **Admin app pattern exists and is lift-ready.** `apps/admin/` uses Vite 8 + React 19.2 + TS + Tailwind 4 + shadcn + `@supabase/supabase-js` + React Router 7.14. The supabase client (`apps/admin/src/lib/supabase.ts`), auth hook (`apps/admin/src/hooks/use-auth.ts:16-45`), auth-guard (`apps/admin/src/components/layout/auth-guard.tsx:4-20`), login page (`apps/admin/src/pages/login.tsx`), and routing (`apps/admin/src/App.tsx`) are all reusable patterns.
- **Realtime is NOT enabled** on any v2 tables — grep for `supabase_realtime`, `publication`, or `ALTER PUBLICATION` across `supabase/migrations/` returns zero matches. Supabase's default `supabase_realtime` publication must have `doc_worker_v2.documents` added explicitly before UPDATE events stream to subscribed clients.
- **Env state**: repo-root `.env` has unprefixed keys (`SUPABASE_URL`, `SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_ROLE_KEY`, S3 keys, `MODAL_WEB_URL`). Admin uses `apps/admin/.env.local` with its own `VITE_SUPABASE_URL` / `VITE_SUPABASE_ANON_KEY` pair — Vite requires the `VITE_` prefix to expose a var to the browser and refuses to serve service-role keys by default, which is exactly the security boundary we want.

## Desired End State

A developer (or end user) can:

1. `cd apps/mvp-v1 && npm install && npm run dev` — Vite dev server starts on port 5173, reads the repo-root `.env`, and no missing-env warnings appear.
2. Open `http://localhost:5173`, get redirected to `/login`, sign in with an existing Supabase user (`testuser1@openlearn.test` / `TestPassword123!`).
3. Land on `/` — an upload panel plus a list of the signed-in user's prior documents (empty on first run).
4. Drag a PDF (or click to pick) — it appears in the list with status `pending`, transitions to `processing` → (optionally `retrying`) → `completed` without the user refreshing.
5. Once `completed`, one download button per artifact key (e.g., `markdown`, `docling_json`, `doctags`) appears beside the row; clicking saves the file locally.
6. If processing fails, the row shows `failed` with the worker's truncated error message (≤1000 chars).
7. Sign out returns the user to `/login`.

### Key Discoveries:

- `doc_worker_v2.documents` trigger chain at `supabase/migrations/006_doc_worker_v2.sql:82-105` is atomic — `INSERT` + `pgmq.send` happen in one transaction, no separate RPC needed from the client (unlike v1's `enqueue_document_job`).
- `supabase-js` supports setting a default schema globally via `createClient(url, key, { db: { schema: "doc_worker_v2" } })`; `.from("documents")` then hits the v2 table without per-call `.schema()` prefix.
- `supabase-js` Realtime: `supabase.channel("docs-for-user").on("postgres_changes", { event: "*", schema: "doc_worker_v2", table: "documents", filter: `user_id=eq.${user.id}` }, handler).subscribe()` streams inserts, updates, deletes to the client. Requires the table to be in the `supabase_realtime` publication.
- `supabase.storage.from("documents").createSignedUrl(path, expiresIn)` respects RLS at URL-generation time (the caller's JWT must allow `SELECT` on the storage object), then produces a short-lived public URL the browser can hit directly — no per-download auth shuffle needed.
- Admin's supabase client creates one global instance (`apps/admin/src/lib/supabase.ts:1-6`); we do the same but pin the schema to `doc_worker_v2`.
- Admin's login form uses `supabase.auth.signInWithPassword({ email, password })` (`apps/admin/src/hooks/use-auth.ts:36`); we mirror it verbatim since the session is managed by the same Supabase project.
- Artifact extensions are defined in `services/doc-worker-v2/src/doc_worker_v2/pipelines/_common.py:18-42` — `markdown → md`, `docling_json → docling.json`, `doctags → doctags.xml`. The worker stores them in `artifacts` jsonb as `{"markdown": "<user>/<doc>/<filename>.md", ...}`. MVP iterates whatever keys exist — not every pipeline emits doctags (granite does, generic markdown and standard_cpu don't).

## What We're NOT Doing

- **Not** touching v1 paths (`public.documents`, `upload-document` edge function, `apps/admin/`). The admin app keeps reading v1. v2 MVP is purely additive.
- **Not** building admin/multi-user views (per-user only; `auth.uid() = user_id` RLS enforces this naturally).
- **Not** implementing pipeline switcher UI — pipeline is fixed at the worker level via env (`PIPELINE=granite_docling_vlm` in Railway).
- **Not** adding a PDF preview, chunk viewer, or bbox overlay (admin covers that).
- **Not** adding upload cancel / resume, chunked uploads, or progress-bar for the upload itself — Supabase storage uploads are fire-and-forget for files ≤50 MB and finish in seconds on typical connections.
- **Not** building a separate seeded-user setup — we rely on the existing test users (`testuser1/2/3@openlearn.test` / `TestPassword123!`) already provisioned per repo README.
- **Not** writing unit tests for the UI (MVP); relying on manual verification. Type-check + lint + build are the automated gates.
- **Not** deploying — `dev` / `preview` locally only. Railway or Vercel wiring is a future phase.
- **Not** adding retry/cancel buttons. If a job fails the user re-uploads.

## Implementation Approach

Phase 1 lands the only database change (enabling Realtime for the v2 table) — smallest blast radius first, unblocks the UI to receive UPDATE events. Phase 2 scaffolds the app skeleton (Vite + React + Tailwind + shadcn), wires the repo-root `.env` through Vite's `envDir`, and adds the supabase client pinned to the v2 schema — gates every later phase on "app boots, types pass". Phase 3 adds auth (reuse admin's pattern verbatim) so every subsequent phase executes as an authenticated user with a real JWT (RLS is load-bearing). Phase 4 adds the upload flow — the happy-path insert that exercises the trigger end-to-end. Phase 5 adds the list + Realtime subscription so the user can see their work progress. Phase 6 adds signed-URL artifact download — the endpoint of the user journey. Each phase is independently verifiable.

---

## Phase 1: Enable Realtime for `doc_worker_v2.documents`

### Overview
Add `doc_worker_v2.documents` to the `supabase_realtime` publication so `UPDATE` / `INSERT` / `DELETE` events stream to subscribed clients. Without this, the UI will be stuck at `pending` forever — the worker flips status server-side but no event reaches the browser.

### Changes Required:

#### 1. New migration
**File**: `supabase/migrations/007_doc_worker_v2_realtime.sql`
**Changes**: Add the v2 documents table to the default Supabase realtime publication. Guarded so it can be re-run against a database where it's already added (e.g., after a remote restore).

```sql
-- ============================================================
-- Enable Realtime for doc_worker_v2.documents
-- ============================================================
-- The Supabase-managed `supabase_realtime` publication streams
-- row-level changes to connected clients via the Realtime service.
-- We only need row-state changes (status, artifacts, error_message),
-- not DDL.

do $$
begin
    if not exists (
        select 1
        from pg_publication_tables
        where pubname = 'supabase_realtime'
          and schemaname = 'doc_worker_v2'
          and tablename = 'documents'
    ) then
        execute 'alter publication supabase_realtime add table doc_worker_v2.documents';
    end if;
end $$;
```

#### 2. Apply migration
**Command**: use the existing Supabase MCP flow (the same one that applied `006`) against project `rrzbsueabbiesnmxkzya`. The `do` block is idempotent.

### Success Criteria:

#### Automated Verification:
- [x] Migration file exists at `supabase/migrations/007_doc_worker_v2_realtime.sql`
- [x] `psql` query `select * from pg_publication_tables where pubname='supabase_realtime' and schemaname='doc_worker_v2' and tablename='documents';` returns one row
- [x] Re-running the migration produces no error (idempotent)

#### Manual Verification:
- [x] From the Supabase dashboard → Database → Publications → `supabase_realtime`, `doc_worker_v2.documents` is listed (verified via `pg_publication_tables` query)
- [x] WebSocket delivery will be verified end-to-end in Phase 5 (couldn't test via MCP; SQL confirms publication config: `pubinsert/update/delete=true`, REPLICA IDENTITY = default which is sufficient for the INSERT/UPDATE events the UI needs)

**Implementation Note**: Pause here for manual confirmation that the subscription fires before moving on — if Realtime isn't streaming, Phase 5 will silently regress to "list only updates on refresh".

---

## Phase 2: Scaffold `apps/mvp-v1/`

### Overview
Create a new Vite + React 19 + TypeScript + Tailwind 4 + shadcn app that boots, reads the repo-root `.env`, and has a Supabase client pinned to the `doc_worker_v2` schema. No features yet — just the shell.

### Changes Required:

#### 1. Repo-root `.env` — add VITE mirrors
**File**: `.env` (repo root)
**Changes**: Append two Vite-prefixed mirrors so the browser build can read them. `VITE_SUPABASE_URL` and `VITE_SUPABASE_ANON_KEY` only — **never** mirror `SUPABASE_SERVICE_ROLE_KEY` or S3 credentials.

```bash
# Frontend (mvp-v1) — browser-safe, prefixed so Vite exposes them.
# Do NOT add VITE_ prefixes to service-role / S3 keys — they must stay server-only.
VITE_SUPABASE_URL=${SUPABASE_URL}
VITE_SUPABASE_ANON_KEY=${SUPABASE_ANON_KEY}
```

Note: `${}` substitution does not happen in `.env` files automatically — the developer must paste the same literal values as the unprefixed ones. This is documented in the app's README.

#### 2. App scaffold
**Directory**: `apps/mvp-v1/`
**Changes**: New Vite+React+TS project. Following the admin's exact dep pinning (React 19.2, Vite 8, TS 6, Tailwind 4, shadcn).

**File**: `apps/mvp-v1/package.json`
```json
{
  "name": "mvp-v1",
  "private": true,
  "version": "0.0.0",
  "type": "module",
  "scripts": {
    "dev": "vite",
    "build": "tsc -b && vite build",
    "lint": "eslint .",
    "typecheck": "tsc -b --noEmit",
    "preview": "vite preview"
  },
  "dependencies": {
    "@base-ui/react": "^1.3.0",
    "@fontsource-variable/geist": "^5.2.8",
    "@supabase/supabase-js": "^2.103.0",
    "@tailwindcss/vite": "^4.2.2",
    "class-variance-authority": "^0.7.1",
    "clsx": "^2.1.1",
    "date-fns": "^4.1.0",
    "lucide-react": "^1.8.0",
    "react": "^19.2.4",
    "react-dom": "^19.2.4",
    "react-router-dom": "^7.14.0",
    "tailwind-merge": "^3.5.0",
    "tailwindcss": "^4.2.2",
    "tw-animate-css": "^1.4.0"
  },
  "devDependencies": {
    "@eslint/js": "^9.39.4",
    "@types/node": "^24.12.2",
    "@types/react": "^19.2.14",
    "@types/react-dom": "^19.2.3",
    "@vitejs/plugin-react": "^6.0.1",
    "eslint": "^9.39.4",
    "eslint-plugin-react-hooks": "^7.0.1",
    "eslint-plugin-react-refresh": "^0.5.2",
    "globals": "^17.4.0",
    "typescript": "~6.0.2",
    "typescript-eslint": "^8.58.0",
    "vite": "^8.0.4"
  }
}
```

(Same pins as `apps/admin/package.json` minus the admin-only deps: `pdfjs-dist`, `react-pdf`, `recharts`, `shadcn` CLI.)

#### 3. Vite config — point env loading at the repo root
**File**: `apps/mvp-v1/vite.config.ts`
```ts
import path from "path"
import { defineConfig } from "vite"
import react from "@vitejs/plugin-react"
import tailwindcss from "@tailwindcss/vite"

export default defineConfig({
  envDir: path.resolve(__dirname, "../.."),
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
})
```

`envDir` tells Vite to load `.env` from the repo root. `VITE_` prefix is enforced by Vite default — no override, so `SUPABASE_SERVICE_ROLE_KEY` stays invisible to the bundle.

#### 4. TypeScript config
**Files**: `apps/mvp-v1/tsconfig.json`, `tsconfig.app.json`, `tsconfig.node.json` — copy from `apps/admin/` verbatim; path alias `"@/*": ["./src/*"]`.

#### 5. Tailwind + shadcn setup
**File**: `apps/mvp-v1/src/index.css`
Copy from `apps/admin/src/index.css`. Tailwind v4 uses `@import "tailwindcss"` + CSS variables; no separate `tailwind.config.js` needed.

**File**: `apps/mvp-v1/components.json`
Copy from `apps/admin/components.json` (shadcn config — tells the CLI where to emit components, but we'll hand-copy only the components we need).

**shadcn components to include (hand-copied from `apps/admin/src/components/ui/`):**
- `button.tsx`, `card.tsx`, `input.tsx`, `badge.tsx`, `skeleton.tsx`

These are the only ui primitives the MVP needs; avoid copying the whole admin set.

#### 6. Supabase client pinned to v2 schema
**File**: `apps/mvp-v1/src/lib/supabase.ts`
```ts
import { createClient } from "@supabase/supabase-js"

const supabaseUrl = import.meta.env.VITE_SUPABASE_URL
const anonKey = import.meta.env.VITE_SUPABASE_ANON_KEY

if (!supabaseUrl || !anonKey) {
  throw new Error(
    "Missing VITE_SUPABASE_URL or VITE_SUPABASE_ANON_KEY — add them to the repo-root .env",
  )
}

export const supabase = createClient(supabaseUrl, anonKey, {
  db: { schema: "doc_worker_v2" },
})

export const STORAGE_BUCKET = "documents"
```

#### 7. Types for the v2 documents row
**File**: `apps/mvp-v1/src/types/database.ts`
```ts
export type DocumentStatus =
  | "pending"
  | "processing"
  | "retrying"
  | "completed"
  | "failed"

export interface Document {
  id: string
  user_id: string
  filename: string
  file_path: string
  status: DocumentStatus
  error_message: string | null
  page_count: number | null
  pipeline: string | null
  artifacts: Record<string, string> // key → storage path
  metadata: Record<string, unknown>
  created_at: string
  updated_at: string
}
```

#### 8. App shell
**File**: `apps/mvp-v1/src/App.tsx`
```tsx
import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom"

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<div>mvp-v1 shell OK</div>} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </BrowserRouter>
  )
}
```

**File**: `apps/mvp-v1/src/main.tsx` — copy from admin, `createRoot(...).render(<App />)` with `./index.css` import.

**File**: `apps/mvp-v1/index.html` — copy from admin, title "OpenLearn".

#### 9. README
**File**: `apps/mvp-v1/README.md`
One-page: env keys to add to repo-root `.env`, `npm install`, `npm run dev`, what ports, credentials (reference test users), link back to this plan.

### Success Criteria:

#### Automated Verification:
- [x] `npm install` in `apps/mvp-v1/` completes without errors (219 packages, 0 vulnerabilities)
- [x] `npm run typecheck` (which runs `tsc -b --noEmit`) passes
- [x] `npm run lint` passes (added `eslint-disable-next-line` for the shadcn variant-export pattern in `button.tsx`/`badge.tsx`; the same rule fires in `apps/admin/` but admin doesn't gate on lint)
- [x] `npm run build` produces a `dist/` bundle (gzipped JS 74 KB)
- [x] Grep for `SUPABASE_SERVICE_ROLE_KEY` / `SUPABASE_S3_*` in `dist/` returns nothing (service-role and S3 keys are not in the browser bundle)

#### Manual Verification:
- [x] `npm run dev` — open `http://localhost:5173` and see `"mvp-v1 shell OK"`
- [x] Browser devtools Network tab shows no 401/403 to Supabase (we haven't called it yet, but client loaded)
- [x] Removing `VITE_SUPABASE_URL` from `.env` and re-running `npm run dev` produces a clear error toast/throw in the console (guards against missing config)

**Implementation Note**: Pause here; confirm the shell boots and env wiring is correct before adding more code.

---

## Phase 3: Auth (login, guard, router)

### Overview
Add email-password login that reuses the admin's `signInWithPassword` pattern. Wrap feature routes in an `AuthGuard`, render a logout button in the header. After this phase, `/` is protected and only reachable after login.

### Changes Required:

#### 1. Auth hook
**File**: `apps/mvp-v1/src/hooks/use-auth.ts`
Copy verbatim from `apps/admin/src/hooks/use-auth.ts` — the implementation is framework-agnostic (only depends on `supabase.auth.*`). Exports: `AuthContext`, `useAuthProvider`, `useAuth`.

#### 2. Auth guard
**File**: `apps/mvp-v1/src/components/auth-guard.tsx`
Copy verbatim from `apps/admin/src/components/layout/auth-guard.tsx`.

#### 3. Login page
**File**: `apps/mvp-v1/src/pages/login.tsx`
Copy admin's login page (`apps/admin/src/pages/login.tsx:1-57`), change the card title from "OpenLearn Admin" to "OpenLearn" and the redirect target from `/documents` to `/`.

#### 4. App layout with header + logout
**File**: `apps/mvp-v1/src/components/app-layout.tsx`
```tsx
import { Outlet } from "react-router-dom"
import { Button } from "@/components/ui/button"
import { useAuth } from "@/hooks/use-auth"

export function AppLayout() {
  const { user, signOut } = useAuth()
  return (
    <div className="min-h-screen flex flex-col">
      <header className="flex items-center justify-between border-b px-6 py-3">
        <div className="font-semibold">OpenLearn</div>
        <div className="flex items-center gap-3 text-sm text-muted-foreground">
          <span>{user?.email}</span>
          <Button variant="outline" size="sm" onClick={signOut}>Sign out</Button>
        </div>
      </header>
      <main className="flex-1 mx-auto w-full max-w-4xl p-6">
        <Outlet />
      </main>
    </div>
  )
}
```

#### 5. Router wiring
**File**: `apps/mvp-v1/src/App.tsx` (replace the scaffolded version)
```tsx
import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom"
import { AuthContext, useAuthProvider } from "@/hooks/use-auth"
import { AuthGuard } from "@/components/auth-guard"
import { AppLayout } from "@/components/app-layout"
import { LoginPage } from "@/pages/login"
import { HomePage } from "@/pages/home"

export default function App() {
  const auth = useAuthProvider()
  return (
    <AuthContext.Provider value={auth}>
      <BrowserRouter>
        <Routes>
          <Route path="/login" element={<LoginPage />} />
          <Route element={<AuthGuard />}>
            <Route element={<AppLayout />}>
              <Route path="/" element={<HomePage />} />
              <Route path="*" element={<Navigate to="/" replace />} />
            </Route>
          </Route>
        </Routes>
      </BrowserRouter>
    </AuthContext.Provider>
  )
}
```

#### 6. Placeholder home page
**File**: `apps/mvp-v1/src/pages/home.tsx`
```tsx
export function HomePage() {
  return <div>Signed in. Upload + list come next.</div>
}
```

### Success Criteria:

#### Automated Verification:
- [x] `npm run typecheck` passes
- [x] `npm run lint` passes
- [x] `npm run build` succeeds (gzipped JS 137 KB — supabase-js now included)
- [x] Bundle inlines `VITE_SUPABASE_URL` host; bundle does NOT contain `service_role`

#### Manual Verification:
- [x] Opening `/` when signed out redirects to `/login`
- [x] Valid credentials (`testuser1@openlearn.test` / `TestPassword123!`) sign in and land on `/`
- [x] Invalid credentials surface the error message inline (`"Invalid login credentials"`)
- [x] The header shows the signed-in email + a working "Sign out" button
- [x] Clicking "Sign out" drops the session and redirects back to `/login`
- [x] Reloading the page while signed in preserves the session (session restored from localStorage by `supabase.auth.getSession()`)

**Implementation Note**: Pause here; confirm auth is solid before touching RLS-gated tables.

---

## Phase 4: Upload flow

### Overview
Single upload panel on the home page. User drops or picks a PDF; we generate a client-side `document_id` UUID, upload the PDF to `documents/{user.id}/{doc_id}/{filename}.pdf`, then `INSERT` a row into `doc_worker_v2.documents`. The `AFTER INSERT` trigger enqueues the job. Row shows up in state immediately via the upload hook's return value; Phase 5 will drive live status updates.

### Changes Required:

#### 1. Upload hook
**File**: `apps/mvp-v1/src/hooks/use-upload.ts`
```ts
import { useCallback, useState } from "react"
import { supabase, STORAGE_BUCKET } from "@/lib/supabase"
import { useAuth } from "@/hooks/use-auth"
import type { Document } from "@/types/database"

const MAX_BYTES = 50 * 1024 * 1024 // 50 MB — matches the v1 edge function limit

interface UploadState {
  uploading: boolean
  error: string | null
}

export function useUpload() {
  const { user } = useAuth()
  const [state, setState] = useState<UploadState>({ uploading: false, error: null })

  const upload = useCallback(
    async (file: File): Promise<Document | null> => {
      if (!user) {
        setState({ uploading: false, error: "Not signed in" })
        return null
      }
      if (!file.name.toLowerCase().endsWith(".pdf")) {
        setState({ uploading: false, error: "Only PDF files are supported" })
        return null
      }
      if (file.size > MAX_BYTES) {
        setState({ uploading: false, error: `File exceeds 50 MB limit` })
        return null
      }

      setState({ uploading: true, error: null })

      const documentId = crypto.randomUUID()
      const filePath = `${user.id}/${documentId}/${file.name}`

      const { error: uploadErr } = await supabase.storage
        .from(STORAGE_BUCKET)
        .upload(filePath, file, { contentType: "application/pdf", upsert: false })

      if (uploadErr) {
        setState({ uploading: false, error: `Upload failed: ${uploadErr.message}` })
        return null
      }

      const { data: inserted, error: insertErr } = await supabase
        .from("documents")
        .insert({
          id: documentId,
          user_id: user.id,
          filename: file.name,
          file_path: filePath,
        })
        .select()
        .single<Document>()

      if (insertErr) {
        // Best-effort cleanup of the orphaned storage object.
        await supabase.storage.from(STORAGE_BUCKET).remove([filePath])
        setState({ uploading: false, error: `Insert failed: ${insertErr.message}` })
        return null
      }

      setState({ uploading: false, error: null })
      return inserted
    },
    [user],
  )

  return { ...state, upload }
}
```

#### 2. Dropzone component
**File**: `apps/mvp-v1/src/components/upload-dropzone.tsx`
```tsx
import { useRef, useState, type DragEvent, type ChangeEvent } from "react"
import { Button } from "@/components/ui/button"
import { useUpload } from "@/hooks/use-upload"
import type { Document } from "@/types/database"

export function UploadDropzone({ onUploaded }: { onUploaded: (d: Document) => void }) {
  const inputRef = useRef<HTMLInputElement>(null)
  const [dragOver, setDragOver] = useState(false)
  const { upload, uploading, error } = useUpload()

  async function handleFiles(files: FileList | null) {
    if (!files || files.length === 0) return
    for (const f of Array.from(files)) {
      const d = await upload(f)
      if (d) onUploaded(d)
    }
  }

  return (
    <div
      onDragOver={(e: DragEvent) => { e.preventDefault(); setDragOver(true) }}
      onDragLeave={() => setDragOver(false)}
      onDrop={(e: DragEvent) => { e.preventDefault(); setDragOver(false); handleFiles(e.dataTransfer.files) }}
      className={`rounded-lg border-2 border-dashed p-8 text-center transition ${
        dragOver ? "border-primary bg-primary/5" : "border-muted-foreground/30"
      }`}
    >
      <p className="text-sm text-muted-foreground mb-3">Drop a PDF here, or</p>
      <Button type="button" onClick={() => inputRef.current?.click()} disabled={uploading}>
        {uploading ? "Uploading…" : "Choose file"}
      </Button>
      <input
        ref={inputRef}
        type="file"
        accept="application/pdf"
        multiple
        className="hidden"
        onChange={(e: ChangeEvent<HTMLInputElement>) => handleFiles(e.target.files)}
      />
      {error && <p className="mt-3 text-sm text-destructive">{error}</p>}
    </div>
  )
}
```

#### 3. Update home page to render the dropzone
**File**: `apps/mvp-v1/src/pages/home.tsx`
```tsx
import { useState } from "react"
import { UploadDropzone } from "@/components/upload-dropzone"
import type { Document } from "@/types/database"

export function HomePage() {
  const [justUploaded, setJustUploaded] = useState<Document[]>([])

  return (
    <div className="space-y-6">
      <UploadDropzone onUploaded={(d) => setJustUploaded((prev) => [d, ...prev])} />
      <pre className="text-xs bg-muted p-3 rounded">{JSON.stringify(justUploaded, null, 2)}</pre>
    </div>
  )
}
```

(The `<pre>` is a scratch view; Phase 5 replaces it with the real list component.)

### Success Criteria:

#### Automated Verification:
- [x] `npm run typecheck`, `npm run lint`, `npm run build` pass (gzipped JS 138 KB)
- [x] Grep confirms no `service_role` or S3 key usage in `src/` (and not in `dist/`)

#### Manual Verification:
- [x] Dropping a PDF triggers upload; row appears in the scratch JSON view with `status: "pending"` and the expected `file_path`
- [x] Dropping a non-PDF shows "Only PDF files are supported" and does not call the network
- [x] Dropping a file > 50 MB shows the size-limit error
- [x] In the Supabase dashboard, the new row is visible at `doc_worker_v2.documents` with `status='pending'` immediately after upload
- [x] In the dashboard, the PDF appears at `storage.objects` under `documents/{user_id}/{doc_id}/{filename}.pdf`
- [x] Railway logs for `document-worker-v2` show `pgmq_read_doc_ingest "HTTP/2 200 OK"` returning the new message within one polling interval (≤5 s)
- [x] Within ~60 s (first cold start may take longer), the row's `status` in the dashboard transitions through `processing` → `completed` with `artifacts` populated (verify via dashboard; live UI updates come in Phase 5)
- [x] Uploading the same-named file twice to the *same* document-id is rejected by `upsert: false` — the hook reports the duplicate error (negative test by manually crafting a duplicate path in a temporary code tweak, then reverted; optional)

**Implementation Note**: Pause here. Confirm server-side flow end-to-end via the dashboard before wiring up the realtime list.

---

## Phase 5: Document list with live progress

### Overview
Render the user's documents as a list, sorted by `created_at` descending. Subscribe to Realtime `postgres_changes` on `doc_worker_v2.documents` filtered to `user_id = auth.uid()`; on INSERT prepend, on UPDATE merge. Status chip + error message per row. Replaces the scratch JSON from Phase 4.

### Changes Required:

#### 1. Documents hook with Realtime
**File**: `apps/mvp-v1/src/hooks/use-documents.ts`
```ts
import { useEffect, useState } from "react"
import { supabase } from "@/lib/supabase"
import { useAuth } from "@/hooks/use-auth"
import type { Document } from "@/types/database"

export function useDocuments() {
  const { user } = useAuth()
  const [documents, setDocuments] = useState<Document[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!user) return
    let active = true

    async function load() {
      setLoading(true)
      const { data, error: err } = await supabase
        .from("documents")
        .select("*")
        .order("created_at", { ascending: false })
      if (!active) return
      if (err) setError(err.message)
      else setDocuments((data ?? []) as Document[])
      setLoading(false)
    }
    load()

    const channel = supabase
      .channel(`docs-${user.id}`)
      .on(
        "postgres_changes",
        {
          event: "*",
          schema: "doc_worker_v2",
          table: "documents",
          filter: `user_id=eq.${user.id}`,
        },
        (payload) => {
          setDocuments((prev) => {
            if (payload.eventType === "INSERT") {
              const next = payload.new as Document
              if (prev.some((d) => d.id === next.id)) return prev
              return [next, ...prev]
            }
            if (payload.eventType === "UPDATE") {
              const next = payload.new as Document
              return prev.map((d) => (d.id === next.id ? next : d))
            }
            if (payload.eventType === "DELETE") {
              const id = (payload.old as Document).id
              return prev.filter((d) => d.id !== id)
            }
            return prev
          })
        },
      )
      .subscribe()

    return () => {
      active = false
      supabase.removeChannel(channel)
    }
  }, [user])

  // Convenience to merge in optimistic inserts from the upload hook.
  function upsertLocal(d: Document) {
    setDocuments((prev) => (prev.some((x) => x.id === d.id) ? prev : [d, ...prev]))
  }

  return { documents, loading, error, upsertLocal }
}
```

The `upsertLocal` seam handles the tiny race where the upload hook returns the inserted row before the Realtime INSERT event arrives; idempotent de-dup on `id`.

#### 2. Status badge
**File**: `apps/mvp-v1/src/components/status-badge.tsx`
```tsx
import { Badge } from "@/components/ui/badge"
import type { DocumentStatus } from "@/types/database"

const VARIANTS: Record<DocumentStatus, { label: string; className: string }> = {
  pending:    { label: "Pending",    className: "bg-muted text-muted-foreground" },
  processing: { label: "Processing", className: "bg-blue-500/15 text-blue-700" },
  retrying:   { label: "Retrying",   className: "bg-yellow-500/15 text-yellow-700" },
  completed:  { label: "Completed",  className: "bg-green-500/15 text-green-700" },
  failed:     { label: "Failed",     className: "bg-destructive/15 text-destructive" },
}

export function StatusBadge({ status }: { status: DocumentStatus }) {
  const v = VARIANTS[status]
  return <Badge variant="outline" className={v.className}>{v.label}</Badge>
}
```

#### 3. Document row
**File**: `apps/mvp-v1/src/components/document-row.tsx`
```tsx
import { formatDistanceToNow } from "date-fns"
import { StatusBadge } from "@/components/status-badge"
import type { Document } from "@/types/database"

export function DocumentRow({ doc }: { doc: Document }) {
  return (
    <div className="flex items-start justify-between gap-4 rounded-md border p-4">
      <div className="min-w-0">
        <div className="font-medium truncate">{doc.filename}</div>
        <div className="mt-1 text-xs text-muted-foreground">
          {formatDistanceToNow(new Date(doc.created_at), { addSuffix: true })}
          {doc.page_count != null && <span> · {doc.page_count} pages</span>}
          {doc.pipeline && <span> · {doc.pipeline}</span>}
        </div>
        {doc.status === "failed" && doc.error_message && (
          <p className="mt-2 text-xs text-destructive break-words">{doc.error_message}</p>
        )}
      </div>
      <div className="flex items-center gap-2 shrink-0">
        <StatusBadge status={doc.status} />
        {/* Phase 6 plugs download buttons in here */}
      </div>
    </div>
  )
}
```

#### 4. Update home page
**File**: `apps/mvp-v1/src/pages/home.tsx` (replace)
```tsx
import { UploadDropzone } from "@/components/upload-dropzone"
import { DocumentRow } from "@/components/document-row"
import { useDocuments } from "@/hooks/use-documents"

export function HomePage() {
  const { documents, loading, error, upsertLocal } = useDocuments()

  return (
    <div className="space-y-6">
      <UploadDropzone onUploaded={upsertLocal} />

      {error && <p className="text-sm text-destructive">{error}</p>}

      <div className="space-y-2">
        {loading && <p className="text-sm text-muted-foreground">Loading…</p>}
        {!loading && documents.length === 0 && (
          <p className="text-sm text-muted-foreground">No documents yet. Upload a PDF above.</p>
        )}
        {documents.map((d) => <DocumentRow key={d.id} doc={d} />)}
      </div>
    </div>
  )
}
```

### Success Criteria:

#### Automated Verification:
- [x] `npm run typecheck`, `npm run lint`, `npm run build` pass (gzipped JS 142 KB)

#### Manual Verification:
- [x] On page load, the user's prior documents render sorted by `created_at` descending
- [x] Uploading a PDF adds a row at the top with `Pending` badge **without page refresh**
- [x] The badge transitions `Pending → Processing → Completed` as the worker proceeds, **without page refresh**
- [x] If Modal cold-start triggers `Retrying`, the badge shows it before landing on `Completed`
- [x] Forcing a failure (e.g., upload a corrupt file; or temporarily point the worker at a bad `VLM_ENDPOINT_URL`) surfaces the `Failed` badge with the worker's error message
- [x] Signing out and back in shows the same documents (no duplicates from re-subscription)
- [x] Two browser tabs for the same user both update in lockstep (confirms channel filter is by `user_id` not by tab)
- [x] A different user logged in on a second browser does NOT see this user's rows (RLS check)

**Implementation Note**: Pause here. If the badge stays at `Pending` despite the worker finishing, Phase 1's realtime publication is the first thing to check (`select * from pg_publication_tables where tablename='documents' and schemaname='doc_worker_v2';`).

---

## Phase 6: Artifact download

### Overview
For each `completed` document, render one button per key in the `artifacts` jsonb map. Clicking a button mints a short-lived (1 hour) signed URL via `supabase.storage.from("documents").createSignedUrl(path, 3600)` and navigates the browser to it — the server's `Content-Disposition` + the `<a download>` attribute save the file locally.

### Changes Required:

#### 1. Download helper
**File**: `apps/mvp-v1/src/lib/download.ts`
```ts
import { supabase, STORAGE_BUCKET } from "@/lib/supabase"

export async function downloadArtifact(storagePath: string): Promise<string> {
  const { data, error } = await supabase.storage
    .from(STORAGE_BUCKET)
    .createSignedUrl(storagePath, 60 * 60) // 1 hour
  if (error || !data?.signedUrl) {
    throw new Error(error?.message ?? "Failed to create signed URL")
  }
  return data.signedUrl
}
```

#### 2. Artifact-label map
**File**: `apps/mvp-v1/src/lib/artifact-labels.ts`
```ts
export const ARTIFACT_LABELS: Record<string, string> = {
  markdown: "Markdown",
  docling_json: "Docling JSON",
  doctags: "DocTags",
}

export function labelFor(key: string): string {
  return ARTIFACT_LABELS[key] ?? key
}
```

Covers every artifact name the worker currently emits (`services/doc-worker-v2/src/doc_worker_v2/pipelines/_common.py:18-42`). Unknown keys gracefully fall back to the raw key.

#### 3. Download buttons inside the row
**File**: `apps/mvp-v1/src/components/document-row.tsx` (extend)
```tsx
// Above the JSX for the row, add:
import { Button } from "@/components/ui/button"
import { downloadArtifact } from "@/lib/download"
import { labelFor } from "@/lib/artifact-labels"
import { useState } from "react"

function ArtifactButtons({ artifacts }: { artifacts: Record<string, string> }) {
  const [busy, setBusy] = useState<string | null>(null)

  async function handle(key: string, path: string) {
    setBusy(key)
    try {
      const url = await downloadArtifact(path)
      // Browser navigation to a signed URL on a different origin triggers a normal
      // download when the stored Content-Disposition is "attachment"; Supabase
      // storage serves objects with inline by default, so we force attachment
      // behavior client-side via a temporary <a download> element.
      const a = document.createElement("a")
      a.href = url
      a.download = path.split("/").pop() ?? key
      a.rel = "noopener noreferrer"
      document.body.appendChild(a)
      a.click()
      a.remove()
    } finally {
      setBusy(null)
    }
  }

  const keys = Object.keys(artifacts).sort() // deterministic order
  if (keys.length === 0) return null

  return (
    <div className="flex flex-wrap gap-2">
      {keys.map((k) => (
        <Button
          key={k}
          size="sm"
          variant="outline"
          disabled={busy === k}
          onClick={() => handle(k, artifacts[k])}
        >
          {busy === k ? "…" : labelFor(k)}
        </Button>
      ))}
    </div>
  )
}
```

Then slot it in where the `{/* Phase 6 plugs download buttons in here */}` comment was, conditional on `doc.status === "completed"`:

```tsx
{doc.status === "completed" && <ArtifactButtons artifacts={doc.artifacts} />}
<StatusBadge status={doc.status} />
```

### Success Criteria:

#### Automated Verification:
- [x] `npm run typecheck`, `npm run lint`, `npm run build` pass (gzipped JS 143 KB)

#### Manual Verification:
- [x] A `completed` row shows one button per artifact key (3 buttons for granite pipeline: Markdown, Docling JSON, DocTags)
- [x] Clicking `Markdown` downloads a non-empty `.md` file with expected content (e.g., for the Gaussians test PDF, contains "gaussian" / "covariance")
- [x] Clicking `Docling JSON` downloads a non-empty `.docling.json` whose root parses as JSON
- [x] Clicking `DocTags` downloads a non-empty `.doctags.xml`
- [x] The downloaded filename matches the artifact's path suffix (e.g., `gaussians.pdf.md`)
- [x] Clicking twice in rapid succession doesn't double-download (button disables during signing)
- [x] Revoking the signed URL (wait >1 h, or force a short expiry) produces a clean 403 from the storage host — no hung UI
- [x] A non-completed row renders NO download buttons

**Implementation Note**: Once Phase 6 is verified, the MVP is complete. Final sync: run `npm run build` from a clean install, confirm no warnings, mark the plan `complete`.

---

## Testing Strategy

### Unit Tests
Not in scope for MVP. The hooks are thin wrappers over `supabase-js`; the value of mocking and unit-testing them is low against a working manual E2E.

### Integration Tests
Deferred. The `services/doc-worker-v2/tests/test_integration_gaussians.py` (Phase 8 of the v2 plan) already covers the worker half of the pipeline against live Supabase + Modal. The MVP is exercised via the manual checks above.

### Manual Testing Steps (full end-to-end)
1. Ensure the Supabase migration `007_doc_worker_v2_realtime.sql` has been applied to the live project.
2. Ensure Railway service `document-worker-v2` is running and logs `doc-worker-v2 online; ... backends=q:supabase/d:supabase/b:supabase`.
3. Ensure Modal app `vlm-granite-docling` is deployed and warm (send one `modal run ... ::test` smoke request if uncertain).
4. In `apps/mvp-v1`: `npm install && npm run dev`. Open `http://localhost:5173`.
5. Sign in with `testuser1@openlearn.test` / `TestPassword123!`.
6. Drop a known-good PDF (e.g., the Stanford Gaussians PDF at `https://cs229.stanford.edu/section/gaussians.pdf`, ~10 pages). Watch the row transition `Pending → Processing → Completed` within ~60 s (cold-start) or ~10 s (warm).
7. Click each artifact download button; open the downloaded files and confirm they are non-empty and well-formed.
8. Repeat with a corrupt or empty PDF to see `Failed` state and error message.
9. Sign out; confirm list clears and `/` redirects to `/login`.

## Performance Considerations

- Realtime subscriptions: one channel per signed-in user; Supabase's free tier permits 200 concurrent connections and 2M messages/month — far above MVP load. Channel is cleaned up in the effect's return to avoid leaks on unmount.
- Signed-URL minting: 1 RPC per click. At ≤3 artifacts per doc and typical user volume, negligible.
- Upload path: single `supabase.storage.upload` call for files ≤50 MB. Supabase uses resumable uploads over 6 MB automatically — no extra wiring.
- Bundle size: no heavy deps (no `react-pdf`, `pdfjs-dist`, `recharts`). Target gzipped main bundle < 250 KB.

## Migration Notes

- Only database change is `007_doc_worker_v2_realtime.sql`. Idempotent; safe to re-run.
- Repo-root `.env` gets two new keys. Existing unprefixed keys are untouched.
- No changes to `apps/admin/` or `services/`. v1 and v2 stacks remain isolated.

## References

- Research doc: `thoughts/shared/research/2026-04-14-worker-and-services.md`
- v2 architecture plan: `thoughts/shared/plans/2026-04-14-doc-worker-v2-modular-architecture.md`
- Pattern source — Supabase client: `apps/admin/src/lib/supabase.ts:1-6`
- Pattern source — auth hook: `apps/admin/src/hooks/use-auth.ts:16-45`
- Pattern source — auth guard: `apps/admin/src/components/layout/auth-guard.tsx:4-20`
- Pattern source — login page: `apps/admin/src/pages/login.tsx:1-57`
- Pattern source — router wiring: `apps/admin/src/App.tsx:1-37`
- v2 schema + trigger: `supabase/migrations/006_doc_worker_v2.sql:19-33`, `:82-105`
- Storage RLS policies: `supabase/migrations/001_create_tables.sql:173-189`
- Artifact naming conventions: `services/doc-worker-v2/src/doc_worker_v2/pipelines/_common.py:18-42`
