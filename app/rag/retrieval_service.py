from app.rag.embeddings import EmbeddingProvider
from app.rag.exceptions import RagConfigurationError, RetrievalError
from app.rag.models import RetrievalMode, SearchResult
from app.rag.repository import KnowledgeRepository


class KnowledgeRetrievalService:
    def __init__(
        self,
        repository: KnowledgeRepository,
        embedding_provider: EmbeddingProvider | None,
        *,
        default_top_k: int = 5,
        default_threshold: float = 0.0,
        default_mode: RetrievalMode = "hybrid",
        default_semantic_candidate_count: int = 20,
        default_lexical_candidate_count: int = 20,
        default_rrf_k: int = 60,
        default_semantic_weight: float = 1.0,
        default_lexical_weight: float = 1.0,
    ) -> None:
        self._repository = repository
        self._embedding_provider = embedding_provider
        self._default_top_k = default_top_k
        self._default_threshold = default_threshold
        self._default_mode = default_mode
        self._default_semantic_candidate_count = (
            default_semantic_candidate_count
        )
        self._default_lexical_candidate_count = default_lexical_candidate_count
        self._default_rrf_k = default_rrf_k
        self._default_semantic_weight = default_semantic_weight
        self._default_lexical_weight = default_lexical_weight

    def search(
        self,
        query: str,
        top_k: int | None = None,
        similarity_threshold: float | None = None,
        document_types: list[str] | None = None,
        mode: RetrievalMode | None = None,
        semantic_candidate_count: int | None = None,
        lexical_candidate_count: int | None = None,
        rrf_k: int | None = None,
        semantic_weight: float | None = None,
        lexical_weight: float | None = None,
    ) -> list[SearchResult]:
        normalized_query = query.strip()
        if not normalized_query:
            raise RetrievalError("Search query must not be empty")
        effective_mode = self._default_mode if mode is None else mode
        if effective_mode not in ("semantic", "lexical", "hybrid"):
            raise RetrievalError("mode must be semantic, lexical, or hybrid")
        effective_top_k = self._default_top_k if top_k is None else top_k
        if not 1 <= effective_top_k <= 20:
            raise RetrievalError("top_k must be between 1 and 20")
        threshold = (
            self._default_threshold
            if similarity_threshold is None
            else similarity_threshold
        )
        if not -1.0 <= threshold <= 1.0:
            raise RetrievalError(
                "similarity_threshold must be between -1 and 1"
            )
        semantic_candidates = self._candidate_count(
            semantic_candidate_count,
            self._default_semantic_candidate_count,
            effective_top_k,
            "semantic_candidate_count",
        )
        lexical_candidates = self._candidate_count(
            lexical_candidate_count,
            self._default_lexical_candidate_count,
            effective_top_k,
            "lexical_candidate_count",
        )
        effective_rrf_k = self._bounded(
            rrf_k,
            self._default_rrf_k,
            1,
            1000,
            "rrf_k",
        )
        effective_semantic_weight = self._weight(
            semantic_weight,
            self._default_semantic_weight,
            "semantic_weight",
        )
        effective_lexical_weight = self._weight(
            lexical_weight,
            self._default_lexical_weight,
            "lexical_weight",
        )

        if effective_mode == "lexical":
            return self._repository.lexical_search(
                normalized_query,
                effective_top_k,
                document_types,
            )

        if self._embedding_provider is None:
            raise RagConfigurationError(
                "OPENAI_API_KEY is required for semantic and hybrid search"
            )
        query_embedding = self._embedding_provider.embed_query(normalized_query)
        if effective_mode == "semantic":
            results = self._repository.semantic_search(
                query_embedding,
                effective_top_k,
                threshold,
                document_types,
            )
            return sorted(
                results,
                key=lambda item: item.similarity,
                reverse=True,
            )
        return self._repository.hybrid_search(
            normalized_query,
            query_embedding,
            effective_top_k,
            semantic_candidates,
            lexical_candidates,
            threshold,
            effective_rrf_k,
            effective_semantic_weight,
            effective_lexical_weight,
            document_types,
        )

    @staticmethod
    def _candidate_count(
        value: int | None,
        default: int,
        top_k: int,
        name: str,
    ) -> int:
        effective = default if value is None else value
        if not top_k <= effective <= 100:
            raise RetrievalError(f"{name} must be between top_k and 100")
        return effective

    @staticmethod
    def _bounded(
        value: int | None,
        default: int,
        lower: int,
        upper: int,
        name: str,
    ) -> int:
        effective = default if value is None else value
        if not lower <= effective <= upper:
            raise RetrievalError(
                f"{name} must be between {lower} and {upper}"
            )
        return effective

    @staticmethod
    def _weight(
        value: float | None,
        default: float,
        name: str,
    ) -> float:
        effective = default if value is None else value
        if not 0 < effective <= 10:
            raise RetrievalError(f"{name} must be greater than 0 and at most 10")
        return effective
