"""Course-faithful page, table-context, and visual-context chunking tests."""

from datetime import UTC, datetime
from hashlib import sha256

import pytest

from stakeholder_intelligence_agent.contracts.source import (
    DocumentVersion,
    PdfPageLocation,
    SourceElement,
)
from stakeholder_intelligence_agent.ingestion.normalization import build_chunk_seeds


def _version() -> DocumentVersion:
    return DocumentVersion(
        document_version_id="version-a",
        document_id="document-a",
        version_number=1,
        content_hash="a" * 64,
        state="INDEXING",
        is_active=False,
        original_artifact_id="artifact-original",
        ingestion_key="ingestion-a",
        created_at=datetime(2026, 7, 20, tzinfo=UTC),
    )


def _element(  # noqa: PLR0913 - compact canonical-element fixture factory
    element_id: str,
    element_type: str,
    content: str | None,
    *,
    page: int = 1,
    parent_element_id: str | None = None,
    artifact_id: str | None = None,
) -> SourceElement:
    payload = content if content is not None else element_id
    return SourceElement.model_validate(
        {
            "element_id": element_id,
            "document_version_id": "version-a",
            "element_type": element_type,
            "original_content": content,
            "location": PdfPageLocation(filename="operations.pdf", page=page),
            "parent_element_id": parent_element_id,
            "artifact_id": artifact_id,
            "content_hash": sha256(payload.encode()).hexdigest(),
            "extraction_method": "test",
        }
    )


@pytest.mark.unit
def test_page_table_and_visual_chunks_preserve_course_context() -> None:
    elements = (
        _element("heading", "text", "Target operating model"),
        _element("paragraph", "text", "Operations owns the weekly approval handoff."),
        _element("table", "table", "| Role | Responsibility |\n| --- | --- |\n| Ops | Approve |"),
        _element("chart", "chart", None, artifact_id="artifact-chart"),
        _element(
            "vision",
            "vision_description",
            "The chart shows approval delays rising from two to five days.",
            parent_element_id="chart",
        ),
        _element("page-two", "text", "This belongs only to page two.", page=2),
    )

    chunks = build_chunk_seeds(
        _version(),
        elements,
        chunk_characters=10_000,
        overlap=200,
    )

    page_one = next(
        chunk
        for chunk in chunks
        if isinstance(chunk.location, PdfPageLocation)
        and chunk.location.page == 1
        and set(chunk.element_ids) == {"heading", "paragraph", "table"}
    )
    table = next(
        chunk
        for chunk in chunks
        if chunk.text.count("[Table]") == 1 and "[Surrounding context]" in chunk.text
    )
    visual = next(chunk for chunk in chunks if "[Derived visual description]" in chunk.text)

    assert "[Source: operations.pdf; Page: 1]" in page_one.text
    assert "Target operating model" in page_one.text
    assert "Operations owns" in page_one.text
    assert "| Role | Responsibility |" in page_one.text
    assert table.element_ids == ("heading", "paragraph", "table")
    assert "Target operating model" in table.text
    assert "Operations owns" in table.text
    assert visual.element_ids == ("paragraph", "table", "vision")
    assert "Operations owns" in visual.text
    assert "| Role | Responsibility |" in visual.text
    assert "rising from two to five days" in visual.text
    assert "This belongs only to page two" not in page_one.text


@pytest.mark.unit
def test_contextual_chunk_ids_are_deterministic() -> None:
    elements = (
        _element("heading", "text", "Operational ownership"),
        _element("paragraph", "text", "The PM owns escalation."),
    )

    first = build_chunk_seeds(_version(), elements, chunk_characters=500, overlap=50)
    second = build_chunk_seeds(_version(), elements, chunk_characters=500, overlap=50)

    assert first == second
    assert first[0].element_ids == ("heading", "paragraph")
