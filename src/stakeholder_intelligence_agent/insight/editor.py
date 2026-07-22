"""Lean model-authored editor draft plus deterministic server-owned report fields."""

# Pydantic resolves these field types while building the private tool schema.
# ruff: noqa: TC001

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from stakeholder_intelligence_agent.contracts import Citation, InsightReport
from stakeholder_intelligence_agent.contracts.common import NonEmptyText, OpaqueId
from stakeholder_intelligence_agent.contracts.insight import (
    BuyInSignal,
    Contradiction,
    EvidenceGap,
    FollowUpRecommendation,
    OperationalRisk,
    ReportClaim,
    ReportStatus,
    ResearchedTopicOutcome,
    ResponsibilityFinding,
    RunMetadata,
    TopicOutcomeStatus,
)
from stakeholder_intelligence_agent.errors import EvidencePolicyError
from stakeholder_intelligence_agent.ingestion.identity import stable_id

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime

    from stakeholder_intelligence_agent.config import Settings
    from stakeholder_intelligence_agent.contracts import (
        EvidenceRecord,
        InsightRuntimeContext,
        ResearchPlan,
    )


class EditorTopicDraft(BaseModel):
    """Model-authored outcome for one server-owned plan topic identity."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    topic_id: OpaqueId
    status: TopicOutcomeStatus
    summary: NonEmptyText


class EditorFindingDraft(BaseModel):
    """Model-authored finding without a model-controlled claim identity."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    statement: NonEmptyText
    evidence_ids: tuple[OpaqueId, ...]


class EditorResponsibilityDraft(BaseModel):
    """Model-authored responsibility without a model-controlled claim identity."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    responsibility: NonEmptyText
    attribution: NonEmptyText
    uncertainty: NonEmptyText
    evidence_ids: tuple[OpaqueId, ...]


class EditorOperationalRiskDraft(BaseModel):
    """Model-authored operational risk without a model-controlled claim identity."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    risk: NonEmptyText
    impact: NonEmptyText
    responsibility_context: NonEmptyText
    uncertainty: NonEmptyText
    evidence_ids: tuple[OpaqueId, ...]


class EditorReportDraft(BaseModel):
    """Only the analytical fields that genuinely require model judgment."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    status: ReportStatus
    executive_summary: NonEmptyText
    researched_topics: tuple[EditorTopicDraft, ...]
    findings: tuple[EditorFindingDraft, ...]
    responsibilities: tuple[EditorResponsibilityDraft, ...]
    operational_risks: tuple[EditorOperationalRiskDraft, ...]
    buy_in_signals: tuple[BuyInSignal, ...]
    contradictions: tuple[Contradiction, ...]
    evidence_gaps: tuple[EvidenceGap, ...]
    open_questions: tuple[NonEmptyText, ...]
    follow_up_recommendations: tuple[FollowUpRecommendation, ...]


def _server_owned_claims(
    draft: EditorReportDraft,
    run_id: str,
) -> tuple[
    tuple[ReportClaim, ...],
    tuple[ResponsibilityFinding, ...],
    tuple[OperationalRisk, ...],
]:
    """Hydrate deterministic section-unique claim IDs after tool-input validation."""
    findings = tuple(
        ReportClaim(
            claim_id=stable_id("claim", run_id, "finding", str(index)),
            statement=finding.statement,
            evidence_ids=finding.evidence_ids,
        )
        for index, finding in enumerate(draft.findings, start=1)
    )
    responsibilities = tuple(
        ResponsibilityFinding(
            claim_id=stable_id("claim", run_id, "responsibility", str(index)),
            responsibility=responsibility.responsibility,
            attribution=responsibility.attribution,
            uncertainty=responsibility.uncertainty,
            evidence_ids=responsibility.evidence_ids,
        )
        for index, responsibility in enumerate(draft.responsibilities, start=1)
    )
    operational_risks = tuple(
        OperationalRisk(
            claim_id=stable_id("claim", run_id, "operational-risk", str(index)),
            risk=risk.risk,
            impact=risk.impact,
            responsibility_context=risk.responsibility_context,
            uncertainty=risk.uncertainty,
            evidence_ids=risk.evidence_ids,
        )
        for index, risk in enumerate(draft.operational_risks, start=1)
    )
    return findings, responsibilities, operational_risks


def _claim_ids_by_evidence(
    findings: tuple[ReportClaim, ...],
    responsibilities: tuple[ResponsibilityFinding, ...],
    operational_risks: tuple[OperationalRisk, ...],
) -> dict[str, set[str]]:
    mapping: dict[str, set[str]] = {}
    for finding in findings:
        for evidence_id in finding.evidence_ids:
            mapping.setdefault(evidence_id, set()).add(finding.claim_id)
    for responsibility in responsibilities:
        for evidence_id in responsibility.evidence_ids:
            mapping.setdefault(evidence_id, set()).add(responsibility.claim_id)
    for risk in operational_risks:
        for evidence_id in risk.evidence_ids:
            mapping.setdefault(evidence_id, set()).add(risk.claim_id)
    return mapping


def _require_contradiction_references_are_citable(
    draft: EditorReportDraft,
    claim_evidence_ids: set[str],
) -> None:
    referenced: set[str] = set()
    for contradiction in draft.contradictions:
        referenced.update(contradiction.evidence_ids)
    if not referenced <= claim_evidence_ids:
        raise EvidencePolicyError


def _display_label(record: EvidenceRecord) -> str:
    filename = getattr(record.location, "filename", None)
    if isinstance(filename, str):
        return filename
    return "Finalized stakeholder interview"


def _normalized_report_status(
    draft: EditorReportDraft,
    topics: tuple[ResearchedTopicOutcome, ...] | None = None,
    *,
    has_supported_claims: bool | None = None,
) -> ReportStatus:
    """Deterministically downgrade unsupported status claims without upgrading them."""
    normalized_topics = topics or tuple(
        ResearchedTopicOutcome(
            topic_id=topic.topic_id,
            title=topic.topic_id,
            status=topic.status,
            summary=topic.summary,
        )
        for topic in draft.researched_topics
    )
    supported = (
        bool(draft.findings or draft.responsibilities or draft.operational_risks)
        if has_supported_claims is None
        else has_supported_claims
    )
    statuses = {topic.status for topic in normalized_topics}
    if not supported:
        return "insufficient_evidence"
    if draft.status == "insufficient_evidence":
        raise EvidencePolicyError
    if draft.status == "complete" and statuses == {"completed"} and bool(draft.findings):
        return "complete"
    return "partial"


def build_server_owned_report(  # noqa: PLR0913
    draft: EditorReportDraft,
    *,
    context: InsightRuntimeContext,
    plan: ResearchPlan,
    settings: Settings,
    evidence_by_id: Mapping[str, EvidenceRecord],
    topic_artifact_statuses: Mapping[str, str] | None = None,
    started_at: datetime,
    completed_at: datetime,
) -> InsightReport:
    """Hydrate identity, locators, citations, and runtime metadata without model guesses."""
    findings, responsibilities, operational_risks = _server_owned_claims(draft, context.run_id)
    planned_topics = {topic.topic_id: topic for topic in plan.topics}
    reported_topics = {topic.topic_id: topic for topic in draft.researched_topics}
    if planned_topics.keys() != reported_topics.keys():
        raise EvidencePolicyError

    claim_ids_by_evidence = _claim_ids_by_evidence(
        findings,
        responsibilities,
        operational_risks,
    )
    evidence_ids = set(claim_ids_by_evidence)
    if evidence_ids != set(evidence_by_id):
        raise EvidencePolicyError
    _require_contradiction_references_are_citable(draft, evidence_ids)

    normalized_signals: list[BuyInSignal] = []
    for signal in draft.buy_in_signals:
        supported_ids = tuple(
            evidence_id for evidence_id in signal.evidence_ids if evidence_id in evidence_ids
        )
        if (
            signal.category not in {"insufficient_evidence", "topic_not_discussed"}
            and not supported_ids
        ):
            normalized_signals.append(
                signal.model_copy(
                    update={
                        "category": "insufficient_evidence",
                        "explanation": (
                            "No claim-linked evidence supports a qualitative buy-in signal."
                        ),
                        "evidence_ids": (),
                    }
                )
            )
        else:
            normalized_signals.append(signal.model_copy(update={"evidence_ids": supported_ids}))
    normalized_recommendations = tuple(
        recommendation.model_copy(
            update={
                "evidence_ids": tuple(
                    evidence_id
                    for evidence_id in recommendation.evidence_ids
                    if evidence_id in evidence_ids
                )
            }
        )
        for recommendation in draft.follow_up_recommendations
    )

    topic_artifact_statuses = topic_artifact_statuses or {}
    normalized_topics = tuple(
        ResearchedTopicOutcome(
            topic_id=topic.topic_id,
            title=topic.title,
            status=(
                "failed"
                if topic_artifact_statuses.get(topic.topic_id) == "failed"
                else (
                    "insufficient_evidence"
                    if topic_artifact_statuses.get(topic.topic_id) == "insufficient_evidence"
                    else reported_topics[topic.topic_id].status
                )
            ),
            summary=reported_topics[topic.topic_id].summary,
            evidence_ids=tuple(
                evidence_id
                for evidence_id in sorted(evidence_ids)
                if evidence_by_id[evidence_id].topic_id == topic.topic_id
            ),
        )
        for topic in plan.topics
    )
    normalized_gaps = draft.evidence_gaps
    if not evidence_ids and not normalized_gaps:
        normalized_gaps = tuple(
            EvidenceGap(
                topic=topic.title,
                description=reported_topics[topic.topic_id].summary,
                impact=f"The planned objective remains unsupported: {topic.objective}",
            )
            for topic in plan.topics
        )
    ordered_evidence_ids = tuple(sorted(evidence_ids))
    citations = tuple(
        Citation(
            citation_id=stable_id("citation", context.run_id, evidence_id),
            evidence_id=evidence_id,
            display_label=_display_label(evidence_by_id[evidence_id]),
            source_location=evidence_by_id[evidence_id].location,
            claim_ids=tuple(sorted(claim_ids_by_evidence[evidence_id])),
        )
        for evidence_id in ordered_evidence_ids
    )
    return InsightReport(
        report_id=stable_id("report", context.run_id),
        engagement_id=context.access.engagement_id,
        question=context.question,
        status=_normalized_report_status(
            draft,
            normalized_topics,
            has_supported_claims=bool(findings or responsibilities or operational_risks),
        ),
        executive_summary=draft.executive_summary,
        researched_topics=normalized_topics,
        findings=findings,
        responsibilities=responsibilities,
        operational_risks=operational_risks,
        buy_in_signals=tuple(normalized_signals),
        contradictions=draft.contradictions,
        evidence_gaps=normalized_gaps,
        open_questions=draft.open_questions,
        follow_up_recommendations=normalized_recommendations,
        evidence_ids=ordered_evidence_ids,
        citations=citations,
        run_metadata=RunMetadata(
            run_id=context.run_id,
            started_at=started_at,
            completed_at=completed_at,
            primary_model_id=settings.gemini_primary_chat_model,
            fallback_model_id=settings.gemini_fallback_chat_model,
            topic_count=len(plan.topics),
            status_detail=(
                "The report completed through the bounded multi-agent workflow; "
                "authoritative execution totals accompany the report as server metrics."
            ),
        ),
    )
