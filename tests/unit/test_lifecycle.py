"""Explicit valid, retry, terminal, skip, and scope-changing lifecycle tests."""

from datetime import UTC, datetime, timedelta
from itertools import pairwise

import pytest

from stakeholder_intelligence_agent.contracts import (
    DocumentVersion,
    Engagement,
    InsightRun,
    InterviewSession,
    InvitationToken,
    PMAccess,
    Stakeholder,
    Transcript,
    TranscriptIngestionVersion,
)
from stakeholder_intelligence_agent.contracts.lifecycle import (
    validate_document_version_transition,
    validate_engagement_transition,
    validate_insight_run_transition,
    validate_interview_transition,
    validate_invitation_transition,
    validate_pm_access_transition,
    validate_stakeholder_transition,
    validate_transcript_transition,
    validate_transcript_version_transition,
)
from stakeholder_intelligence_agent.errors import LifecycleTransitionError

NOW = datetime(2026, 7, 15, 1, 0, tzinfo=UTC)
LATER = NOW + timedelta(minutes=1)
DIGEST = "d" * 64


def test_simple_domain_lifecycles_allow_only_forward_terminal_transitions() -> None:
    engagement = Engagement(
        engagement_id="engagement-a",
        name="Engagement A",
        status="active",
        created_at=NOW,
        updated_at=NOW,
    )
    archived = Engagement.model_validate(
        engagement.model_dump() | {"status": "archived", "updated_at": LATER}
    )
    validate_engagement_transition(engagement, archived)
    with pytest.raises(LifecycleTransitionError):
        validate_engagement_transition(archived, engagement)

    pm_access = PMAccess(
        pm_access_id="pm-a",
        token_hash=DIGEST,
        status="active",
        created_at=NOW,
    )
    revoked_pm = PMAccess.model_validate(
        pm_access.model_dump() | {"status": "revoked", "revoked_at": LATER}
    )
    validate_pm_access_transition(pm_access, revoked_pm)

    active_stakeholder = Stakeholder(
        stakeholder_id="stakeholder-a",
        engagement_id="engagement-a",
        display_name="Stakeholder A",
        status="active",
        created_at=NOW,
        updated_at=NOW,
    )
    revoked_stakeholder = Stakeholder.model_validate(
        active_stakeholder.model_dump() | {"status": "revoked", "updated_at": LATER}
    )
    validate_stakeholder_transition(active_stakeholder, revoked_stakeholder)
    with pytest.raises(LifecycleTransitionError):
        validate_stakeholder_transition(revoked_stakeholder, active_stakeholder)


def test_invitation_activation_preserves_scope_and_hash() -> None:
    active = InvitationToken(
        invitation_id="invitation-a",
        engagement_id="engagement-a",
        stakeholder_id="stakeholder-a",
        token_hash=DIGEST,
        status="active",
        created_at=NOW,
        expires_at=NOW + timedelta(days=1),
        created_by_pm_access_id="pm-a",
    )
    activated = InvitationToken.model_validate(
        active.model_dump() | {"status": "activated", "activated_at": LATER}
    )
    validate_invitation_transition(active, activated)
    expired_session = InvitationToken.model_validate(activated.model_dump() | {"status": "expired"})
    validate_invitation_transition(activated, expired_session)
    tampered = InvitationToken.model_validate(
        active.model_dump()
        | {
            "engagement_id": "engagement-b",
            "status": "activated",
            "activated_at": LATER,
        }
    )
    with pytest.raises(LifecycleTransitionError):
        validate_invitation_transition(active, tampered)


def _interview(status: str, *, failed_after_finalization: bool = False) -> InterviewSession:
    payload: dict[str, object] = {
        "interview_session_id": "session-a",
        "engagement_id": "engagement-a",
        "stakeholder_id": "stakeholder-a",
        "invitation_id": "invitation-a",
        "thread_id": "thread-a",
        "status": status,
        "started_at": NOW,
    }
    if status in {"finalized", "ingesting", "ready"} or failed_after_finalization:
        payload.update(
            {
                "finalized_at": LATER,
                "transcript_id": "transcript-a",
                "ingestion_version_id": "transcript-version-a",
            }
        )
    if status == "failed":
        payload.update(
            {
                "failure_code": "INGESTION_FAILED",
                "failure_message": "The interview workflow did not complete.",
            }
        )
    return InterviewSession.model_validate(payload)


def test_interview_lifecycle_supports_finalization_and_stage_aware_retry() -> None:
    draft = _interview("draft")
    finalizing = _interview("finalizing")
    finalized = _interview("finalized")
    ingesting = _interview("ingesting")
    ready = _interview("ready")
    validate_interview_transition(draft, finalizing)
    validate_interview_transition(finalizing, finalized)
    validate_interview_transition(finalized, ingesting)
    validate_interview_transition(ingesting, ready)
    with pytest.raises(LifecycleTransitionError):
        validate_interview_transition(draft, ready)

    prefinal_failure = _interview("failed")
    validate_interview_transition(finalizing, prefinal_failure)
    validate_interview_transition(prefinal_failure, finalizing)
    postfinal_failure = _interview("failed", failed_after_finalization=True)
    validate_interview_transition(ingesting, postfinal_failure)
    validate_interview_transition(postfinal_failure, ingesting)


def test_transcript_finalization_is_one_way_and_scope_immutable() -> None:
    draft = Transcript(
        transcript_id="transcript-a",
        interview_session_id="session-a",
        engagement_id="engagement-a",
        stakeholder_id="stakeholder-a",
        status="draft",
    )
    finalized = Transcript.model_validate(
        draft.model_dump() | {"status": "finalized", "finalized_at": LATER, "content_hash": DIGEST}
    )
    validate_transcript_transition(draft, finalized)
    tampered = Transcript.model_validate(
        draft.model_dump()
        | {
            "status": "finalized",
            "finalized_at": LATER,
            "content_hash": DIGEST,
            "engagement_id": "engagement-b",
        }
    )
    with pytest.raises(LifecycleTransitionError):
        validate_transcript_transition(draft, tampered)


def _transcript_version(state: str) -> TranscriptIngestionVersion:
    payload: dict[str, object] = {
        "transcript_ingestion_version_id": "transcript-version-a",
        "transcript_id": "transcript-a",
        "content_hash": DIGEST,
        "state": state,
        "is_active": state == "READY",
        "created_at": NOW,
    }
    if state in {"READY", "SUPERSEDED"}:
        payload["ready_at"] = LATER
    if state == "FAILED":
        payload.update(
            {
                "failure_code": "INDEXING_FAILED",
                "failure_message": "Transcript indexing failed.",
            }
        )
    return TranscriptIngestionVersion.model_validate(payload)


def test_transcript_version_lifecycle_is_retry_safe() -> None:
    received = _transcript_version("RECEIVED")
    indexing = _transcript_version("INDEXING")
    ready = _transcript_version("READY")
    superseded = _transcript_version("SUPERSEDED")
    failed = _transcript_version("FAILED")
    validate_transcript_version_transition(received, indexing)
    validate_transcript_version_transition(indexing, ready)
    validate_transcript_version_transition(ready, superseded)
    validate_transcript_version_transition(indexing, failed)
    validate_transcript_version_transition(failed, indexing)
    with pytest.raises(LifecycleTransitionError):
        validate_transcript_version_transition(received, ready)


def _document_version(state: str) -> DocumentVersion:
    payload: dict[str, object] = {
        "document_version_id": "document-version-a",
        "document_id": "document-a",
        "version_number": 1,
        "content_hash": DIGEST,
        "state": state,
        "is_active": state == "READY",
        "original_artifact_id": "artifact-a",
        "ingestion_key": "ingestion-a",
        "created_at": NOW,
    }
    if state in {"READY", "SUPERSEDED"}:
        payload["ready_at"] = LATER
    if state == "SUPERSEDED":
        payload["superseded_at"] = LATER + timedelta(minutes=1)
    if state == "FAILED":
        payload.update(
            {
                "failure_code": "EXTRACTION_FAILED",
                "failure_message": "Document extraction failed.",
            }
        )
    return DocumentVersion.model_validate(payload)


def test_document_version_full_path_retry_and_atomic_activation() -> None:
    states = [
        "RECEIVED",
        "VALIDATING",
        "EXTRACTING",
        "ENRICHING",
        "INDEXING",
        "READY",
        "SUPERSEDED",
    ]
    versions = [_document_version(state) for state in states]
    for previous, proposed in pairwise(versions):
        validate_document_version_transition(previous, proposed)
    failed = _document_version("FAILED")
    validate_document_version_transition(_document_version("INDEXING"), failed)
    validate_document_version_transition(failed, _document_version("VALIDATING"))
    with pytest.raises(LifecycleTransitionError):
        validate_document_version_transition(versions[0], versions[-2])


def _insight_run(status: str) -> InsightRun:
    payload: dict[str, object] = {
        "run_id": "run-a",
        "engagement_id": "engagement-a",
        "thread_id": "thread-a",
        "status": status,
        "requested_question": "What is the operating risk?",
        "started_at": NOW,
    }
    if status in {
        "researching",
        "editing",
        "validating",
        "complete",
        "partial",
        "insufficient_evidence",
    }:
        payload["plan_id"] = "plan-a"
    if status in {"complete", "partial", "insufficient_evidence"}:
        payload.update({"report_id": "report-a", "completed_at": LATER})
    if status == "failed":
        payload.update(
            {
                "failure_code": "PROVIDER_FAILED",
                "failure_message": "The provider did not complete the run.",
                "completed_at": LATER,
            }
        )
    return InsightRun.model_validate(payload)


def test_insight_run_requires_the_agent_stage_order() -> None:
    states = ["queued", "planning", "researching", "editing", "validating", "complete"]
    runs = [_insight_run(state) for state in states]
    for previous, proposed in pairwise(runs):
        validate_insight_run_transition(previous, proposed)
    with pytest.raises(LifecycleTransitionError):
        validate_insight_run_transition(runs[1], runs[-1])
    validate_insight_run_transition(runs[0], _insight_run("failed"))
