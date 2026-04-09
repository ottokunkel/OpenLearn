"""Full pipeline runner for integration testing.

Executes every stage of the document processing pipeline against a live
Supabase instance and reports results back via callbacks.
"""

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Optional

import psycopg
import psycopg.rows
from psycopg_pool import ConnectionPool

from worker.config import Settings
from worker.converter import DoclingConverter
from worker.db import ChunkWriter, DocumentUpdater, PgmqQueue, create_pool
from worker.embedder import Embedder
from worker.s3 import S3Client

USER_1 = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


@dataclass
class StageResult:
    name: str
    ok: bool
    elapsed: float
    detail: str = ""
    data: dict = field(default_factory=dict)


class PipelineRunner:
    """Runs each pipeline stage sequentially, yielding StageResults."""

    def __init__(
        self,
        settings: Settings,
        pdf_path: str,
        on_log: Optional[Callable[[str], None]] = None,
        on_stage_start: Optional[Callable[[str], None]] = None,
        on_stage_done: Optional[Callable[[StageResult], None]] = None,
        on_vlm_status: Optional[Callable[[int, int, int], None]] = None,
        on_vlm_retry: Optional[Callable[[int, int, str, float], None]] = None,
    ):
        self.settings = settings
        self.pdf_path = pdf_path
        self._log = on_log or (lambda m: None)
        self._stage_start = on_stage_start or (lambda n: None)
        self._stage_done = on_stage_done or (lambda r: None)
        self._on_vlm_status = on_vlm_status
        self._on_vlm_retry = on_vlm_retry

        self._pool: ConnectionPool | None = None
        self._doc_id: str | None = None
        self._msg_id: int | None = None
        self._total_chunks = 0
        self._page_count = 0
        self._stages_run: list[StageResult] = []

    def run(self) -> list[StageResult]:
        stages = [
            ("Connect", self._connect),
            ("Create Document", self._create_document),
            ("Enqueue Job", self._enqueue_job),
            ("Download PDF", self._download_pdf),
            ("Convert PDF", self._convert_pdf),
            ("Embed & Write Chunks", self._embed_and_write),
            ("Update Status", self._update_status),
            ("Verify", self._verify),
            ("Vector Search", self._vector_search),
            ("Cleanup", self._cleanup),
        ]

        for name, fn in stages:
            self._stage_start(name)
            t0 = time.perf_counter()
            try:
                detail, data = fn()
                elapsed = time.perf_counter() - t0
                result = StageResult(name, True, elapsed, detail, data)
            except Exception as e:
                elapsed = time.perf_counter() - t0
                result = StageResult(name, False, elapsed, str(e))
                self._log(f"[red]  FAILED: {e}[/red]")

            self._stages_run.append(result)
            self._stage_done(result)

            if not result.ok and name != "Cleanup":
                # Try cleanup even after failure
                self._stage_start("Cleanup")
                t0 = time.perf_counter()
                try:
                    detail, data = self._cleanup()
                    r = StageResult("Cleanup", True, time.perf_counter() - t0, detail, data)
                except Exception as e:
                    r = StageResult("Cleanup", False, time.perf_counter() - t0, str(e))
                self._stages_run.append(r)
                self._stage_done(r)
                break

        return self._stages_run

    # -- Stages --

    def _connect(self) -> tuple[str, dict]:
        self._pool = create_pool(self.settings.database_url)
        with self._pool.connection() as conn:
            row = conn.execute("SELECT version()").fetchone()
            version = list(row.values())[0] if row else "unknown"
        self._log(f"  Connected: {version[:60]}")
        return version[:60], {}

    def _create_document(self) -> tuple[str, dict]:
        self._doc_id = str(uuid.uuid4())
        filename = os.path.basename(self.pdf_path)
        file_path = f"{USER_1}/{self._doc_id}/{filename}"

        with self._pool.connection() as conn:
            conn.execute(
                "INSERT INTO documents (id, user_id, filename, file_path, status) "
                "VALUES (%s, %s, %s, %s, 'pending')",
                [self._doc_id, USER_1, filename, file_path],
            )
        self._log(f"  doc_id={self._doc_id}")
        return self._doc_id, {"doc_id": self._doc_id, "filename": filename, "file_path": file_path}

    def _enqueue_job(self) -> tuple[str, dict]:
        filename = os.path.basename(self.pdf_path)
        file_path = f"{USER_1}/{self._doc_id}/{filename}"

        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT pgmq.send(%s, %s::jsonb)",
                [
                    "document_jobs",
                    json.dumps({
                        "document_id": self._doc_id,
                        "user_id": USER_1,
                        "file_path": file_path,
                        "filename": filename,
                        "s3_bucket": self.settings.s3_output_bucket,
                        "s3_key": file_path,
                    }),
                ],
            ).fetchone()
            self._msg_id = list(row.values())[0]

        # Read the message back (claims it)
        queue = PgmqQueue(self._pool)
        result = queue.wait_for_job(timeout=2)
        if result is None:
            raise RuntimeError("Failed to read back enqueued message")
        read_id, job = result
        self._job = job
        self._read_id = read_id

        self._log(f"  msg_id={self._msg_id} -> read_id={read_id}")
        return f"msg_id={self._msg_id}", {"msg_id": self._msg_id, "job": job}

    def _download_pdf(self) -> tuple[str, dict]:
        # For integration testing we use the local file directly
        # In production the worker would download from S3
        size = os.path.getsize(self.pdf_path)
        self._local_path = self.pdf_path
        self._log(f"  {self.pdf_path} ({size:,} bytes)")
        return f"{size:,} bytes", {"local_path": self.pdf_path, "size": size}

    def _convert_pdf(self) -> tuple[str, dict]:
        converter = DoclingConverter(self.settings)
        self._log("  Running VLM conversion...")

        conversion = converter.convert_pdf(
            self._local_path,
            on_status=self._on_vlm_status,
            on_retry=self._on_vlm_retry,
        )

        self._doc_dict = conversion.doc_dict
        self._markdown = conversion.markdown
        self._page_count = conversion.page_count
        self._chunks = list(conversion.chunk_iterator)

        self._log(f"  {self._page_count} pages, {len(self._chunks)} chunks, {len(self._markdown):,} chars markdown")
        return (
            f"{self._page_count} pages, {len(self._chunks)} chunks",
            {"page_count": self._page_count, "chunk_count": len(self._chunks), "markdown_len": len(self._markdown)},
        )

    def _embed_and_write(self) -> tuple[str, dict]:
        if self.settings.embedding_api_key:
            embedder = Embedder.from_settings(self.settings)
            self._log(f"  Embedding {len(self._chunks)} chunks with {self.settings.embedding_model}...")
            texts = [c["text"] for c in self._chunks]
            embeddings = embedder.embed_batch(texts)
            for chunk, emb in zip(self._chunks, embeddings):
                chunk["embedding"] = emb
            embed_detail = f"embedded with {self.settings.embedding_model}"
        else:
            # Generate zero-vector embeddings for testing pgvector write
            for chunk in self._chunks:
                chunk["embedding"] = [0.0] * self.settings.embedding_dimensions
            embed_detail = "zero-vector embeddings (no API key)"
            self._log(f"  No embedding API key, using zero vectors")

        writer = ChunkWriter(self._pool)
        batch_size = self.settings.chunk_batch_size
        total = len(self._chunks)

        for i in range(0, total, batch_size):
            batch = self._chunks[i : i + batch_size]
            writer.insert_chunks(
                self._doc_id, USER_1, batch, start_index=i,
                metadata={"vlm_model": self.settings.vlm_model, "integration_test": True},
            )
            self._log(f"  Wrote batch {i // batch_size + 1} ({len(batch)} chunks)")

        self._total_chunks = total
        self._log(f"  {total} chunks written to pgvector")
        return f"{total} chunks, {embed_detail}", {"total_chunks": total}

    def _update_status(self) -> tuple[str, dict]:
        updater = DocumentUpdater(self._pool)
        updater.mark_completed(
            self._doc_id,
            page_count=self._page_count,
            total_chunks=self._total_chunks,
            doc_json_path=f"output/{self._doc_id}/document.json",
            markdown_path=f"output/{self._doc_id}/markdown.md",
            metadata={"vlm_model": self.settings.vlm_model, "integration_test": True},
        )

        # Archive the pgmq message
        queue = PgmqQueue(self._pool)
        queue.ack_job(self._read_id)

        self._log("  Document marked completed, pgmq message archived")
        return "completed", {}

    def _verify(self) -> tuple[str, dict]:
        with self._pool.connection() as conn:
            doc = conn.execute(
                "SELECT status, page_count, total_chunks FROM documents WHERE id = %s",
                [self._doc_id],
            ).fetchone()

            chunk_count = conn.execute(
                "SELECT count(*) as cnt FROM document_chunks WHERE document_id = %s",
                [self._doc_id],
            ).fetchone()

        if doc["status"] != "completed":
            raise RuntimeError(f"Expected status=completed, got {doc['status']}")
        if doc["total_chunks"] != self._total_chunks:
            raise RuntimeError(f"total_chunks mismatch: {doc['total_chunks']} vs {self._total_chunks}")
        if chunk_count["cnt"] != self._total_chunks:
            raise RuntimeError(f"Actual chunk rows: {chunk_count['cnt']} vs {self._total_chunks}")

        self._log(f"  status={doc['status']}, pages={doc['page_count']}, chunks={chunk_count['cnt']}")
        return f"{doc['status']}, {chunk_count['cnt']} chunks verified", doc

    def _vector_search(self) -> tuple[str, dict]:
        # Use the first chunk's embedding as the query vector
        if not self._chunks or "embedding" not in self._chunks[0]:
            return "skipped (no embeddings)", {}

        query_vec = json.dumps(self._chunks[0]["embedding"])
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT content, 1 - (embedding <=> %s::vector) AS similarity "
                "FROM document_chunks WHERE document_id = %s "
                "ORDER BY embedding <=> %s::vector LIMIT 3",
                [query_vec, self._doc_id, query_vec],
            ).fetchall()

        results = []
        for r in rows:
            preview = r["content"][:80] + "..." if len(r["content"]) > 80 else r["content"]
            results.append({"content": preview, "similarity": round(r["similarity"], 4)})
            self._log(f"  sim={r['similarity']:.4f}  {preview}")

        return f"{len(rows)} results", {"matches": results}

    def _cleanup(self) -> tuple[str, dict]:
        if not self._pool or not self._doc_id:
            return "nothing to clean", {}

        with self._pool.connection() as conn:
            conn.execute("DELETE FROM document_chunks WHERE document_id = %s", [self._doc_id])
            conn.execute("DELETE FROM documents WHERE id = %s", [self._doc_id])

        self._pool.close()
        self._log(f"  Deleted doc {self._doc_id} and chunks")
        return f"deleted {self._doc_id}", {}
