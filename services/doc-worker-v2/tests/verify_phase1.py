#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# dependencies = [
#   "supabase>=2.11",
#   "python-dotenv>=1.0",
# ]
# ///
"""Phase 1 client-level verification.

Exercises the two manual checks from the plan as real Supabase client calls:

  1. RLS on `doc_worker_v2.documents`: user_a can read own row, user_b cannot.
  2. Archive wrapper populates `pgmq.a_doc_ingest`: service role archives the
     message enqueued by the trigger; we print the archived msg_id so the
     caller can confirm via `select * from pgmq.a_doc_ingest where msg_id = ...`.

Run from repo root:

    uv run services/doc-worker-v2/tests/verify_phase1.py

Requires the following env vars (auto-loaded from repo-root .env):
    SUPABASE_URL
    SUPABASE_SERVICE_ROLE_KEY
    SUPABASE_ANON_KEY

Also requires `doc_worker_v2` to be listed in Supabase → Project Settings →
API → Exposed schemas (PostgREST).
"""
from __future__ import annotations

import os
import secrets
import sys
from pathlib import Path

from dotenv import load_dotenv
from supabase import Client, create_client

REPO_ROOT = Path(__file__).resolve().parents[3]
load_dotenv(REPO_ROOT / ".env")

SUPABASE_URL = os.environ["SUPABASE_URL"]
SERVICE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
ANON_KEY = os.environ["SUPABASE_ANON_KEY"]

OK = "\033[32m✓\033[0m"
FAIL = "\033[31m✗\033[0m"


def _sb(key: str) -> Client:
    return create_client(SUPABASE_URL, key)


def _mk_user(admin: Client, email: str, password: str) -> str:
    resp = admin.auth.admin.create_user(
        {"email": email, "password": password, "email_confirm": True}
    )
    return resp.user.id


def _auth_client(email: str, password: str) -> Client:
    c = _sb(ANON_KEY)
    c.auth.sign_in_with_password({"email": email, "password": password})
    return c


def main() -> int:
    admin = _sb(SERVICE_KEY)
    suffix = secrets.token_hex(4)
    email_a = f"v2-verify-a-{suffix}@example.test"
    email_b = f"v2-verify-b-{suffix}@example.test"
    password = secrets.token_urlsafe(16)

    user_a_id = _mk_user(admin, email_a, password)
    user_b_id = _mk_user(admin, email_b, password)
    print(f"[setup] user_a={user_a_id[:8]}… user_b={user_b_id[:8]}…")

    failures: list[str] = []
    archived_msg_id: int | None = None

    try:
        client_a = _auth_client(email_a, password)
        client_b = _auth_client(email_b, password)

        # --- RLS check ------------------------------------------------------
        doc_a = (
            client_a.schema("doc_worker_v2").table("documents")
            .insert({
                "user_id": user_a_id,
                "filename": "rls-test.pdf",
                "file_path": f"{user_a_id}/rls/rls-test.pdf",
            })
            .execute().data[0]
        )
        doc_id = doc_a["id"]
        print(f"[rls] user_a inserted doc_id={doc_id}")

        rows_a = (
            client_a.schema("doc_worker_v2").table("documents")
            .select("id").eq("id", doc_id).execute().data
        )
        if len(rows_a) == 1:
            print(f"  {OK} user_a reads own row")
        else:
            failures.append(f"user_a expected 1 row, got {len(rows_a)}")
            print(f"  {FAIL} user_a expected 1 row, got {len(rows_a)}")

        rows_b = (
            client_b.schema("doc_worker_v2").table("documents")
            .select("id").eq("id", doc_id).execute().data
        )
        if len(rows_b) == 0:
            print(f"  {OK} user_b blocked by RLS (got 0 rows)")
        else:
            failures.append(f"user_b expected 0 rows, got {len(rows_b)}: {rows_b}")
            print(f"  {FAIL} user_b expected 0 rows, got {len(rows_b)}: {rows_b}")

        # --- Archive check --------------------------------------------------
        # Read up to 50 messages; find ours. VT=5s is short so we don't hide
        # real messages from the worker for long.
        read_resp = admin.rpc(
            "pgmq_read_doc_ingest", {"vt": 5, "qty": 50}
        ).execute()
        msgs = read_resp.data or []
        my_msg = next(
            (m for m in msgs if m["message"].get("document_id") == doc_id), None
        )
        if my_msg is None:
            failures.append(
                f"could not find my enqueued message; read {len(msgs)} msgs"
            )
            print(f"  {FAIL} enqueued message not found (trigger may not have fired)")
        else:
            my_msg_id = my_msg["msg_id"]
            print(f"[archive] found enqueued msg_id={my_msg_id}, archiving…")
            arch_resp = admin.rpc(
                "pgmq_archive_doc_ingest", {"msg_id": my_msg_id}
            ).execute()
            if arch_resp.data is True:
                archived_msg_id = my_msg_id
                print(f"  {OK} pgmq_archive_doc_ingest({my_msg_id}) returned true")
            else:
                failures.append(f"archive returned {arch_resp.data!r}, expected True")
                print(f"  {FAIL} archive returned {arch_resp.data!r}")

    finally:
        # Cleanup — user delete cascades to documents via FK ON DELETE CASCADE
        for uid in (user_a_id, user_b_id):
            try:
                admin.auth.admin.delete_user(uid)
            except Exception as e:
                print(f"[cleanup] warning deleting user {uid[:8]}…: {e}")

    print()
    if failures:
        print(f"FAILED ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("ALL PHASE 1 CLIENT VERIFICATIONS PASSED")
    if archived_msg_id is not None:
        print(f"ARCHIVED_MSG_ID={archived_msg_id}  "
              f"(verify: select * from pgmq.a_doc_ingest where msg_id = {archived_msg_id};)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
