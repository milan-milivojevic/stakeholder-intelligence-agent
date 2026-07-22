"""Deterministic server ownership checks for the lean editor draft."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

import pytest

from stakeholder_intelligence_agent.contracts import EvidenceRecord
from stakeholder_intelligence_agent.contracts.insight import ResearchPlan, ResearchTopic
from stakeholder_intelligence_agent.contracts.source import TranscriptTurnsLocation
from stakeholder_intelligence_agent.errors import EvidencePolicyError
from stakeholder_intelligence_agent.insight.editor import (
    EditorReportDraft,
    _normalized_report_status,
    build_server_owned_report,
)
from tests.helpers import insight_context

if TYPE_CHECKING:
    from stakeholder_intelligence_agent.config import Settings

TopicStatus = Literal["completed", "failed", "insufficient_evidence"]
NOW = datetime(2026, 7, 21, tzinfo=UTC)


def _draft(*statuses: TopicStatus) -> EditorReportDraft:
    return EditorReportDraft.model_validate(
        {
            "status": "complete",
            "executive_summary": "The bounded report contains a deliberately minimal test summary.",
            "researched_topics": [
                {
                    "topic_id": f"topic-{index}",
                    "title": f"Ignored model title {index}",
                    "status": status,
                    "summary": "The bounded research topic produced this outcome.",
                }
                for index, status in enumerate(statuses, start=1)
            ],
            "findings": [
                {
                    "claim_id": "ignored-model-claim",
                    "statement": "The bounded test has one supported finding.",
                    "evidence_ids": ["evidence-test"],
                }
            ],
            "responsibilities": [],
            "operational_risks": [],
            "buy_in_signals": [],
            "contradictions": [],
            "evidence_gaps": [],
            "open_questions": [],
            "follow_up_recommendations": [],
        }
    )


def test_complete_editor_status_is_preserved_when_every_topic_completed() -> None:
    assert _normalized_report_status(_draft("completed", "completed")) == "complete"


def test_complete_editor_status_is_downgraded_for_mixed_topic_outcomes() -> None:
    assert _normalized_report_status(_draft("completed", "insufficient_evidence")) == "partial"


def test_all_unsupported_topics_are_downgraded_to_insufficient_evidence() -> None:
    draft = _draft("insufficient_evidence").model_copy(update={"findings": ()})
    assert _normalized_report_status(draft) == "insufficient_evidence"


def test_insufficient_status_with_supported_claims_is_rejected_not_upgraded() -> None:
    draft = _draft("completed").model_copy(update={"status": "insufficient_evidence"})
    with pytest.raises(EvidencePolicyError):
        _normalized_report_status(draft)


def test_editor_tool_schema_does_not_require_server_owned_claim_ids() -> None:
    schema = EditorReportDraft.model_json_schema()
    assert "claim_id" not in schema["$defs"]["EditorFindingDraft"]["required"]
    assert "claim_id" not in schema["$defs"]["EditorResponsibilityDraft"]["required"]
    assert "claim_id" not in schema["$defs"]["EditorOperationalRiskDraft"]["required"]


def test_server_replaces_duplicate_model_claim_ids_before_report_validation(
    settings: Settings,
) -> None:
    context = insight_context(run_id="run-duplicate-claim")
    plan = ResearchPlan(
        plan_id="plan-duplicate-claim",
        run_id=context.run_id,
        engagement_id=context.access.engagement_id,
        question=context.question,
        topics=(
            ResearchTopic(
                topic_id="topic-operations",
                title="Operational responsibilities",
                objective="Identify supported operational responsibilities.",
                questions=("Which responsibilities are supported?",),
                required_source_types=("interview",),
                priority=1,
            ),
        ),
        source_strategy=("interview",),
        completion_criteria=("Cite the finalized stakeholder interview.",),
        created_at=NOW,
    )
    evidence = EvidenceRecord(
        evidence_id="evidence-operations",
        run_id=context.run_id,
        engagement_id=context.access.engagement_id,
        topic_id="topic-operations",
        source_id="transcript-operations",
        source_version_id="transcript-version-operations",
        source_type="interview",
        stakeholder_id="stakeholder-operations",
        location=TranscriptTurnsLocation(
            stakeholder_id="stakeholder-operations",
            transcript_id="transcript-operations",
            turn_start=0,
            turn_end=4,
        ),
        original_excerpt="The stakeholder described operational responsibilities.",
        content_hash="a" * 64,
        researcher_id="researcher-operations",
        created_at=NOW,
    )
    draft = EditorReportDraft.model_validate(
        {
            "status": "complete",
            "executive_summary": "The interview supports an operational responsibility.",
            "researched_topics": [
                {
                    "topic_id": "topic-operations",
                    "title": "A model-proposed title is ignored",
                    "status": "completed",
                    "summary": "The finalized interview was reviewed.",
                    "evidence_ids": (evidence.evidence_id,),
                }
            ],
            "findings": [
                {
                    "claim_id": "duplicate-model-claim",
                    "statement": "Store Operations coordinates the pilot workflow.",
                    "evidence_ids": (evidence.evidence_id,),
                }
            ],
            "responsibilities": [
                {
                    "claim_id": "duplicate-model-claim",
                    "responsibility": "Coordinate the pilot workflow.",
                    "attribution": "Store Operations",
                    "uncertainty": "Low uncertainty based on the completed interview.",
                    "evidence_ids": (evidence.evidence_id,),
                }
            ],
            "operational_risks": [],
            "buy_in_signals": [],
            "contradictions": [],
            "evidence_gaps": [],
            "open_questions": [],
            "follow_up_recommendations": [],
        }
    )

    first = build_server_owned_report(
        draft,
        context=context,
        plan=plan,
        settings=settings,
        evidence_by_id={evidence.evidence_id: evidence},
        started_at=NOW,
        completed_at=NOW,
    )
    second = build_server_owned_report(
        draft,
        context=context,
        plan=plan,
        settings=settings,
        evidence_by_id={evidence.evidence_id: evidence},
        started_at=NOW,
        completed_at=NOW,
    )

    claim_ids = (first.findings[0].claim_id, first.responsibilities[0].claim_id)
    assert len(set(claim_ids)) == 2
    assert all(claim_id.startswith("claim-") for claim_id in claim_ids)
    assert first.citations[0].claim_ids == tuple(sorted(claim_ids))
    assert second.findings[0].claim_id == first.findings[0].claim_id
    assert second.responsibilities[0].claim_id == first.responsibilities[0].claim_id
    assert first.researched_topics[0].title == "Operational responsibilities"
