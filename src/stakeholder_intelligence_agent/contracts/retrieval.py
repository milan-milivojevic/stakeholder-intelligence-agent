"""Canonical server-resolved retrieval filter and candidate contracts."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, model_validator

from stakeholder_intelligence_agent.contracts.common import (
    CanonicalModel,
    FiniteFloat,
    NonEmptyText,
    OpaqueId,
    ShortText,
)
from stakeholder_intelligence_agent.contracts.source import (
    DocumentType,
    RetrievalSourceType,
    SourceLocation,
)


class RetrievalFilterInput(CanonicalModel):
    """Optional model-proposed narrowing fields; no scope authority is present."""

    stakeholder_id: OpaqueId | None = None
    role: ShortText | None = None
    department: ShortText | None = None
    doc_type: DocumentType | Literal["transcript"] | None = None
    source_type: RetrievalSourceType | None = None


class RetrievalFilter(RetrievalFilterInput):
    """Server-owned mandatory scope combined with validated optional narrowing."""

    engagement_id: OpaqueId
    active_ready_only: Literal[True] = True


class RetrievalMetadata(CanonicalModel):
    """Complete six-key metadata plus version eligibility state."""

    engagement_id: OpaqueId
    stakeholder_id: OpaqueId | None
    role: ShortText | None
    department: ShortText | None
    doc_type: DocumentType | Literal["transcript"]
    source_type: RetrievalSourceType
    source_version_state: Literal["READY"]
    is_active_ready: Literal[True]

    @model_validator(mode="after")
    def validate_context_metadata(self) -> Self:
        if self.source_type == "engagement_document" and any(
            value is not None for value in (self.stakeholder_id, self.role, self.department)
        ):
            raise ValueError("Engagement retrieval metadata requires null stakeholder fields.")
        if (
            self.source_type in {"stakeholder_document", "interview"}
            and self.stakeholder_id is None
        ):
            raise ValueError("Stakeholder and interview metadata require stakeholder_id.")
        if (self.source_type == "interview") != (self.doc_type == "transcript"):
            raise ValueError("Interview source_type and transcript doc_type must agree.")
        return self


class RetrievalCandidate(CanonicalModel):
    """Candidate after Qdrant-native RRF and required BGE reranking."""

    chunk_id: OpaqueId
    hybrid_rank: int = Field(ge=1)
    rrf_score: FiniteFloat
    reranker_score: FiniteFloat
    final_rank: int = Field(ge=1)
    source_preview: NonEmptyText
    location: SourceLocation
    metadata: RetrievalMetadata

    @model_validator(mode="after")
    def require_positive_rrf_score(self) -> Self:
        if self.rrf_score <= 0:
            raise ValueError("rrf_score must be positive.")
        return self
