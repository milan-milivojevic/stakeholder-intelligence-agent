"""Ordered hybrid retrieval service and safe optional-filter behavior."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from stakeholder_intelligence_agent.contracts.retrieval import (
    RetrievalFilterInput,
    RetrievalMetadata,
)
from stakeholder_intelligence_agent.contracts.source import PdfPageLocation
from stakeholder_intelligence_agent.errors import RetrievalFilterError
from stakeholder_intelligence_agent.retrieval import (
    ChannelHit,
    HybridRetrievalService,
    StakeholderFilterCandidate,
)
from tests.fakes import (
    DeterministicReranker,
    DeterministicVectorizer,
    InMemoryHybridSearchBackend,
    StaticFilterExtractor,
)
from tests.helpers import pm_access

if TYPE_CHECKING:
    from stakeholder_intelligence_agent.config import Settings
    from stakeholder_intelligence_agent.contracts.access import AccessContext


@dataclass(slots=True)
class StaticRetrievalRepository:
    """Return an active version unless an intentionally over-narrow role is used."""

    calls: list[RetrievalFilterInput] = field(default_factory=list)

    async def initialize(self) -> None:
        return None

    async def stakeholder_filter_candidates(
        self,
        access: AccessContext,
        *,
        now: datetime,
    ) -> tuple[StakeholderFilterCandidate, ...]:
        del access, now
        return (
            StakeholderFilterCandidate(
                stakeholder_id="stakeholder-alex",
                display_name="Alex Morgan",
                role="Operations manager",
                department="Operations",
            ),
        )

    async def active_ready_version_ids(
        self,
        access: AccessContext,
        filters: RetrievalFilterInput,
        *,
        now: datetime,
    ) -> tuple[str, ...]:
        del access, now
        self.calls.append(filters)
        return () if filters.role == "Unknown role" else ("version-a",)


def _hit(chunk_id: str, text: str, score: float) -> ChannelHit:
    return ChannelHit(
        chunk_id=chunk_id,
        score=score,
        source_id=f"source-{chunk_id}",
        source_version_id="version-a",
        element_ids=(f"element-{chunk_id}",),
        text=text,
        location=PdfPageLocation(filename="evidence.pdf", page=1),
        metadata=RetrievalMetadata(
            engagement_id="engagement-a",
            stakeholder_id=None,
            role=None,
            department=None,
            doc_type="pdf",
            source_type="engagement_document",
            source_version_state="READY",
            is_active_ready=True,
        ),
    )


async def test_service_preserves_optional_filters_and_returns_no_cross_filter_fallback(
    settings: Settings,
) -> None:
    access = pm_access().model_copy(
        update={"permissions": frozenset({"insight:run", "source:read"})}
    )
    repository = StaticRetrievalRepository()
    backend = InMemoryHybridSearchBackend()
    reranker = DeterministicReranker(
        scores_by_text={
            "Operational ownership evidence.": 0.2,
            "Conditional support evidence.": 0.9,
        }
    )
    service = HybridRetrievalService(
        settings=settings,
        repository=repository,
        filter_extractor=StaticFilterExtractor(RetrievalFilterInput(role="Unknown role")),
        vectorizer=DeterministicVectorizer(),
        search_backend=backend,
        reranker=reranker,
        clock=lambda: datetime(2026, 7, 15, 13, 0, tzinfo=UTC),
    )

    result = await service.retrieve(access, "What is the operational risk?")

    assert result.retrieval_filter.engagement_id == "engagement-a"
    assert result.retrieval_filter.role == "Unknown role"
    assert not result.trace.optional_filters_relaxed
    assert [call.role for call in repository.calls] == ["Unknown role"]
    assert backend.calls == []
    assert result.items == ()
    assert result.trace.rrf_chunk_ids == ()
    assert reranker.calls == []
    assert result.trace.reranker_model is None


async def test_filter_extraction_failure_fails_closed_before_search(
    settings: Settings,
) -> None:
    repository = StaticRetrievalRepository()
    hit = _hit("a", "Same-engagement evidence.", 1.0)
    service = HybridRetrievalService(
        settings=settings,
        repository=repository,
        filter_extractor=StaticFilterExtractor(fail=True),
        vectorizer=DeterministicVectorizer(),
        search_backend=InMemoryHybridSearchBackend(hybrid_hits=(hit,)),
        reranker=DeterministicReranker(scores_by_text={hit.text: 1.0}),
    )

    with pytest.raises(RetrievalFilterError):
        await service.retrieve(
            pm_access().model_copy(
                update={"permissions": frozenset({"insight:run", "source:read"})}
            ),
            "Find evidence.",
        )

    assert repository.calls == []


async def test_unknown_model_selected_stakeholder_id_fails_closed(
    settings: Settings,
) -> None:
    repository = StaticRetrievalRepository()
    service = HybridRetrievalService(
        settings=settings,
        repository=repository,
        filter_extractor=StaticFilterExtractor(
            RetrievalFilterInput(stakeholder_id="stakeholder-invented")
        ),
        vectorizer=DeterministicVectorizer(),
        search_backend=InMemoryHybridSearchBackend(),
        reranker=DeterministicReranker(),
    )

    with pytest.raises(RetrievalFilterError):
        await service.retrieve(
            pm_access().model_copy(
                update={"permissions": frozenset({"insight:run", "source:read"})}
            ),
            "What did Alex Morgan report?",
        )

    assert repository.calls == []


async def test_authorized_name_to_id_directory_reaches_structured_extraction(
    settings: Settings,
) -> None:
    repository = StaticRetrievalRepository()
    extractor = StaticFilterExtractor(RetrievalFilterInput(stakeholder_id="stakeholder-alex"))
    service = HybridRetrievalService(
        settings=settings,
        repository=repository,
        filter_extractor=extractor,
        vectorizer=DeterministicVectorizer(),
        search_backend=InMemoryHybridSearchBackend(),
        reranker=DeterministicReranker(),
    )

    result = await service.retrieve(
        pm_access().model_copy(update={"permissions": frozenset({"insight:run", "source:read"})}),
        "What did Alex Morgan report?",
    )

    assert extractor.calls[0][1][0].display_name == "Alex Morgan"
    assert extractor.calls[0][1][0].stakeholder_id == "stakeholder-alex"
    assert result.retrieval_filter.stakeholder_id == "stakeholder-alex"


async def test_service_uses_native_hybrid_rrf_output_before_bge(settings: Settings) -> None:
    repository = StaticRetrievalRepository()
    hybrid = (
        _hit("operational", "Operational ownership evidence.", 0.032),
        _hit("buy-in", "Conditional support evidence.", 0.031),
    )
    backend = InMemoryHybridSearchBackend(hybrid_hits=hybrid)
    reranker = DeterministicReranker(
        scores_by_text={
            "Operational ownership evidence.": 0.2,
            "Conditional support evidence.": 0.9,
        }
    )
    service = HybridRetrievalService(
        settings=settings,
        repository=repository,
        filter_extractor=StaticFilterExtractor(),
        vectorizer=DeterministicVectorizer(),
        search_backend=backend,
        reranker=reranker,
    )

    result = await service.retrieve(
        pm_access().model_copy(update={"permissions": frozenset({"insight:run", "source:read"})}),
        "What is the operational risk?",
    )

    assert len(backend.calls) == 1
    assert result.trace.fusion_method == "qdrant_native_rrf"
    assert result.trace.rrf_chunk_ids == ("operational", "buy-in")
    assert [item.candidate.chunk_id for item in result.items] == ["buy-in", "operational"]
    assert [item.candidate.hybrid_rank for item in result.items] == [2, 1]
    assert [item.candidate.final_rank for item in result.items] == [1, 2]
    assert reranker.calls == [
        (
            "What is the operational risk?",
            ("Operational ownership evidence.", "Conditional support evidence."),
        )
    ]
