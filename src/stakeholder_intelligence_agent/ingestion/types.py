"""Typed internal boundaries for the shared ingestion pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from stakeholder_intelligence_agent.contracts.source import (
        DocumentSource,
        DocumentType,
        DocumentVersion,
        ElementType,
        SearchChunk,
        SourceElement,
        SourceLocation,
        SparseVector,
    )

ArtifactKind = Literal[
    "original",
    "page_render",
    "embedded_image",
    "chart_render",
    "workbook_manifest",
    "extraction_manifest",
    "normalized_render",
]


@dataclass(frozen=True, slots=True)
class ValidatedUpload:
    """Envelope-validated immutable upload bytes and normalized identity fields."""

    filename: str
    normalized_filename: str
    document_type: DocumentType
    media_type: str
    content: bytes
    content_hash: str


@dataclass(frozen=True, slots=True)
class UploadScope:
    """Server-resolved source metadata; callers cannot supply these values."""

    engagement_id: str
    stakeholder_id: str | None
    role: str | None
    department: str | None
    source_type: Literal["stakeholder_document", "engagement_document"]


@dataclass(frozen=True, slots=True)
class ArtifactDraft:
    """One deterministic derived artifact awaiting scoped persistence."""

    key: str
    artifact_kind: ArtifactKind
    media_type: str
    suffix: str
    content: bytes


@dataclass(frozen=True, slots=True)
class ElementDraft:
    """One extracted element before stable version-specific IDs are assigned."""

    key: str
    element_type: ElementType
    original_content: str | None
    location: SourceLocation
    extraction_method: str
    parent_key: str | None = None
    artifact_key: str | None = None
    english_interpretation: str | None = None


@dataclass(frozen=True, slots=True)
class ExtractionBundle:
    """Complete primary-converter output plus narrow fixture-proven supplements."""

    elements: tuple[ElementDraft, ...]
    artifacts: tuple[ArtifactDraft, ...]
    capability_facts: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class StoredArtifact:
    """Persisted artifact metadata suitable for SQLite lineage records."""

    artifact_id: str
    engagement_id: str
    document_version_id: str
    artifact_kind: ArtifactKind
    virtual_path: str
    media_type: str
    content_hash: str


@dataclass(frozen=True, slots=True)
class VectorPair:
    """Dense Gemini and sparse BM25 vectors for one retrieval text."""

    dense: tuple[float, ...]
    sparse: SparseVector


@dataclass(frozen=True, slots=True)
class IngestionStart:
    """Repository claim result for a new, retrying, or already-ready version."""

    source: DocumentSource
    version: DocumentVersion
    attempt_id: str | None
    lease_token: str | None
    idempotent_ready: bool


@dataclass(frozen=True, slots=True)
class ActivationResult:
    """Atomic SQLite activation and the versions that must be retired in Qdrant."""

    version: DocumentVersion
    chunks: tuple[SearchChunk, ...]
    superseded_version_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class IngestionResult:
    """Complete result returned by both PM and stakeholder upload paths."""

    source: DocumentSource
    version: DocumentVersion
    elements: tuple[SourceElement, ...]
    chunks: tuple[SearchChunk, ...]
    attempt_id: str | None
    idempotent: bool


class DocumentExtractor(Protocol):
    """Primary conversion boundary."""

    def extract(self, source_path: Path, upload: ValidatedUpload) -> ExtractionBundle:
        """Extract source elements and preserved derivatives from one original."""
        ...


class VisionEnricher(Protocol):
    """Gemini-only visual-description boundary with deterministic test doubles."""

    async def describe(
        self,
        *,
        content: bytes,
        media_type: str,
        filename: str,
        location: SourceLocation,
    ) -> str:
        """Return a concise English evidence description without following image text."""
        ...


class Vectorizer(Protocol):
    """Dense Gemini and sparse BM25 vector construction boundary."""

    async def vectorize(self, texts: Sequence[str]) -> tuple[VectorPair, ...]:
        """Return one complete vector pair for each supplied text in order."""
        ...


class VectorStager(Protocol):
    """Qdrant staging boundary used before SQLite activation."""

    async def initialize(self) -> None:
        """Ensure the one approved collection and named vector schema exist."""
        ...

    async def stage(self, chunks: Sequence[SearchChunk]) -> None:
        """Upsert complete chunks as inactive staged points."""
        ...

    async def verify(self, version_id: str, expected_chunk_ids: Sequence[str]) -> None:
        """Fail unless every expected dense/sparse point and payload is present."""
        ...

    async def prepare_activation(self, version_id: str) -> None:
        """Set staged points eligible before the authoritative SQLite switch."""
        ...

    async def deactivate(self, version_id: str) -> None:
        """Make every point for one version ineligible."""
        ...
