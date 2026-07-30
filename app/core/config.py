from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Procurement Intake Assistant"
    app_version: str = "0.1.0"
    app_env: str = "development"
    log_level: str = "INFO"
    app_timezone: str = "Europe/Moscow"
    api_v1_prefix: str = "/api/v1"
    telegram_bot_token: str | None = None
    telegram_extraction_mode: Literal[
        "rule", "openai", "hybrid", "fake"
    ] | None = None
    telegram_extraction_debug: bool = False
    supabase_url: str | None = None
    supabase_service_role_key: str | None = None
    database_url: str | None = None
    openai_api_key: str | None = None
    embedding_model: str = "text-embedding-3-small"
    embedding_dimensions: int = Field(default=1536, gt=0)
    embedding_batch_size: int = Field(default=50, ge=1, le=2048)
    rag_top_k: int = Field(default=5, ge=1, le=20)
    rag_similarity_threshold: float = Field(default=0.0, ge=-1.0, le=1.0)
    rag_retrieval_mode: Literal["semantic", "lexical", "hybrid"] = "hybrid"
    rag_semantic_candidate_count: int = Field(default=20, ge=1, le=100)
    rag_lexical_candidate_count: int = Field(default=20, ge=1, le=100)
    rag_rrf_k: int = Field(default=60, ge=1, le=1000)
    rag_semantic_weight: float = Field(default=1.0, gt=0, le=10)
    rag_lexical_weight: float = Field(default=1.0, gt=0, le=10)
    rag_answer_model: str = "gpt-5.6-luna"
    rag_answer_timeout_seconds: float = Field(default=30, gt=0)
    enable_rag_index_endpoint: bool = False
    approval_extraction_provider: Literal[
        "openai",
        "rule_based",
    ] = "openai"
    approval_extraction_model: str = "gpt-5.6-luna"
    approval_extraction_timeout_seconds: float = Field(default=30, gt=0)
    approval_extraction_max_retries: int = Field(default=2, ge=0, le=5)
    approval_extraction_min_confidence: float = Field(
        default=0.70,
        ge=0,
        le=1,
    )

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def supabase_configured(self) -> bool:
        return bool(self.supabase_url and self.supabase_service_role_key)

    @property
    def telegram_configured(self) -> bool:
        return bool(self.telegram_bot_token)

    @property
    def resolved_telegram_extraction_mode(self) -> Literal[
        "rule", "openai", "hybrid", "fake"
    ]:
        if self.telegram_extraction_mode is not None:
            return self.telegram_extraction_mode
        return "hybrid" if self.openai_configured else "rule"

    @property
    def openai_configured(self) -> bool:
        return bool(self.openai_api_key)

    @property
    def approval_extraction_configured(self) -> bool:
        return (
            self.approval_extraction_provider == "rule_based"
            or self.openai_configured
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
