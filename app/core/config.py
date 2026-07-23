from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Procurement Intake Assistant"
    app_version: str = "0.1.0"
    app_env: str = "development"
    log_level: str = "INFO"
    api_v1_prefix: str = "/api/v1"
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
    enable_rag_index_endpoint: bool = False

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def supabase_configured(self) -> bool:
        return bool(self.supabase_url and self.supabase_service_role_key)

    @property
    def openai_configured(self) -> bool:
        return bool(self.openai_api_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()
