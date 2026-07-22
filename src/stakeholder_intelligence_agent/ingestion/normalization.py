"""Stable source-element and retrieval-chunk normalization."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import TYPE_CHECKING

from stakeholder_intelligence_agent.contracts.source import (
    DocumentSource,
    DocumentVersion,
    DocxRenderedPageLocation,
    ImageRegionLocation,
    PdfPageLocation,
    PptxSlideLocation,
    SearchChunk,
    SourceElement,
    SourceLocation,
    XlsxRangeLocation,
)
from stakeholder_intelligence_agent.errors import MandatoryContentMissingError
from stakeholder_intelligence_agent.ingestion.identity import stable_id

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from stakeholder_intelligence_agent.ingestion.types import (
        ElementDraft,
        StoredArtifact,
        VectorPair,
    )


@dataclass(frozen=True, slots=True)
class ChunkSeed:
    """One stable vectorization input before its dense and sparse vectors exist."""

    chunk_id: str
    element_ids: tuple[str, ...]
    text: str
    location: SourceLocation


def materialize_elements(
    version: DocumentVersion,
    drafts: Sequence[ElementDraft],
    *,
    original_artifact: StoredArtifact,
    derived_artifacts: Mapping[str, StoredArtifact],
) -> tuple[SourceElement, ...]:
    """Assign stable IDs, content hashes, artifact lineage, and derived parents."""
    if len({draft.key for draft in drafts}) != len(drafts):
        raise MandatoryContentMissingError
    content_hashes: dict[str, str] = {}
    artifact_ids: dict[str, str | None] = {}
    element_ids: dict[str, str] = {}
    for draft in drafts:
        artifact = (
            original_artifact
            if draft.artifact_key == "$original"
            else derived_artifacts.get(draft.artifact_key or "")
        )
        if draft.element_type in {"image", "chart"} and artifact is None:
            raise MandatoryContentMissingError
        if draft.original_content is not None:
            digest = sha256(draft.original_content.encode()).hexdigest()
        elif artifact is not None:
            digest = artifact.content_hash
        else:
            raise MandatoryContentMissingError
        content_hashes[draft.key] = digest
        artifact_ids[draft.key] = None if artifact is None else artifact.artifact_id
        element_ids[draft.key] = stable_id(
            "element",
            version.document_version_id,
            draft.key,
            digest,
        )

    elements: list[SourceElement] = []
    for draft in drafts:
        parent_id = None
        if draft.parent_key is not None:
            parent_id = element_ids.get(draft.parent_key)
            if parent_id is None:
                raise MandatoryContentMissingError
        elements.append(
            SourceElement(
                element_id=element_ids[draft.key],
                document_version_id=version.document_version_id,
                element_type=draft.element_type,
                original_content=draft.original_content,
                english_interpretation=draft.english_interpretation,
                location=draft.location,
                parent_element_id=parent_id,
                artifact_id=artifact_ids[draft.key],
                content_hash=content_hashes[draft.key],
                extraction_method=draft.extraction_method,
            )
        )
    return tuple(elements)


def build_chunk_seeds(
    version: DocumentVersion,
    elements: Sequence[SourceElement],
    *,
    chunk_characters: int,
    overlap: int,
) -> tuple[ChunkSeed, ...]:
    """Build course-faithful page chunks plus contextual table and visual chunks."""
    seeds: list[ChunkSeed] = []
    units: dict[str, list[SourceElement]] = {}
    unit_locations: dict[str, SourceLocation] = {}
    for element in elements:
        if element.original_content is None:
            continue
        key, location = _context_unit(element.location)
        units.setdefault(key, []).append(element)
        unit_locations.setdefault(key, location)

    for key, unit_elements in units.items():
        location = unit_locations[key]
        original_elements = [
            element
            for element in unit_elements
            if element.element_type in {"text", "table", "ocr_text"}
        ]
        if original_elements:
            seeds.extend(
                _seed_contextual_text(
                    version,
                    kind="page",
                    elements=original_elements,
                    text=(
                        f"{_location_header(location)}\n\n"
                        + "\n\n".join(_render_element(element) for element in original_elements)
                    ),
                    location=location,
                    chunk_characters=chunk_characters,
                    overlap=overlap,
                )
            )

        prior_context: list[SourceElement] = []
        for element in unit_elements:
            if element.element_type == "table":
                context = tuple(prior_context[-2:])
                seeds.extend(
                    _seed_contextual_text(
                        version,
                        kind="table",
                        elements=(*context, element),
                        text=_supplemental_text(location, context, element),
                        location=element.location,
                        chunk_characters=chunk_characters,
                        overlap=overlap,
                    )
                )
            elif element.element_type == "vision_description":
                context = tuple(prior_context[-2:])
                seeds.extend(
                    _seed_contextual_text(
                        version,
                        kind="visual",
                        elements=(*context, element),
                        text=_supplemental_text(location, context, element),
                        location=element.location,
                        chunk_characters=chunk_characters,
                        overlap=overlap,
                    )
                )
            if element.element_type in {"text", "table", "ocr_text"}:
                prior_context.append(element)
    if not seeds:
        raise MandatoryContentMissingError
    return tuple(seeds)


def materialize_chunks(
    source: DocumentSource,
    version: DocumentVersion,
    seeds: Sequence[ChunkSeed],
    vectors: Sequence[VectorPair],
) -> tuple[SearchChunk, ...]:
    """Attach complete vector pairs and all six metadata keys to stable chunks."""
    if len(seeds) != len(vectors):
        raise MandatoryContentMissingError
    return tuple(
        SearchChunk.model_validate(
            {
                "chunk_id": seed.chunk_id,
                "engagement_id": source.engagement_id,
                "source_id": source.document_id,
                "source_version_id": version.document_version_id,
                "element_ids": seed.element_ids,
                "text_for_retrieval": seed.text,
                "location": seed.location,
                "stakeholder_id": source.stakeholder_id,
                "role": source.role,
                "department": source.department,
                "doc_type": source.doc_type,
                "source_type": source.source_type,
                "dense_vector": vector.dense,
                "sparse_vector": vector.sparse,
                "is_active_ready": False,
            }
        )
        for seed, vector in zip(seeds, vectors, strict=True)
    )


def _seed_contextual_text(  # noqa: PLR0913 - explicit immutable chunk inputs
    version: DocumentVersion,
    *,
    kind: str,
    elements: Sequence[SourceElement],
    text: str,
    location: SourceLocation,
    chunk_characters: int,
    overlap: int,
) -> tuple[ChunkSeed, ...]:
    element_ids = tuple(dict.fromkeys(element.element_id for element in elements))
    identity = "|".join(element_ids)
    seeds: list[ChunkSeed] = []
    for index, segment in enumerate(
        _split_text(text.strip(), chunk_characters=chunk_characters, overlap=overlap)
    ):
        digest = sha256(segment.encode()).hexdigest()
        seeds.append(
            ChunkSeed(
                chunk_id=stable_id(
                    "chunk",
                    version.document_version_id,
                    kind,
                    identity,
                    str(index),
                    digest,
                ),
                element_ids=element_ids,
                text=segment,
                location=location,
            )
        )
    return tuple(seeds)


def _supplemental_text(
    location: SourceLocation,
    context: Sequence[SourceElement],
    element: SourceElement,
) -> str:
    parts = [_location_header(location)]
    if context:
        parts.append(
            "[Surrounding context]\n" + "\n".join(_render_element(item) for item in context)
        )
    parts.append(_render_element(element))
    return "\n\n".join(parts)


def _render_element(element: SourceElement) -> str:
    content = (element.original_content or "").strip()
    if element.element_type == "table":
        return f"[Table]\n{content}"
    if element.element_type == "vision_description":
        return f"[Derived visual description]\n{content}"
    if element.element_type == "ocr_text":
        return f"[OCR text]\n{content}"
    return content


def _context_unit(location: SourceLocation) -> tuple[str, SourceLocation]:
    if isinstance(location, PdfPageLocation):
        return (
            f"pdf:{location.filename}:{location.page}",
            location.model_copy(update={"bounding_box": None}),
        )
    if isinstance(location, DocxRenderedPageLocation):
        return (
            f"docx:{location.filename}:{location.rendered_page}",
            location.model_copy(update={"paragraph": None, "bounding_box": None}),
        )
    if isinstance(location, PptxSlideLocation):
        return (
            f"pptx:{location.filename}:{location.slide}",
            location.model_copy(update={"shape_identifier": None, "bounding_box": None}),
        )
    if isinstance(location, XlsxRangeLocation):
        return f"xlsx:{location.filename}:{location.sheet}:{location.cell_range}", location
    if isinstance(location, ImageRegionLocation):
        return (
            f"image:{location.filename}:{location.image_index}:{location.region}",
            location.model_copy(update={"bounding_box": None}),
        )
    return str(location.model_dump(mode="json")), location


def _location_header(location: SourceLocation) -> str:
    if isinstance(location, PdfPageLocation):
        return f"[Source: {location.filename}; Page: {location.page}]"
    if isinstance(location, DocxRenderedPageLocation):
        return f"[Source: {location.filename}; Rendered page: {location.rendered_page}]"
    if isinstance(location, PptxSlideLocation):
        return f"[Source: {location.filename}; Slide: {location.slide}]"
    if isinstance(location, XlsxRangeLocation):
        return (
            f"[Source: {location.filename}; Sheet: {location.sheet}; Range: {location.cell_range}]"
        )
    if isinstance(location, ImageRegionLocation):
        image_region = location.region or location.image_index
        return f"[Source: {location.filename}; Image region: {image_region}]"
    return "[Source: authorized interview transcript]"


def _split_text(text: str, *, chunk_characters: int, overlap: int) -> tuple[str, ...]:
    if len(text) <= chunk_characters:
        return (text,)
    chunks: list[str] = []
    start = 0
    while start < len(text):
        hard_end = min(len(text), start + chunk_characters)
        end = hard_end
        if hard_end < len(text):
            boundary = max(
                text.rfind("\n", start + chunk_characters // 2, hard_end),
                text.rfind(" ", start + chunk_characters // 2, hard_end),
            )
            if boundary > start:
                end = boundary
        segment = text[start:end].strip()
        if segment:
            chunks.append(segment)
        if end >= len(text):
            break
        next_start = max(start + 1, end - overlap)
        start = next_start
    return tuple(chunks)
