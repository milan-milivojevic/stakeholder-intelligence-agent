"""Regression tests for truthful, non-truncating normalized DOCX pages."""

from __future__ import annotations

from stakeholder_intelligence_agent.ingestion.docling_adapter import DoclingExtractor


def test_normalized_docx_render_paginates_all_elements() -> None:
    entries = tuple(
        (f"element-{number}", f"Element {number}: " + "evidence " * 180) for number in range(1, 31)
    )

    page_by_element, rendered_pages = DoclingExtractor._render_text_pages(  # noqa: SLF001
        entries,
        title="long-stakeholder-brief.docx",
    )

    assert len(rendered_pages) > 1
    assert set(page_by_element) == {key for key, _content in entries}
    assert page_by_element["element-1"] == 1
    assert page_by_element["element-30"] == len(rendered_pages)
    assert set(page_by_element.values()) == set(range(1, len(rendered_pages) + 1))
    assert all(content.startswith(b"\x89PNG\r\n\x1a\n") for content in rendered_pages)


def test_normalized_docx_render_maps_image_placeholder() -> None:
    page_by_element, rendered_pages = DoclingExtractor._render_text_pages(  # noqa: SLF001
        (("picture-1", "[Embedded image: picture-1-visual]"),),
        title="visual-brief.docx",
    )

    assert page_by_element == {"picture-1": 1}
    assert len(rendered_pages) == 1
