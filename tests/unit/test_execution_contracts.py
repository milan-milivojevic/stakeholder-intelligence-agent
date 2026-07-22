"""Insight-run, operational-audit, and safe progress contract tests."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from stakeholder_intelligence_agent.contracts import (
    InsightRun,
    OperationalAuditEvent,
    SafeProgressEvent,
)

NOW = datetime(2026, 7, 15, 1, 0, tzinfo=UTC)


def test_terminal_insight_run_requires_plan_report_and_completion() -> None:
    with pytest.raises(ValidationError):
        InsightRun(
            run_id="run-a",
            engagement_id="engagement-a",
            thread_id="thread-a",
            status="complete",
            requested_question="What is the operating risk?",
            started_at=NOW,
        )
    completed = InsightRun(
        run_id="run-a",
        engagement_id="engagement-a",
        thread_id="thread-a",
        status="complete",
        requested_question="What is the operating risk?",
        plan_id="plan-a",
        report_id="report-a",
        started_at=NOW,
        completed_at=NOW + timedelta(minutes=1),
    )
    assert completed.status == "complete"


def test_failed_run_cannot_masquerade_as_report_status() -> None:
    failed = InsightRun(
        run_id="run-a",
        engagement_id="engagement-a",
        thread_id="thread-a",
        status="failed",
        requested_question="What is the operating risk?",
        failure_code="PROVIDER_FAILED",
        failure_message="The model provider did not complete the run.",
        started_at=NOW,
        completed_at=NOW + timedelta(seconds=30),
    )
    assert failed.report_id is None
    with pytest.raises(ValidationError):
        InsightRun.model_validate(failed.model_dump() | {"failure_code": None})


def test_audit_failure_and_retry_fields_are_explicit() -> None:
    denied = OperationalAuditEvent(
        event_id="event-a",
        occurred_at=NOW,
        engagement_id="engagement-a",
        actor="interview route",
        action="activate_invitation",
        status="denied",
        failure_code="ACCESS_DENIED",
        correlation_id="correlation-a",
    )
    assert denied.status == "denied"
    with pytest.raises(ValidationError):
        OperationalAuditEvent.model_validate(
            denied.model_dump() | {"status": "retried", "failure_code": None}
        )


def test_safe_progress_rejects_host_paths_and_private_reasoning() -> None:
    event = SafeProgressEvent(
        event_id="event-a",
        occurred_at=NOW,
        engagement_id="engagement-a",
        run_id="run-a",
        thread_id="thread-a",
        stage="researching",
        status="in_progress",
        subagent="topic-researcher",
        tool_name="retrieve_current_sources",
        artifact_name="research/topic-a/findings.md",
        correlation_id="correlation-a",
    )
    assert event.artifact_name == "research/topic-a/findings.md"
    with pytest.raises(ValidationError):
        SafeProgressEvent.model_validate(
            event.model_dump() | {"artifact_name": "C:\\secret\\findings.md"}
        )
    with pytest.raises(ValidationError):
        SafeProgressEvent.model_validate(event.model_dump() | {"reasoning": "private"})


def test_failed_progress_requires_safe_visible_detail() -> None:
    with pytest.raises(ValidationError):
        SafeProgressEvent(
            event_id="event-a",
            occurred_at=NOW,
            engagement_id="engagement-a",
            stage="ingestion",
            status="failed",
            correlation_id="correlation-a",
        )
