"""Tests for worker/embedder.py."""

import pytest
import responses
from requests.exceptions import HTTPError

from worker.embedder import Embedder


@pytest.fixture
def embedder():
    return Embedder(
        api_url="http://embed.test/v1",
        api_key="test-key",
        model="text-embedding-3-small",
        dimensions=1536,
        batch_size=2,
    )


def _resp(embeddings, start=0):
    return {
        "data": [
            {"embedding": e, "index": start + i} for i, e in enumerate(embeddings)
        ]
    }


class TestEmbedder:
    @responses.activate
    def test_embed_single(self, embedder):
        responses.post("http://embed.test/v1/embeddings", json=_resp([[0.1, 0.2]]))
        assert embedder.embed_single("hi") == [0.1, 0.2]

    @responses.activate
    def test_embed_batch_splits(self, embedder):
        """batch_size=2 with 3 texts → 2 API calls."""
        responses.post("http://embed.test/v1/embeddings", json=_resp([[1.0], [2.0]]))
        responses.post("http://embed.test/v1/embeddings", json=_resp([[3.0]]))
        result = embedder.embed_batch(["a", "b", "c"])
        assert result == [[1.0], [2.0], [3.0]]
        assert len(responses.calls) == 2

    @responses.activate
    def test_api_error(self, embedder):
        responses.post("http://embed.test/v1/embeddings", json={}, status=500)
        with pytest.raises(HTTPError):
            embedder.embed_single("fail")
