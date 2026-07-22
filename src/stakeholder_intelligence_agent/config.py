"""Typed local runtime configuration with Gemini-only validation."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal, Self
from urllib.parse import urlparse

from pydantic import Field, SecretStr, StringConstraints, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ModelId = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class Settings(BaseSettings):
    """Validated environment configuration for the single local backend."""

    model_config = SettingsConfigDict(
        env_prefix="STAKEHOLDER_AI_",
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        # Environment-variable names are documented in conventional uppercase form.
        # Windows treats names case-insensitively, and keeping the same contract on
        # every supported host avoids a platform-specific startup failure.
        case_sensitive=False,
        populate_by_name=True,
        extra="ignore",
    )

    environment: Literal["development", "test"] = "development"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    google_api_key: SecretStr = Field(validation_alias="GOOGLE_API_KEY", min_length=1)
    gemini_primary_chat_model: ModelId
    gemini_fallback_chat_model: ModelId
    gemini_vision_model: ModelId
    gemini_embedding_model: ModelId
    gemini_embedding_dimension: int = Field(default=768, ge=128, le=3_072)

    pm_bootstrap_token: SecretStr = Field(min_length=32)
    token_pepper: SecretStr = Field(min_length=32)
    invitation_ttl_minutes: int = Field(default=1_440, ge=5, le=10_080)
    stakeholder_session_ttl_minutes: int = Field(default=480, ge=5, le=480)
    pm_session_ttl_minutes: int = Field(default=720, ge=5, le=1_440)
    browser_origin: str = "http://127.0.0.1:2024"

    data_root: Path = Path("./data")
    domain_database: Path = Path("./data/domain.sqlite3")
    checkpoint_database: Path = Path("./data/checkpoints.sqlite3")
    originals_root: Path = Path("./data/originals")
    derived_root: Path = Path("./data/derived")
    agent_artifacts_root: Path = Path("./data/agent-artifacts")
    audit_root: Path = Path("./data/audit")
    model_cache_root: Path = Path("./.cache")

    qdrant_url: str = "http://127.0.0.1:6333"
    qdrant_collection: str = "stakeholder_intelligence"
    sparse_model: str = "Qdrant/BM25"
    reranker_model: str = "BAAI/bge-reranker-base"
    reranker_revision: Literal["2cfc18c9415c912f9d8155881c133215df768a70"] = (
        "2cfc18c9415c912f9d8155881c133215df768a70"
    )

    max_upload_bytes: int = Field(default=52_428_800, ge=1, le=104_857_600)
    max_archive_entries: int = Field(default=10_000, ge=10, le=100_000)
    max_archive_uncompressed_bytes: int = Field(
        default=524_288_000,
        ge=1_048_576,
        le=2_147_483_648,
    )
    max_image_pixels: int = Field(default=80_000_000, ge=1_000_000, le=200_000_000)
    max_spreadsheet_cells: int = Field(default=250_000, ge=1_000, le=2_000_000)
    ingestion_lease_seconds: int = Field(default=1_800, ge=60, le=7_200)
    docling_timeout_seconds: int = Field(default=600, ge=30, le=1_800)
    ingestion_chunk_characters: int = Field(default=2_000, ge=500, le=10_000)
    ingestion_chunk_overlap: int = Field(default=200, ge=0, le=2_000)
    max_research_topics: int = Field(default=5, ge=1, le=5)
    max_parallel_researchers: int = Field(default=3, ge=1, le=3)
    max_retrieval_candidates_per_channel: int = Field(default=30, ge=1, le=100)
    max_rerank_candidates: int = Field(default=30, ge=1, le=100)
    max_retrieval_results: int = Field(default=10, ge=1, le=50)
    reranker_batch_size: int = Field(default=16, ge=1, le=128)
    model_run_call_limit: int = Field(default=40, ge=1, le=100)
    model_thread_call_limit: int = Field(default=120, ge=1, le=500)
    tool_run_call_limit: int = Field(default=80, ge=1, le=300)
    tool_thread_call_limit: int = Field(default=240, ge=1, le=1_000)
    retrieval_calls_per_researcher_limit: int = Field(default=3, ge=1, le=5)
    provider_timeout_seconds: int = Field(default=120, ge=10, le=300)
    gemini_requests_per_minute_limit: int = Field(default=15, ge=2, le=10_000)
    gemini_requests_per_minute_headroom: int = Field(default=1, ge=0, le=9_999)
    gemini_rate_limit_wait_timeout_seconds: int = Field(default=180, ge=1, le=900)
    insight_run_timeout_seconds: int = Field(default=900, ge=30, le=3_600)
    provider_sdk_retries: int = Field(default=0, ge=0, le=0)
    summary_trigger_tokens: int = Field(default=12_000, ge=2_000, le=100_000)
    summary_keep_messages: int = Field(default=20, ge=4, le=100)

    langsmith_tracing: bool = Field(default=False, validation_alias="LANGSMITH_TRACING")

    @field_validator(
        "gemini_primary_chat_model",
        "gemini_fallback_chat_model",
        "gemini_vision_model",
        "gemini_embedding_model",
    )
    @classmethod
    def require_gemini_identifier(cls, value: str) -> str:
        """Reject every non-Gemini provider identifier."""
        if "gemini" not in value.lower():
            raise ValueError("All configured runtime model identifiers must be Gemini models.")
        return value

    @field_validator("qdrant_url")
    @classmethod
    def require_local_qdrant(cls, value: str) -> str:
        """Keep the approved Qdrant service on loopback."""
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in {
            "127.0.0.1",
            "localhost",
            "::1",
        }:
            raise ValueError("Qdrant must use a loopback HTTP endpoint.")
        return value.rstrip("/")

    @field_validator("browser_origin")
    @classmethod
    def require_exact_loopback_browser_origin(cls, value: str) -> str:
        """Require one canonical local origin and derive Secure-cookie behavior from its scheme."""
        parsed = urlparse(value)
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.params
            or parsed.query
            or parsed.fragment
            or value != f"{parsed.scheme}://{parsed.netloc}"
        ):
            raise ValueError("The browser origin must be one exact canonical loopback HTTP origin.")
        return value

    @model_validator(mode="after")
    def validate_local_boundaries(self) -> Self:
        """Resolve project paths and enforce approved local identities and limits."""
        data_root = self._resolve_project_path(self.data_root)
        object.__setattr__(self, "data_root", data_root)
        for field_name in (
            "domain_database",
            "checkpoint_database",
            "originals_root",
            "derived_root",
            "agent_artifacts_root",
            "audit_root",
        ):
            resolved = self._resolve_project_path(getattr(self, field_name))
            try:
                resolved.relative_to(data_root)
            except ValueError as error:
                raise ValueError(f"{field_name} must resolve beneath data_root.") from error
            object.__setattr__(self, field_name, resolved)

        model_cache_root = self._resolve_project_path(self.model_cache_root)
        object.__setattr__(self, "model_cache_root", model_cache_root)

        if self.sparse_model != "Qdrant/BM25":
            raise ValueError("The approved sparse model is Qdrant/BM25.")
        if self.reranker_model != "BAAI/bge-reranker-base":
            raise ValueError("The required reranker is BAAI/bge-reranker-base.")
        if self.max_rerank_candidates > self.max_retrieval_candidates_per_channel * 2:
            raise ValueError("The rerank bound cannot exceed the combined retrieval candidates.")
        if self.max_retrieval_results > self.max_rerank_candidates:
            raise ValueError("The result bound cannot exceed the rerank bound.")
        if self.langsmith_tracing:
            raise ValueError(
                "LangSmith tracing must remain disabled for the local project runtime."
            )
        if self.domain_database == self.checkpoint_database:
            raise ValueError("Domain and checkpoint SQLite databases must be separate files.")
        if self.ingestion_chunk_overlap >= self.ingestion_chunk_characters:
            raise ValueError("Ingestion chunk overlap must be smaller than chunk size.")
        if self.gemini_requests_per_minute_headroom >= self.gemini_requests_per_minute_limit:
            raise ValueError("Gemini RPM headroom must remain below the provider limit.")
        return self

    @staticmethod
    def _resolve_project_path(path: Path) -> Path:
        candidate = path if path.is_absolute() else PROJECT_ROOT / path
        return candidate.resolve()

    @property
    def browser_cookie_secure(self) -> bool:
        """Use Secure cookies for HTTPS and only permit the documented loopback HTTP exception."""
        return urlparse(self.browser_origin).scheme == "https"

    @property
    def gemini_effective_requests_per_minute(self) -> int:
        """Return the configured provider limit minus mandatory safety headroom."""
        return self.gemini_requests_per_minute_limit - self.gemini_requests_per_minute_headroom


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Load and cache the validated process configuration."""
    return Settings()
