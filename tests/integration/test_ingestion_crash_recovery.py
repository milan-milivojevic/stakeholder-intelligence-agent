"""Process-crash recovery at every approved document-ingestion boundary."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, override

import pytest

import stakeholder_intelligence_agent.ingestion.service as ingestion_service_module
from stakeholder_intelligence_agent.access import AccessService
from stakeholder_intelligence_agent.ingestion.identity import stable_id
from stakeholder_intelligence_agent.ingestion.normalization import build_chunk_seeds
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
    from collections.abc import Sequence

    from stakeholder_intelligence_agent.config import Settings
    from stakeholder_intelligence_agent.contracts.access import AccessContext
    from stakeholder_intelligence_agent.contracts.source import (
        DocumentVersion,
        SearchChunk,
        SourceElement,
        SourceLocation,
    )
    from stakeholder_intelligence_agent.ingestion.normalization import ChunkSeed
    from stakeholder_intelligence_agent.ingestion.types import (
        ActivationResult,
        ExtractionBundle,
        IngestionStart,
        StoredArtifact,
        ValidatedUpload,
        VectorPair,
    )

pytestmark = pytest.mark.integration

FIXED_NOW = datetime(2026, 7, 15, 10, 0, tzinfo=UTC)
EXPIRED_LEASE = "2026-07-15T09:59:59.000000Z"
FIXTURE = Path(__file__).parents[1] / "fixtures" / "ingestion" / "alpha-mixed-content.pdf"
MEDIA_TYPE = "application/pdf"
BOUNDARIES = (
    "after_domain_version_creation",
    "after_original_persistence",
    "during_docling_extraction",
    "during_ocr_or_vision_enrichment",
    "during_chunk_materialization",
    "during_dense_embedding",
    "during_sparse_representation",
    "during_partial_qdrant_upsert",
    "before_activation",
    "after_activation_response_loss",
)


class SimulatedProcessCrash(BaseException):
    """Bypass normal exception compensation as an abrupt process exit would."""


@dataclass(slots=True)
class CrashController:
    """Arm exactly one named process-crash boundary."""

    active_boundary: str | None = None
    observed: list[str] = field(default_factory=list)

    def arm(self, boundary: str) -> None:
        """Arm one boundary after the baseline version is READY."""
        self.active_boundary = boundary
        self.observed.clear()

    def hit(self, boundary: str) -> None:
        """Raise once at the armed boundary and then permit recovery."""
        if self.active_boundary != boundary:
            return
        self.active_boundary = None
        self.observed.append(boundary)
        raise SimulatedProcessCrash(boundary)


class CrashRepository(IngestionRepository):
    """Inject crashes around durable version creation and activation."""

    def __init__(
        self,
        database: DomainDatabase,
        *,
        lease_seconds: int,
        controller: CrashController,
    ) -> None:
        super().__init__(database, lease_seconds=lease_seconds)
        self._controller = controller

    @override
    async def start(
        self,
        access: AccessContext,
        upload: ValidatedUpload,
        *,
        lease_token: str,
        now: datetime,
    ) -> IngestionStart:
        result = await super().start(access, upload, lease_token=lease_token, now=now)
        self._controller.hit("after_domain_version_creation")
        return result

    @override
    async def activate(
        self,
        access: AccessContext,
        *,
        version_id: str,
        attempt_id: str,
        lease_token: str,
        now: datetime,
    ) -> ActivationResult:
        self._controller.hit("before_activation")
        result = await super().activate(
            access,
            version_id=version_id,
            attempt_id=attempt_id,
            lease_token=lease_token,
            now=now,
        )
        self._controller.hit("after_activation_response_loss")
        return result


class CrashArtifactStore(IngestionArtifactStore):
    """Inject a crash after immutable original bytes reach controlled storage."""

    def __init__(
        self,
        originals_root: Path,
        derived_root: Path,
        *,
        controller: CrashController,
    ) -> None:
        super().__init__(originals_root, derived_root)
        self._controller = controller

    @override
    def write_original(
        self,
        access: AccessContext,
        *,
        document_id: str,
        document_version_id: str,
        artifact_id: str,
        upload: ValidatedUpload,
    ) -> tuple[StoredArtifact, Path]:
        result = super().write_original(
            access,
            document_id=document_id,
            document_version_id=document_version_id,
            artifact_id=artifact_id,
            upload=upload,
        )
        self._controller.hit("after_original_persistence")
        return result


@dataclass(slots=True)
class CrashExtractor:
    """Probe the primary Docling conversion boundary."""

    controller: CrashController
    delegate: DeterministicDocumentExtractor = field(default_factory=DeterministicDocumentExtractor)

    def extract(self, source_path: Path, upload: ValidatedUpload) -> ExtractionBundle:
        result = self.delegate.extract(source_path, upload)
        self.controller.hit("during_docling_extraction")
        return result


@dataclass(slots=True)
class CrashVisionEnricher:
    """Probe the OCR-or-vision enrichment boundary."""

    controller: CrashController
    delegate: DeterministicVisionEnricher = field(default_factory=DeterministicVisionEnricher)

    async def describe(
        self,
        *,
        content: bytes,
        media_type: str,
        filename: str,
        location: SourceLocation,
    ) -> str:
        result = await self.delegate.describe(
            content=content,
            media_type=media_type,
            filename=filename,
            location=location,
        )
        self.controller.hit("during_ocr_or_vision_enrichment")
        return result


@dataclass(slots=True)
class CrashVectorizer:
    """Probe dense and sparse construction as separate required boundaries."""

    controller: CrashController
    delegate: DeterministicVectorizer = field(default_factory=DeterministicVectorizer)

    async def vectorize(self, texts: Sequence[str]) -> tuple[VectorPair, ...]:
        self.controller.hit("during_dense_embedding")
        result = await self.delegate.vectorize(texts)
        self.controller.hit("during_sparse_representation")
        return result


@dataclass(slots=True)
class CrashVectorStager:
    """Probe a partial stable-ID Qdrant upsert before completeness verification."""

    controller: CrashController
    delegate: InMemoryVectorStager = field(default_factory=InMemoryVectorStager)

    async def initialize(self) -> None:
        await self.delegate.initialize()

    async def stage(self, chunks: Sequence[SearchChunk]) -> None:
        staged = tuple(chunks)
        if not staged:
            await self.delegate.stage(staged)
            return
        await self.delegate.stage(staged[:1])
        self.controller.hit("during_partial_qdrant_upsert")
        await self.delegate.stage(staged[1:])

    async def verify(self, version_id: str, expected_chunk_ids: Sequence[str]) -> None:
        await self.delegate.verify(version_id, expected_chunk_ids)

    async def prepare_activation(self, version_id: str) -> None:
        await self.delegate.prepare_activation(version_id)

    async def deactivate(self, version_id: str) -> None:
        await self.delegate.deactivate(version_id)


@dataclass(frozen=True, slots=True)
class RecoveryHarness:
    """Crash-aware ingestion stack backed by real access and SQLite persistence."""

    database: DomainDatabase
    access: AccessContext
    service: IngestionService
    artifacts: CrashArtifactStore
    extractor: CrashExtractor
    vectorizer: CrashVectorizer
    stager: CrashVectorStager
    controller: CrashController


async def _build_harness(settings: Settings) -> RecoveryHarness:
    database = DomainDatabase(settings.domain_database)
    access_service = AccessService(database, settings)
    await access_service.initialize()
    session = await access_service.activate_pm(settings.pm_bootstrap_token.get_secret_value())
    token = session.token.get_secret_value()
    engagement = await access_service.create_engagement(
        token,
        name="Crash recovery engagement",
        description="Synthetic process-interruption verification.",
        correlation_id="correlation-crash-engagement",
    )
    access = await access_service.resolve_pm_context(
        token,
        engagement.engagement_id,
        correlation_id="correlation-crash-ingestion",
        required_permission="document:upload",
    )
    controller = CrashController()
    repository = CrashRepository(
        database,
        lease_seconds=settings.ingestion_lease_seconds,
        controller=controller,
    )
    artifacts = CrashArtifactStore(
        settings.originals_root,
        settings.derived_root,
        controller=controller,
    )
    extractor = CrashExtractor(controller)
    vision = CrashVisionEnricher(controller)
    vectorizer = CrashVectorizer(controller)
    stager = CrashVectorStager(controller)
    lease_number = 0

    def lease_factory() -> str:
        nonlocal lease_number
        lease_number += 1
        return f"lease-crash-recovery-{lease_number}"

    service = IngestionService(
        settings=settings,
        repository=repository,
        validator=UploadValidator(settings),
        artifacts=artifacts,
        extractor=extractor,
        vision=vision,
        vectorizer=vectorizer,
        vector_stager=stager,
        clock=lambda: FIXED_NOW,
        lease_factory=lease_factory,
    )
    await service.initialize()
    return RecoveryHarness(
        database=database,
        access=access,
        service=service,
        artifacts=artifacts,
        extractor=extractor,
        vectorizer=vectorizer,
        stager=stager,
        controller=controller,
    )


async def _rows(
    database: DomainDatabase,
    statement: str,
    parameters: tuple[object, ...] = (),
) -> tuple[dict[str, object], ...]:
    async with database.connection() as connection:
        cursor = await connection.execute(statement, parameters)
        return tuple(dict(row) for row in await cursor.fetchall())


async def _expire_lease(database: DomainDatabase, version_id: str) -> None:
    async with database.transaction() as connection:
        await connection.execute(
            """
            UPDATE document_versions SET lease_expires_at = ?
            WHERE document_version_id = ?
            """,
            (EXPIRED_LEASE, version_id),
        )


def _integer(value: object) -> int:
    assert isinstance(value, int)
    return value


@pytest.mark.parametrize("boundary", BOUNDARIES)
async def test_process_crash_retry_is_atomic_and_duplicate_free(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    harness = await _build_harness(settings)
    baseline_content = FIXTURE.read_bytes()
    replacement_content = baseline_content + b"\n% crash-recovery-version-two\n"
    baseline = await harness.service.ingest(
        harness.access,
        filename=FIXTURE.name,
        declared_media_type=MEDIA_TYPE,
        content=baseline_content,
    )
    real_chunk_builder = build_chunk_seeds

    def crash_aware_chunk_builder(
        version: DocumentVersion,
        elements: Sequence[SourceElement],
        *,
        chunk_characters: int,
        overlap: int,
    ) -> tuple[ChunkSeed, ...]:
        result = real_chunk_builder(
            version,
            elements,
            chunk_characters=chunk_characters,
            overlap=overlap,
        )
        harness.controller.hit("during_chunk_materialization")
        return result

    monkeypatch.setattr(
        ingestion_service_module,
        "build_chunk_seeds",
        crash_aware_chunk_builder,
    )
    harness.controller.arm(boundary)

    with pytest.raises(SimulatedProcessCrash, match=boundary):
        await harness.service.ingest(
            harness.access,
            filename=FIXTURE.name,
            declared_media_type=MEDIA_TYPE,
            content=replacement_content,
        )
    assert harness.controller.observed == [boundary]

    version_rows = await _rows(
        harness.database,
        "SELECT * FROM document_versions ORDER BY version_number",
    )
    assert len(version_rows) == 2
    interrupted = version_rows[1]
    interrupted_id = str(interrupted["document_version_id"])
    response_was_lost = boundary == "after_activation_response_loss"
    expected_visible_id = (
        interrupted_id if response_was_lost else baseline.version.document_version_id
    )
    sqlite_active_ids = {
        str(row["document_version_id"])
        for row in version_rows
        if row["state"] == "READY" and _integer(row["is_active"]) == 1
    }
    vector_eligible_ids = {
        version_id for version_id, eligible in harness.stager.delegate.eligible.items() if eligible
    }
    assert sqlite_active_ids == {expected_visible_id}
    assert sqlite_active_ids & vector_eligible_ids == {expected_visible_id}
    if response_was_lost:
        assert interrupted["state"] == "READY"
        assert _integer(interrupted["is_active"]) == 1
    else:
        assert interrupted["state"] != "READY"
        assert _integer(interrupted["is_active"]) == 0
        active_replacement_chunks = await _rows(
            harness.database,
            """
            SELECT chunk_id FROM search_chunks
            WHERE source_version_id = ? AND is_active_ready = 1
            """,
            (interrupted_id,),
        )
        assert active_replacement_chunks == ()
        await _expire_lease(harness.database, interrupted_id)

    recovered = await harness.service.ingest(
        harness.access,
        filename=FIXTURE.name,
        declared_media_type=MEDIA_TYPE,
        content=replacement_content,
    )
    assert recovered.version.document_version_id == interrupted_id
    assert recovered.idempotent is response_was_lost
    calls_after_recovery = (
        harness.extractor.delegate.calls,
        harness.vectorizer.delegate.calls,
    )
    replay = await harness.service.ingest(
        harness.access,
        filename=FIXTURE.name,
        declared_media_type=MEDIA_TYPE,
        content=replacement_content,
    )
    assert replay.idempotent
    assert replay.version.document_version_id == interrupted_id
    assert {chunk.chunk_id for chunk in replay.chunks} == {
        chunk.chunk_id for chunk in recovered.chunks
    }
    assert calls_after_recovery == (
        harness.extractor.delegate.calls,
        harness.vectorizer.delegate.calls,
    )

    final_versions = await _rows(
        harness.database,
        """
        SELECT document_version_id, state, is_active
        FROM document_versions ORDER BY version_number
        """,
    )
    assert final_versions == (
        {
            "document_version_id": baseline.version.document_version_id,
            "state": "SUPERSEDED",
            "is_active": 0,
        },
        {
            "document_version_id": interrupted_id,
            "state": "READY",
            "is_active": 1,
        },
    )
    chunk_inventory = await _rows(
        harness.database,
        """
        SELECT COUNT(*) AS total, COUNT(DISTINCT chunk_id) AS distinct_total,
            SUM(CASE WHEN is_active_ready = 1 THEN 1 ELSE 0 END) AS active_total
        FROM search_chunks
        """,
    )
    assert _integer(chunk_inventory[0]["total"]) == _integer(chunk_inventory[0]["distinct_total"])
    assert _integer(chunk_inventory[0]["active_total"]) == len(recovered.chunks)
    recovered_chunk_ids = {chunk.chunk_id for chunk in recovered.chunks}
    assert set(harness.stager.delegate.points[interrupted_id]) == recovered_chunk_ids
    assert {
        version_id for version_id, eligible in harness.stager.delegate.eligible.items() if eligible
    } == {interrupted_id}

    original_rows = await _rows(
        harness.database,
        """
        SELECT virtual_path, content_hash FROM ingestion_artifacts
        WHERE document_version_id = ? AND artifact_kind = 'original'
        """,
        (interrupted_id,),
    )
    assert len(original_rows) == 1
    assert original_rows[0]["content_hash"] == sha256(replacement_content).hexdigest()
    original_path = harness.artifacts.resolve_virtual(
        harness.access,
        str(original_rows[0]["virtual_path"]),
    )
    assert original_path.read_bytes() == replacement_content

    attempts = await _rows(
        harness.database,
        """
        SELECT attempt_id, attempt_number, status, failure_code
        FROM ingestion_attempts
        WHERE document_version_id = ? ORDER BY attempt_number
        """,
        (interrupted_id,),
    )
    expected_attempts = 1 if response_was_lost else 2
    assert len(attempts) == expected_attempts
    if response_was_lost:
        assert attempts[0]["status"] == "succeeded"
        assert attempts[0]["failure_code"] is None
    else:
        assert attempts[0]["status"] == "failed"
        assert attempts[0]["failure_code"] == "INTERRUPTED_ATTEMPT"
        assert attempts[1]["status"] == "succeeded"
        assert attempts[1]["failure_code"] is None

    audit_rows = await _rows(
        harness.database,
        """
        SELECT event_id, status, failure_code FROM operational_audit_events
        WHERE actor = 'ingestion_service'
        """,
    )
    audits = {str(row["event_id"]): row for row in audit_rows}
    for attempt in attempts:
        attempt_id = str(attempt["attempt_id"])
        assert stable_id("audit", attempt_id, "started") in audits
        terminal_status = str(attempt["status"])
        terminal_audit = audits[stable_id("audit", attempt_id, terminal_status)]
        assert terminal_audit["status"] == terminal_status
        assert terminal_audit["failure_code"] == attempt["failure_code"]

    lifecycle_events = await _rows(
        harness.database,
        """
        SELECT to_state, attempt_id FROM document_version_events
        WHERE document_version_id = ? ORDER BY rowid
        """,
        (interrupted_id,),
    )
    assert lifecycle_events[-1]["to_state"] == "READY"
    if response_was_lost:
        assert all(event["to_state"] != "FAILED" for event in lifecycle_events)
    else:
        interrupted_events = [event for event in lifecycle_events if event["to_state"] == "FAILED"]
        assert len(interrupted_events) == 1
        assert interrupted_events[0]["attempt_id"] is None
