"""Shared pytest fixtures for the doc-worker-v1 test suite.

Integration fixtures (`client`, `converter`, `gaussians_pdf`, `seeded_document`)
require real credentials. They are lazily resolved so unit-only runs stay offline.
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest
import requests

FIXTURE_URL = "https://cs229.stanford.edu/section/gaussians.pdf"
FIXTURE_PATH = Path(__file__).parent / "fixtures" / "gaussians.pdf"


@pytest.fixture(scope="session")
def cfg():
    from doc_worker import config as config_mod

    return config_mod.load()


@pytest.fixture(scope="session")
def client(cfg):
    from doc_worker import supabase

    return supabase.get_client(cfg)


@pytest.fixture(scope="session")
def converter(cfg):
    from doc_worker.docling import build_converter

    return build_converter(cfg)


@pytest.fixture(scope="session")
def gaussians_pdf() -> bytes:
    if not FIXTURE_PATH.exists():
        FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
        resp = requests.get(FIXTURE_URL, timeout=60)
        if resp.status_code != 200:
            pytest.skip(f"could not fetch fixture PDF: HTTP {resp.status_code}")
        FIXTURE_PATH.write_bytes(resp.content)
    return FIXTURE_PATH.read_bytes()


@pytest.fixture
def test_user_id() -> str:
    val = os.environ.get("TEST_USER_ID")
    if not val:
        pytest.skip("set TEST_USER_ID to a real auth.users uuid for this test")
    return val


@pytest.fixture
def seeded_document(client, gaussians_pdf, test_user_id):
    """Seed a documents row + storage object + pgmq job; clean everything up on teardown."""
    document_id = str(uuid.uuid4())
    filename = "gaussians.pdf"
    file_path = f"{test_user_id}/{document_id}/{filename}"

    client.storage.from_("documents").upload(
        file_path,
        gaussians_pdf,
        {"content-type": "application/pdf"},
    )
    client.table("documents").insert({
        "id": document_id,
        "user_id": test_user_id,
        "filename": filename,
        "file_path": file_path,
        "status": "pending",
        "metadata": {"source": "integration-test"},
    }).execute()
    client.rpc("enqueue_document_job", {
        "p_document_id": document_id,
        "p_user_id": test_user_id,
        "p_file_path": file_path,
        "p_filename": filename,
        "p_metadata": {"source": "integration-test"},
    }).execute()

    yield {
        "document_id": document_id,
        "user_id": test_user_id,
        "file_path": file_path,
        "filename": filename,
    }

    # Teardown — best effort, never fail a passing test because of cleanup noise.
    try:
        client.table("documents").delete().eq("id", document_id).execute()
    except Exception:
        pass
    prefix = f"{test_user_id}/{document_id}"
    try:
        objs = client.storage.from_("documents").list(prefix) or []
        if objs:
            client.storage.from_("documents").remove([f"{prefix}/{o['name']}" for o in objs])
    except Exception:
        pass
