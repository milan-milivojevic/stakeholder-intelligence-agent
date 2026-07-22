"""Independent extension, MIME, signature, size, and archive safety tests."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from stakeholder_intelligence_agent.errors import (
    CorruptSourceError,
    MediaTypeMismatchError,
    UnsupportedDocumentTypeError,
    UploadSizeError,
)
from stakeholder_intelligence_agent.ingestion.validation import UploadValidator

if TYPE_CHECKING:
    from stakeholder_intelligence_agent.config import Settings

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).parents[1] / "fixtures" / "ingestion"


@pytest.mark.parametrize(
    ("filename", "media_type", "document_type"),
    [
        ("alpha-mixed-content.pdf", "application/pdf", "pdf"),
        (
            "alpha-stakeholder-brief.docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "docx",
        ),
        (
            "alpha-evidence-deck.pptx",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            "pptx",
        ),
        (
            "alpha-stakeholder-signals.xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "xlsx",
        ),
        ("alpha-organization-chart.png", "image/png", "png"),
        ("beta-process-map.jpg", "image/jpeg", "jpeg"),
    ],
)
def test_all_allowed_envelopes_and_structures_validate(
    settings: Settings,
    filename: str,
    media_type: str,
    document_type: str,
) -> None:
    validator = UploadValidator(settings)
    upload = validator.validate_envelope(
        filename=filename,
        declared_media_type=media_type,
        content=(FIXTURES / filename).read_bytes(),
    )

    validator.validate_structure(upload)

    assert upload.document_type == document_type
    assert len(upload.content_hash) == 64


def test_mime_and_signature_are_independent(settings: Settings) -> None:
    validator = UploadValidator(settings)
    png = (FIXTURES / "alpha-organization-chart.png").read_bytes()

    with pytest.raises(MediaTypeMismatchError):
        validator.validate_envelope(
            filename="renamed.pdf",
            declared_media_type="application/pdf",
            content=png,
        )
    with pytest.raises(MediaTypeMismatchError):
        validator.validate_envelope(
            filename="alpha-organization-chart.png",
            declared_media_type="application/pdf",
            content=png,
        )


def test_unsupported_empty_oversized_and_corrupt_inputs_are_safe(settings: Settings) -> None:
    validator = UploadValidator(settings)
    with pytest.raises(UnsupportedDocumentTypeError):
        validator.validate_envelope(
            filename="diagram.vsdx",
            declared_media_type="application/vnd.ms-visio.drawing.main+xml",
            content=b"PK\x03\x04fixture",
        )
    with pytest.raises(UploadSizeError):
        validator.validate_envelope(
            filename="empty.pdf",
            declared_media_type="application/pdf",
            content=b"",
        )
    constrained = UploadValidator(settings.model_copy(update={"max_upload_bytes": 10}))
    with pytest.raises(UploadSizeError):
        constrained.validate_envelope(
            filename="large.pdf",
            declared_media_type="application/pdf",
            content=b"%PDF-" + b"x" * 20,
        )
    corrupt = validator.validate_envelope(
        filename="corrupt-source.pdf",
        declared_media_type="application/pdf",
        content=(FIXTURES / "corrupt-source.pdf").read_bytes(),
    )
    with pytest.raises(CorruptSourceError):
        validator.validate_structure(corrupt)
