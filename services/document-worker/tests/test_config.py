"""Tests for worker/config.py."""

import dataclasses

import pytest

from worker.config import Settings, load_settings


def test_defaults():
    s = Settings()
    assert s.redis_url == "redis://localhost:6379/0"
    assert s.vlm_model == "qwen/qwen3.5-flash-02-23"
    assert s.embedding_dimensions == 1536


def test_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        Settings().redis_url = "x"


def test_load_from_env(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://custom:6379/1")
    monkeypatch.setenv("DOCUMENT_TIMEOUT_SECONDS", "60")
    s = load_settings()
    assert s.redis_url == "redis://custom:6379/1"
    assert s.document_timeout_seconds == 60
