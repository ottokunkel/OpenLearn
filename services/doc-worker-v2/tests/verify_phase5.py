#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# dependencies = [
#   "supabase>=2.11",
#   "python-dotenv>=1.0",
#   "doc-worker-v2",
# ]
# [tool.uv.sources]
# doc-worker-v2 = { path = "..", editable = true }
# ///
"""Phase 5 end-to-end verification against live Supabase + Modal.

1. Seeds a user + uploads gaussians.pdf to storage
2. Inserts a doc_worker_v2.documents row (trigger enqueues on doc_ingest)
3. Builds real backends + pipeline via di.build_*
4. Reads the enqueued message and invokes processor.process directly
5. Asserts row transitions to 'completed' with correct artifacts + page_count
6. Fetches the uploaded markdown object and spot-checks content

Then spawns the worker subprocess and SIGTERMs it after the online heartbeat
to verify graceful drain.

Run from repo root:
    uv run services/doc-worker-v2/tests/verify_phase5.py
"""
from __future__ import annotations

import os
import secrets
import signal
import subprocess
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from supabase import create_client

REPO_ROOT = Path(__file__).resolve().parents[3]
load_dotenv(REPO_ROOT / ".env")

OK = "\033[32m✓\033[0m"
FAIL = "\033[31m✗\033[0m"

FIXTURE = Path(__file__).parent / "fixtures" / "gaussians.pdf"


def _derive_vlm_url() -> str:
    if url := os.environ.get("VLM_ENDPOINT_URL"):
        return url
    if base := os.environ.get("MODAL_WEB_URL"):
        return base.rstrip("/") + "/v1/chat/completions"
    raise RuntimeError("Need VLM_ENDPOINT_URL or MODAL_WEB_URL in .env")


def run_end_to_end() -> tuple[list[str], dict]:
    """Returns (failures, context). Cleans up the throwaway user on teardown."""
    # Make sure VLM_ENDPOINT_URL is visible to Settings and any subprocess we spawn.
    os.environ["VLM_ENDPOINT_URL"] = _derive_vlm_url()

    from doc_worker_v2 import di, processor
    from doc_worker_v2.config import Settings

    url = os.environ["SUPABASE_URL"]
    service_key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    anon_key = os.environ["SUPABASE_ANON_KEY"]

    admin = create_client(url, service_key)

    suffix = secrets.token_hex(4)
    email = f"v2-phase5-{suffix}@example.test"
    password = secrets.token_urlsafe(16)
    user_id = admin.auth.admin.create_user(
        {"email": email, "password": password, "email_confirm": True}
    ).user.id
    print(f"[setup] user={user_id[:8]}…")

    ctx: dict = {"user_id": user_id}
    failures: list[str] = []

    try:
        pdf_bytes = FIXTURE.read_bytes()
        pdf_path = f"{user_id}/phase5/gaussians.pdf"

        # Upload as service role so we don't need bucket policies for the anon user.
        admin.storage.from_("documents").upload(
            pdf_path, pdf_bytes, {"content-type": "application/pdf", "upsert": "true"}
        )
        print(f"[setup] uploaded {pdf_path} ({len(pdf_bytes)} bytes)")

        # Insert row as the user so RLS accepts; trigger enqueues.
        user_client = create_client(url, anon_key)
        user_client.auth.sign_in_with_password({"email": email, "password": password})
        doc = (
            user_client.schema("doc_worker_v2").table("documents")
            .insert({
                "user_id": user_id,
                "filename": "gaussians.pdf",
                "file_path": pdf_path,
            })
            .execute().data[0]
        )
        doc_id = doc["id"]
        ctx["doc_id"] = doc_id
        print(f"[setup] inserted doc_id={doc_id}, trigger enqueued msg")

        # Build real backends and pipeline from env.
        s = Settings()
        print(f"[settings] pipeline={s.pipeline.value} backends=q:{s.queue_backend.value}/"
              f"d:{s.document_store_backend.value}/b:{s.blob_store_backend.value}")

        queue = di.build_queue(s)
        docs_backend = di.build_document_store(s)
        blobs = di.build_blob_store(s)
        pipeline = di.build_pipeline(s)
        print("[build] all backends + pipeline constructed")

        # Read up to 50 messages until we find ours (VT=30s).
        t0 = time.monotonic()
        our_msg = None
        for _ in range(3):
            for m in queue.read(visibility_timeout_s=30, qty=50):
                if m.payload.get("document_id") == doc_id:
                    our_msg = m
                    break
            if our_msg:
                break
            time.sleep(1)
        if our_msg is None:
            failures.append("could not read our enqueued message")
            print(f"  {FAIL} message not found")
            return failures, ctx
        print(f"  {OK} read msg_id={our_msg.msg_id} (trigger fired)")

        # Drive the worker's processor directly (cold-start is ~30-60s).
        print("[process] invoking processor.process — this can take 30-60s on cold start")
        t0 = time.monotonic()
        processor.process(our_msg, queue, docs_backend, blobs, pipeline, max_retries=3)
        elapsed = time.monotonic() - t0
        print(f"  {OK} processor.process returned in {elapsed:.1f}s")

        # Assert row state.
        row = (
            admin.schema("doc_worker_v2").table("documents")
            .select("status, page_count, artifacts, pipeline, error_message")
            .eq("id", doc_id).execute().data[0]
        )
        print(f"[row] {row}")
        if row["status"] != "completed":
            failures.append(f"status={row['status']!r}, want 'completed' (error={row['error_message']!r})")
            print(f"  {FAIL} status={row['status']}")
        else:
            print(f"  {OK} status=completed")
        if not (8 <= (row["page_count"] or 0) <= 16):
            failures.append(f"page_count={row['page_count']} outside [8,16]")
            print(f"  {FAIL} page_count={row['page_count']}")
        else:
            print(f"  {OK} page_count={row['page_count']}")
        if row["pipeline"] != pipeline.name:
            failures.append(f"pipeline={row['pipeline']!r}, want {pipeline.name!r}")
            print(f"  {FAIL} pipeline={row['pipeline']}")
        else:
            print(f"  {OK} pipeline={row['pipeline']}")

        # Artifacts map must include markdown + docling_json.
        artifacts = row["artifacts"] or {}
        for key in ("markdown", "docling_json"):
            if key not in artifacts:
                failures.append(f"artifacts missing {key!r}: {artifacts}")
                print(f"  {FAIL} artifacts missing {key}")
                continue
            path = artifacts[key]
            obj = admin.storage.from_("documents").download(path)
            if not obj or len(obj) == 0:
                failures.append(f"artifact {key} at {path} is empty")
                print(f"  {FAIL} {key} empty")
            else:
                print(f"  {OK} {key} @ {path} ({len(obj)} bytes)")
        # Spot-check markdown content.
        if "markdown" in artifacts:
            md = admin.storage.from_("documents").download(artifacts["markdown"]).decode(errors="replace").lower()
            if "gaussian" not in md and "covariance" not in md:
                failures.append("markdown missing expected keywords")
                print(f"  {FAIL} markdown missing keywords (first 200: {md[:200]!r})")
            else:
                print(f"  {OK} markdown contains 'gaussian'/'covariance'")

        # Message should have been deleted on success.
        remaining = queue.read(visibility_timeout_s=1, qty=50)
        if any(m.payload.get("document_id") == doc_id for m in remaining):
            failures.append("original message not deleted after success")
            print(f"  {FAIL} message still visible")
        else:
            print(f"  {OK} queue.delete happened")

    finally:
        try:
            admin.auth.admin.delete_user(user_id)
        except Exception as e:
            print(f"[cleanup] user delete warning: {e}")

    return failures, ctx


def run_sigterm_drain() -> list[str]:
    """Launch worker subprocess, SIGTERM it once online, verify clean exit."""
    failures: list[str] = []

    env = os.environ.copy()
    env["VLM_ENDPOINT_URL"] = _derive_vlm_url()
    # Short poll interval so the subprocess reaches 'online' quickly.
    env["POLL_INTERVAL_S"] = "2"
    env["LOG_LEVEL"] = "INFO"

    print("\n[sigterm] spawning worker subprocess")
    proc = subprocess.Popen(
        ["uv", "run", "--no-sync", "doc-worker-v2"],
        cwd=str(Path(__file__).resolve().parents[1]),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    online = False
    start = time.monotonic()
    online_line = ""
    assert proc.stdout is not None
    while time.monotonic() - start < 90:
        line = proc.stdout.readline()
        if not line:
            if proc.poll() is not None:
                break
            continue
        print(f"  [worker] {line.rstrip()}")
        if "online" in line:
            online = True
            online_line = line
            break

    if not online:
        failures.append("worker never logged 'online' within 90s")
        print(f"  {FAIL} never saw 'online' log")
        proc.kill()
        proc.wait(timeout=5)
        return failures

    print(f"  {OK} online: {online_line.rstrip()}")
    print("  [sigterm] sending SIGTERM…")
    proc.send_signal(signal.SIGTERM)

    exit_code = None
    try:
        exit_code = proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        failures.append("worker did not exit within 30s of SIGTERM")
        print(f"  {FAIL} hung on shutdown; killing")
        proc.kill()
        proc.wait(timeout=5)
        return failures

    # Drain remaining output.
    remaining = proc.stdout.read() or ""
    for ln in remaining.splitlines():
        print(f"  [worker] {ln}")

    if exit_code != 0:
        failures.append(f"worker exit_code={exit_code}")
        print(f"  {FAIL} exit_code={exit_code}")
    else:
        print(f"  {OK} exit_code=0")

    if "draining" not in (online_line + remaining) and "draining" not in online_line:
        # Check the last tail for 'draining' since it's printed after SIGTERM handler fires.
        if "draining" not in remaining:
            failures.append("missing 'draining' log after SIGTERM")
            print(f"  {FAIL} missing 'draining' log")
        else:
            print(f"  {OK} saw 'draining' log")
    else:
        print(f"  {OK} saw 'draining' log")
    if "exited cleanly" not in remaining:
        failures.append("missing 'exited cleanly' log")
        print(f"  {FAIL} missing 'exited cleanly' log")
    else:
        print(f"  {OK} saw 'exited cleanly' log")

    return failures


def main() -> int:
    all_failures: list[str] = []

    print("=== Phase 5 end-to-end ===")
    e2e_fails, _ = run_end_to_end()
    all_failures.extend(f"e2e: {f}" for f in e2e_fails)

    print("\n=== Phase 5 SIGTERM drain ===")
    sigterm_fails = run_sigterm_drain()
    all_failures.extend(f"sigterm: {f}" for f in sigterm_fails)

    print()
    if all_failures:
        print(f"FAILED ({len(all_failures)}):")
        for f in all_failures:
            print(f"  - {f}")
        return 1
    print("ALL PHASE 5 LIVE CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
