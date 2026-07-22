"""Retrieval authority, evidence registry, citation, and report contract tests."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from stakeholder_intelligence_agent.contracts import (
    Citation,
    EvidenceRecord,
    InsightReport,
    RetrievalCandidate,
    RetrievalFilter,
    RetrievalFilterInput,
    RetrievalMetadata,
)
from stakeholder_intelligence_agent.contracts.insight import (
    BuyInSignal,
    Contradiction,
    ReportClaim,
    ResearchedTopicOutcome,
    RunMetadata,
)
from stakeholder_intelligence_agent.contracts.source import BoundingBox, PdfPageLocation
from stakeholder_intelligence_agent.retrieval.repository import RetrievalRepository

NOW = datetime(2026, 7, 15, 1, 0, tzinfo=UTC)
DIGEST = "c" * 64
LOCATION = PdfPageLocation(filename="brief.pdf", page=1)


def test_evidence_location_allows_only_serialization_scale_coordinate_drift() -> None:
    authoritative = PdfPageLocation(
        filename="brief.pdf",
        page=1,
        bounding_box=BoundingBox(
            x0=83.70074462890625,
            y0=127.33367919921875,
            x1=527.9000244140625,
            y1=405.3650207519531,
            coordinate_space="points",
        ),
    )
    bounding_box = authoritative.bounding_box
    assert bounding_box is not None
    json_round_trip = authoritative.model_copy(
        update={"bounding_box": bounding_box.model_copy(update={"y0": 127.33367919921876})}
    )
    material_change = authoritative.model_copy(
        update={"bounding_box": bounding_box.model_copy(update={"y0": 127.33368})}
    )

    assert RetrievalRepository._locations_match(  # noqa: SLF001
        authoritative,
        json_round_trip,
    )
    assert not RetrievalRepository._locations_match(  # noqa: SLF001
        authoritative,
        material_change,
    )
    assert not RetrievalRepository._locations_match(  # noqa: SLF001
        authoritative,
        authoritative.model_copy(update={"page": 2}),
    )


def test_model_filter_cannot_supply_server_scope() -> None:
    with pytest.raises(ValidationError):
        RetrievalFilterInput.model_validate(
            {"engagement_id": "forged-engagement", "department": "Operations"}
        )
    resolved = RetrievalFilter(
        engagement_id="engagement-a",
        department="Operations",
    )
    assert resolved.active_ready_only is True
    with pytest.raises(ValidationError):
        RetrievalFilter.model_validate(
            {"engagement_id": "engagement-a", "active_ready_only": False}
        )


def test_retrieval_candidate_requires_native_hybrid_rank_and_ready_metadata() -> None:
    metadata = RetrievalMetadata(
        engagement_id="engagement-a",
        stakeholder_id=None,
        role=None,
        department=None,
        doc_type="pdf",
        source_type="engagement_document",
        source_version_state="READY",
        is_active_ready=True,
    )
    candidate = RetrievalCandidate(
        chunk_id="chunk-a",
        hybrid_rank=1,
        rrf_score=0.03,
        reranker_score=0.81,
        final_rank=1,
        source_preview="Supported source preview.",
        location=LOCATION,
        metadata=metadata,
    )
    assert candidate.final_rank == 1
    with pytest.raises(ValidationError):
        RetrievalCandidate.model_validate(candidate.model_dump() | {"hybrid_rank": 0})


def test_evidence_preserves_original_excerpt_and_enforces_attribution() -> None:
    excerpt = "  Original source excerpt is not normalized.  "
    record = EvidenceRecord(
        evidence_id="evidence-a",
        run_id="run-a",
        engagement_id="engagement-a",
        topic_id="topic-a",
        source_id="document-a",
        source_version_id="document-version-a",
        source_type="engagement_document",
        stakeholder_id=None,
        location=LOCATION,
        original_excerpt=excerpt,
        content_hash=DIGEST,
        researcher_id="topic-researcher",
        created_at=NOW,
    )
    assert record.original_excerpt == excerpt
    with pytest.raises(ValidationError):
        EvidenceRecord.model_validate(record.model_dump() | {"stakeholder_id": "stakeholder-a"})


def test_citation_requires_a_tagged_source_location() -> None:
    with pytest.raises(ValidationError):
        Citation.model_validate(
            {
                "citation_id": "citation-a",
                "evidence_id": "evidence-a",
                "display_label": "Brief, page 1",
                "source_location": "page 1",
                "claim_ids": ["claim-a"],
            }
        )


def _complete_report() -> InsightReport:
    return InsightReport(
        report_id="report-a",
        engagement_id="engagement-a",
        question="Where is the operating risk?",
        status="complete",
        executive_summary="The evidence supports one bounded operating risk.",
        researched_topics=(
            ResearchedTopicOutcome(
                topic_id="topic-a",
                title="Operating risk",
                status="completed",
                summary="The topic was researched with current evidence.",
                evidence_ids=("evidence-a",),
            ),
        ),
        findings=(
            ReportClaim(
                claim_id="claim-a",
                statement="One handoff is not assigned.",
                evidence_ids=("evidence-a",),
            ),
        ),
        responsibilities=(),
        operational_risks=(),
        buy_in_signals=(),
        contradictions=(),
        evidence_gaps=(),
        open_questions=(),
        follow_up_recommendations=(),
        evidence_ids=("evidence-a",),
        citations=(
            Citation(
                citation_id="citation-a",
                evidence_id="evidence-a",
                display_label="Brief, page 1",
                source_location=LOCATION,
                claim_ids=("claim-a",),
            ),
        ),
        run_metadata=RunMetadata(
            run_id="run-a",
            started_at=NOW,
            completed_at=NOW,
            primary_model_id="gemini-test-primary",
            fallback_model_id="gemini-test-fallback",
            topic_count=1,
            status_detail="The bounded report completed.",
        ),
    )


def test_complete_report_resolves_typed_citation_and_claim() -> None:
    report = _complete_report()
    assert report.citations[0].source_location.kind == "pdf_page"


def test_report_requires_unique_citations_covering_every_declared_evidence() -> None:
    report = _complete_report()
    duplicate = report.citations[0].model_copy(update={"evidence_id": "evidence-b"})
    payload = report.model_dump()
    payload["evidence_ids"] = ("evidence-a", "evidence-b")
    payload["findings"][0]["evidence_ids"] = ("evidence-a", "evidence-b")
    with pytest.raises(ValidationError, match="requires a citation"):
        InsightReport.model_validate(payload)

    payload["citations"] = (
        report.citations[0].model_dump(),
        duplicate.model_dump(),
    )
    with pytest.raises(ValidationError, match="citation IDs must be unique"):
        InsightReport.model_validate(payload)


def test_partial_report_requires_a_cited_supported_result_and_explicit_gap() -> None:
    report = _complete_report()
    payload = report.model_dump()
    payload.update(
        {
            "status": "partial",
            "researched_topics": (
                report.researched_topics[0].model_dump(),
                {
                    "topic_id": "topic-b",
                    "title": "Ownership gap",
                    "status": "failed",
                    "summary": "The bounded researcher did not complete.",
                    "evidence_ids": (),
                },
            ),
            "evidence_gaps": (
                {
                    "topic": "Ownership gap",
                    "description": "The second topic did not produce validated evidence.",
                    "impact": "The report cannot make an ownership conclusion for that topic.",
                },
            ),
            "run_metadata": report.run_metadata.model_copy(update={"topic_count": 2}).model_dump(),
        }
    )
    partial = InsightReport.model_validate(payload)
    assert partial.status == "partial"
    assert partial.researched_topics[1].status == "failed"


def test_buy_in_is_qualitative_and_assertions_require_evidence() -> None:
    payload = {
        "topic": "Pilot",
        "stakeholder_id": "stakeholder-a",
        "role": "Operations Lead",
        "department": "Operations",
        "category": "confirmed_support",
        "explanation": "The stakeholder explicitly supports the pilot.",
        "evidence_ids": ("evidence-a",),
    }
    assert BuyInSignal.model_validate(payload).category == "confirmed_support"
    with pytest.raises(ValidationError):
        BuyInSignal.model_validate(payload | {"score": 95})
    with pytest.raises(ValidationError, match="requires evidence"):
        BuyInSignal.model_validate(payload | {"evidence_ids": ()})


def test_contradiction_requires_distinguishable_evidence_for_both_sides() -> None:
    payload = {
        "topic": "Cycle time",
        "side_a": {
            "statement": "The cycle takes two days.",
            "department": "Operations",
            "evidence_ids": ("evidence-a",),
        },
        "side_b": {
            "statement": "The cycle takes seven days.",
            "department": "Finance",
            "evidence_ids": ("evidence-b",),
        },
        "interpretation": "The current evidence does not resolve the differing durations.",
        "evidence_ids": ("evidence-a", "evidence-b"),
    }
    assert Contradiction.model_validate(payload).side_b.department == "Finance"
    payload["side_b"] = payload["side_a"]
    with pytest.raises(ValidationError, match="distinguishable evidence"):
        Contradiction.model_validate(payload)
