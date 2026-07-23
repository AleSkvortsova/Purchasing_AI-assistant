import pytest

from app.rag.embeddings import FakeEmbeddingProvider, OpenAIEmbeddingProvider
from app.rag.exceptions import RagConfigurationError


def test_fake_embedding_provider_is_stable() -> None:
    provider = FakeEmbeddingProvider(dimensions=12)

    assert provider.embed_query("одинаковый текст") == provider.embed_query(
        "одинаковый текст"
    )
    assert provider.embed_query("первый текст") != provider.embed_query(
        "второй текст"
    )


def test_fake_embedding_provider_has_requested_dimensions() -> None:
    provider = FakeEmbeddingProvider(dimensions=7)

    vectors = provider.embed_texts(["один", "два"])

    assert len(vectors) == 2
    assert all(len(vector) == 7 for vector in vectors)


def test_openai_embedding_provider_requires_key() -> None:
    with pytest.raises(
        RagConfigurationError,
        match="OPENAI_API_KEY is not configured",
    ):
        OpenAIEmbeddingProvider(
            api_key=None,
            model="text-embedding-3-small",
            dimensions=1536,
        )
