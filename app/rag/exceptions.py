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


class AnswerGenerationError(RagError):
    """Raised when a grounded answer cannot be generated safely."""


class AnswerProviderUnavailableError(AnswerGenerationError):
    """Raised when the external answer provider cannot be reached."""


class MalformedAnswerResponseError(AnswerGenerationError):
    """Raised when the provider response does not satisfy the DTO contract."""
