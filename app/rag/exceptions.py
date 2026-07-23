class RagError(Exception):
    """Base error for the local retrieval subsystem."""


class RagConfigurationError(RagError):
    """Raised when required backend configuration is absent."""


class EmbeddingError(RagError):
    """Raised when an embedding provider fails or returns invalid data."""


class KnowledgeRepositoryError(RagError):
    """Raised when knowledge persistence or search fails."""


class IndexingError(RagError):
    """Raised when an indexing run cannot complete safely."""


class RetrievalError(RagError):
    """Raised when a retrieval request is invalid or cannot be completed."""
