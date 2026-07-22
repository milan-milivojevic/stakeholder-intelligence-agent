"""Canonical evidence-registry and report-citation contracts."""

from __future__ import annotations

from typing import Self

from pydantic import Field, field_validator, model_validator

from stakeholder_intelligence_agent.contracts.common import (
    CanonicalModel,
    ContentHash,
    NonEmptyText,
    OpaqueId,
    OriginalText,
    ShortText,
    UtcDatetime,
)
from stakeholder_intelligence_agent.contracts.source import (
    RetrievalSourceType,
    SourceLocation,
)


class EvidenceRecord(CanonicalModel):
    """Immutable report-run evidence identity with resolvable source lineage."""

    evidence_id: OpaqueId
    run_id: OpaqueId
    engagement_id: OpaqueId
    topic_id: OpaqueId
    source_id: OpaqueId
    source_version_id: OpaqueId
    source_type: RetrievalSourceType
    stakeholder_id: OpaqueId | None
    location: SourceLocation
    original_excerpt: OriginalText
    english_interpretation: NonEmptyText | None = None
    content_hash: ContentHash
    researcher_id: OpaqueId
    created_at: UtcDatetime

    @field_validator("original_excerpt")
    @classmethod
    def reject_blank_excerpt(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("original_excerpt cannot be blank.")
        return value

    @model_validator(mode="after")
    def validate_attribution(self) -> Self:
        if self.source_type == "engagement_document" and self.stakeholder_id is not None:
            raise ValueError("Engagement-document evidence cannot attribute a stakeholder.")
        if (
            self.source_type in {"stakeholder_document", "interview"}
            and self.stakeholder_id is None
        ):
            raise ValueError("Stakeholder and interview evidence require stakeholder_id.")
        return self


class Citation(CanonicalModel):
    """Report citation linked to registered evidence and report claims."""

    citation_id: OpaqueId
    evidence_id: OpaqueId
    display_label: ShortText
    source_location: SourceLocation
    claim_ids: tuple[OpaqueId, ...] = Field(min_length=1)
