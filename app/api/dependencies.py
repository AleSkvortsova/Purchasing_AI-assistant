from functools import lru_cache
from typing import Annotated

from fastapi import Depends, HTTPException, status

from app.core.config import get_settings
from app.rag.embeddings import OpenAIEmbeddingProvider
from app.rag.indexing_service import KnowledgeIndexingService
from app.rag.repository import (
    KnowledgeRepository,
    SupabaseKnowledgeRepository,
)
from app.rag.retrieval_service import KnowledgeRetrievalService
from app.repositories.request import RequestRepository
from app.repositories.supabase import SupabaseRequestRepository
from app.services.database import DatabaseHealthService
from app.services.requests import RequestService


@lru_cache
def get_supabase_repository() -> SupabaseRequestRepository | None:
    settings = get_settings()
    if not settings.supabase_configured:
        return None
    assert settings.supabase_url is not None
    assert settings.supabase_service_role_key is not None
    return SupabaseRequestRepository.from_credentials(
        settings.supabase_url,
        settings.supabase_service_role_key,
    )


def get_request_repository() -> RequestRepository:
    repository = get_supabase_repository()
    if repository is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Supabase is not configured",
        )
    return repository


def get_request_service(
    repository: Annotated[RequestRepository, Depends(get_request_repository)],
) -> RequestService:
    return RequestService(repository)


def get_database_health_service() -> DatabaseHealthService:
    return DatabaseHealthService(get_supabase_repository())


@lru_cache
def get_supabase_knowledge_repository() -> SupabaseKnowledgeRepository | None:
    settings = get_settings()
    if not settings.supabase_configured:
        return None
    assert settings.supabase_url is not None
    assert settings.supabase_service_role_key is not None
    return SupabaseKnowledgeRepository.from_credentials(
        settings.supabase_url,
        settings.supabase_service_role_key,
    )


@lru_cache
def get_openai_embedding_provider() -> OpenAIEmbeddingProvider | None:
    settings = get_settings()
    if not settings.openai_configured:
        return None
    return OpenAIEmbeddingProvider(
        api_key=settings.openai_api_key,
        model=settings.embedding_model,
        dimensions=settings.embedding_dimensions,
        batch_size=settings.embedding_batch_size,
    )


def get_optional_knowledge_repository() -> KnowledgeRepository | None:
    return get_supabase_knowledge_repository()


def get_knowledge_repository() -> KnowledgeRepository:
    repository = get_supabase_knowledge_repository()
    if repository is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Supabase knowledge repository is not configured",
        )
    return repository


def get_embedding_provider() -> OpenAIEmbeddingProvider:
    provider = get_openai_embedding_provider()
    if provider is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="OPENAI_API_KEY is not configured",
        )
    return provider


def get_optional_embedding_provider() -> OpenAIEmbeddingProvider | None:
    return get_openai_embedding_provider()


def get_retrieval_service(
    repository: Annotated[
        KnowledgeRepository,
        Depends(get_knowledge_repository),
    ],
    provider: Annotated[
        OpenAIEmbeddingProvider | None,
        Depends(get_optional_embedding_provider),
    ],
) -> KnowledgeRetrievalService:
    settings = get_settings()
    return KnowledgeRetrievalService(
        repository,
        provider,
        default_top_k=settings.rag_top_k,
        default_threshold=settings.rag_similarity_threshold,
        default_mode=settings.rag_retrieval_mode,
        default_semantic_candidate_count=(
            settings.rag_semantic_candidate_count
        ),
        default_lexical_candidate_count=settings.rag_lexical_candidate_count,
        default_rrf_k=settings.rag_rrf_k,
        default_semantic_weight=settings.rag_semantic_weight,
        default_lexical_weight=settings.rag_lexical_weight,
    )


def get_indexing_service(
    repository: Annotated[
        KnowledgeRepository,
        Depends(get_knowledge_repository),
    ],
    provider: Annotated[
        OpenAIEmbeddingProvider,
        Depends(get_embedding_provider),
    ],
) -> KnowledgeIndexingService:
    settings = get_settings()
    return KnowledgeIndexingService(
        repository,
        provider,
        embedding_model=settings.embedding_model,
    )


def get_enabled_indexing_service(
) -> KnowledgeIndexingService:
    settings = get_settings()
    if not settings.enable_rag_index_endpoint:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="RAG indexing endpoint is disabled",
        )
    repository = get_knowledge_repository()
    provider = get_embedding_provider()
    return KnowledgeIndexingService(
        repository,
        provider,
        embedding_model=settings.embedding_model,
    )
