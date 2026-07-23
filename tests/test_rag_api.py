from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies import (
    get_openai_embedding_provider,
    get_retrieval_service,
    get_supabase_knowledge_repository,
)
from app.core.config import get_settings
from app.main import app
from tests.test_rag_retrieval import prepared_service


@pytest.fixture(autouse=True)
def clear_rag_dependencies() -> Iterator[None]:
    app.dependency_overrides.clear()
    get_settings.cache_clear()
    get_supabase_knowledge_repository.cache_clear()
    get_openai_embedding_provider.cache_clear()
    yield
    app.dependency_overrides.clear()
    get_settings.cache_clear()
    get_supabase_knowledge_repository.cache_clear()
    get_openai_embedding_provider.cache_clear()


def test_rag_health_without_openai(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)

    with TestClient(app) as client:
        response = client.get("/api/v1/rag/health")

    assert response.status_code == 200
    assert response.json() == {
        "configured": False,
        "database_configured": False,
        "openai_configured": False,
        "embedding_model": "text-embedding-3-small",
        "embedding_dimensions": 1536,
        "index_statistics": None,
    }


def test_rag_search_with_fake_dependencies() -> None:
    service, chunks = prepared_service()
    app.dependency_overrides[get_retrieval_service] = lambda: service

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/rag/search",
            json={
                "query": chunks[0].content,
                "top_k": 1,
                "similarity_threshold": -1.0,
                "document_types": ["regulation"],
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["query"] == chunks[0].content
    assert body["mode"] == "hybrid"
    assert body["count"] == 1
    assert body["results"][0]["document_type"] == "regulation"
    assert "section_path" in body["results"][0]


def test_rag_search_accepts_semantic_mode() -> None:
    service, chunks = prepared_service()
    app.dependency_overrides[get_retrieval_service] = lambda: service

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/rag/search",
            json={
                "query": chunks[0].content,
                "mode": "semantic",
                "top_k": 1,
            },
        )

    assert response.status_code == 200
    assert response.json()["mode"] == "semantic"


def test_rag_index_endpoint_is_disabled_by_default(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ENABLE_RAG_INDEX_ENDPOINT", raising=False)

    with TestClient(app) as client:
        response = client.post("/api/v1/rag/index", json={})

    assert response.status_code == 404
    assert response.json()["detail"] == "RAG indexing endpoint is disabled"
