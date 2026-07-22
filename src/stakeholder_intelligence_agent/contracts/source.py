"""Canonical document, source-location, element, and search-chunk contracts."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import BeforeValidator, Field, StringConstraints, field_validator, model_validator

from stakeholder_intelligence_agent.contracts.common import (
    CanonicalModel,
    ContentHash,
    ExternalFilename,
    FailureCode,
    FiniteFloat,
    NonEmptyText,
    OpaqueId,
    OriginalText,
    ShortText,
    UtcDatetime,
)

DocumentType = Literal["pdf", "docx", "xlsx", "pptx", "png", "jpeg"]
DocumentSourceType = Literal["stakeholder_document", "engagement_document"]
RetrievalSourceType = Literal["stakeholder_document", "engagement_document", "interview"]
DocumentVersionState = Literal[
    "RECEIVED",
    "VALIDATING",
    "EXTRACTING",
    "ENRICHING",
    "INDEXING",
    "READY",
    "FAILED",
    "SUPERSEDED",
]
ElementType = Literal["text", "table", "image", "chart", "ocr_text", "vision_description"]


def _normalize_media_type(value: object) -> object:
    return value.strip().lower() if isinstance(value, str) else value


def _normalize_a1_range(value: object) -> object:
    return value.strip().upper() if isinstance(value, str) else value


MediaType = Annotated[
    str,
    BeforeValidator(_normalize_media_type),
    StringConstraints(pattern=r"^[a-z0-9][a-z0-9.+-]*/[a-z0-9][a-z0-9.+-]*$"),
]


class BoundingBox(CanonicalModel):
    """Optional source precision in an explicit coordinate space."""

    x0: FiniteFloat
    y0: FiniteFloat
    x1: FiniteFloat
    y1: FiniteFloat
    coordinate_space: Literal["points", "pixels", "normalized"]

    @model_validator(mode="after")
    def validate_geometry(self) -> Self:
        if min(self.x0, self.y0) < 0 or self.x1 <= self.x0 or self.y1 <= self.y0:
            raise ValueError("Bounding-box coordinates must define a positive region.")
        if self.coordinate_space == "normalized" and max(self.x1, self.y1) > 1:
            raise ValueError("Normalized bounding-box coordinates cannot exceed 1.")
        return self


class PdfPageLocation(CanonicalModel):
    kind: Literal["pdf_page"] = "pdf_page"
    filename: ExternalFilename
    page: int = Field(ge=1)
    bounding_box: BoundingBox | None = None


class DocxRenderedPageLocation(CanonicalModel):
    kind: Literal["docx_rendered_page"] = "docx_rendered_page"
    filename: ExternalFilename
    rendered_page: int = Field(ge=1)
    section: ShortText | None = None
    paragraph: int | None = Field(default=None, ge=1)
    bounding_box: BoundingBox | None = None


class PptxSlideLocation(CanonicalModel):
    kind: Literal["pptx_slide"] = "pptx_slide"
    filename: ExternalFilename
    slide: int = Field(ge=1)
    shape_identifier: ShortText | None = None
    bounding_box: BoundingBox | None = None


class XlsxRangeLocation(CanonicalModel):
    kind: Literal["xlsx_range"] = "xlsx_range"
    filename: ExternalFilename
    sheet: ShortText
    cell_range: Annotated[
        str,
        BeforeValidator(_normalize_a1_range),
        StringConstraints(
            pattern=r"^[A-Z]{1,3}[1-9][0-9]*(?::[A-Z]{1,3}[1-9][0-9]*)?$",
        ),
    ]
    chart_identifier: ShortText | None = None
    image_identifier: ShortText | None = None


class ImageRegionLocation(CanonicalModel):
    kind: Literal["image_region"] = "image_region"
    filename: ExternalFilename
    image_index: int | None = Field(default=None, ge=1)
    region: ShortText | None = None
    bounding_box: BoundingBox | None = None

    @model_validator(mode="after")
    def require_image_locator(self) -> Self:
        if self.image_index is None and self.region is None:
            raise ValueError("Image locations require image_index or region.")
        return self


class TranscriptTurnsLocation(CanonicalModel):
    kind: Literal["transcript_turns"] = "transcript_turns"
    stakeholder_id: OpaqueId
    transcript_id: OpaqueId
    turn_start: int = Field(ge=0)
    turn_end: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_turn_range(self) -> Self:
        if self.turn_end < self.turn_start:
            raise ValueError("turn_end must not precede turn_start.")
        return self


SourceLocation = Annotated[
    PdfPageLocation
    | DocxRenderedPageLocation
    | PptxSlideLocation
    | XlsxRangeLocation
    | ImageRegionLocation
    | TranscriptTurnsLocation,
    Field(discriminator="kind"),
]


class DocumentSource(CanonicalModel):
    """Stable logical document with all six metadata keys represented."""

    document_id: OpaqueId
    engagement_id: OpaqueId
    stakeholder_id: OpaqueId | None
    role: ShortText | None
    department: ShortText | None
    doc_type: DocumentType
    source_type: DocumentSourceType
    original_filename: ExternalFilename
    media_type: MediaType
    created_at: UtcDatetime

    @model_validator(mode="after")
    def validate_upload_context(self) -> Self:
        if self.source_type == "stakeholder_document" and self.stakeholder_id is None:
            raise ValueError("Stakeholder documents require stakeholder_id.")
        if self.source_type == "engagement_document" and any(
            value is not None for value in (self.stakeholder_id, self.role, self.department)
        ):
            raise ValueError("Engagement documents require null stakeholder metadata.")
        return self


class DocumentVersion(CanonicalModel):
    """Idempotent immutable-byte version with retrieval-atomic activation."""

    document_version_id: OpaqueId
    document_id: OpaqueId
    version_number: int = Field(ge=1)
    content_hash: ContentHash
    state: DocumentVersionState
    is_active: bool
    original_artifact_id: OpaqueId
    ingestion_key: OpaqueId
    created_at: UtcDatetime
    ready_at: UtcDatetime | None = None
    superseded_at: UtcDatetime | None = None
    failure_code: FailureCode | None = None
    failure_message: ShortText | None = None

    @model_validator(mode="after")
    def validate_version_state(self) -> Self:
        self._validate_version_timestamps()
        self._validate_ready_state()
        self._validate_superseded_state()
        self._validate_failure_state()
        return self

    def _validate_version_timestamps(self) -> None:
        if self.ready_at is not None and self.ready_at < self.created_at:
            raise ValueError("ready_at must not precede created_at.")
        if self.superseded_at is not None and self.superseded_at < self.created_at:
            raise ValueError("superseded_at must not precede created_at.")

    def _validate_ready_state(self) -> None:
        if self.state == "READY":
            if not self.is_active or self.ready_at is None or self.superseded_at is not None:
                raise ValueError("READY document versions must be active and not superseded.")
            if self.failure_code is not None or self.failure_message is not None:
                raise ValueError("READY document versions cannot carry failure detail.")
        elif self.is_active:
            raise ValueError("Only READY document versions may be active.")

    def _validate_superseded_state(self) -> None:
        if self.state == "SUPERSEDED":
            if self.ready_at is None or self.superseded_at is None:
                raise ValueError("SUPERSEDED versions require ready_at and superseded_at.")
        elif self.superseded_at is not None:
            raise ValueError("Only SUPERSEDED versions may carry superseded_at.")
        if self.state not in {"READY", "SUPERSEDED"} and self.ready_at is not None:
            raise ValueError("Pre-READY document versions cannot have ready_at.")

    def _validate_failure_state(self) -> None:
        has_failure = self.failure_code is not None or self.failure_message is not None
        if self.state == "FAILED":
            if self.failure_code is None or self.failure_message is None:
                raise ValueError("FAILED document versions require safe failure detail.")
        elif has_failure:
            raise ValueError("Only FAILED document versions may carry failure detail.")


class SourceElement(CanonicalModel):
    """Original or derived source element with explicit lineage."""

    element_id: OpaqueId
    document_version_id: OpaqueId
    element_type: ElementType
    original_content: OriginalText | None = None
    english_interpretation: NonEmptyText | None = None
    location: SourceLocation
    parent_element_id: OpaqueId | None = None
    artifact_id: OpaqueId | None = None
    content_hash: ContentHash
    extraction_method: ShortText

    @model_validator(mode="after")
    def validate_element_payload(self) -> Self:
        if self.element_type in {"text", "table", "ocr_text", "vision_description"}:
            if self.original_content is None:
                raise ValueError("Textual source elements require original_content.")
            if not self.original_content.strip():
                raise ValueError("original_content cannot be blank.")
        if self.element_type in {"image", "chart"} and self.artifact_id is None:
            raise ValueError("Visual source elements require a preserved artifact_id.")
        if (
            self.element_type in {"ocr_text", "vision_description"}
            and self.parent_element_id is None
        ):
            raise ValueError("Derived source elements require parent_element_id.")
        return self


class SparseVector(CanonicalModel):
    """Bounded Qdrant sparse vector with deterministic ordered indices."""

    indices: tuple[int, ...] = Field(min_length=1, max_length=100_000)
    values: tuple[FiniteFloat, ...] = Field(min_length=1, max_length=100_000)

    @field_validator("indices")
    @classmethod
    def validate_indices(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if any(index < 0 for index in value):
            raise ValueError("Sparse-vector indices must be non-negative.")
        if tuple(sorted(set(value))) != value:
            raise ValueError("Sparse-vector indices must be unique and increasing.")
        return value

    @model_validator(mode="after")
    def validate_shape(self) -> Self:
        if len(self.indices) != len(self.values):
            raise ValueError("Sparse-vector indices and values must have equal length.")
        if any(value == 0 for value in self.values):
            raise ValueError("Sparse-vector values must omit zero entries.")
        return self


class SearchChunk(CanonicalModel):
    """Complete retrieval unit with dense, sparse, scope, state, and lineage data."""

    chunk_id: OpaqueId
    engagement_id: OpaqueId
    source_id: OpaqueId
    source_version_id: OpaqueId
    element_ids: tuple[OpaqueId, ...] = Field(min_length=1)
    text_for_retrieval: NonEmptyText
    location: SourceLocation
    stakeholder_id: OpaqueId | None
    role: ShortText | None
    department: ShortText | None
    doc_type: DocumentType | Literal["transcript"]
    source_type: RetrievalSourceType
    dense_vector: tuple[FiniteFloat, ...] = Field(min_length=128, max_length=3_072)
    sparse_vector: SparseVector
    is_active_ready: bool

    @model_validator(mode="after")
    def validate_source_metadata(self) -> Self:
        if self.source_type == "engagement_document" and any(
            value is not None for value in (self.stakeholder_id, self.role, self.department)
        ):
            raise ValueError("Engagement chunks require null stakeholder metadata.")
        if self.source_type == "stakeholder_document" and self.stakeholder_id is None:
            raise ValueError("Stakeholder chunks require stakeholder_id.")
        if self.source_type == "interview":
            if self.stakeholder_id is None or self.doc_type != "transcript":
                raise ValueError("Interview chunks require stakeholder_id and transcript doc_type.")
        elif self.doc_type == "transcript":
            raise ValueError("Only interview chunks may use transcript doc_type.")
        return self
