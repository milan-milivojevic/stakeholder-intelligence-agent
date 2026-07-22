"""Canonical access-domain and transcript contract verification."""

from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from stakeholder_intelligence_agent.contracts import (
    Engagement,
    InterviewSession,
    InvitationToken,
    PMAccess,
    Stakeholder,
    Transcript,
    TranscriptIngestionVersion,
    TranscriptTurn,
)

NOW = datetime(2026, 7, 15, 1, 0, tzinfo=UTC)
DIGEST = "a" * 64


def test_domain_timestamps_are_normalized_to_utc() -> None:
    local_time = datetime(2026, 7, 15, 3, 0, tzinfo=timezone(timedelta(hours=2)))
    engagement = Engagement(
        engagement_id="engagement-a",
        name="Transformation A",
        status="active",
        created_at=local_time,
        updated_at=local_time,
    )
    assert engagement.created_at == NOW
    assert engagement.created_at.tzinfo is UTC


def test_engagement_rejects_backward_time_and_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        Engagement(
            engagement_id="engagement-a",
            name="Transformation A",
            status="active",
            created_at=NOW,
            updated_at=NOW - timedelta(seconds=1),
        )
    with pytest.raises(ValidationError):
        Stakeholder.model_validate(
            {
                "stakeholder_id": "stakeholder-a",
                "engagement_id": "engagement-a",
                "display_name": "A. Stakeholder",
                "status": "active",
                "created_at": NOW,
                "updated_at": NOW,
                "score": 100,
            }
        )


@pytest.mark.parametrize(
    ("status", "revoked_at", "valid"),
    [
        ("active", None, True),
        ("revoked", NOW, True),
        ("active", NOW, False),
        ("revoked", None, False),
    ],
)
def test_pm_access_status_requires_consistent_revocation(
    status: str,
    revoked_at: datetime | None,
    valid: bool,
) -> None:
    payload = {
        "pm_access_id": "pm-a",
        "token_hash": DIGEST,
        "status": status,
        "created_at": NOW,
        "revoked_at": revoked_at,
    }
    if valid:
        assert PMAccess.model_validate(payload).status == status
    else:
        with pytest.raises(ValidationError):
            PMAccess.model_validate(payload)


@pytest.mark.parametrize(
    ("status", "activated_at", "revoked_at", "valid"),
    [
        ("active", None, None, True),
        ("activated", NOW + timedelta(minutes=1), None, True),
        ("expired", None, None, True),
        ("revoked", None, NOW + timedelta(minutes=1), True),
        ("activated", None, None, False),
        ("active", NOW + timedelta(minutes=1), None, False),
        ("expired", None, NOW + timedelta(minutes=1), False),
    ],
)
def test_invitation_token_lifecycle_shape(
    status: str,
    activated_at: datetime | None,
    revoked_at: datetime | None,
    valid: bool,
) -> None:
    payload = {
        "invitation_id": "invitation-a",
        "engagement_id": "engagement-a",
        "stakeholder_id": "stakeholder-a",
        "token_hash": DIGEST,
        "status": status,
        "created_at": NOW,
        "expires_at": NOW + timedelta(days=1),
        "activated_at": activated_at,
        "revoked_at": revoked_at,
        "created_by_pm_access_id": "pm-a",
    }
    if valid:
        token = InvitationToken.model_validate(payload)
        assert "raw_token" not in token.model_dump()
    else:
        with pytest.raises(ValidationError):
            InvitationToken.model_validate(payload)


def test_interview_ready_requires_finalized_identities() -> None:
    with pytest.raises(ValidationError):
        InterviewSession(
            interview_session_id="session-a",
            engagement_id="engagement-a",
            stakeholder_id="stakeholder-a",
            invitation_id="invitation-a",
            thread_id="thread-a",
            status="ready",
            started_at=NOW,
        )
    ready = InterviewSession(
        interview_session_id="session-a",
        engagement_id="engagement-a",
        stakeholder_id="stakeholder-a",
        invitation_id="invitation-a",
        thread_id="thread-a",
        status="ready",
        started_at=NOW,
        finalized_at=NOW + timedelta(minutes=10),
        transcript_id="transcript-a",
        ingestion_version_id="transcript-version-a",
    )
    assert ready.status == "ready"


def test_failed_interview_preserves_post_finalization_identities() -> None:
    base = {
        "interview_session_id": "session-a",
        "engagement_id": "engagement-a",
        "stakeholder_id": "stakeholder-a",
        "invitation_id": "invitation-a",
        "thread_id": "thread-a",
        "status": "failed",
        "started_at": NOW,
        "failure_code": "INDEXING_FAILED",
        "failure_message": "The finalized interview could not be indexed.",
    }
    with pytest.raises(ValidationError):
        InterviewSession.model_validate(base | {"finalized_at": NOW + timedelta(minutes=1)})
    failed = InterviewSession.model_validate(
        base
        | {
            "finalized_at": NOW + timedelta(minutes=1),
            "transcript_id": "transcript-a",
            "ingestion_version_id": "transcript-version-a",
        }
    )
    assert failed.transcript_id == "transcript-a"


def test_transcript_finalization_and_original_turn_preservation() -> None:
    original = "  Original source text remains exactly like this.  "
    turn = TranscriptTurn(
        turn_index=0,
        speaker="stakeholder",
        original_text=original,
        created_at=NOW,
    )
    assert turn.original_text == original

    with pytest.raises(ValidationError):
        Transcript(
            transcript_id="transcript-a",
            interview_session_id="session-a",
            engagement_id="engagement-a",
            stakeholder_id="stakeholder-a",
            status="finalized",
        )
    finalized = Transcript(
        transcript_id="transcript-a",
        interview_session_id="session-a",
        engagement_id="engagement-a",
        stakeholder_id="stakeholder-a",
        status="finalized",
        finalized_at=NOW,
        content_hash=DIGEST,
    )
    assert finalized.content_hash == DIGEST


def test_transcript_ingestion_ready_is_active_and_failure_is_explicit() -> None:
    with pytest.raises(ValidationError):
        TranscriptIngestionVersion(
            transcript_ingestion_version_id="version-a",
            transcript_id="transcript-a",
            content_hash=DIGEST,
            state="READY",
            is_active=False,
            created_at=NOW,
            ready_at=NOW,
        )
    failed = TranscriptIngestionVersion(
        transcript_ingestion_version_id="version-a",
        transcript_id="transcript-a",
        content_hash=DIGEST,
        state="FAILED",
        is_active=False,
        created_at=NOW,
        failure_code="INDEXING_FAILED",
        failure_message="The transcript could not be indexed.",
    )
    assert failed.failure_code == "INDEXING_FAILED"
