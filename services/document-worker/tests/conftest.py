"""Shared fixtures for document-worker tests."""

from unittest.mock import MagicMock

import pytest

from worker.config import Settings


@pytest.fixture
def settings():
    """Return a Settings instance with test-safe defaults."""
    return Settings(
        redis_url="redis://localhost:6379/0",
        s3_endpoint_url="",
        s3_region="us-east-1",
        s3_access_key="testing",
        s3_secret_key="testing",
        s3_output_bucket="test-output",
        embedding_api_url="http://localhost:9999/v1",
        embedding_api_key="",
        embedding_model="text-embedding-3-small",
        embedding_dimensions=1536,
        embedding_batch_size=100,
        download_dir="/tmp/test_downloads",
        chunk_batch_size=100,
        database_url="",
    )


@pytest.fixture
def mock_pool():
    """Create a MagicMock simulating psycopg_pool.ConnectionPool context manager chain.

    Usage:
        pool, conn, cursor = mock_pool   (unpack the tuple)

    The pool supports:
        with pool.connection() as conn:
            conn.execute(...)
            with conn.cursor() as cur:
                cur.executemany(...)
            conn.commit()
    """
    pool = MagicMock()
    conn = MagicMock()
    cursor = MagicMock()

    # pool.connection() returns a context manager yielding conn
    pool.connection.return_value.__enter__ = MagicMock(return_value=conn)
    pool.connection.return_value.__exit__ = MagicMock(return_value=False)

    # conn.cursor() returns a context manager yielding cursor
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    return pool, conn, cursor
