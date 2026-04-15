#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# dependencies = [
#   "python-dotenv>=1.0",
#   "doc-worker-v2[postgres]",
# ]
# [tool.uv.sources]
# doc-worker-v2 = { path = "..", editable = true }
# ///
"""Phase 9 verification: round-trip storage-postgres against a live Postgres.

Requires a DSN pointing at a Postgres with the pgmq extension installed and
the `doc_worker_v2.documents` table + `doc_ingest` queue migrated (i.e. the
same Supabase Postgres used elsewhere in this plan, reachable via its pooler
DSN).

Run from repo root:

    POSTGRES_DSN="postgresql://..." uv run services/doc-worker-v2/tests/verify_phase9.py

What it checks:
  1. PostgresPgmqQueue.read/delete/archive against a synthetic pgmq enqueue
  2. PostgresDocumentStore.mark_processing/retrying/failed/completed against
     a throwaway doc_worker_v2.documents row (FK satisfied via a throwaway
     auth.users row created with raw SQL — skipped if auth.users insert fails)
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[3]
load_dotenv(REPO_ROOT / ".env")

OK = "\033[32m✓\033[0m"
FAIL = "\033[31m✗\033[0m"


def main() -> int:
    dsn = os.environ.get("POSTGRES_DSN")
    if not dsn:
        print("set POSTGRES_DSN to a postgres:// URL with pgmq + doc_worker_v2 migrated")
        return 2

    import psycopg

    from storage_postgres.documents import PostgresDocumentStore
    from storage_postgres.queue import PostgresPgmqQueue

    failures: list[str] = []

    # --- Queue round-trip ----------------------------------------------------
    print("=== queue round-trip ===")
    q = PostgresPgmqQueue(dsn)
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        payload = {"sentinel": f"phase9-{uuid.uuid4()}"}
        cur.execute(
            "SELECT pgmq.send('doc_ingest', %s::jsonb)", (json.dumps(payload),)
        )
        enq_msg_id = cur.fetchone()[0]
        conn.commit()
    print(f"[setup] enqueued msg_id={enq_msg_id}")

    our: object | None = None
    for _ in range(5):
        for m in q.read(visibility_timeout_s=600, qty=50):
            if m.payload.get("sentinel") == payload["sentinel"]:
                our = m
                break
        if our is not None:
            break
    if our is None:
        failures.append("queue.read did not return our enqueued message")
        print(f"  {FAIL} read")
    else:
        print(f"  {OK} read msg_id={our.msg_id}")

    # archive first sentinel, enqueue + delete a second
    if our is not None:
        q.archive(our.msg_id)
        print(f"  {OK} archive msg_id={our.msg_id}")

    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        payload2 = {"sentinel": f"phase9-{uuid.uuid4()}"}
        cur.execute(
            "SELECT pgmq.send('doc_ingest', %s::jsonb)", (json.dumps(payload2),)
        )
        conn.commit()
    msg2 = None
    for _ in range(5):
        for m in q.read(visibility_timeout_s=600, qty=50):
            if m.payload.get("sentinel") == payload2["sentinel"]:
                msg2 = m
                break
        if msg2 is not None:
            break
    if msg2 is None:
        failures.append("second sentinel not visible")
        print(f"  {FAIL} read#2")
    else:
        q.delete(msg2.msg_id)
        print(f"  {OK} delete msg_id={msg2.msg_id}")
        # confirm it's gone
        residual = [m for m in q.read(visibility_timeout_s=1, qty=100)
                    if m.payload.get("sentinel") == payload2["sentinel"]]
        if residual:
            failures.append("deleted message still visible")
            print(f"  {FAIL} delete did not remove msg")
        else:
            print(f"  {OK} delete verified")

    # --- Document-store round-trip ------------------------------------------
    print("\n=== document-store round-trip ===")
    # We need a user to satisfy FK. Create one via admin client if we have
    # service key handy; else fall back to reusing an existing doc row.
    ds = PostgresDocumentStore(dsn)
    doc_id: str | None = None
    created_user_id: str | None = None

    supa_url = os.environ.get("SUPABASE_URL")
    supa_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if supa_url and supa_key:
        from supabase import create_client
        admin = create_client(supa_url, supa_key)
        import secrets
        email = f"phase9-{secrets.token_hex(4)}@example.test"
        created = admin.auth.admin.create_user(
            {"email": email, "password": secrets.token_urlsafe(16), "email_confirm": True}
        )
        created_user_id = created.user.id
        doc_id = str(uuid.uuid4())
        with psycopg.connect(dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO doc_worker_v2.documents (id, user_id, filename, file_path, status)
                VALUES (%s, %s, %s, %s, 'pending')
                """,
                (doc_id, created_user_id, "phase9.pdf", f"{created_user_id}/{doc_id}/phase9.pdf"),
            )
            conn.commit()
        print(f"[setup] user={created_user_id[:8]}… doc={doc_id[:8]}…")

    if doc_id is None:
        print(f"  {FAIL} no way to seed a doc row (need SUPABASE_* to create user)")
        failures.append("could not seed doc row")
    else:
        try:
            ds.mark_processing(doc_id)
            ds.mark_retrying(doc_id, 1, 3, "simulated")
            ds.mark_failed(doc_id, "simulated-fail")
            ds.mark_completed(
                doc_id,
                page_count=11,
                artifacts={"markdown": "x/y.md", "docling_json": "x/y.docling.json"},
                pipeline="phase9_test",
            )

            with psycopg.connect(dsn) as conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT status, page_count, artifacts, pipeline, error_message "
                    "FROM doc_worker_v2.documents WHERE id = %s", (doc_id,))
                row = cur.fetchone()
            status, page_count, artifacts, pipeline, err = row
            checks = [
                (status == "completed", f"status={status}"),
                (page_count == 11, f"page_count={page_count}"),
                (artifacts == {"markdown": "x/y.md", "docling_json": "x/y.docling.json"},
                 f"artifacts={artifacts}"),
                (pipeline == "phase9_test", f"pipeline={pipeline}"),
                (err in (None, ""), f"error_message={err}"),
            ]
            for cond, label in checks:
                if cond:
                    print(f"  {OK} {label}")
                else:
                    failures.append(label)
                    print(f"  {FAIL} {label}")
        finally:
            # cleanup — delete doc row, then user
            with psycopg.connect(dsn) as conn, conn.cursor() as cur:
                cur.execute("DELETE FROM doc_worker_v2.documents WHERE id = %s", (doc_id,))
                conn.commit()
            if created_user_id and supa_url and supa_key:
                try:
                    admin.auth.admin.delete_user(created_user_id)
                except Exception:
                    pass

    print()
    if failures:
        print(f"FAILED ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("ALL PHASE 9 ROUND-TRIP CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
