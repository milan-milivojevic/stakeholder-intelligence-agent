"""Canonical access-domain, stakeholder, invitation, and transcript contracts."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from stakeholder_intelligence_agent.contracts.common import (
    CanonicalModel,
    ContentHash,
    FailureCode,
    NonEmptyText,
    OpaqueId,
    OriginalText,
    ShortText,
    UtcDatetime,
)

EngagementStatus = Literal["active", "archived"]
PMAccessStatus = Literal["active", "revoked"]
StakeholderStatus = Literal["active", "revoked"]
InvitationStatus = Literal["active", "activated", "expired", "revoked"]
InterviewSessionStatus = Literal[
    "draft",
    "finalizing",
    "finalized",
    "ingesting",
    "ready",
    "failed",
]
TranscriptStatus = Literal["draft", "finalized"]
TranscriptVersionState = Literal["RECEIVED", "INDEXING", "READY", "FAILED", "SUPERSEDED"]


class Engagement(CanonicalModel):
    """One immutable isolation identity with a narrow archive lifecycle."""

    engagement_id: OpaqueId
    name: ShortText
    description: NonEmptyText | None = None
    status: EngagementStatus
    created_at: UtcDatetime
    updated_at: UtcDatetime

    @model_validator(mode="after")
    def validate_timing(self) -> Self:
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must not precede created_at.")
        return self


class PMAccess(CanonicalModel):
    """Minimal local PM bearer-token record; never a user account."""

    pm_access_id: OpaqueId
    token_hash: ContentHash
    status: PMAccessStatus
    created_at: UtcDatetime
    revoked_at: UtcDatetime | None = None

    @model_validator(mode="after")
    def validate_revocation(self) -> Self:
        if self.status == "active" and self.revoked_at is not None:
            raise ValueError("Active PM access cannot have revoked_at.")
        if self.status == "revoked" and self.revoked_at is None:
            raise ValueError("Revoked PM access requires revoked_at.")
        if self.revoked_at is not None and self.revoked_at < self.created_at:
            raise ValueError("revoked_at must not precede created_at.")
        return self


class Stakeholder(CanonicalModel):
    """One engagement-owned stakeholder without scoring fields."""

    stakeholder_id: OpaqueId
    engagement_id: OpaqueId
    display_name: ShortText
    role: ShortText | None = None
    department: ShortText | None = None
    status: StakeholderStatus
    created_at: UtcDatetime
    updated_at: UtcDatetime

    @model_validator(mode="after")
    def validate_timing(self) -> Self:
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must not precede created_at.")
        return self


class InvitationToken(CanonicalModel):
    """Invitation lifecycle bound to one stakeholder and engagement."""

    invitation_id: OpaqueId
    engagement_id: OpaqueId
    stakeholder_id: OpaqueId
    token_hash: ContentHash
    status: InvitationStatus
    created_at: UtcDatetime
    expires_at: UtcDatetime
    activated_at: UtcDatetime | None = None
    revoked_at: UtcDatetime | None = None
    created_by_pm_access_id: OpaqueId

    @model_validator(mode="after")
    def validate_lifecycle_timestamps(self) -> Self:
        if self.expires_at <= self.created_at:
            raise ValueError("expires_at must be later than created_at.")
        if self.activated_at is not None and (
            self.activated_at < self.created_at or self.activated_at >= self.expires_at
        ):
            raise ValueError("activated_at must be inside the invitation lifetime.")
        if self.revoked_at is not None and self.revoked_at < self.created_at:
            raise ValueError("revoked_at must not precede created_at.")

        if self.status == "active" and (
            self.activated_at is not None or self.revoked_at is not None
        ):
            raise ValueError("Active invitations cannot have terminal timestamps.")
        if self.status == "activated" and self.activated_at is None:
            raise ValueError("Activated invitations require activated_at.")
        if self.status == "expired" and self.revoked_at is not None:
            raise ValueError("Expired invitations cannot be revoked.")
        if self.status == "revoked" and self.revoked_at is None:
            raise ValueError("Revoked invitations require revoked_at.")
        return self


class InterviewSession(CanonicalModel):
    """Authorized interview lifecycle separate from checkpoint authorization."""

    interview_session_id: OpaqueId
    engagement_id: OpaqueId
    stakeholder_id: OpaqueId
    invitation_id: OpaqueId
    thread_id: OpaqueId
    status: InterviewSessionStatus
    started_at: UtcDatetime
    finalized_at: UtcDatetime | None = None
    transcript_id: OpaqueId | None = None
    ingestion_version_id: OpaqueId | None = None
    failure_code: FailureCode | None = None
    failure_message: ShortText | None = None

    @model_validator(mode="after")
    def validate_session_state(self) -> Self:
        if self.finalized_at is not None and self.finalized_at < self.started_at:
            raise ValueError("finalized_at must not precede started_at.")
        has_failure = self.failure_code is not None or self.failure_message is not None
        if self.status == "failed":
            if self.failure_code is None or self.failure_message is None:
                raise ValueError("Failed interview sessions require safe failure detail.")
            if self.finalized_at is not None and (
                self.transcript_id is None or self.ingestion_version_id is None
            ):
                raise ValueError(
                    "Post-finalization failures require transcript and ingestion identity."
                )
            if self.finalized_at is None and self.ingestion_version_id is not None:
                raise ValueError("Pre-finalization failures cannot carry ingestion identity.")
        elif has_failure:
            raise ValueError("Only failed interview sessions may carry failure detail.")

        if self.status in {"draft", "finalizing"} and (
            self.finalized_at is not None or self.ingestion_version_id is not None
        ):
            raise ValueError("Unfinalized sessions cannot carry finalized ingestion state.")
        if self.status in {"finalized", "ingesting", "ready"} and (
            self.finalized_at is None
            or self.transcript_id is None
            or self.ingestion_version_id is None
        ):
            raise ValueError("Finalized session states require transcript and ingestion identity.")
        return self


class Transcript(CanonicalModel):
    """Materialized raw transcript with an explicit immutable boundary."""

    transcript_id: OpaqueId
    interview_session_id: OpaqueId
    engagement_id: OpaqueId
    stakeholder_id: OpaqueId
    role: ShortText | None = None
    department: ShortText | None = None
    status: TranscriptStatus
    language_observations: tuple[ShortText, ...] = ()
    finalized_at: UtcDatetime | None = None
    content_hash: ContentHash | None = None

    @model_validator(mode="after")
    def validate_finalization(self) -> Self:
        if self.status == "draft":
            if self.finalized_at is not None or self.content_hash is not None:
                raise ValueError("Draft transcripts cannot have finalization fields.")
        elif self.finalized_at is None or self.content_hash is None:
            raise ValueError("Finalized transcripts require finalized_at and content_hash.")
        return self


class TranscriptTurn(CanonicalModel):
    """One exact raw turn using the project-wide zero-based index policy."""

    turn_index: int = Field(ge=0)
    speaker: Literal["stakeholder", "assistant"]
    original_text: OriginalText
    created_at: UtcDatetime
    checkpoint_message_id: OpaqueId | None = None

    @field_validator("original_text")
    @classmethod
    def reject_blank_original_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("original_text cannot be blank.")
        return value


class TranscriptIngestionVersion(CanonicalModel):
    """Retry-stable searchable version of one finalized transcript."""

    transcript_ingestion_version_id: OpaqueId
    transcript_id: OpaqueId
    content_hash: ContentHash
    state: TranscriptVersionState
    is_active: bool
    created_at: UtcDatetime
    ready_at: UtcDatetime | None = None
    failure_code: FailureCode | None = None
    failure_message: ShortText | None = None

    @model_validator(mode="after")
    def validate_version_state(self) -> Self:
        if self.ready_at is not None and self.ready_at < self.created_at:
            raise ValueError("ready_at must not precede created_at.")
        has_failure = self.failure_code is not None or self.failure_message is not None
        if self.state == "READY":
            if not self.is_active or self.ready_at is None:
                raise ValueError("READY transcript versions must be active and have ready_at.")
            if has_failure:
                raise ValueError("READY transcript versions cannot carry failure detail.")
        elif self.is_active:
            raise ValueError("Only READY transcript versions may be active.")

        if self.state == "FAILED":
            if self.failure_code is None or self.failure_message is None:
                raise ValueError("FAILED transcript versions require safe failure detail.")
        elif has_failure:
            raise ValueError("Only FAILED transcript versions may carry failure detail.")
        if self.state not in {"READY", "SUPERSEDED"} and self.ready_at is not None:
            raise ValueError("Pre-READY transcript versions cannot have ready_at.")
        return self
