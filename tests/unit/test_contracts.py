"""Canonical access, research-plan, and report contract tests."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from stakeholder_intelligence_agent.contracts import ResearchPlan, ResearchTopic
from stakeholder_intelligence_agent.contracts.insight import (
    EvidenceGap,
    InsightReport,
    ResearchedTopicOutcome,
    RunMetadata,
)
from tests.helpers import insight_context, pm_access, stakeholder_access


def test_pm_context_rejects_stakeholder_scope() -> None:
    payload = pm_access().model_dump()
    payload["stakeholder_id"] = "stakeholder-a"
    with pytest.raises(ValidationError):
        type(pm_access()).model_validate(payload)


def test_stakeholder_context_rejects_mismatched_principal_identity() -> None:
    payload = stakeholder_access().model_dump()
    payload["principal_id"] = "stakeholder-b"
    with pytest.raises(ValidationError):
        type(stakeholder_access()).model_validate(payload)


def test_research_plan_rejects_dependency_cycle() -> None:
    context = insight_context()
    with pytest.raises(ValidationError):
        ResearchPlan(
            plan_id="plan-a",
            run_id=context.run_id,
            engagement_id=context.access.engagement_id,
            question=context.question,
            topics=(
                ResearchTopic(
                    topic_id="topic-a",
                    title="Topic A",
                    objective="Research A.",
                    questions=("Question A?",),
                    dependencies=("topic-b",),
                    priority=1,
                ),
                ResearchTopic(
                    topic_id="topic-b",
                    title="Topic B",
                    objective="Research B.",
                    questions=("Question B?",),
                    dependencies=("topic-a",),
                    priority=2,
                ),
            ),
            source_strategy=("document", "interview"),
            completion_criteria=("Every material claim uses registered evidence.",),
            created_at=datetime.now(UTC),
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("questions", ("Question A?", "Question A?")),
        ("required_source_types", ("document", "document")),
        ("dependencies", ("topic-b", "topic-b")),
    ],
)
def test_research_topic_rejects_duplicate_work_items(
    field: str,
    value: tuple[str, ...],
) -> None:
    payload = {
        "topic_id": "topic-a",
        "title": "Topic A",
        "objective": "Research A.",
        "questions": ("Question A?",),
        "required_source_types": ("document",),
        "dependencies": ("topic-b",),
        "priority": 1,
    }
    payload[field] = value
    with pytest.raises(ValidationError):
        ResearchTopic.model_validate(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source_strategy", ("document", "document")),
        (
            "completion_criteria",
            ("Every claim is supported.", "Every claim is supported."),
        ),
    ],
)
def test_research_plan_rejects_duplicate_strategy_items(
    field: str,
    value: tuple[str, ...],
) -> None:
    payload = {
        "plan_id": "plan-a",
        "run_id": "run-a",
        "engagement_id": "engagement-a",
        "question": "What is the operating risk?",
        "topics": (
            {
                "topic_id": "topic-a",
                "title": "Topic A",
                "objective": "Research A.",
                "questions": ("Question A?",),
                "priority": 1,
            },
        ),
        "source_strategy": ("document", "interview"),
        "completion_criteria": ("Every claim is supported.",),
        "created_at": datetime.now(UTC),
    }
    payload[field] = value
    with pytest.raises(ValidationError):
        ResearchPlan.model_validate(payload)


def test_honest_insufficient_evidence_report_accepts_empty_evidence() -> None:
    context = insight_context()
    now = datetime.now(UTC)
    report = InsightReport(
        report_id="report-a",
        engagement_id=context.access.engagement_id,
        question=context.question,
        status="insufficient_evidence",
        executive_summary="No authorized current source supports a responsible conclusion.",
        researched_topics=(
            ResearchedTopicOutcome(
                topic_id="topic-a",
                title="Operational risk",
                status="insufficient_evidence",
                summary="No READY authorized source was available.",
            ),
        ),
        findings=(),
        responsibilities=(),
        operational_risks=(),
        buy_in_signals=(),
        contradictions=(),
        evidence_gaps=(
            EvidenceGap(
                topic="Operational risk",
                description="No current authorized source was available.",
                impact="The requested risk conclusion cannot be supported.",
            ),
        ),
        open_questions=("Which READY source should establish the current process?",),
        follow_up_recommendations=(),
        evidence_ids=(),
        citations=(),
        run_metadata=RunMetadata(
            run_id=context.run_id,
            started_at=now,
            completed_at=now,
            primary_model_id="gemini-test-primary",
            fallback_model_id="gemini-test-fallback",
            topic_count=1,
            status_detail="Research completed without authorized evidence.",
        ),
    )
    assert report.status == "insufficient_evidence"
    assert report.evidence_ids == ()


def test_complete_report_cannot_omit_evidence() -> None:
    context = insight_context()
    now = datetime.now(UTC)
    with pytest.raises(ValidationError):
        InsightReport(
            report_id="report-a",
            engagement_id=context.access.engagement_id,
            question=context.question,
            status="complete",
            executive_summary="Unsupported.",
            researched_topics=(
                ResearchedTopicOutcome(
                    topic_id="topic-a",
                    title="Topic A",
                    status="completed",
                    summary="Unsupported.",
                ),
            ),
            findings=(),
            responsibilities=(),
            operational_risks=(),
            buy_in_signals=(),
            contradictions=(),
            evidence_gaps=(),
            open_questions=(),
            follow_up_recommendations=(),
            evidence_ids=(),
            citations=(),
            run_metadata=RunMetadata(
                run_id=context.run_id,
                started_at=now,
                completed_at=now,
                primary_model_id="gemini-test-primary",
                fallback_model_id="gemini-test-fallback",
                topic_count=1,
                status_detail="Invalid complete report.",
            ),
        )
