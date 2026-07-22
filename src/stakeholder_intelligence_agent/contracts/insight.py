"""Canonical research-plan and insight-report contracts."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from stakeholder_intelligence_agent.contracts.common import (
    NonEmptyText,
    OpaqueId,
    ShortText,
    UtcDatetime,
)
from stakeholder_intelligence_agent.contracts.evidence import Citation

SourceType = Literal["document", "interview"]
ReportStatus = Literal["complete", "partial", "insufficient_evidence"]
TopicOutcomeStatus = Literal["completed", "failed", "insufficient_evidence"]
BuyInCategory = Literal[
    "confirmed_support",
    "conditional_support",
    "expressed_concern",
    "insufficient_evidence",
    "topic_not_discussed",
]


class ResearchTopic(BaseModel):
    """One bounded topic that maps to one researcher task call."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    topic_id: OpaqueId
    title: ShortText
    objective: NonEmptyText
    questions: tuple[NonEmptyText, ...] = Field(min_length=1, max_length=10)
    required_source_types: tuple[SourceType, ...] = ()
    dependencies: tuple[OpaqueId, ...] = ()
    priority: int = Field(ge=1, le=5)

    @model_validator(mode="after")
    def validate_repeated_values(self) -> Self:
        """Reject duplicate work items without forbidding parallel priorities."""
        if len(self.questions) != len(set(self.questions)):
            raise ValueError("Research-topic questions must be unique.")
        if len(self.required_source_types) != len(set(self.required_source_types)):
            raise ValueError("Research-topic source types must be unique.")
        if len(self.dependencies) != len(set(self.dependencies)):
            raise ValueError("Research-topic dependencies must be unique.")
        return self


class ResearchPlan(BaseModel):
    """Server-scoped structured plan that supplements TODO and Markdown artifacts."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    plan_id: OpaqueId
    run_id: OpaqueId
    engagement_id: OpaqueId
    question: NonEmptyText
    topics: tuple[ResearchTopic, ...] = Field(min_length=1, max_length=5)
    source_strategy: tuple[SourceType, ...] = Field(min_length=1, max_length=2)
    completion_criteria: tuple[NonEmptyText, ...] = Field(min_length=1, max_length=10)
    created_at: UtcDatetime

    @model_validator(mode="after")
    def validate_topic_graph(self) -> Self:
        """Reject duplicate, missing, self, and cyclic topic dependencies."""
        if len(self.source_strategy) != len(set(self.source_strategy)):
            raise ValueError("Research-plan source types must be unique.")
        if len(self.completion_criteria) != len(set(self.completion_criteria)):
            raise ValueError("Research-plan completion criteria must be unique.")
        topic_ids = [topic.topic_id for topic in self.topics]
        if len(topic_ids) != len(set(topic_ids)):
            raise ValueError("Research topic IDs must be unique.")
        known = set(topic_ids)
        dependencies = {topic.topic_id: set(topic.dependencies) for topic in self.topics}
        for topic_id, topic_dependencies in dependencies.items():
            if topic_id in topic_dependencies:
                raise ValueError("A research topic cannot depend on itself.")
            if not topic_dependencies <= known:
                raise ValueError("Every research dependency must reference a plan topic.")

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(topic_id: str) -> None:
            if topic_id in visiting:
                raise ValueError("Research topic dependencies must be acyclic.")
            if topic_id in visited:
                return
            visiting.add(topic_id)
            for dependency in dependencies[topic_id]:
                visit(dependency)
            visiting.remove(topic_id)
            visited.add(topic_id)

        for topic_id in topic_ids:
            visit(topic_id)
        return self


class ReportClaim(BaseModel):
    """A material evidence-grounded finding."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    claim_id: OpaqueId
    statement: NonEmptyText
    evidence_ids: tuple[OpaqueId, ...] = Field(min_length=1)


class ResearchedTopicOutcome(BaseModel):
    """Editor-visible outcome for one planned research topic."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    topic_id: OpaqueId
    title: ShortText
    status: TopicOutcomeStatus
    summary: NonEmptyText
    evidence_ids: tuple[OpaqueId, ...] = ()


class ResponsibilityFinding(BaseModel):
    """Evidence-grounded responsibility with explicit uncertainty."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    claim_id: OpaqueId
    responsibility: NonEmptyText
    attribution: NonEmptyText
    uncertainty: NonEmptyText
    evidence_ids: tuple[OpaqueId, ...] = Field(min_length=1)


class OperationalRisk(BaseModel):
    """Evidence-grounded operational risk without unsupported scoring."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    claim_id: OpaqueId
    risk: NonEmptyText
    impact: NonEmptyText
    responsibility_context: NonEmptyText
    uncertainty: NonEmptyText
    evidence_ids: tuple[OpaqueId, ...] = Field(min_length=1)


class BuyInSignal(BaseModel):
    """Qualitative topic-specific support or concern signal."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    topic: ShortText
    stakeholder_id: OpaqueId | None = None
    role: ShortText | None = None
    department: ShortText | None = None
    category: BuyInCategory
    explanation: NonEmptyText
    evidence_ids: tuple[OpaqueId, ...] = ()

    @model_validator(mode="after")
    def require_support_when_asserted(self) -> Self:
        """Require evidence for support or concern assertions."""
        if (
            self.category not in {"insufficient_evidence", "topic_not_discussed"}
            and not self.evidence_ids
        ):
            raise ValueError("An asserted buy-in signal requires evidence.")
        return self


class ContradictionSide(BaseModel):
    """One independently supported side of a contradiction."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    statement: NonEmptyText
    stakeholder_id: OpaqueId | None = None
    role: ShortText | None = None
    department: ShortText | None = None
    evidence_ids: tuple[OpaqueId, ...] = Field(min_length=1)


class Contradiction(BaseModel):
    """Two-sided evidence-grounded disagreement."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    topic: ShortText
    side_a: ContradictionSide
    side_b: ContradictionSide
    interpretation: NonEmptyText
    evidence_ids: tuple[OpaqueId, ...] = Field(min_length=2)

    @model_validator(mode="after")
    def validate_both_sides(self) -> Self:
        """Require the declared evidence set to cover both supported sides."""
        side_evidence = set(self.side_a.evidence_ids) | set(self.side_b.evidence_ids)
        if not side_evidence <= set(self.evidence_ids):
            raise ValueError("Contradiction evidence_ids must cover both sides.")
        if set(self.side_a.evidence_ids) == set(self.side_b.evidence_ids):
            raise ValueError("Contradiction sides require distinguishable evidence.")
        return self


class EvidenceGap(BaseModel):
    """A material absence or weakness in permitted evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    topic: ShortText
    description: NonEmptyText
    impact: NonEmptyText


class FollowUpRecommendation(BaseModel):
    """A non-executing, evidence-aware next step."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    recommendation: NonEmptyText
    rationale: NonEmptyText
    evidence_ids: tuple[OpaqueId, ...] = ()


class RunMetadata(BaseModel):
    """Immutable report identity, production timing, model IDs, and safe status detail."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: OpaqueId
    started_at: UtcDatetime
    completed_at: UtcDatetime
    primary_model_id: ShortText
    fallback_model_id: ShortText
    topic_count: int = Field(ge=1, le=5)
    status_detail: NonEmptyText

    @field_validator("primary_model_id", "fallback_model_id")
    @classmethod
    def require_gemini_model(cls, value: str) -> str:
        """Reject non-Gemini provider identifiers in report metadata."""
        if "gemini" not in value.lower():
            raise ValueError("Runtime report model IDs must identify Gemini models.")
        return value

    @model_validator(mode="after")
    def validate_timing(self) -> Self:
        """Require non-negative run duration."""
        if self.completed_at < self.started_at:
            raise ValueError("completed_at must not precede started_at.")
        return self


class InsightReport(BaseModel):
    """Strict evidence-grounded report produced only by the editor subagent."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    report_id: OpaqueId
    engagement_id: OpaqueId
    question: NonEmptyText
    status: ReportStatus
    executive_summary: NonEmptyText
    researched_topics: tuple[ResearchedTopicOutcome, ...] = Field(min_length=1, max_length=5)
    findings: tuple[ReportClaim, ...]
    responsibilities: tuple[ResponsibilityFinding, ...]
    operational_risks: tuple[OperationalRisk, ...]
    buy_in_signals: tuple[BuyInSignal, ...]
    contradictions: tuple[Contradiction, ...]
    evidence_gaps: tuple[EvidenceGap, ...]
    open_questions: tuple[NonEmptyText, ...]
    follow_up_recommendations: tuple[FollowUpRecommendation, ...]
    evidence_ids: tuple[OpaqueId, ...]
    citations: tuple[Citation, ...]
    run_metadata: RunMetadata

    @model_validator(mode="after")
    def validate_evidence_links(self) -> Self:
        """Validate internal evidence and claim references deterministically."""
        evidence_ids = set(self.evidence_ids)
        if len(evidence_ids) != len(self.evidence_ids):
            raise ValueError("Report evidence_ids must be unique.")

        claims: list[ReportClaim | ResponsibilityFinding | OperationalRisk] = [
            *self.findings,
            *self.responsibilities,
            *self.operational_risks,
        ]
        claim_ids = {claim.claim_id for claim in claims}
        if len(claim_ids) != len(claims):
            raise ValueError("Report claim IDs must be unique.")

        referenced = self._collect_evidence_references()
        if not referenced <= evidence_ids:
            raise ValueError("Every report evidence reference must appear in evidence_ids.")

        self._validate_citations(evidence_ids, claim_ids)
        self._validate_status_semantics()
        return self

    def _collect_evidence_references(self) -> set[str]:
        """Collect every nested evidence reference in the report."""
        referenced: set[str] = set()
        claims: tuple[ReportClaim | ResponsibilityFinding | OperationalRisk, ...] = (
            *self.findings,
            *self.responsibilities,
            *self.operational_risks,
        )
        for claim in claims:
            referenced.update(claim.evidence_ids)
        for topic in self.researched_topics:
            referenced.update(topic.evidence_ids)
        for signal in self.buy_in_signals:
            referenced.update(signal.evidence_ids)
        for contradiction in self.contradictions:
            referenced.update(contradiction.evidence_ids)
        for recommendation in self.follow_up_recommendations:
            referenced.update(recommendation.evidence_ids)
        return referenced

    def _validate_citations(self, evidence_ids: set[str], claim_ids: set[str]) -> None:
        """Require every citation to resolve inside this report."""
        citation_ids = [citation.citation_id for citation in self.citations]
        if len(citation_ids) != len(set(citation_ids)):
            raise ValueError("Report citation IDs must be unique.")
        cited_evidence_ids = {citation.evidence_id for citation in self.citations}
        if cited_evidence_ids != evidence_ids:
            raise ValueError("Every declared report evidence item requires a citation.")
        for citation in self.citations:
            if citation.evidence_id not in evidence_ids:
                raise ValueError("Every citation must reference declared report evidence.")
            if not set(citation.claim_ids) <= claim_ids:
                raise ValueError("Every citation claim must resolve to a report claim.")

    def _validate_status_semantics(self) -> None:
        """Keep evidence sufficiency distinct from operational success."""
        if self.status == "complete":
            if not self.findings or not self.evidence_ids or not self.citations:
                raise ValueError("A complete report requires findings, evidence, and citations.")
            if any(topic.status != "completed" for topic in self.researched_topics):
                raise ValueError("Every researched topic must complete for a complete report.")
            return

        if self.status == "partial":
            supported_sections = (
                self.findings,
                self.responsibilities,
                self.operational_risks,
                self.contradictions,
            )
            asserted_signals = tuple(
                signal
                for signal in self.buy_in_signals
                if signal.category not in {"insufficient_evidence", "topic_not_discussed"}
            )
            if (
                (not any(supported_sections) and not asserted_signals)
                or not self.evidence_ids
                or not self.citations
            ):
                raise ValueError("A partial report requires at least one cited supported result.")
            if not any(topic.status == "completed" for topic in self.researched_topics):
                raise ValueError("A partial report requires at least one completed topic.")
            return

        if not self.evidence_gaps:
            raise ValueError("An insufficient_evidence report requires explicit evidence gaps.")
        if self.findings or self.responsibilities or self.operational_risks or self.contradictions:
            raise ValueError("An insufficient_evidence report cannot assert supported findings.")
        if any(
            signal.category not in {"insufficient_evidence", "topic_not_discussed"}
            for signal in self.buy_in_signals
        ):
            raise ValueError("An insufficient_evidence report cannot assert buy-in conclusions.")
        if any(topic.status == "completed" for topic in self.researched_topics):
            raise ValueError("Insufficient evidence cannot mark a research topic completed.")
