#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# dependencies = [
#   "python-dotenv>=1.0",
#   "supabase>=2.11",
#   "doc-worker-v2[postgres]",
# ]
# [tool.uv.sources]
# doc-worker-v2 = { path = "..", editable = true }
# ///
"""Phase 9 end-to-end: run doc-worker-v2 with mixed backends
(postgres queue + docs, supabase blobs), drive a Gaussians job, assert
completion, then cleanly SIGTERM the worker.

Requires env (loaded from repo-root .env):
  SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, POSTGRES_DSN, VLM_ENDPOINT_URL

Run from repo root:
    POSTGRES_DSN=postgresql://... VLM_ENDPOINT_URL=... \
        uv run services/doc-worker-v2/tests/verify_phase9_e2e.py
"""
from __future__ import annotations

import os
import secrets
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

from dotenv import load_dotenv
from supabase import create_client

REPO_ROOT = Path(__file__).resolve().parents[3]
load_dotenv(REPO_ROOT / ".env")

FIXTURE = Path(__file__).parent / "fixtures" / "gaussians.pdf"
OK = "\033[32m✓\033[0m"
FAIL = "\033[31m✗\033[0m"


def main() -> int:
    for key in ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY", "POSTGRES_DSN", "VLM_ENDPOINT_URL"):
        if not os.environ.get(key):
            print(f"missing {key}")
            return 2

    url = os.environ["SUPABASE_URL"]
    service_key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    admin = create_client(url, service_key)

    failures: list[str] = []
    worker: subprocess.Popen[str] | None = None

    # --- seed user + PDF + row ---------------------------------------------
    suffix = secrets.token_hex(4)
    email = f"v2-phase9-{suffix}@example.test"
    user_id = admin.auth.admin.create_user(
        {"email": email, "password": secrets.token_urlsafe(16), "email_confirm": True}
    ).user.id
    doc_id = str(uuid.uuid4())
    filename = "gaussians.pdf"
    file_path = f"{user_id}/{doc_id}/{filename}"
    print(f"[setup] user={user_id[:8]}… doc={doc_id[:8]}…")

    try:
        admin.storage.from_("documents").upload(
            file_path, FIXTURE.read_bytes(),
            {"content-type": "application/pdf", "upsert": "true"},
        )
        print(f"  {OK} uploaded {file_path}")

        # --- start worker subprocess with mixed backends -------------------
        env = os.environ.copy()
        env["QUEUE_BACKEND"] = "postgres"
        env["DOCUMENT_STORE_BACKEND"] = "postgres"
        env["BLOB_STORE_BACKEND"] = "supabase"
        env["POLL_INTERVAL_S"] = "2"
        env["LOG_LEVEL"] = "INFO"

        print("[worker] spawning")
        worker = subprocess.Popen(
            ["uv", "run", "--no-sync", "doc-worker-v2"],
            cwd=str(Path(__file__).resolve().parents[1]),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

        # wait for 'online'
        online = False
        start = time.monotonic()
        assert worker.stdout is not None
        while time.monotonic() - start < 30:
            line = worker.stdout.readline()
            if not line:
                if worker.poll() is not None:
                    break
                continue
            print(f"  [worker] {line.rstrip()}")
            if "online" in line:
                online = True
                break
        if not online:
            failures.append("worker never reached 'online'")
            return 1
        print(f"  {OK} worker online (mixed backends)")

        # --- insert doc row via service role (trigger enqueues) -----------
        admin.schema("doc_worker_v2").table("documents").insert({
            "id": doc_id, "user_id": user_id,
            "filename": filename, "file_path": file_path,
        }).execute()
        print(f"  {OK} inserted row; trigger enqueued msg to pgmq.doc_ingest")

        # --- wait for completion (worker uses postgres queue to read) -----
        deadline = time.monotonic() + 180
        status = None
        while time.monotonic() < deadline:
            # Drain any new worker log lines so we can see progress.
            while worker.stdout.readable():
                line = worker.stdout.readline()
                if not line:
                    break
                print(f"  [worker] {line.rstrip()}")
                if not line.strip():
                    break
            row = admin.schema("doc_worker_v2").table("documents").select(
                "status, page_count, artifacts, pipeline, error_message"
            ).eq("id", doc_id).single().execute().data
            status = row["status"]
            if status in ("completed", "failed"):
                break
            time.sleep(2)

        print(f"[row] {row}")
        if row["status"] != "completed":
            failures.append(f"status={row['status']!r} error={row['error_message']!r}")
            print(f"  {FAIL} status={row['status']}")
        else:
            print(f"  {OK} status=completed")
        if not (8 <= (row["page_count"] or 0) <= 16):
            failures.append(f"page_count={row['page_count']}")
            print(f"  {FAIL} page_count={row['page_count']}")
        else:
            print(f"  {OK} page_count={row['page_count']}")
        artifacts = row["artifacts"] or {}
        for key in ("markdown", "docling_json"):
            if key not in artifacts:
                failures.append(f"artifacts missing {key}")
                print(f"  {FAIL} missing {key}")
            else:
                obj = admin.storage.from_("documents").download(artifacts[key])
                if not obj:
                    failures.append(f"{key} empty")
                    print(f"  {FAIL} {key} empty")
                else:
                    print(f"  {OK} {key} ({len(obj)} bytes) @ {artifacts[key]}")

    finally:
        if worker is not None and worker.poll() is None:
            print("[worker] SIGTERM")
            worker.send_signal(signal.SIGTERM)
            try:
                worker.wait(timeout=15)
            except subprocess.TimeoutExpired:
                worker.kill()
            tail = (worker.stdout.read() or "") if worker.stdout else ""
            for ln in tail.splitlines():
                print(f"  [worker] {ln}")
        try:
            admin.schema("doc_worker_v2").table("documents").delete().eq("id", doc_id).execute()
        except Exception:
            pass
        try:
            prefix = f"{user_id}/{doc_id}"
            objs = admin.storage.from_("documents").list(prefix) or []
            if objs:
                admin.storage.from_("documents").remove(
                    [f"{prefix}/{o['name']}" for o in objs]
                )
        except Exception:
            pass
        try:
            admin.auth.admin.delete_user(user_id)
        except Exception:
            pass

    print()
    if failures:
        print(f"FAILED ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PHASE 9 E2E (mixed backends) PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
