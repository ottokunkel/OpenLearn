"""End-to-end integration test against live Supabase + Modal.

Gated by RUN_INTEGRATION_TESTS=1. Requires:
- SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, VLM_ENDPOINT_URL in repo-root .env.
- TEST_USER_ID set to a real auth.users uuid (FK on doc_worker_v2.documents).
- Modal VLM endpoint reachable.

Run from repo root:

    RUN_INTEGRATION_TESTS=1 TEST_USER_ID=<uuid> \\
        uv run --directory services/doc-worker-v2 --with pytest \\
        pytest tests/test_integration_gaussians.py -v
"""
from __future__ import annotations

import os
import time

import pytest

pytestmark = pytest.mark.integration


@pytest.mark.skipif(
    os.environ.get("RUN_INTEGRATION_TESTS") != "1",
    reason="set RUN_INTEGRATION_TESTS=1 to hit live Supabase + Modal",
)
def test_gaussians_end_to_end(
    settings, queue, docs, blobs, pipeline, admin_client, seeded_document
):
    from doc_worker_v2 import processor

    document_id = seeded_document["document_id"]
    bucket = settings.supabase_storage_bucket

    # Drain the queue until we find our message. Other tenants' messages are
    # skipped — their VT will re-release them naturally.
    my_msg = None
    for _ in range(10):
        for m in queue.read(visibility_timeout_s=600, qty=50):
            if m.payload.get("document_id") == document_id:
                my_msg = m
                break
        if my_msg:
            break
        time.sleep(1)
    assert my_msg is not None, "did not find seeded message on the queue"

    # Run one job synchronously end-to-end (cold start can be 30-60s).
    processor.process(my_msg, queue, docs, blobs, pipeline, max_retries=3)

    # --- DB assertions -----------------------------------------------------
    row = (
        admin_client.schema("doc_worker_v2")
        .table("documents")
        .select("status, page_count, artifacts, pipeline, error_message")
        .eq("id", document_id)
        .single()
        .execute()
        .data
    )
    assert row["status"] == "completed", row
    assert row["error_message"] in (None, ""), row
    assert row["page_count"] and 8 <= row["page_count"] <= 16, row
    assert row["pipeline"] == pipeline.name, row

    artifacts = row["artifacts"] or {}
    assert "markdown" in artifacts, artifacts
    assert "docling_json" in artifacts, artifacts

    # --- Storage assertions ------------------------------------------------
    md_bytes = admin_client.storage.from_(bucket).download(artifacts["markdown"])
    md = md_bytes.decode("utf-8", errors="replace")
    assert len(md) > 500, f"markdown too short ({len(md)} bytes)"
    lowered = md.lower()
    assert any(tok in lowered for tok in ("multivariate", "covariance", "gaussian")), \
        "expected Gaussians content tokens not found in markdown"

    docling_bytes = admin_client.storage.from_(bucket).download(artifacts["docling_json"])
    assert len(docling_bytes) > 0
