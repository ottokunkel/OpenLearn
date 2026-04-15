"""Shared pytest fixtures for the doc-worker-v2 test suite.

Integration fixtures (`admin_client`, `queue`, `docs`, `blobs`, `pipeline`,
`gaussians_pdf`, `seeded_document`) require real credentials and are gated by
`RUN_INTEGRATION_TESTS=1`. Unit tests (test_processor.py) don't touch any of
this — they use in-memory fakes.
"""
from __future__ import annotations

import os
import secrets
import uuid
from pathlib import Path

import pytest
import requests

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURE_URL = "https://cs229.stanford.edu/section/gaussians.pdf"
FIXTURE_PATH = Path(__file__).parent / "fixtures" / "gaussians.pdf"


def _load_repo_env() -> None:
    """Load repo-root .env so Settings() resolves SUPABASE_* / VLM_ENDPOINT_URL."""
    from dotenv import load_dotenv

    load_dotenv(REPO_ROOT / ".env")


@pytest.fixture(scope="session")
def settings():
    _load_repo_env()
    from doc_worker_v2.config import Settings

    return Settings()


@pytest.fixture(scope="session")
def queue(settings):
    from doc_worker_v2 import di

    return di.build_queue(settings)


@pytest.fixture(scope="session")
def docs(settings):
    from doc_worker_v2 import di

    return di.build_document_store(settings)


@pytest.fixture(scope="session")
def blobs(settings):
    from doc_worker_v2 import di

    return di.build_blob_store(settings)


@pytest.fixture(scope="session")
def pipeline(settings):
    from doc_worker_v2 import di

    return di.build_pipeline(settings)


@pytest.fixture(scope="session")
def admin_client(settings):
    """Service-role Supabase client for seeding + teardown."""
    from supabase import create_client

    assert settings.supabase_url and settings.supabase_service_role_key
    return create_client(settings.supabase_url, settings.supabase_service_role_key)


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
def test_user_id(admin_client):
    """Use TEST_USER_ID if set, else create + tear down a throwaway auth user."""
    existing = os.environ.get("TEST_USER_ID")
    if existing:
        yield existing
        return

    suffix = secrets.token_hex(4)
    email = f"v2-pytest-{suffix}@example.test"
    password = secrets.token_urlsafe(16)
    created = admin_client.auth.admin.create_user(
        {"email": email, "password": password, "email_confirm": True}
    )
    user_id = created.user.id
    try:
        yield user_id
    finally:
        try:
            admin_client.auth.admin.delete_user(user_id)
        except Exception:
            pass


@pytest.fixture
def seeded_document(admin_client, gaussians_pdf, test_user_id, settings):
    """Upload PDF + insert `doc_worker_v2.documents` row (trigger enqueues).

    Cleans up storage objects and the row on teardown.
    """
    document_id = str(uuid.uuid4())
    filename = "gaussians.pdf"
    file_path = f"{test_user_id}/{document_id}/{filename}"
    bucket = settings.supabase_storage_bucket

    admin_client.storage.from_(bucket).upload(
        file_path,
        gaussians_pdf,
        {"content-type": "application/pdf", "upsert": "true"},
    )
    admin_client.schema("doc_worker_v2").table("documents").insert({
        "id": document_id,
        "user_id": test_user_id,
        "filename": filename,
        "file_path": file_path,
    }).execute()

    yield {
        "document_id": document_id,
        "user_id": test_user_id,
        "file_path": file_path,
        "filename": filename,
    }

    # Teardown — best-effort.
    try:
        admin_client.schema("doc_worker_v2").table("documents").delete().eq(
            "id", document_id
        ).execute()
    except Exception:
        pass
    prefix = f"{test_user_id}/{document_id}"
    try:
        objs = admin_client.storage.from_(bucket).list(prefix) or []
        if objs:
            admin_client.storage.from_(bucket).remove(
                [f"{prefix}/{o['name']}" for o in objs]
            )
    except Exception:
        pass
