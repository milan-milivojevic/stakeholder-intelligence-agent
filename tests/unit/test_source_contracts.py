"""Canonical source, locator, version, element, and vector contract tests."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import TypeAdapter, ValidationError

from stakeholder_intelligence_agent.contracts import (
    DocumentSource,
    DocumentVersion,
    SearchChunk,
    SourceElement,
    SparseVector,
)
from stakeholder_intelligence_agent.contracts.source import (
    ImageRegionLocation,
    PdfPageLocation,
    SourceLocation,
    TranscriptTurnsLocation,
)

NOW = datetime(2026, 7, 15, 1, 0, tzinfo=UTC)
DIGEST = "b" * 64
LOCATION_ADAPTER: TypeAdapter[SourceLocation] = TypeAdapter(SourceLocation)


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "pdf_page", "filename": "brief.pdf", "page": 1},
        {
            "kind": "docx_rendered_page",
            "filename": "brief.docx",
            "rendered_page": 1,
            "section": "Operating model",
            "paragraph": 2,
        },
        {
            "kind": "pptx_slide",
            "filename": "brief.pptx",
            "slide": 3,
            "shape_identifier": "shape-7",
        },
        {
            "kind": "xlsx_range",
            "filename": "risks.xlsx",
            "sheet": "Risks",
            "cell_range": "a1:c8",
        },
        {
            "kind": "image_region",
            "filename": "process.png",
            "image_index": 1,
        },
        {
            "kind": "transcript_turns",
            "stakeholder_id": "stakeholder-a",
            "transcript_id": "transcript-a",
            "turn_start": 0,
            "turn_end": 2,
        },
    ],
)
def test_every_canonical_source_location_is_discriminated(payload: dict[str, object]) -> None:
    location = LOCATION_ADAPTER.validate_python(payload)
    assert location.kind == payload["kind"]


def test_source_location_json_schema_preserves_kind_discriminator() -> None:
    schema = LOCATION_ADAPTER.json_schema()
    assert schema["discriminator"]["propertyName"] == "kind"
    assert set(schema["discriminator"]["mapping"]) == {
        "pdf_page",
        "docx_rendered_page",
        "pptx_slide",
        "xlsx_range",
        "image_region",
        "transcript_turns",
    }


def test_locator_numbering_and_path_syntax_are_strict() -> None:
    with pytest.raises(ValidationError):
        LOCATION_ADAPTER.validate_python({"kind": "pdf_page", "filename": "brief.pdf", "page": 0})
    with pytest.raises(ValidationError):
        LOCATION_ADAPTER.validate_python({"kind": "image_region", "filename": "process.png"})
    with pytest.raises(ValidationError):
        LOCATION_ADAPTER.validate_python(
            {"kind": "pdf_page", "filename": "../brief.pdf", "page": 1}
        )


def test_document_source_enforces_upload_context_and_six_metadata_keys() -> None:
    source = DocumentSource(
        document_id="document-a",
        engagement_id="engagement-a",
        stakeholder_id="stakeholder-a",
        role=None,
        department=None,
        doc_type="pdf",
        source_type="stakeholder_document",
        original_filename="Stakeholder brief.pdf",
        media_type="APPLICATION/PDF",
        created_at=NOW,
    )
    assert source.media_type == "application/pdf"
    for key in (
        "engagement_id",
        "stakeholder_id",
        "role",
        "department",
        "doc_type",
        "source_type",
    ):
        assert key in source.model_dump()

    with pytest.raises(ValidationError):
        DocumentSource(
            document_id="document-b",
            engagement_id="engagement-a",
            stakeholder_id="stakeholder-a",
            role=None,
            department=None,
            doc_type="pdf",
            source_type="engagement_document",
            original_filename="Brief.pdf",
            media_type="application/pdf",
            created_at=NOW,
        )


def test_document_version_ready_and_failure_states_are_unambiguous() -> None:
    ready = DocumentVersion(
        document_version_id="document-version-a",
        document_id="document-a",
        version_number=1,
        content_hash=DIGEST,
        state="READY",
        is_active=True,
        original_artifact_id="artifact-a",
        ingestion_key="ingestion-a",
        created_at=NOW,
        ready_at=NOW + timedelta(minutes=1),
    )
    assert ready.is_active
    with pytest.raises(ValidationError):
        DocumentVersion.model_validate(ready.model_dump() | {"state": "FAILED"})


def test_source_element_preserves_original_content_and_requires_lineage() -> None:
    original = "  Original extracted text remains unchanged.  "
    element = SourceElement(
        element_id="element-a",
        document_version_id="document-version-a",
        element_type="text",
        original_content=original,
        location=PdfPageLocation(filename="brief.pdf", page=1),
        content_hash=DIGEST,
        extraction_method="docling",
    )
    assert element.original_content == original
    with pytest.raises(ValidationError):
        SourceElement(
            element_id="element-b",
            document_version_id="document-version-a",
            element_type="vision_description",
            original_content="A process map.",
            location=ImageRegionLocation(filename="map.png", image_index=1),
            content_hash=DIGEST,
            extraction_method="docling",
        )


def test_sparse_vector_requires_ordered_nonzero_shape() -> None:
    assert SparseVector(indices=(1, 7), values=(0.2, 0.8)).indices == (1, 7)
    with pytest.raises(ValidationError):
        SparseVector(indices=(7, 1), values=(0.2, 0.8))
    with pytest.raises(ValidationError):
        SparseVector(indices=(1,), values=(0.0,))


def test_search_chunk_requires_complete_context_metadata() -> None:
    chunk = SearchChunk(
        chunk_id="chunk-a",
        engagement_id="engagement-a",
        source_id="transcript-a",
        source_version_id="transcript-version-a",
        element_ids=("turn-0",),
        text_for_retrieval="Original stakeholder statement.",
        location=TranscriptTurnsLocation(
            stakeholder_id="stakeholder-a",
            transcript_id="transcript-a",
            turn_start=0,
            turn_end=0,
        ),
        stakeholder_id="stakeholder-a",
        role=None,
        department=None,
        doc_type="transcript",
        source_type="interview",
        dense_vector=(0.1,) * 128,
        sparse_vector=SparseVector(indices=(1,), values=(0.5,)),
        is_active_ready=True,
    )
    assert chunk.source_type == "interview"
    with pytest.raises(ValidationError):
        SearchChunk.model_validate(chunk.model_dump() | {"stakeholder_id": None})
