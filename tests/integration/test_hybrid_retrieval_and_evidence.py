"""Real SQLite/Qdrant retrieval atomicity, isolation, evidence, and drill-down tests."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from blockbuster import blockbuster_ctx
from qdrant_client import AsyncQdrantClient
from qdrant_client.http import models

from stakeholder_intelligence_agent.access import AccessService
from stakeholder_intelligence_agent.contracts.retrieval import (
    RetrievalCandidate,
    RetrievalMetadata,
)
from stakeholder_intelligence_agent.contracts.source import BoundingBox, PdfPageLocation
from stakeholder_intelligence_agent.errors import EvidenceRegistrationError
from stakeholder_intelligence_agent.ingestion.qdrant import QdrantVectorStager
from stakeholder_intelligence_agent.ingestion.repository import IngestionRepository
from stakeholder_intelligence_agent.ingestion.service import IngestionService
from stakeholder_intelligence_agent.ingestion.storage import IngestionArtifactStore
from stakeholder_intelligence_agent.ingestion.validation import UploadValidator
from stakeholder_intelligence_agent.persistence import DomainDatabase
from stakeholder_intelligence_agent.retrieval import (
    EvidenceRegistry,
    HybridRetrievalService,
    QdrantHybridSearcher,
    RetrievalRepository,
)
from stakeholder_intelligence_agent.retrieval.types import RetrievedItem
from tests.fakes import (
    DeterministicDocumentExtractor,
    DeterministicReranker,
    DeterministicVectorizer,
    DeterministicVisionEnricher,
    StaticFilterExtractor,
)

if TYPE_CHECKING:
    from stakeholder_intelligence_agent.config import Settings
    from stakeholder_intelligence_agent.contracts.access import AccessContext
    from stakeholder_intelligence_agent.contracts.source import SearchChunk

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).parents[1] / "fixtures" / "ingestion"


@dataclass(slots=True)
class RetrievalHarness:
    settings: Settings
    database: DomainDatabase
    access: AccessService
    alpha_pm: AccessContext
    alpha_stakeholder: AccessContext
    ingestion: IngestionService
    repository: RetrievalRepository
    qdrant: AsyncQdrantClient
    vector_stager: QdrantVectorStager


async def _harness(settings: Settings) -> RetrievalHarness:
    settings = settings.model_copy(update={"gemini_embedding_dimension": 128})
    database = DomainDatabase(settings.domain_database)
    access = AccessService(database, settings)
    await access.initialize()
    pm_session = await access.activate_pm(settings.pm_bootstrap_token.get_secret_value())
    pm_token = pm_session.token.get_secret_value()
    engagement = await access.create_engagement(
        pm_token,
        name="Alpha retrieval",
        description="Synthetic hybrid retrieval engagement.",
        correlation_id="retrieval-alpha",
    )
    stakeholder = await access.create_stakeholder(
        pm_token,
        engagement.engagement_id,
        display_name="Alex Morgan",
        role="Operations manager",
        department="Operations",
        correlation_id="retrieval-alpha-stakeholder",
    )
    invitation = await access.issue_invitation(
        pm_token,
        engagement.engagement_id,
        stakeholder.stakeholder_id,
        correlation_id="retrieval-alpha-invitation",
    )
    activated = await access.activate_invitation(
        invitation.token.get_secret_value(),
        correlation_id="retrieval-alpha-activation",
    )
    alpha_stakeholder = await access.resolve_stakeholder_context(
        activated.access_session.token.get_secret_value(),
        correlation_id="retrieval-alpha-stakeholder-context",
        required_permission="source:read",
    )
    alpha_pm = await access.resolve_pm_context(
        pm_token,
        engagement.engagement_id,
        correlation_id="retrieval-alpha-pm-context",
        required_permission="source:read",
        thread_id="retrieval-alpha-thread",
    )
    qdrant = AsyncQdrantClient(location=":memory:")
    vector_stager = QdrantVectorStager(settings, client=qdrant)
    ingestion = IngestionService(
        settings=settings,
        repository=IngestionRepository(
            database,
            lease_seconds=settings.ingestion_lease_seconds,
        ),
        validator=UploadValidator(settings),
        artifacts=IngestionArtifactStore(settings.originals_root, settings.derived_root),
        extractor=DeterministicDocumentExtractor(),
        vision=DeterministicVisionEnricher(),
        vectorizer=DeterministicVectorizer(),
        vector_stager=vector_stager,
    )
    await ingestion.initialize()
    repository = RetrievalRepository(database)
    await repository.initialize()
    return RetrievalHarness(
        settings=settings,
        database=database,
        access=access,
        alpha_pm=alpha_pm,
        alpha_stakeholder=alpha_stakeholder,
        ingestion=ingestion,
        repository=repository,
        qdrant=qdrant,
        vector_stager=vector_stager,
    )


async def _beta_pm(harness: RetrievalHarness) -> AccessContext:
    session = await harness.access.activate_pm(
        harness.settings.pm_bootstrap_token.get_secret_value()
    )
    token = session.token.get_secret_value()
    engagement = await harness.access.create_engagement(
        token,
        name="Beta retrieval",
        description=None,
        correlation_id="retrieval-beta",
    )
    return await harness.access.resolve_pm_context(
        token,
        engagement.engagement_id,
        correlation_id="retrieval-beta-context",
        required_permission="source:read",
        thread_id="retrieval-beta-thread",
    )


def _retrieval_service(harness: RetrievalHarness) -> HybridRetrievalService:
    return HybridRetrievalService(
        settings=harness.settings,
        repository=harness.repository,
        filter_extractor=StaticFilterExtractor(),
        vectorizer=DeterministicVectorizer(),
        search_backend=QdrantHybridSearcher(harness.settings, client=harness.qdrant),
        reranker=DeterministicReranker(),
    )


async def test_real_qdrant_search_requires_sqlite_active_version_and_engagement_scope(
    settings: Settings,
) -> None:
    harness = await _harness(settings)
    try:
        first = await harness.ingestion.ingest(
            harness.alpha_pm,
            filename="risk-map.png",
            declared_media_type="image/png",
            content=(FIXTURES / "alpha-organization-chart.png").read_bytes(),
        )
        beta_pm = await _beta_pm(harness)
        beta = await harness.ingestion.ingest(
            beta_pm,
            filename="beta-process-map.jpg",
            declared_media_type="image/jpeg",
            content=(FIXTURES / "beta-process-map.jpg").read_bytes(),
        )
        service = _retrieval_service(harness)

        alpha_result = await service.retrieve(
            harness.alpha_stakeholder,
            first.chunks[0].text_for_retrieval,
        )
        beta_result = await service.retrieve(beta_pm, beta.chunks[0].text_for_retrieval)

        assert alpha_result.trace.rrf_chunk_ids
        assert alpha_result.trace.fusion_method == "qdrant_native_rrf"
        assert beta_result.items
        assert all(
            item.candidate.metadata.engagement_id == harness.alpha_pm.engagement_id
            for item in alpha_result.items
        )
        assert all(
            item.source_version_id != beta.version.document_version_id
            for item in alpha_result.items
        )
        assert all(
            item.candidate.metadata.engagement_id == beta_pm.engagement_id
            for item in beta_result.items
        )

        replacement = await harness.ingestion.ingest(
            harness.alpha_pm,
            filename="risk-map.png",
            declared_media_type="image/png",
            content=(FIXTURES / "alpha-influence-chart.png").read_bytes(),
        )
        await harness.qdrant.set_payload(
            collection_name=harness.settings.qdrant_collection,
            payload={"is_active_ready": True, "vector_stage_state": "PREPARED"},
            points=models.Filter(
                must=[
                    models.FieldCondition(
                        key="source_version_id",
                        match=models.MatchValue(value=first.version.document_version_id),
                    )
                ]
            ),
            wait=True,
        )
        stale_probe = await service.retrieve(
            harness.alpha_pm,
            first.chunks[0].text_for_retrieval,
        )

        assert replacement.version.document_version_id != first.version.document_version_id
        assert all(
            item.source_version_id != first.version.document_version_id
            for item in stale_probe.items
        )
        assert all(
            item.source_version_id == replacement.version.document_version_id
            for item in stale_probe.items
        )
    finally:
        await harness.qdrant.close()


def _retrieved_item(chunk: SearchChunk) -> RetrievedItem:
    metadata = RetrievalMetadata(
        engagement_id=chunk.engagement_id,
        stakeholder_id=chunk.stakeholder_id,
        role=chunk.role,
        department=chunk.department,
        doc_type=chunk.doc_type,
        source_type=chunk.source_type,
        source_version_state="READY",
        is_active_ready=True,
    )
    return RetrievedItem(
        candidate=RetrievalCandidate(
            chunk_id=chunk.chunk_id,
            hybrid_rank=1,
            rrf_score=1 / 61 + 1 / 61,
            reranker_score=1.0,
            final_rank=1,
            source_preview=chunk.text_for_retrieval,
            location=chunk.location,
            metadata=metadata,
        ),
        source_id=chunk.source_id,
        source_version_id=chunk.source_version_id,
        element_ids=chunk.element_ids,
        original_excerpt=chunk.text_for_retrieval,
    )


async def test_evidence_registration_uses_authoritative_location_after_float_round_trip(
    settings: Settings,
) -> None:
    harness = await _harness(settings)
    try:
        ingested = await harness.ingestion.ingest(
            harness.alpha_pm,
            filename="bounded-location.pdf",
            declared_media_type="application/pdf",
            content=(FIXTURES / "alpha-mixed-content.pdf").read_bytes(),
        )
        chunk = ingested.chunks[0]
        authoritative = PdfPageLocation(
            filename="bounded-location.pdf",
            page=1,
            bounding_box=BoundingBox(
                x0=83.70074462890625,
                y0=127.33367919921875,
                x1=527.9000244140625,
                y1=405.3650207519531,
                coordinate_space="points",
            ),
        )
        async with harness.database.connection() as connection:
            await connection.execute(
                "UPDATE search_chunks SET location_json = ? WHERE chunk_id = ?",
                (json.dumps(authoritative.model_dump(mode="json")), chunk.chunk_id),
            )
        bounding_box = authoritative.bounding_box
        assert bounding_box is not None
        round_tripped = authoritative.model_copy(
            update={"bounding_box": bounding_box.model_copy(update={"y0": 127.33367919921876})}
        )
        item = _retrieved_item(chunk)
        item = replace(
            item,
            candidate=item.candidate.model_copy(update={"location": round_tripped}),
        )

        record = await harness.repository.register_evidence(
            harness.alpha_pm,
            run_id="run-location-round-trip",
            topic_id="topic-location-round-trip",
            researcher_id="researcher-location-round-trip",
            item=item,
            now=datetime(2026, 7, 15, 13, 59, tzinfo=UTC),
        )

        assert record.location == authoritative
    finally:
        await harness.qdrant.close()


async def test_evidence_registration_is_exact_append_only_idempotent_and_drillable(
    settings: Settings,
) -> None:
    harness = await _harness(settings)
    try:
        source_bytes = (FIXTURES / "alpha-organization-chart.png").read_bytes()
        ingested = await harness.ingestion.ingest(
            harness.alpha_pm,
            filename="evidence-map.png",
            declared_media_type="image/png",
            content=source_bytes,
        )
        item = _retrieved_item(ingested.chunks[0])
        registry = EvidenceRegistry(
            harness.repository,
            IngestionArtifactStore(
                harness.settings.originals_root,
                harness.settings.derived_root,
            ),
        )
        first_time = datetime(2026, 7, 15, 14, 0, tzinfo=UTC)
        first = await registry.register(
            harness.alpha_pm,
            run_id="run-a",
            topic_id="topic-a",
            researcher_id="researcher-a",
            item=item,
            now=first_time,
        )
        second = await registry.register(
            harness.alpha_pm,
            run_id="run-a",
            topic_id="topic-a",
            researcher_id="researcher-a",
            item=item,
            now=datetime(2026, 7, 15, 14, 1, tzinfo=UTC),
        )
        with blockbuster_ctx(scanned_modules=["stakeholder_intelligence_agent.retrieval.evidence"]):
            drill_down = await registry.drill_down(
                harness.alpha_pm,
                first.evidence_id,
                now=datetime(2026, 7, 15, 14, 2, tzinfo=UTC),
            )

        assert second == first
        assert first.content_hash == sha256(item.original_excerpt.encode()).hexdigest()
        assert drill_down.original.virtual_path.startswith(
            f"originals/{harness.alpha_pm.engagement_id}/"
        )
        assert drill_down.original.content_hash == sha256(source_bytes).hexdigest()
        assert all(":" not in artifact.virtual_path for artifact in drill_down.related_artifacts)

        tampered = RetrievedItem(
            candidate=item.candidate,
            source_id=item.source_id,
            source_version_id=item.source_version_id,
            element_ids=item.element_ids,
            original_excerpt="Tampered excerpt.",
        )
        with pytest.raises(EvidenceRegistrationError):
            await registry.register(
                harness.alpha_pm,
                run_id="run-a",
                topic_id="topic-a",
                researcher_id="researcher-a",
                item=tampered,
                now=datetime(2026, 7, 15, 14, 3, tzinfo=UTC),
            )

        beta_pm = await _beta_pm(harness)
        with pytest.raises(EvidenceRegistrationError):
            await registry.drill_down(
                beta_pm,
                first.evidence_id,
                now=datetime(2026, 7, 15, 14, 4, tzinfo=UTC),
            )

        async with harness.database.connection() as connection:
            with pytest.raises(sqlite3.IntegrityError):
                await connection.execute(
                    "UPDATE evidence_records SET topic_id = 'changed' WHERE evidence_id = ?",
                    (first.evidence_id,),
                )
            with pytest.raises(sqlite3.IntegrityError):
                await connection.execute(
                    "DELETE FROM evidence_records WHERE evidence_id = ?",
                    (first.evidence_id,),
                )

        await harness.ingestion.ingest(
            harness.alpha_pm,
            filename="evidence-map.png",
            declared_media_type="image/png",
            content=(FIXTURES / "alpha-influence-chart.png").read_bytes(),
        )
        with pytest.raises(EvidenceRegistrationError):
            await registry.drill_down(
                harness.alpha_pm,
                first.evidence_id,
                now=datetime(2026, 7, 15, 14, 5, tzinfo=UTC),
            )
    finally:
        await harness.qdrant.close()
