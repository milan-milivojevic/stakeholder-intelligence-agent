"""Explicit lifecycle transition guards for canonical immutable contracts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pydantic import BaseModel

from stakeholder_intelligence_agent.contracts.domain import (
    Engagement,
    InterviewSession,
    InvitationToken,
    PMAccess,
    Stakeholder,
    Transcript,
    TranscriptIngestionVersion,
)
from stakeholder_intelligence_agent.contracts.execution import InsightRun
from stakeholder_intelligence_agent.contracts.source import DocumentVersion
from stakeholder_intelligence_agent.errors import LifecycleTransitionError

ENGAGEMENT_TRANSITIONS: Mapping[str, frozenset[str]] = {
    "active": frozenset({"archived"}),
    "archived": frozenset(),
}
PM_ACCESS_TRANSITIONS: Mapping[str, frozenset[str]] = {
    "active": frozenset({"revoked"}),
    "revoked": frozenset(),
}
STAKEHOLDER_TRANSITIONS: Mapping[str, frozenset[str]] = {
    "active": frozenset({"revoked"}),
    "revoked": frozenset(),
}
INVITATION_TRANSITIONS: Mapping[str, frozenset[str]] = {
    "active": frozenset({"activated", "expired", "revoked"}),
    "activated": frozenset({"expired"}),
    "expired": frozenset(),
    "revoked": frozenset(),
}
INTERVIEW_TRANSITIONS: Mapping[str, frozenset[str]] = {
    "draft": frozenset({"finalizing", "failed"}),
    "finalizing": frozenset({"finalized", "failed"}),
    "finalized": frozenset({"ingesting", "failed"}),
    "ingesting": frozenset({"ready", "failed"}),
    "ready": frozenset(),
    "failed": frozenset({"finalizing", "ingesting"}),
}
TRANSCRIPT_TRANSITIONS: Mapping[str, frozenset[str]] = {
    "draft": frozenset({"finalized"}),
    "finalized": frozenset(),
}
TRANSCRIPT_VERSION_TRANSITIONS: Mapping[str, frozenset[str]] = {
    "RECEIVED": frozenset({"INDEXING", "FAILED"}),
    "INDEXING": frozenset({"READY", "FAILED"}),
    "READY": frozenset({"SUPERSEDED"}),
    "FAILED": frozenset({"INDEXING"}),
    "SUPERSEDED": frozenset(),
}
DOCUMENT_VERSION_TRANSITIONS: Mapping[str, frozenset[str]] = {
    "RECEIVED": frozenset({"VALIDATING", "FAILED"}),
    "VALIDATING": frozenset({"EXTRACTING", "FAILED"}),
    "EXTRACTING": frozenset({"ENRICHING", "FAILED"}),
    "ENRICHING": frozenset({"INDEXING", "FAILED"}),
    "INDEXING": frozenset({"READY", "FAILED"}),
    "READY": frozenset({"SUPERSEDED"}),
    "FAILED": frozenset({"VALIDATING"}),
    "SUPERSEDED": frozenset(),
}
INSIGHT_RUN_TRANSITIONS: Mapping[str, frozenset[str]] = {
    "queued": frozenset({"planning", "failed"}),
    "planning": frozenset({"researching", "failed"}),
    "researching": frozenset({"editing", "failed"}),
    "editing": frozenset({"validating", "failed"}),
    "validating": frozenset({"complete", "partial", "insufficient_evidence", "failed"}),
    "complete": frozenset(),
    "partial": frozenset(),
    "insufficient_evidence": frozenset(),
    "failed": frozenset(),
}


def _require_transition(
    previous_state: str,
    proposed_state: str,
    transitions: Mapping[str, frozenset[str]],
) -> None:
    if proposed_state not in transitions.get(previous_state, frozenset()):
        raise LifecycleTransitionError


def _require_unchanged(
    previous: BaseModel,
    proposed: BaseModel,
    field_names: Sequence[str],
) -> None:
    for field_name in field_names:
        if getattr(previous, field_name) != getattr(proposed, field_name):
            raise LifecycleTransitionError


def _require_once_set_unchanged(
    previous: BaseModel,
    proposed: BaseModel,
    field_names: Sequence[str],
) -> None:
    for field_name in field_names:
        previous_value: Any = getattr(previous, field_name)
        if previous_value is not None and previous_value != getattr(proposed, field_name):
            raise LifecycleTransitionError


def validate_engagement_transition(previous: Engagement, proposed: Engagement) -> None:
    _require_transition(previous.status, proposed.status, ENGAGEMENT_TRANSITIONS)
    _require_unchanged(previous, proposed, ("engagement_id", "created_at"))
    if proposed.updated_at < previous.updated_at:
        raise LifecycleTransitionError


def validate_pm_access_transition(previous: PMAccess, proposed: PMAccess) -> None:
    _require_transition(previous.status, proposed.status, PM_ACCESS_TRANSITIONS)
    _require_unchanged(
        previous,
        proposed,
        ("pm_access_id", "token_hash", "created_at"),
    )


def validate_stakeholder_transition(previous: Stakeholder, proposed: Stakeholder) -> None:
    _require_transition(previous.status, proposed.status, STAKEHOLDER_TRANSITIONS)
    _require_unchanged(
        previous,
        proposed,
        ("stakeholder_id", "engagement_id", "created_at"),
    )
    if proposed.updated_at < previous.updated_at:
        raise LifecycleTransitionError


def validate_invitation_transition(
    previous: InvitationToken,
    proposed: InvitationToken,
) -> None:
    _require_transition(previous.status, proposed.status, INVITATION_TRANSITIONS)
    _require_unchanged(
        previous,
        proposed,
        (
            "invitation_id",
            "engagement_id",
            "stakeholder_id",
            "token_hash",
            "created_at",
            "created_by_pm_access_id",
        ),
    )
    _require_once_set_unchanged(previous, proposed, ("activated_at", "revoked_at"))


def validate_interview_transition(
    previous: InterviewSession,
    proposed: InterviewSession,
) -> None:
    _require_transition(previous.status, proposed.status, INTERVIEW_TRANSITIONS)
    _require_unchanged(
        previous,
        proposed,
        (
            "interview_session_id",
            "engagement_id",
            "stakeholder_id",
            "invitation_id",
            "thread_id",
            "started_at",
        ),
    )
    _require_once_set_unchanged(
        previous,
        proposed,
        ("finalized_at", "transcript_id", "ingestion_version_id"),
    )
    if previous.status == "failed":
        if previous.finalized_at is None and proposed.status != "finalizing":
            raise LifecycleTransitionError
        if previous.finalized_at is not None and proposed.status != "ingesting":
            raise LifecycleTransitionError


def validate_transcript_transition(previous: Transcript, proposed: Transcript) -> None:
    _require_transition(previous.status, proposed.status, TRANSCRIPT_TRANSITIONS)
    _require_unchanged(
        previous,
        proposed,
        (
            "transcript_id",
            "interview_session_id",
            "engagement_id",
            "stakeholder_id",
            "role",
            "department",
        ),
    )


def validate_transcript_version_transition(
    previous: TranscriptIngestionVersion,
    proposed: TranscriptIngestionVersion,
) -> None:
    _require_transition(
        previous.state,
        proposed.state,
        TRANSCRIPT_VERSION_TRANSITIONS,
    )
    _require_unchanged(
        previous,
        proposed,
        (
            "transcript_ingestion_version_id",
            "transcript_id",
            "content_hash",
            "created_at",
        ),
    )
    _require_once_set_unchanged(previous, proposed, ("ready_at",))


def validate_document_version_transition(
    previous: DocumentVersion,
    proposed: DocumentVersion,
) -> None:
    _require_transition(previous.state, proposed.state, DOCUMENT_VERSION_TRANSITIONS)
    _require_unchanged(
        previous,
        proposed,
        (
            "document_version_id",
            "document_id",
            "version_number",
            "content_hash",
            "original_artifact_id",
            "ingestion_key",
            "created_at",
        ),
    )
    _require_once_set_unchanged(previous, proposed, ("ready_at", "superseded_at"))


def validate_insight_run_transition(previous: InsightRun, proposed: InsightRun) -> None:
    _require_transition(previous.status, proposed.status, INSIGHT_RUN_TRANSITIONS)
    _require_unchanged(
        previous,
        proposed,
        ("run_id", "engagement_id", "thread_id", "requested_question", "started_at"),
    )
    _require_once_set_unchanged(previous, proposed, ("plan_id", "report_id"))
