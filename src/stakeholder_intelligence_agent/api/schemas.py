"""Closed English request and response contracts for custom domain routes."""

# Pydantic resolves these field types at runtime while generating route schemas.
# ruff: noqa: TC001

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, StringConstraints

from stakeholder_intelligence_agent.contracts import (
    Engagement,
    EvidenceRecord,
    InsightExecutionMetrics,
    InsightReport,
    InsightRun,
    InterviewSession,
    Stakeholder,
    Transcript,
    TranscriptIngestionVersion,
)
from stakeholder_intelligence_agent.contracts.common import (
    CanonicalModel,
    FailureCode,
    NonEmptyText,
    OpaqueId,
    OriginalText,
    ShortText,
    UtcDatetime,
)
from stakeholder_intelligence_agent.contracts.source import (
    DocumentSource,
    DocumentVersion,
    DocumentVersionState,
    ElementType,
    SourceLocation,
)

BearerSecret = Annotated[str, StringConstraints(min_length=32, max_length=1024)]


class PMActivationRequest(CanonicalModel):
    """One-time exchange of the local PM bootstrap secret."""

    bootstrap_token: BearerSecret


class StakeholderActivationRequest(CanonicalModel):
    """Open or resume the interview bound to an invitation link."""

    invitation_token: BearerSecret


class SessionTokenResponse(CanonicalModel):
    """Raw bearer returned exactly once and never persisted in this representation."""

    access_session_id: OpaqueId
    access_token: BearerSecret
    token_type: Literal["bearer"] = "bearer"  # noqa: S105 -- protocol type, not a secret.
    expires_at: UtcDatetime


class StakeholderActivationResponse(CanonicalModel):
    """Limited stakeholder session and its fixed interview mapping."""

    session: SessionTokenResponse
    engagement_id: OpaqueId
    stakeholder_id: OpaqueId
    interview_session_id: OpaqueId
    thread_id: OpaqueId


class BrowserSessionView(CanonicalModel):
    """Safe browser session metadata without a bearer, cookie value, or token hash."""

    principal_type: Literal["pm", "stakeholder"]
    access_session_id: OpaqueId
    expires_at: UtcDatetime
    engagement_id: OpaqueId | None = None
    stakeholder_id: OpaqueId | None = None
    interview_session_id: OpaqueId | None = None
    thread_id: OpaqueId | None = None


class EngagementCreateRequest(CanonicalModel):
    name: ShortText
    description: NonEmptyText | None = None


class EngagementListResponse(CanonicalModel):
    engagements: tuple[Engagement, ...]


class EngagementContextResponse(CanonicalModel):
    engagement: Engagement


class StakeholderCreateRequest(CanonicalModel):
    display_name: ShortText
    role: ShortText | None = None
    department: ShortText | None = None


class StakeholderListResponse(CanonicalModel):
    stakeholders: tuple[Stakeholder, ...]


class StakeholderResponse(CanonicalModel):
    stakeholder: Stakeholder


class InvitationSummary(CanonicalModel):
    invitation_id: OpaqueId
    engagement_id: OpaqueId
    stakeholder_id: OpaqueId
    status: Literal["active", "activated", "expired", "revoked"]
    created_at: UtcDatetime
    expires_at: UtcDatetime
    activated_at: UtcDatetime | None = None
    revoked_at: UtcDatetime | None = None


class InvitationIssuedResponse(CanonicalModel):
    invitation: InvitationSummary
    invitation_token: BearerSecret


class InvitationLinkResponse(CanonicalModel):
    """A current invitation link returned only for an explicit PM copy action."""

    invitation: InvitationSummary
    invitation_token: BearerSecret


class InvitationListResponse(CanonicalModel):
    invitations: tuple[InvitationSummary, ...]


class DocumentSummary(CanonicalModel):
    source: DocumentSource
    latest_version: DocumentVersion


class DocumentListResponse(CanonicalModel):
    documents: tuple[DocumentSummary, ...]


class UploadResponse(CanonicalModel):
    document: DocumentSummary
    element_count: int = Field(ge=0)
    chunk_count: int = Field(ge=0)
    attempt_id: OpaqueId | None = None
    idempotent: bool


class DocumentProcessingCount(CanonicalModel):
    name: ShortText
    count: int = Field(ge=0)


class DocumentLifecycleEvent(CanonicalModel):
    event_id: OpaqueId
    from_state: DocumentVersionState | None = None
    to_state: DocumentVersionState
    occurred_at: UtcDatetime


class DocumentElementPreview(CanonicalModel):
    element_id: OpaqueId
    document_version_id: OpaqueId
    element_type: ElementType
    location: SourceLocation
    extraction_method: ShortText
    content_preview: NonEmptyText | None = None
    english_interpretation: NonEmptyText | None = None


class DocumentArtifactSummary(CanonicalModel):
    artifact_id: OpaqueId
    artifact_kind: ShortText
    media_type: ShortText
    content_hash: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
    download_path: NonEmptyText


class DocumentProcessingDetailsResponse(CanonicalModel):
    document: DocumentSummary
    lifecycle_events: tuple[DocumentLifecycleEvent, ...]
    element_count: int = Field(ge=0)
    element_counts: tuple[DocumentProcessingCount, ...]
    chunk_count: int = Field(ge=0)
    artifact_count: int = Field(ge=0)
    artifact_counts: tuple[DocumentProcessingCount, ...]
    artifacts: tuple[DocumentArtifactSummary, ...]
    element_previews: tuple[DocumentElementPreview, ...]


class InterviewContextResponse(CanonicalModel):
    engagement: Engagement
    stakeholder: Stakeholder
    interview_session: InterviewSession


class InterviewSessionListResponse(CanonicalModel):
    interview_sessions: tuple[InterviewSession, ...]


class InterviewTurnRequest(CanonicalModel):
    original_text: OriginalText
    message_id: OpaqueId | None = None


class InterviewHistoryTurn(CanonicalModel):
    """One client-visible turn without checkpoint or storage identifiers."""

    turn_index: int = Field(ge=0)
    speaker: Literal["stakeholder", "assistant"]
    text: OriginalText


class InterviewPreviewResponse(CanonicalModel):
    interview_session: InterviewSession
    transcript: Transcript
    turns: tuple[InterviewHistoryTurn, ...]


class InterviewStatusResponse(CanonicalModel):
    interview_session: InterviewSession
    transcript: Transcript | None = None
    ingestion_version: TranscriptIngestionVersion | None = None
    turns: tuple[InterviewHistoryTurn, ...] = ()
    turn_count: int = Field(default=0, ge=0)
    completion_recommended: bool = False


class InterviewFinishResponse(CanonicalModel):
    interview_session: InterviewSession
    transcript: Transcript
    ingestion_version: TranscriptIngestionVersion
    chunk_count: int = Field(ge=0)
    idempotent: bool


class InsightCreateRequest(CanonicalModel):
    question: NonEmptyText


class SafeRunEvent(CanonicalModel):
    event_id: OpaqueId
    occurred_at: UtcDatetime
    actor: ShortText
    action: ShortText
    from_status: ShortText | None = None
    to_status: ShortText | None = None
    topic_id: OpaqueId | None = None
    source_ids: tuple[OpaqueId, ...] = ()
    evidence_ids: tuple[OpaqueId, ...] = ()
    artifact_name: ShortText | None = None
    failure_code: FailureCode | None = None
    correlation_id: OpaqueId


class InsightStatusResponse(CanonicalModel):
    run: InsightRun


class InsightRunListResponse(CanonicalModel):
    runs: tuple[InsightRun, ...]


class InsightReportResponse(CanonicalModel):
    run: InsightRun
    report: InsightReport
    metrics: InsightExecutionMetrics


class SourceArtifactSummary(CanonicalModel):
    artifact_id: OpaqueId
    artifact_kind: ShortText
    media_type: ShortText
    content_hash: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
    download_path: NonEmptyText | None = None


class EvidenceDrillDownResponse(CanonicalModel):
    evidence: EvidenceRecord
    original: SourceArtifactSummary
    related_artifacts: tuple[SourceArtifactSummary, ...]


class OperationResponse(CanonicalModel):
    status: Literal["ok"] = "ok"


class ApiErrorDetail(CanonicalModel):
    code: FailureCode
    message: ShortText
    correlation_id: OpaqueId


class ApiErrorResponse(CanonicalModel):
    error: ApiErrorDetail
