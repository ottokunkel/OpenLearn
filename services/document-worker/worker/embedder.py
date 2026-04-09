"""Embedding client for OpenAI-compatible APIs with batching support."""

import logging
from dataclasses import dataclass

import requests

from worker.config import Settings

logger = logging.getLogger(__name__)


@dataclass
class Embedder:
    """Generates embeddings via any OpenAI-compatible /v1/embeddings endpoint."""

    api_url: str
    api_key: str
    model: str
    dimensions: int
    batch_size: int = 100

    @classmethod
    def from_settings(cls, settings: Settings) -> "Embedder":
        return cls(
            api_url=settings.embedding_api_url,
            api_key=settings.embedding_api_key,
            model=settings.embedding_model,
            dimensions=settings.embedding_dimensions,
            batch_size=settings.embedding_batch_size,
        )

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed a list of texts, handling batching internally.

        Returns a list of embedding vectors in the same order as the input texts.
        """
        all_embeddings: list[list[float]] = []

        for i in range(0, len(texts), self.batch_size):
            batch = texts[i : i + self.batch_size]
            embeddings = self._call_api(batch)
            all_embeddings.extend(embeddings)

        return all_embeddings

    def embed_single(self, text: str) -> list[float]:
        """Embed a single text string."""
        return self._call_api([text])[0]

    def _call_api(self, texts: list[str]) -> list[list[float]]:
        """Call the embedding API for a batch of texts."""
        url = f"{self.api_url}/embeddings"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "input": texts,
            "dimensions": self.dimensions,
        }

        response = requests.post(url, json=payload, headers=headers, timeout=60)
        response.raise_for_status()

        data = response.json()
        # OpenAI format: {"data": [{"embedding": [...], "index": 0}, ...]}
        sorted_items = sorted(data["data"], key=lambda x: x["index"])
        return [item["embedding"] for item in sorted_items]
