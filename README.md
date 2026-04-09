# Docling Document Conversion Worker

A distributed PDF document conversion pipeline built on [Docling](https://github.com/DS4SD/docling) with Vision Language Model (VLM) support. Converts PDFs into structured chunks with vector embeddings, stored in Supabase with pgvector for semantic search.

## Architecture

```
User ──► Supabase Edge Function (auth + upload)
              │
              ├── Supabase Storage (PDF file)
              ├── documents table (status: pending)
              └── pgmq.send('document_jobs', ...)
                          │
         Worker (polls pgmq) ──► Docling VLM conversion ──► embeddings
              │
              ├── document_chunks table (pgvector)
              ├── documents table (status: completed)
              └── Supabase Storage (document.json, markdown.md)
                          │
User ──► Supabase client (RLS-enforced queries + vector similarity search)
```

Users can only access their own documents and chunks via row-level security.

## Prerequisites

- [Supabase](https://supabase.com) project (free tier works)
- [Supabase CLI](https://supabase.com/docs/guides/cli) (`brew install supabase/tap/supabase`)
- Python 3.12+
- Docker (for containerized deployment)
- [OpenRouter](https://openrouter.ai) API key (for VLM document conversion)
- Embedding API key (OpenAI or compatible) — optional but required for vector search

## Setup

### 1. Supabase Project

Create a Supabase project at [supabase.com](https://supabase.com/dashboard) or via CLI:

```bash
supabase projects create docling-worker --org-id <your-org-id>
```

### 2. Enable Extensions

In the Supabase SQL Editor, run:

```sql
CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA extensions;
CREATE EXTENSION IF NOT EXISTS pgmq;
```

Or enable **pgvector** and **pgmq** from the Database > Extensions page in the dashboard.

### 3. Run Database Migration

```bash
supabase link --project-ref <your-project-ref>
supabase db push
```

This creates:
- `documents` table — tracks uploaded files and processing status
- `document_chunks` table — stores chunks with pgvector embeddings
- RLS policies — users can only see their own data
- `document_jobs` pgmq queue — job queue for the worker
- `enqueue_document_job()` — helper function for the edge function
- `match_document_chunks()` — vector similarity search function
- `documents` storage bucket — file storage with per-user RLS

### 4. Enable Supabase Storage S3 Access

1. Go to **Storage > S3 Access Keys** in the Supabase dashboard
2. Generate a new access key pair
3. Note the S3 endpoint URL: `https://<project-ref>.supabase.co/storage/v1/s3`

### 5. Deploy Edge Function

```bash
supabase functions deploy upload-document
```

### 6. Configure Worker

Copy `.env.example` to `.env` and fill in:

```bash
cp .env.example .env
```

Required variables for Supabase mode:

| Variable | Description |
|----------|-------------|
| `DATABASE_URL` | Direct Postgres connection string from Supabase (Settings > Database > Connection string > URI) |
| `SUPABASE_URL` | Your project URL (`https://<ref>.supabase.co`) |
| `SUPABASE_SERVICE_KEY` | Service role key (Settings > API > service_role) |
| `S3_ENDPOINT_URL` | `https://<ref>.supabase.co/storage/v1/s3` |
| `S3_ACCESS_KEY` | From Storage > S3 Access Keys |
| `S3_SECRET_KEY` | From Storage > S3 Access Keys |
| `S3_OUTPUT_BUCKET` | `documents` |
| `OPENROUTER_API_KEY` | Your OpenRouter API key |
| `EMBEDDING_API_KEY` | Your OpenAI API key (optional, enables embeddings) |

### 7. Run Worker

**Docker:**

```bash
docker compose up worker --build
```

Scale horizontally:

```bash
docker compose up --scale worker=3
```

**Bare metal:**

```bash
pip install -r requirements.txt
python -m worker
```

## Usage

### Upload a Document

```bash
curl -X POST https://<ref>.supabase.co/functions/v1/upload-document \
  -H "Authorization: Bearer <user-jwt>" \
  -F "file=@document.pdf"
```

Response:

```json
{
  "document_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "pending"
}
```

Optional metadata via query parameter:

```bash
curl -X POST "https://<ref>.supabase.co/functions/v1/upload-document?metadata=%7B%22course_id%22%3A%22cs101%22%7D" \
  -H "Authorization: Bearer <user-jwt>" \
  -F "file=@lecture.pdf"
```

### Check Document Status

```javascript
const { data } = await supabase
  .from('documents')
  .select('id, filename, status, page_count, total_chunks, metadata')
  .eq('id', documentId)
  .single()
```

### List Your Documents

```javascript
const { data } = await supabase
  .from('documents')
  .select('id, filename, status, page_count, total_chunks, created_at')
  .order('created_at', { ascending: false })
```

### Get Chunks for a Document

```javascript
const { data } = await supabase
  .from('document_chunks')
  .select('chunk_index, content, headings, label, page_no')
  .eq('document_id', documentId)
  .order('chunk_index')
```

### Vector Similarity Search

```javascript
const { data } = await supabase.rpc('match_document_chunks', {
  query_embedding: embeddingVector,  // float[1536]
  match_threshold: 0.7,
  match_count: 10,
  filter_document_id: null  // or a specific document UUID
})
```

Results are automatically scoped to the authenticated user's documents via RLS.

## Development (Redis Mode)

The worker can run without Supabase using Redis as the queue backend. Leave `DATABASE_URL` empty and set `REDIS_URL`:

```bash
docker compose --profile redis up
```

In Redis mode, the worker reads jobs from Redis and pushes results back to a Redis callback queue. There is no PGVector integration or RLS in this mode.

## Metadata

Documents and chunks include rich metadata:

**Document metadata** (set by edge function + worker):
- `original_filename` — original upload filename
- `file_size_bytes` — file size
- `mime_type` — always `application/pdf`
- `uploaded_at` — upload timestamp
- `vlm_model` — VLM model used for conversion
- `embedding_model` — embedding model used (if enabled)
- `embedding_dimensions` — vector dimensions
- `processing_completed_at` — when processing finished
- Any custom metadata passed via the `?metadata=` query parameter

**Chunk metadata** (set by worker):
- `embedding_model` — model used to generate the embedding
- `embedding_dimensions` — vector dimensions
- `vlm_model` — VLM model used for conversion
- `chunk_method` — chunking algorithm (`hybrid_chunker`)
- `processed_at` — when the chunk was processed

## Configuration Reference

See [`.env.example`](.env.example) for all available configuration options.
