"""Scoped Qdrant-native hybrid retrieval and mandatory BGE reranking."""

from __future__ import annotations

from datetime import UTC, datetime
from time import perf_counter
from typing import TYPE_CHECKING

from stakeholder_intelligence_agent.contracts.retrieval import (
    RetrievalCandidate,
    RetrievalFilter,
    RetrievalFilterInput,
)
from stakeholder_intelligence_agent.errors import (
    RerankingError,
    RetrievalExecutionError,
    RetrievalFilterError,
)
from stakeholder_intelligence_agent.retrieval.types import (
    RetrievalResult,
    RetrievalTrace,
    RetrievedItem,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from stakeholder_intelligence_agent.config import Settings
    from stakeholder_intelligence_agent.contracts.access import AccessContext
    from stakeholder_intelligence_agent.ingestion.types import VectorPair
    from stakeholder_intelligence_agent.retrieval.types import (
        ActiveVersionRepository,
        ChannelHit,
        FilterExtractor,
        HybridSearchBackend,
        QueryVectorizer,
        Reranker,
    )

_MAX_QUERY_CHARACTERS = 20_000
_MAX_PREVIEW_CHARACTERS = 2_000


class HybridRetrievalService:
    """Run the approved retrieval stages while SQLite retains scope authority."""

    def __init__(
        self,
        *,
        settings: Settings,
        repository: ActiveVersionRepository,
        filter_extractor: FilterExtractor,
        vectorizer: QueryVectorizer,
        search_backend: HybridSearchBackend,
        reranker: Reranker,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._repository = repository
        self._filter_extractor = filter_extractor
        self._vectorizer = vectorizer
        self._search = search_backend
        self._reranker = reranker
        self._per_channel = settings.max_retrieval_candidates_per_channel
        self._rerank_limit = settings.max_rerank_candidates
        self._result_limit = settings.max_retrieval_results
        self._clock = clock or (lambda: datetime.now(UTC))

    async def initialize(self) -> None:
        """Apply retrieval-domain persistence migrations."""
        await self._repository.initialize()

    async def retrieve(self, access: AccessContext, query: str) -> RetrievalResult:
        """Execute scoped optional filters, Qdrant-native RRF, and BGE in order."""
        query = query.strip()
        if not query or len(query) > _MAX_QUERY_CHARACTERS:
            raise RetrievalExecutionError
        started = perf_counter()
        now = self._clock()
        stakeholder_candidates = await self._repository.stakeholder_filter_candidates(
            access,
            now=now,
        )
        proposed = await self._filter_extractor.extract(query, stakeholder_candidates)
        if proposed.stakeholder_id is not None and proposed.stakeholder_id not in {
            candidate.stakeholder_id for candidate in stakeholder_candidates
        }:
            raise RetrievalFilterError
        active = await self._repository.active_ready_version_ids(
            access,
            proposed,
            now=now,
        )
        retrieval_filter = self._server_filter(access, proposed)
        if not active:
            return self._empty_result(
                query=query,
                retrieval_filter=retrieval_filter,
                started=started,
            )

        vectors = await self._vectorizer.vectorize_query(query)
        fused, hybrid_ms = await self._timed_hybrid(vectors, retrieval_filter, active)
        if not fused:
            return self._empty_result(
                query=query,
                retrieval_filter=retrieval_filter,
                started=started,
                hybrid_ms=hybrid_ms,
            )
        reranked = await self._reranker.rerank(
            query,
            tuple(item.text for item in fused),
        )
        if len(reranked.scores) != len(fused):
            raise RerankingError
        scored = sorted(
            zip(fused, reranked.scores, strict=True),
            key=lambda pair: (-pair[1], -pair[0].score, pair[0].chunk_id),
        )[: self._result_limit]
        hybrid_ranks = {item.chunk_id: rank for rank, item in enumerate(fused, start=1)}
        items = tuple(
            RetrievedItem(
                candidate=RetrievalCandidate(
                    chunk_id=fused_hit.chunk_id,
                    hybrid_rank=hybrid_ranks[fused_hit.chunk_id],
                    rrf_score=fused_hit.score,
                    reranker_score=reranker_score,
                    final_rank=rank,
                    source_preview=fused_hit.text[:_MAX_PREVIEW_CHARACTERS],
                    location=fused_hit.location,
                    metadata=fused_hit.metadata,
                ),
                source_id=fused_hit.source_id,
                source_version_id=fused_hit.source_version_id,
                element_ids=fused_hit.element_ids,
                original_excerpt=fused_hit.text,
            )
            for rank, (fused_hit, reranker_score) in enumerate(scored, start=1)
        )
        return RetrievalResult(
            query=query,
            retrieval_filter=retrieval_filter,
            items=items,
            trace=RetrievalTrace(
                rrf_chunk_ids=tuple(item.chunk_id for item in fused),
                reranked_chunk_ids=tuple(item.candidate.chunk_id for item in items),
                fusion_method="qdrant_native_rrf",
                filter_extraction_degraded=False,
                optional_filters_relaxed=False,
                reranker_model=reranked.model_id,
                reranker_device=reranked.device,
                hybrid_latency_ms=hybrid_ms,
                reranker_latency_ms=reranked.duration_ms,
                total_latency_ms=(perf_counter() - started) * 1_000,
            ),
        )

    async def _timed_hybrid(
        self,
        vectors: VectorPair,
        retrieval_filter: RetrievalFilter,
        active: tuple[str, ...],
    ) -> tuple[tuple[ChannelHit, ...], float]:
        started = perf_counter()
        result = await self._search.search_hybrid(
            vectors,
            retrieval_filter,
            active,
            prefetch_limit=self._per_channel,
            limit=self._rerank_limit,
        )
        return result, (perf_counter() - started) * 1_000

    @staticmethod
    def _server_filter(
        access: AccessContext,
        optional: RetrievalFilterInput,
    ) -> RetrievalFilter:
        return RetrievalFilter(
            engagement_id=access.engagement_id,
            active_ready_only=True,
            **optional.model_dump(),
        )

    @staticmethod
    def _empty_result(
        *,
        query: str,
        retrieval_filter: RetrievalFilter,
        started: float,
        hybrid_ms: float = 0.0,
    ) -> RetrievalResult:
        return RetrievalResult(
            query=query,
            retrieval_filter=retrieval_filter,
            items=(),
            trace=RetrievalTrace(
                rrf_chunk_ids=(),
                reranked_chunk_ids=(),
                fusion_method="qdrant_native_rrf",
                filter_extraction_degraded=False,
                optional_filters_relaxed=False,
                reranker_model=None,
                reranker_device=None,
                hybrid_latency_ms=hybrid_ms,
                reranker_latency_ms=0.0,
                total_latency_ms=(perf_counter() - started) * 1_000,
            ),
        )
