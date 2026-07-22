"""Shared six-format ingestion, isolation, retry, version, and failure verification."""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from stakeholder_intelligence_agent.access import AccessService
from stakeholder_intelligence_agent.errors import (
    AccessDeniedError,
    ArtifactScopeError,
    CorruptSourceError,
    EnrichmentFailedError,
    IndexingFailedError,
    MediaTypeMismatchError,
    TranscriptImmutableError,
    UnsupportedDocumentTypeError,
)
from stakeholder_intelligence_agent.ingestion.repository import IngestionRepository
from stakeholder_intelligence_agent.ingestion.service import IngestionService
from stakeholder_intelligence_agent.ingestion.storage import IngestionArtifactStore
from stakeholder_intelligence_agent.ingestion.validation import UploadValidator
from stakeholder_intelligence_agent.persistence import DomainDatabase
from tests.fakes import (
    DeterministicDocumentExtractor,
    DeterministicVectorizer,
    DeterministicVisionEnricher,
    InMemoryVectorStager,
)

if TYPE_CHECKING:
    from stakeholder_intelligence_agent.config import Settings
    from stakeholder_intelligence_agent.contracts.access import AccessContext

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).parents[1] / "fixtures" / "ingestion"
FORMAT_CASES = (
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


@dataclass(slots=True)
class Harness:
    """One real access/persistence boundary with deterministic AI and converter doubles."""

    database: DomainDatabase
    access_service: AccessService
    service: IngestionService
    pm_context: AccessContext
    stakeholder_context: AccessContext
    extractor: DeterministicDocumentExtractor
    vision: DeterministicVisionEnricher
    vectorizer: DeterministicVectorizer
    stager: InMemoryVectorStager


async def _harness(settings: Settings) -> Harness:
    database = DomainDatabase(settings.domain_database)
    access_service = AccessService(database, settings)
    await access_service.initialize()
    pm_session = await access_service.activate_pm(settings.pm_bootstrap_token.get_secret_value())
    pm_token = pm_session.token.get_secret_value()
    engagement = await access_service.create_engagement(
        pm_token,
        name="Alpha transformation",
        description="Synthetic ingestion test engagement.",
        correlation_id="correlation-create-engagement",
    )
    stakeholder = await access_service.create_stakeholder(
        pm_token,
        engagement.engagement_id,
        display_name="Alex Morgan",
        role="Operations manager",
        department="Operations",
        correlation_id="correlation-create-stakeholder",
    )
    invitation = await access_service.issue_invitation(
        pm_token,
        engagement.engagement_id,
        stakeholder.stakeholder_id,
        correlation_id="correlation-issue-invitation",
    )
    activated = await access_service.activate_invitation(
        invitation.token.get_secret_value(),
        correlation_id="correlation-activate-invitation",
    )
    stakeholder_context = await access_service.resolve_stakeholder_context(
        activated.access_session.token.get_secret_value(),
        correlation_id="correlation-stakeholder-upload",
        required_permission="document:upload",
    )
    pm_context = await access_service.resolve_pm_context(
        pm_token,
        engagement.engagement_id,
        correlation_id="correlation-pm-upload",
        required_permission="document:upload",
    )
    extractor = DeterministicDocumentExtractor()
    vision = DeterministicVisionEnricher()
    vectorizer = DeterministicVectorizer()
    stager = InMemoryVectorStager()
    service = IngestionService(
        settings=settings,
        repository=IngestionRepository(
            database,
            lease_seconds=settings.ingestion_lease_seconds,
        ),
        validator=UploadValidator(settings),
        artifacts=IngestionArtifactStore(settings.originals_root, settings.derived_root),
        extractor=extractor,
        vision=vision,
        vectorizer=vectorizer,
        vector_stager=stager,
        clock=lambda: datetime(2026, 7, 15, 10, 0, tzinfo=UTC),
        lease_factory=lambda: "lease-stage-six",
    )
    await service.initialize()
    return Harness(
        database=database,
        access_service=access_service,
        service=service,
        pm_context=pm_context,
        stakeholder_context=stakeholder_context,
        extractor=extractor,
        vision=vision,
        vectorizer=vectorizer,
        stager=stager,
    )


@pytest.mark.parametrize(("filename", "media_type", "location_kind"), FORMAT_CASES)
async def test_every_format_uses_the_same_service_in_both_upload_contexts(
    settings: Settings,
    filename: str,
    media_type: str,
    location_kind: str,
) -> None:
    harness = await _harness(settings)
    content = (FIXTURES / filename).read_bytes()

    stakeholder = await harness.service.ingest(
        harness.stakeholder_context,
        filename=filename,
        declared_media_type=media_type,
        content=content,
    )
    project_manager = await harness.service.ingest(
        harness.pm_context,
        filename=filename,
        declared_media_type=media_type,
        content=content,
    )

    assert stakeholder.version.state == project_manager.version.state == "READY"
    assert stakeholder.version.is_active
    assert project_manager.version.is_active
    assert stakeholder.source.source_type == "stakeholder_document"
    assert stakeholder.source.stakeholder_id == harness.stakeholder_context.stakeholder_id
    assert stakeholder.source.role == "Operations manager"
    assert stakeholder.source.department == "Operations"
    assert project_manager.source.source_type == "engagement_document"
    assert project_manager.source.stakeholder_id is None
    assert project_manager.source.role is None
    assert project_manager.source.department is None
    assert all(chunk.location.kind == location_kind for chunk in stakeholder.chunks)
    assert all(chunk.is_active_ready for chunk in (*stakeholder.chunks, *project_manager.chunks))
    assert all(len(chunk.dense_vector) == 128 for chunk in stakeholder.chunks)
    assert all(chunk.sparse_vector.indices for chunk in stakeholder.chunks)
    assert any(element.element_type == "vision_description" for element in stakeholder.elements)
    originals = tuple(settings.originals_root.rglob(f"original.{filename.rsplit('.', 1)[-1]}"))
    assert any(path.read_bytes() == content for path in originals)


async def test_duplicate_concurrent_upload_is_idempotent_and_duplicate_free(
    settings: Settings,
) -> None:
    harness = await _harness(settings)
    filename, media_type, _ = FORMAT_CASES[0]
    content = (FIXTURES / filename).read_bytes()

    first, second = await asyncio.gather(
        harness.service.ingest(
            harness.pm_context,
            filename=filename,
            declared_media_type=media_type,
            content=content,
        ),
        harness.service.ingest(
            harness.pm_context,
            filename=filename,
            declared_media_type=media_type,
            content=content,
        ),
    )

    assert {first.idempotent, second.idempotent} == {False, True}
    assert first.version.document_version_id == second.version.document_version_id
    assert harness.extractor.calls == 1
    async with harness.database.connection() as connection:
        versions = await connection.execute("SELECT COUNT(*) AS count FROM document_versions")
        chunks = await connection.execute("SELECT COUNT(*) AS count FROM search_chunks")
        assert int((await versions.fetchone())["count"]) == 1  # type: ignore[index]
        assert int((await chunks.fetchone())["count"]) == len(first.chunks)  # type: ignore[index]


async def test_stakeholder_can_withdraw_and_reupload_only_before_interview_completion(
    settings: Settings,
) -> None:
    harness = await _harness(settings)
    filename, media_type, _ = FORMAT_CASES[0]
    content = (FIXTURES / filename).read_bytes()
    first = await harness.service.ingest(
        harness.stakeholder_context,
        filename=filename,
        declared_media_type=media_type,
        content=content,
    )

    await harness.service.delete_stakeholder_document(
        harness.stakeholder_context,
        first.source.document_id,
    )
    assert harness.stager.eligible[first.version.document_version_id] is False
    async with harness.database.connection() as connection:
        source = await connection.execute(
            "SELECT deleted_at FROM document_sources WHERE document_id = ?",
            (first.source.document_id,),
        )
        version = await connection.execute(
            "SELECT state, is_active FROM document_versions WHERE document_version_id = ?",
            (first.version.document_version_id,),
        )
        assert (await source.fetchone())["deleted_at"] is not None  # type: ignore[index]
        version_row = await version.fetchone()
        assert version_row is not None
        assert (version_row["state"], version_row["is_active"]) == ("SUPERSEDED", 0)

    replacement = await harness.service.ingest(
        harness.stakeholder_context,
        filename=filename,
        declared_media_type=media_type,
        content=content,
    )
    assert replacement.version.version_number == 2
    assert replacement.version.is_active
    async with harness.database.transaction() as connection:
        await connection.execute(
            """
            UPDATE interview_sessions SET status = 'ready', finalized_at = ?
            WHERE interview_session_id = ?
            """,
            (
                datetime(2026, 7, 15, 11, 0, tzinfo=UTC).isoformat(),
                harness.stakeholder_context.interview_session_id,
            ),
        )
    with pytest.raises(TranscriptImmutableError):
        await harness.service.delete_stakeholder_document(
            harness.stakeholder_context,
            replacement.source.document_id,
        )


async def test_pm_can_withdraw_only_pm_owned_engagement_documents(settings: Settings) -> None:
    harness = await _harness(settings)
    filename, media_type, _ = FORMAT_CASES[0]
    content = (FIXTURES / filename).read_bytes()
    stakeholder = await harness.service.ingest(
        harness.stakeholder_context,
        filename=filename,
        declared_media_type=media_type,
        content=content,
    )
    project_manager = await harness.service.ingest(
        harness.pm_context,
        filename=filename,
        declared_media_type=media_type,
        content=content,
    )

    with pytest.raises(AccessDeniedError):
        await harness.service.delete_pm_document(
            harness.pm_context,
            stakeholder.source.document_id,
        )

    await harness.service.delete_pm_document(
        harness.pm_context,
        project_manager.source.document_id,
    )
    assert harness.stager.eligible[project_manager.version.document_version_id] is False
    assert harness.stager.eligible[stakeholder.version.document_version_id] is True
    async with harness.database.connection() as connection:
        cursor = await connection.execute(
            """
            SELECT document_id, deleted_at FROM document_sources
            WHERE document_id IN (?, ?)
            """,
            (project_manager.source.document_id, stakeholder.source.document_id),
        )
        deleted_at = {row["document_id"]: row["deleted_at"] for row in await cursor.fetchall()}
    assert deleted_at[project_manager.source.document_id] is not None
    assert deleted_at[stakeholder.source.document_id] is None


async def test_restart_returns_ready_version_without_reextracting_or_revectorizing(
    settings: Settings,
) -> None:
    harness = await _harness(settings)
    filename, media_type, _ = FORMAT_CASES[0]
    content = (FIXTURES / filename).read_bytes()
    first = await harness.service.ingest(
        harness.pm_context,
        filename=filename,
        declared_media_type=media_type,
        content=content,
    )
    restarted_extractor = DeterministicDocumentExtractor()
    restarted_vectorizer = DeterministicVectorizer()
    restarted = IngestionService(
        settings=settings,
        repository=IngestionRepository(
            DomainDatabase(settings.domain_database),
            lease_seconds=settings.ingestion_lease_seconds,
        ),
        validator=UploadValidator(settings),
        artifacts=IngestionArtifactStore(settings.originals_root, settings.derived_root),
        extractor=restarted_extractor,
        vision=DeterministicVisionEnricher(),
        vectorizer=restarted_vectorizer,
        vector_stager=harness.stager,
        clock=lambda: datetime(2026, 7, 15, 10, 0, tzinfo=UTC),
    )
    await restarted.initialize()

    second = await restarted.ingest(
        harness.pm_context,
        filename=filename,
        declared_media_type=media_type,
        content=content,
    )

    assert second.idempotent
    assert second.version.document_version_id == first.version.document_version_id
    assert restarted_extractor.calls == 0
    assert restarted_vectorizer.calls == 0


async def test_changed_and_reverted_bytes_create_monotonic_versions_with_one_active_ready(
    settings: Settings,
) -> None:
    harness = await _harness(settings)
    first_bytes = (FIXTURES / "alpha-organization-chart.png").read_bytes()
    second_bytes = (FIXTURES / "alpha-influence-chart.png").read_bytes()

    first = await harness.service.ingest(
        harness.pm_context,
        filename="stakeholder-map.png",
        declared_media_type="image/png",
        content=first_bytes,
    )
    second = await harness.service.ingest(
        harness.pm_context,
        filename="stakeholder-map.png",
        declared_media_type="image/png",
        content=second_bytes,
    )
    reverted = await harness.service.ingest(
        harness.pm_context,
        filename="stakeholder-map.png",
        declared_media_type="image/png",
        content=first_bytes,
    )

    assert (first.version.version_number, second.version.version_number) == (1, 2)
    assert reverted.version.version_number == 3
    assert harness.stager.eligible[first.version.document_version_id] is False
    assert harness.stager.eligible[second.version.document_version_id] is False
    assert harness.stager.eligible[reverted.version.document_version_id] is True
    async with harness.database.connection() as connection:
        cursor = await connection.execute(
            """
            SELECT version_number, state, is_active FROM document_versions
            ORDER BY version_number
            """
        )
        rows = await cursor.fetchall()
    assert [(row["version_number"], row["state"], row["is_active"]) for row in rows] == [
        (1, "SUPERSEDED", 0),
        (2, "SUPERSEDED", 0),
        (3, "READY", 1),
    ]


async def test_failed_vision_attempt_is_visible_non_searchable_and_retry_safe(
    settings: Settings,
) -> None:
    harness = await _harness(settings)
    filename = "alpha-organization-chart.png"
    content = (FIXTURES / filename).read_bytes()
    harness.vision.fail = True

    with pytest.raises(EnrichmentFailedError):
        await harness.service.ingest(
            harness.pm_context,
            filename=filename,
            declared_media_type="image/png",
            content=content,
        )
    async with harness.database.connection() as connection:
        failed_cursor = await connection.execute("SELECT * FROM document_versions")
        failed = await failed_cursor.fetchone()
        artifact_cursor = await connection.execute(
            "SELECT artifact_kind, content_hash FROM ingestion_artifacts"
        )
        original = await artifact_cursor.fetchone()
    assert failed is not None
    assert failed["state"] == "FAILED"
    assert failed["is_active"] == 0
    assert failed["failure_code"] == "ENRICHMENT_FAILED"
    assert original is not None
    assert original["artifact_kind"] == "original"

    harness.vision.fail = False
    retried = await harness.service.ingest(
        harness.pm_context,
        filename=filename,
        declared_media_type="image/png",
        content=content,
    )

    assert retried.version.state == "READY"
    assert retried.version.version_number == 1
    assert harness.extractor.calls == 2
    async with harness.database.connection() as connection:
        attempts = await connection.execute(
            "SELECT attempt_number, status FROM ingestion_attempts ORDER BY attempt_number"
        )
        rows = await attempts.fetchall()
    assert [(row["attempt_number"], row["status"]) for row in rows] == [
        (1, "failed"),
        (2, "succeeded"),
    ]


async def test_failed_new_vector_preparation_keeps_previous_ready_version_active(
    settings: Settings,
) -> None:
    harness = await _harness(settings)
    first_bytes = (FIXTURES / "alpha-organization-chart.png").read_bytes()
    changed_bytes = (FIXTURES / "alpha-influence-chart.png").read_bytes()
    first = await harness.service.ingest(
        harness.pm_context,
        filename="risk-map.png",
        declared_media_type="image/png",
        content=first_bytes,
    )
    harness.stager.fail_at = "prepare_activation"

    with pytest.raises(IndexingFailedError):
        await harness.service.ingest(
            harness.pm_context,
            filename="risk-map.png",
            declared_media_type="image/png",
            content=changed_bytes,
        )

    assert harness.stager.eligible[first.version.document_version_id] is True
    async with harness.database.connection() as connection:
        cursor = await connection.execute(
            """
            SELECT version_number, state, is_active, failure_code
            FROM document_versions ORDER BY version_number
            """
        )
        rows = await cursor.fetchall()
    assert [tuple(row) for row in rows] == [
        (1, "READY", 1, None),
        (2, "FAILED", 0, "INDEXING_FAILED"),
    ]


async def test_forged_server_context_is_rechecked_against_persistence(
    settings: Settings,
) -> None:
    harness = await _harness(settings)
    forged = harness.pm_context.model_copy(update={"principal_id": "pm-forged"})

    with pytest.raises(AccessDeniedError) as denial:
        await harness.service.ingest(
            forged,
            filename="alpha-organization-chart.png",
            declared_media_type="image/png",
            content=(FIXTURES / "alpha-organization-chart.png").read_bytes(),
        )

    assert str(denial.value) == "Access is not authorized."
    async with harness.database.connection() as connection:
        cursor = await connection.execute("SELECT COUNT(*) AS count FROM document_sources")
        row = await cursor.fetchone()
    assert row is not None
    assert int(row["count"]) == 0


async def test_corrupt_mismatched_and_unsupported_sources_fail_visibly(
    settings: Settings,
) -> None:
    harness = await _harness(settings)
    with pytest.raises(MediaTypeMismatchError):
        await harness.service.ingest(
            harness.pm_context,
            filename="mismatched-content.pdf",
            declared_media_type="application/pdf",
            content=(FIXTURES / "mismatched-content.pdf").read_bytes(),
        )
    with pytest.raises(UnsupportedDocumentTypeError):
        await harness.service.ingest(
            harness.pm_context,
            filename="unsupported-diagram.vsdx",
            declared_media_type="application/vnd.ms-visio.drawing.main+xml",
            content=(FIXTURES / "unsupported-diagram.vsdx").read_bytes(),
        )
    with pytest.raises(CorruptSourceError):
        await harness.service.ingest(
            harness.pm_context,
            filename="corrupt-source.pdf",
            declared_media_type="application/pdf",
            content=(FIXTURES / "corrupt-source.pdf").read_bytes(),
        )

    async with harness.database.connection() as connection:
        sources = await connection.execute("SELECT COUNT(*) AS count FROM document_sources")
        versions = await connection.execute("SELECT state, failure_code FROM document_versions")
        source_count = await sources.fetchone()
        rows = await versions.fetchall()
    assert source_count is not None
    assert int(source_count["count"]) == 1
    assert [(row["state"], row["failure_code"]) for row in rows] == [("FAILED", "CORRUPT_SOURCE")]


async def test_cross_engagement_context_cannot_load_another_engagement_version(
    settings: Settings,
) -> None:
    alpha = await _harness(settings)
    filename = "alpha-organization-chart.png"
    result = await alpha.service.ingest(
        alpha.pm_context,
        filename=filename,
        declared_media_type="image/png",
        content=(FIXTURES / filename).read_bytes(),
    )
    pm_session = await alpha.access_service.activate_pm(
        settings.pm_bootstrap_token.get_secret_value()
    )
    pm_token = pm_session.token.get_secret_value()
    beta_engagement = await alpha.access_service.create_engagement(
        pm_token,
        name="Beta transformation",
        description=None,
        correlation_id="correlation-beta",
    )
    beta_context = await alpha.access_service.resolve_pm_context(
        pm_token,
        beta_engagement.engagement_id,
        correlation_id="correlation-beta-upload",
        required_permission="document:upload",
    )
    repository = IngestionRepository(
        alpha.database,
        lease_seconds=settings.ingestion_lease_seconds,
    )

    with pytest.raises(AccessDeniedError):
        await repository.load_ready_result(
            beta_context,
            result.version.document_version_id,
            now=datetime(2026, 7, 15, 10, 0, tzinfo=UTC),
        )

    async with alpha.database.connection() as connection:
        cursor = await connection.execute(
            """
            SELECT virtual_path FROM ingestion_artifacts
            WHERE document_version_id = ? AND artifact_kind = 'original'
            """,
            (result.version.document_version_id,),
        )
        artifact = await cursor.fetchone()
    assert artifact is not None
    virtual_path = str(artifact["virtual_path"])
    artifact_store = IngestionArtifactStore(
        settings.originals_root,
        settings.derived_root,
    )
    resolved = artifact_store.resolve_virtual(
        alpha.pm_context,
        virtual_path,
    )
    assert resolved.read_bytes() == (FIXTURES / filename).read_bytes()
    with pytest.raises(ArtifactScopeError):
        artifact_store.resolve_virtual(beta_context, virtual_path)


async def test_version_history_events_are_append_only(settings: Settings) -> None:
    harness = await _harness(settings)
    await harness.service.ingest(
        harness.pm_context,
        filename="alpha-organization-chart.png",
        declared_media_type="image/png",
        content=(FIXTURES / "alpha-organization-chart.png").read_bytes(),
    )

    async with harness.database.connection() as connection:
        cursor = await connection.execute(
            "SELECT event_id FROM document_version_events ORDER BY occurred_at LIMIT 1"
        )
        event = await cursor.fetchone()
        assert event is not None
        with pytest.raises(sqlite3.IntegrityError):
            await connection.execute(
                "UPDATE document_version_events SET to_state = 'FAILED' WHERE event_id = ?",
                (event["event_id"],),
            )
        with pytest.raises(sqlite3.IntegrityError):
            await connection.execute(
                "DELETE FROM document_version_events WHERE event_id = ?",
                (event["event_id"],),
            )
