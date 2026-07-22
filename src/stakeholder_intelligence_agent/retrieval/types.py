"""Typed internal boundaries for scoped hybrid retrieval and evidence drill-down."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    from stakeholder_intelligence_agent.contracts.access import AccessContext
    from stakeholder_intelligence_agent.contracts.evidence import EvidenceRecord
    from stakeholder_intelligence_agent.contracts.retrieval import (
        RetrievalCandidate,
        RetrievalFilter,
        RetrievalFilterInput,
        RetrievalMetadata,
    )
    from stakeholder_intelligence_agent.contracts.source import SourceLocation
    from stakeholder_intelligence_agent.ingestion.types import VectorPair


@dataclass(frozen=True, slots=True)
class ChannelHit:
    """One fully validated Qdrant result."""

    chunk_id: str
    score: float
    source_id: str
    source_version_id: str
    element_ids: tuple[str, ...]
    text: str
    location: SourceLocation
    metadata: RetrievalMetadata


@dataclass(frozen=True, slots=True)
class RerankResult:
    """Required cross-encoder output plus reproducibility metadata."""

    scores: tuple[float, ...]
    model_id: str
    device: str
    duration_ms: float


@dataclass(frozen=True, slots=True)
class RetrievedItem:
    """Canonical candidate plus the source lineage needed for evidence registration."""

    candidate: RetrievalCandidate
    source_id: str
    source_version_id: str
    element_ids: tuple[str, ...]
    original_excerpt: str


@dataclass(frozen=True, slots=True)
class RetrievalTrace:
    """Safe deterministic stage trace without prompts or private reasoning."""

    rrf_chunk_ids: tuple[str, ...]
    reranked_chunk_ids: tuple[str, ...]
    fusion_method: Literal["qdrant_native_rrf"]
    filter_extraction_degraded: bool
    optional_filters_relaxed: bool
    reranker_model: str | None
    reranker_device: str | None
    hybrid_latency_ms: float
    reranker_latency_ms: float
    total_latency_ms: float


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    """Scoped result returned to interview and researcher tool adapters."""

    query: str
    retrieval_filter: RetrievalFilter
    items: tuple[RetrievedItem, ...]
    trace: RetrievalTrace


@dataclass(frozen=True, slots=True)
class SourceArtifactReference:
    """One engagement-scoped virtual source artifact reference."""

    artifact_id: str
    artifact_kind: str
    virtual_path: str
    media_type: str
    content_hash: str


@dataclass(frozen=True, slots=True)
class SourceDrillDown:
    """Authorized evidence source view without exposing a host filesystem path."""

    evidence: EvidenceRecord
    original: SourceArtifactReference
    related_artifacts: tuple[SourceArtifactReference, ...]


@dataclass(frozen=True, slots=True)
class StakeholderFilterCandidate:
    """One server-authorized name-to-ID option exposed to filter extraction."""

    stakeholder_id: str
    display_name: str
    role: str | None
    department: str | None


class FilterExtractor(Protocol):
    """Optional Gemini structured-filter extraction boundary."""

    async def extract(
        self,
        query: str,
        stakeholder_candidates: Sequence[StakeholderFilterCandidate] = (),
    ) -> RetrievalFilterInput:
        """Return only optional validated narrowing fields."""
        ...


class ActiveVersionRepository(Protocol):
    """SQLite authority boundary used by the retrieval pipeline."""

    async def initialize(self) -> None:
        """Apply persistence migrations."""
        ...

    async def stakeholder_filter_candidates(
        self,
        access: AccessContext,
        *,
        now: datetime,
    ) -> tuple[StakeholderFilterCandidate, ...]:
        """Return only stakeholder identities visible to the current principal."""
        ...

    async def active_ready_version_ids(
        self,
        access: AccessContext,
        filters: RetrievalFilterInput,
        *,
        now: datetime,
    ) -> tuple[str, ...]:
        """Return only same-engagement active READY source versions."""
        ...


class QueryVectorizer(Protocol):
    """Dedicated dense and sparse query-vector boundary."""

    async def vectorize_query(self, text: str) -> VectorPair:
        """Return one complete Gemini-query/BM25-query vector pair."""
        ...


class HybridSearchBackend(Protocol):
    """Qdrant-native dense/BM25 hybrid search boundary."""

    async def search_hybrid(
        self,
        vectors: VectorPair,
        retrieval_filter: RetrievalFilter,
        active_version_ids: Sequence[str],
        *,
        prefetch_limit: int,
        limit: int,
    ) -> tuple[ChannelHit, ...]:
        """Fuse dense and sparse prefetches through Qdrant's built-in RRF."""
        ...


class Reranker(Protocol):
    """Required BGE cross-encoder boundary."""

    async def rerank(self, query: str, texts: Sequence[str]) -> RerankResult:
        """Score every fused candidate in its supplied order."""
        ...
