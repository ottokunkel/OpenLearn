# OpenLearn

A platform for intelligent document processing and learning. Converts PDFs into structured, searchable chunks with vector embeddings powered by Vision Language Models.

## Repository Structure

```
OpenLearn/
├── supabase/                   # Shared database migrations & edge functions
├── services/
│   └── document-worker/        # Docling PDF conversion pipeline
└── apps/                       # Frontend applications (coming soon)
```

## Services

### Document Worker

Distributed PDF conversion pipeline built on [Docling](https://github.com/DS4SD/docling) with VLM support. Converts PDFs into structured chunks with vector embeddings, stored in Supabase with pgvector for semantic search.

See [services/document-worker/](services/document-worker/) for setup and usage.

## Supabase Setup

```bash
supabase link --project-ref <your-project-ref>
supabase db push
supabase functions deploy upload-document
```

## Testing

### Document Worker

Unit tests cover the core processing pipeline — PDF conversion, embedding generation, storage, queuing, and job orchestration. All external dependencies (S3, Postgres, Redis, VLM APIs) are mocked so tests run locally without infrastructure.

```bash
cd services/document-worker
pip install -r requirements.txt -r requirements-dev.txt
pytest tests/ -v
```

| Module | What's tested |
|--------|--------------|
| `test_config` | Settings defaults, immutability, env var loading |
| `test_converter` | VLM retry logic (429/503 backoff, cancellation), request stats |
| `test_embedder` | Batch splitting, API error propagation |
| `test_processor` | End-to-end job pipeline, failure handling, temp file cleanup |
| `test_db` | pgmq queue reads/acks, pgvector chunk writes, document status updates |
| `test_queue` | Redis queue polling, error message formatting |
| `test_s3` | S3 download/upload for PDFs, JSON, and markdown |

### Integration Tests

Integration tests run against a live Supabase database. They test document CRUD, pgvector similarity search, pgmq queue operations, and RLS user isolation. Skipped automatically when `DATABASE_URL` is not set.

```bash
DATABASE_URL=postgresql://postgres.[ref]:[password]@aws-0-[region].pooler.supabase.com:6543/postgres \
  pytest tests/test_integration.py -v
```

To exclude integration tests during normal development:

```bash
pytest tests/ -m "not integration"
```

### Pipeline TUI

A Textual TUI that runs the full pipeline against a live Supabase instance — connect, create document, enqueue, VLM convert, embed, write chunks to pgvector, verify, and vector search. Requires `DATABASE_URL` and an `OPENROUTER_API_KEY` in `.env`.

```bash
cd services/document-worker
python -m integration                              # default: gaussians.pdf
python -m integration --pdf path/to/custom.pdf     # custom PDF
```

### Test Users

Three test users exist in Supabase Auth for manual integration testing:

| Email | Password | Purpose |
|-------|----------|---------|
| `testuser1@openlearn.test` | `TestPassword123!` | Primary user with test document |
| `testuser2@openlearn.test` | `TestPassword123!` | RLS isolation testing |
| `testuser3@openlearn.test` | `TestPassword123!` | Empty state (no documents) |

## Quick Start

```bash
# Copy and configure environment
cp services/document-worker/.env.example services/document-worker/.env

# Run the document worker
docker compose up document-worker --build
```
