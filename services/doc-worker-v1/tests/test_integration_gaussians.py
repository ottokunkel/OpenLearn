"""End-to-end integration test against live Modal + Supabase.

Gated by RUN_INTEGRATION_TESTS=1. Requires:
- SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, VLM_ENDPOINT_URL in env (or .env).
- TEST_USER_ID set to a real auth.users uuid (the FK on public.documents).
- The Modal VLM endpoint must be reachable.
"""
from __future__ import annotations

import json
import os

import pytest

pytestmark = pytest.mark.integration


@pytest.mark.skipif(
    os.environ.get("RUN_INTEGRATION_TESTS") != "1",
    reason="set RUN_INTEGRATION_TESTS=1 to hit live Modal + Supabase",
)
def test_gaussians_end_to_end(client, converter, seeded_document):
    from DoclingWorker import job_processor, queue

    document_id = seeded_document["document_id"]

    # Drain the queue until we find our message. Other tenants' messages are
    # simply skipped — their visibility timeout will re-release them naturally.
    my_msg = None
    for _ in range(10):
        msgs = queue.read(client, visibility_timeout_s=600, qty=10)
        my_msg = next(
            (m for m in msgs if m.message.get("document_id") == document_id),
            None,
        )
        if my_msg:
            break
    assert my_msg is not None, "did not find seeded message on the queue"

    # Run one job synchronously end-to-end.
    job_processor.process(my_msg, client, converter)

    # --- DB assertions -----------------------------------------------------
    row = (
        client.table("documents")
        .select("status,page_count,markdown_path,doc_json_path,error_message")
        .eq("id", document_id)
        .single()
        .execute()
        .data
    )
    assert row["status"] == "completed", row
    assert row["error_message"] in (None, ""), row
    assert row["page_count"] and 8 <= row["page_count"] <= 16, row
    assert row["markdown_path"] and row["doc_json_path"]

    # --- Storage assertions -------------------------------------------------
    md_bytes = client.storage.from_("documents").download(row["markdown_path"])
    md = md_bytes.decode("utf-8")
    assert len(md) > 500, f"markdown too short ({len(md)} bytes)"
    lowered = md.lower()
    assert any(tok in lowered for tok in ("multivariate", "covariance", "gaussian")), \
        "expected Gaussians content tokens not found in markdown"

    doctags_bytes = client.storage.from_("documents").download(row["doc_json_path"])
    doctags = json.loads(doctags_bytes.decode("utf-8"))
    assert isinstance(doctags, dict)
    assert "pages" in doctags
    assert len(doctags["pages"]) == row["page_count"]
