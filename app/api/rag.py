import time
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.dependencies import (
    get_enabled_indexing_service,
    get_optional_knowledge_repository,
    get_retrieval_service,
)
from app.core.config import get_settings
from app.rag.exceptions import (
    IndexingError,
    KnowledgeRepositoryError,
    RagError,
    RetrievalError,
)
from app.rag.indexing_service import KnowledgeIndexingService
from app.rag.models import (
    IndexingReport,
    RagHealthResponse,
    RagIndexRequest,
    RagSearchRequest,
    RagSearchResponse,
)
from app.rag.repository import KnowledgeRepository
from app.rag.retrieval_service import KnowledgeRetrievalService

router = APIRouter(prefix="/rag", tags=["rag"])


@router.get("/health", response_model=RagHealthResponse)
def rag_health(
    repository: Annotated[
        KnowledgeRepository | None,
        Depends(get_optional_knowledge_repository),
    ],
) -> RagHealthResponse:
    settings = get_settings()
    statistics = None
    if repository is not None:
        try:
            statistics = repository.get_index_statistics()
        except KnowledgeRepositoryError:
            statistics = None
    return RagHealthResponse(
        configured=settings.supabase_configured and settings.openai_configured,
        database_configured=settings.supabase_configured,
        openai_configured=settings.openai_configured,
        embedding_model=settings.embedding_model,
        embedding_dimensions=settings.embedding_dimensions,
        index_statistics=statistics,
    )


@router.post("/search", response_model=RagSearchResponse)
def rag_search(
    request: RagSearchRequest,
    service: Annotated[
        KnowledgeRetrievalService,
        Depends(get_retrieval_service),
    ],
) -> RagSearchResponse:
    started = time.perf_counter()
    try:
        results = service.search(
            request.query,
            mode=request.mode,
            top_k=request.top_k,
            similarity_threshold=request.similarity_threshold,
            document_types=request.document_types,
            semantic_candidate_count=request.semantic_candidate_count,
            lexical_candidate_count=request.lexical_candidate_count,
            rrf_k=request.rrf_k,
            semantic_weight=request.semantic_weight,
            lexical_weight=request.lexical_weight,
        )
    except RetrievalError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except RagError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    return RagSearchResponse(
        query=request.query,
        mode=request.mode or get_settings().rag_retrieval_mode,
        count=len(results),
        results=results,
        duration_ms=round((time.perf_counter() - started) * 1000),
    )


@router.post("/index", response_model=IndexingReport)
def rag_index(
    request: RagIndexRequest,
    service: Annotated[
        KnowledgeIndexingService,
        Depends(get_enabled_indexing_service),
    ],
) -> IndexingReport:
    try:
        return service.index(
            force_reembed=request.force_reembed,
            skip_delete=request.skip_delete,
        )
    except IndexingError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
