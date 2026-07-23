import hashlib
import math
from typing import Protocol

from openai import OpenAI

from app.rag.exceptions import EmbeddingError, RagConfigurationError


class EmbeddingProvider(Protocol):
    def embed_texts(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


class OpenAIEmbeddingProvider:
    def __init__(
        self,
        *,
        api_key: str | None,
        model: str,
        dimensions: int,
        batch_size: int = 50,
        client: OpenAI | None = None,
    ) -> None:
        if not api_key:
            raise RagConfigurationError("OPENAI_API_KEY is not configured")
        if dimensions <= 0:
            raise ValueError("Embedding dimensions must be positive")
        if batch_size <= 0:
            raise ValueError("Embedding batch size must be positive")
        self.model = model
        self.dimensions = dimensions
        self.batch_size = batch_size
        self._client = client or OpenAI(api_key=api_key)

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if any(not text.strip() for text in texts):
            raise EmbeddingError("Cannot embed empty text")

        embeddings: list[list[float]] = []
        try:
            for start in range(0, len(texts), self.batch_size):
                batch = texts[start : start + self.batch_size]
                response = self._client.embeddings.create(
                    input=batch,
                    model=self.model,
                    dimensions=self.dimensions,
                    encoding_format="float",
                )
                ordered = sorted(response.data, key=lambda item: item.index)
                vectors = [list(item.embedding) for item in ordered]
                if len(vectors) != len(batch):
                    raise EmbeddingError(
                        "OpenAI returned a different number of embeddings "
                        "than input texts"
                    )
                self._validate_dimensions(vectors)
                embeddings.extend(vectors)
        except (EmbeddingError, RagConfigurationError):
            raise
        except Exception as exc:
            raise EmbeddingError("OpenAI embeddings request failed") from exc
        return embeddings

    def embed_query(self, text: str) -> list[float]:
        return self.embed_texts([text])[0]

    def _validate_dimensions(self, vectors: list[list[float]]) -> None:
        if any(len(vector) != self.dimensions for vector in vectors):
            raise EmbeddingError(
                "OpenAI returned an embedding with an unexpected dimension"
            )


class FakeEmbeddingProvider:
    def __init__(
        self,
        dimensions: int = 16,
        model: str = "fake-embedding",
    ) -> None:
        if dimensions <= 0:
            raise ValueError("Embedding dimensions must be positive")
        self.dimensions = dimensions
        self.model = model
        self.calls = 0

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        return [self._embed(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self.embed_texts([text])[0]

    def _embed(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        values = [
            (digest[index % len(digest)] / 127.5) - 1.0
            for index in range(self.dimensions)
        ]
        norm = math.sqrt(sum(value * value for value in values))
        return [value / norm for value in values] if norm else values
