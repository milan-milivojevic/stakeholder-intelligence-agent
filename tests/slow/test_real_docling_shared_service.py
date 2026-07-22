"""Real Docling through the shared PM/stakeholder ingestion service and lifecycle."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from stakeholder_intelligence_agent.access import AccessService
from stakeholder_intelligence_agent.ingestion.docling_adapter import DoclingExtractor
from stakeholder_intelligence_agent.ingestion.repository import IngestionRepository
from stakeholder_intelligence_agent.ingestion.service import IngestionService
from stakeholder_intelligence_agent.ingestion.storage import IngestionArtifactStore
from stakeholder_intelligence_agent.ingestion.validation import UploadValidator
from stakeholder_intelligence_agent.persistence import DomainDatabase
from tests.fakes import (
    DeterministicVectorizer,
    DeterministicVisionEnricher,
    InMemoryVectorStager,
)

if TYPE_CHECKING:
    from stakeholder_intelligence_agent.config import Settings

pytestmark = [pytest.mark.slow, pytest.mark.integration, pytest.mark.timeout(900)]

FIXTURES = Path(__file__).parents[1] / "fixtures" / "ingestion"
CASES = (
    ("alpha-mixed-content.pdf", "application/pdf", "pdf_page"),
    ("alpha-scanned-workshop-note.pdf", "application/pdf", "pdf_page"),
    (
        "alpha-stakeholder-brief.docx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "docx_rendered_page",
    ),
    (
        "alpha-evidence-deck.pptx",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "pptx_slide",
    ),
    (
        "alpha-stakeholder-signals.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "xlsx_range",
    ),
    ("alpha-organization-chart.png", "image/png", "image_region"),
    ("beta-process-map.jpg", "image/jpeg", "image_region"),
)


async def test_real_docling_matrix_reaches_ready_in_both_authorized_upload_modes(
    settings: Settings,
) -> None:
    database = DomainDatabase(settings.domain_database)
    access = AccessService(database, settings)
    await access.initialize()
    pm_session = await access.activate_pm(settings.pm_bootstrap_token.get_secret_value())
    pm_token = pm_session.token.get_secret_value()
    engagement = await access.create_engagement(
        pm_token,
        name="Real Docling matrix",
        description="Deterministic synthetic acceptance matrix.",
        correlation_id="real-docling-engagement",
    )
    stakeholder = await access.create_stakeholder(
        pm_token,
        engagement.engagement_id,
        display_name="Alex Morgan",
        role="Operations manager",
        department="Operations",
        correlation_id="real-docling-stakeholder",
    )
    invitation = await access.issue_invitation(
        pm_token,
        engagement.engagement_id,
        stakeholder.stakeholder_id,
        correlation_id="real-docling-invitation",
    )
    activated = await access.activate_invitation(
        invitation.token.get_secret_value(),
        correlation_id="real-docling-activation",
    )
    stakeholder_context = await access.resolve_stakeholder_context(
        activated.access_session.token.get_secret_value(),
        correlation_id="real-docling-stakeholder-upload",
        required_permission="document:upload",
    )
    pm_context = await access.resolve_pm_context(
        pm_token,
        engagement.engagement_id,
        correlation_id="real-docling-pm-upload",
        required_permission="document:upload",
    )
    vision = DeterministicVisionEnricher()
    stager = InMemoryVectorStager()
    service = IngestionService(
        settings=settings,
        repository=IngestionRepository(
            database,
            lease_seconds=settings.ingestion_lease_seconds,
        ),
        validator=UploadValidator(settings),
        artifacts=IngestionArtifactStore(settings.originals_root, settings.derived_root),
        extractor=DoclingExtractor(settings),
        vision=vision,
        vectorizer=DeterministicVectorizer(),
        vector_stager=stager,
        clock=lambda: datetime(2026, 7, 15, 12, 0, tzinfo=UTC),
    )
    await service.initialize()

    for filename, media_type, location_kind in CASES:
        content = (FIXTURES / filename).read_bytes()
        stakeholder_result = await service.ingest(
            stakeholder_context,
            filename=filename,
            declared_media_type=media_type,
            content=content,
        )
        pm_result = await service.ingest(
            pm_context,
            filename=filename,
            declared_media_type=media_type,
            content=content,
        )
        assert stakeholder_result.version.state == "READY"
        assert pm_result.version.state == "READY"
        assert stakeholder_result.source.stakeholder_id == stakeholder.stakeholder_id
        assert pm_result.source.stakeholder_id is None
        assert stakeholder_result.chunks
        assert pm_result.chunks
        assert all(chunk.location.kind == location_kind for chunk in stakeholder_result.chunks)
        assert all(chunk.is_active_ready for chunk in stakeholder_result.chunks)
        assert any(
            element.element_type == "vision_description" for element in stakeholder_result.elements
        )
        assert stager.eligible[stakeholder_result.version.document_version_id]
        assert stager.eligible[pm_result.version.document_version_id]

    async with database.connection() as connection:
        cursor = await connection.execute(
            """
            SELECT COUNT(*) AS version_count,
                SUM(CASE WHEN state = 'READY' AND is_active = 1 THEN 1 ELSE 0 END)
                    AS ready_count
            FROM document_versions
            """
        )
        row = await cursor.fetchone()
    assert row is not None
    assert int(row["version_count"]) == len(CASES) * 2
    assert int(row["ready_count"]) == len(CASES) * 2
    assert len(vision.calls) >= len(CASES) * 2
