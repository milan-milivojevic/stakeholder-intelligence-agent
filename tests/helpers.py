"""Typed builders for canonical test objects."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from stakeholder_intelligence_agent.contracts import (
    AccessContext,
    InsightRuntimeContext,
    InterviewRuntimeContext,
)


def pm_access(
    *,
    engagement_id: str = "engagement-a",
    thread_id: str = "thread-a",
) -> AccessContext:
    """Return a non-expired PM context with the minimal insight permission."""
    issued = datetime.now(UTC)
    return AccessContext(
        principal_type="pm",
        principal_id="pm-local",
        engagement_id=engagement_id,
        stakeholder_id=None,
        interview_session_id=None,
        thread_id=thread_id,
        permissions=frozenset({"insight:run"}),
        issued_at=issued,
        expires_at=issued + timedelta(hours=1),
        correlation_id="correlation-a",
    )


def insight_context(
    *,
    engagement_id: str = "engagement-a",
    thread_id: str = "thread-a",
    run_id: str = "run-a",
    question: str = "What operational risks are supported by current evidence?",
) -> InsightRuntimeContext:
    """Return a valid trusted PM insight runtime context."""
    return InsightRuntimeContext(
        access=pm_access(engagement_id=engagement_id, thread_id=thread_id),
        run_id=run_id,
        question=question,
    )


def stakeholder_access(
    *,
    engagement_id: str = "engagement-a",
    stakeholder_id: str = "stakeholder-a",
    thread_id: str = "interview-thread-a",
) -> AccessContext:
    """Return a non-expired stakeholder context for one interview session."""
    issued = datetime.now(UTC)
    return AccessContext(
        principal_type="stakeholder",
        principal_id=stakeholder_id,
        engagement_id=engagement_id,
        stakeholder_id=stakeholder_id,
        interview_session_id="interview-session-a",
        thread_id=thread_id,
        permissions=frozenset(
            {
                "document:upload",
                "interview:finalize",
                "interview:participate",
                "source:read",
            }
        ),
        issued_at=issued,
        expires_at=issued + timedelta(hours=1),
        correlation_id="interview-correlation-a",
    )


def interview_context(
    *,
    engagement_id: str = "engagement-a",
    stakeholder_id: str = "stakeholder-a",
    thread_id: str = "interview-thread-a",
) -> InterviewRuntimeContext:
    """Return a valid trusted interview runtime context."""
    return InterviewRuntimeContext(
        access=stakeholder_access(
            engagement_id=engagement_id,
            stakeholder_id=stakeholder_id,
            thread_id=thread_id,
        ),
        role="Operations manager",
        department="Operations",
    )
