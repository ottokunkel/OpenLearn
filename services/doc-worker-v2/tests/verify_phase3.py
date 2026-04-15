#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# dependencies = [
#   "supabase>=2.11",
#   "python-dotenv>=1.0",
#   "storage-supabase",
# ]
# [tool.uv.sources]
# storage-supabase = { path = "../../storage-supabase", editable = true }
# ///
"""Phase 3 live Supabase-backend checks.

Exercises SupabaseQueue, SupabaseDocumentStore, SupabaseBlobStore against the
real project. Creates a throwaway user, inserts a row (trigger enqueues), and
drives each backend through its happy path. Cleans up on exit.
"""
from __future__ import annotations

import os
import secrets
import sys
from pathlib import Path

from dotenv import load_dotenv
from supabase import create_client

REPO_ROOT = Path(__file__).resolve().parents[3]
load_dotenv(REPO_ROOT / ".env")

URL = os.environ["SUPABASE_URL"]
SERVICE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]

OK = "\033[32m✓\033[0m"
FAIL = "\033[31m✗\033[0m"


def main() -> int:
    from storage_supabase.blobs import SupabaseBlobStore
    from storage_supabase.documents import SupabaseDocumentStore
    from storage_supabase.queue import SupabaseQueue

    admin = create_client(URL, SERVICE_KEY)
    suffix = secrets.token_hex(4)
    email = f"v2-phase3-{suffix}@example.test"
    password = secrets.token_urlsafe(16)

    user = admin.auth.admin.create_user(
        {"email": email, "password": password, "email_confirm": True}
    ).user
    user_id = user.id
    print(f"[setup] user={user_id[:8]}…")

    failures: list[str] = []
    msg_id_to_cleanup: int | None = None
    try:
        # Sign in as the user so RLS lets us insert
        anon_key = os.environ["SUPABASE_ANON_KEY"]
        u_client = create_client(URL, anon_key)
        u_client.auth.sign_in_with_password({"email": email, "password": password})

        # Use the BlobStore as the user to upload an PDF-ish blob the worker
        # can "download" later. Also exercises the upsert path.
        blobs = SupabaseBlobStore(URL, SERVICE_KEY, "documents")
        pdf_path = f"{user_id}/phase3/test.pdf"
        blobs.upload(pdf_path, b"%PDF-1.4 fake", "application/pdf")
        downloaded = blobs.download(pdf_path)
        if downloaded != b"%PDF-1.4 fake":
            failures.append(f"blob round-trip mismatch: {downloaded!r}")
            print(f"  {FAIL} blob round-trip")
        else:
            print(f"  {OK} blob upload + download round-trip")

        # Insert a documents row AS THE USER (so RLS accepts it).
        doc = (
            u_client.schema("doc_worker_v2").table("documents")
            .insert({
                "user_id": user_id,
                "filename": "phase3.pdf",
                "file_path": pdf_path,
            })
            .execute().data[0]
        )
        doc_id = doc["id"]
        print(f"[docs] inserted doc_id={doc_id}")

        # DocumentStore runs as service_role and flips the status.
        docs = SupabaseDocumentStore(URL, SERVICE_KEY)
        docs.mark_processing(doc_id)
        row = (
            admin.schema("doc_worker_v2").table("documents")
            .select("status, error_message")
            .eq("id", doc_id).execute().data[0]
        )
        if row["status"] != "processing":
            failures.append(f"mark_processing status={row['status']!r}")
            print(f"  {FAIL} mark_processing -> {row['status']}")
        else:
            print(f"  {OK} mark_processing -> processing")

        docs.mark_retrying(doc_id, attempt=1, max_attempts=3, error="boom")
        row = (
            admin.schema("doc_worker_v2").table("documents")
            .select("status, error_message")
            .eq("id", doc_id).execute().data[0]
        )
        if row["status"] != "retrying" or "attempt 1/3" not in (row["error_message"] or ""):
            failures.append(f"mark_retrying row={row}")
            print(f"  {FAIL} mark_retrying: {row}")
        else:
            print(f"  {OK} mark_retrying -> retrying, error='{row['error_message']}'")

        docs.mark_completed(
            doc_id,
            page_count=5,
            artifacts={"markdown": f"{user_id}/{doc_id}/out.md"},
            pipeline="granite_docling_vlm",
        )
        row = (
            admin.schema("doc_worker_v2").table("documents")
            .select("status, page_count, artifacts, pipeline, error_message")
            .eq("id", doc_id).execute().data[0]
        )
        if (
            row["status"] != "completed"
            or row["page_count"] != 5
            or row["pipeline"] != "granite_docling_vlm"
            or row["artifacts"].get("markdown") != f"{user_id}/{doc_id}/out.md"
            or row["error_message"] is not None
        ):
            failures.append(f"mark_completed row={row}")
            print(f"  {FAIL} mark_completed: {row}")
        else:
            print(f"  {OK} mark_completed -> {row}")

        # Queue: find the enqueued message via the real backend and archive it.
        q = SupabaseQueue(URL, SERVICE_KEY)
        msgs = q.read(visibility_timeout_s=5, qty=50)
        mine = next((m for m in msgs if m.payload.get("document_id") == doc_id), None)
        if mine is None:
            failures.append(f"queue.read didn't find our msg (got {len(msgs)})")
            print(f"  {FAIL} queue.read: our message not found")
        else:
            print(f"  {OK} queue.read: msg_id={mine.msg_id} read_ct={mine.read_ct}")
            msg_id_to_cleanup = mine.msg_id
            q.archive(mine.msg_id)
            print(f"  {OK} queue.archive(msg_id={mine.msg_id})")

        # Exercise mark_failed last so the final row shows 'failed' — purely for visibility.
        docs.mark_failed(doc_id, "terminal")
        print(f"  {OK} mark_failed path exercised")

    finally:
        # ON DELETE CASCADE drops the documents row; storage objects we leave
        # to cleanup to Supabase retention.
        try:
            admin.auth.admin.delete_user(user_id)
        except Exception as e:
            print(f"[cleanup] user delete warning: {e}")

    print()
    if failures:
        print(f"FAILED ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("ALL PHASE 3 CHECKS PASSED")
    if msg_id_to_cleanup is not None:
        print(f"(archived msg_id={msg_id_to_cleanup})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
