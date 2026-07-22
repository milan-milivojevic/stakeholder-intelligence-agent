"""Process-local providers for the single Agent Server business backend."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from functools import lru_cache
from typing import TYPE_CHECKING

from stakeholder_intelligence_agent.access import AccessService
from stakeholder_intelligence_agent.api.read_repository import DomainReadRepository
from stakeholder_intelligence_agent.api.runtime import ApplicationServices
from stakeholder_intelligence_agent.artifacts import ScopedArtifactStore
from stakeholder_intelligence_agent.config import get_settings
from stakeholder_intelligence_agent.ingestion import IngestionService
from stakeholder_intelligence_agent.ingestion.adapters import GeminiBm25Vectorizer
from stakeholder_intelligence_agent.ingestion.qdrant import QdrantVectorStager
from stakeholder_intelligence_agent.ingestion.storage import IngestionArtifactStore
from stakeholder_intelligence_agent.insight import (
    InsightExecutionService,
    InsightGraphDependencies,
    InsightRunRepository,
    build_insight_graph,
)
from stakeholder_intelligence_agent.interview import (
    InterviewConversationService,
    TranscriptIngestionService,
    TranscriptRepository,
    build_interview_graph,
)
from stakeholder_intelligence_agent.persistence import DomainDatabase
from stakeholder_intelligence_agent.persistence.checkpointer import open_sqlite_checkpointer
from stakeholder_intelligence_agent.retrieval import (
    EvidenceRegistry,
    RetrievalRepository,
    build_production_retrieval,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from typing import Any

    from langgraph.checkpoint.base import BaseCheckpointSaver

    from stakeholder_intelligence_agent.config import Settings


@lru_cache(maxsize=1)
def get_domain_database() -> DomainDatabase:
    """Return the separate migrated domain database provider."""
    return DomainDatabase(get_settings().domain_database)


@lru_cache(maxsize=1)
def get_access_service() -> AccessService:
    """Return the process-local access service with server-owned configuration."""
    return AccessService(get_domain_database(), get_settings())


def _assemble_application_services(
    settings: Settings,
    database: DomainDatabase,
    access: AccessService,
    saver: BaseCheckpointSaver[Any],
) -> ApplicationServices:
    """Build synchronous clients, stores, models, and graphs outside the ASGI event loop."""
    ingestion = IngestionService.from_settings(settings)
    retrieval = build_production_retrieval(settings)
    source_artifacts = IngestionArtifactStore(settings.originals_root, settings.derived_root)
    agent_artifacts = ScopedArtifactStore(settings.agent_artifacts_root)
    evidence_repository = RetrievalRepository(database)
    evidence = EvidenceRegistry(evidence_repository, source_artifacts)
    transcript_repository = TranscriptRepository(
        database,
        lease_seconds=settings.ingestion_lease_seconds,
    )
    transcript_ingestion = TranscriptIngestionService(
        settings=settings,
        repository=transcript_repository,
        vectorizer=GeminiBm25Vectorizer(settings),
        vector_stager=QdrantVectorStager(settings),
    )
    insight_runs = InsightRunRepository(database)
    interview_graph = build_interview_graph(
        settings,
        checkpointer=saver,
        retrieval_service=retrieval,
    )
    interview = InterviewConversationService(
        repository=transcript_repository,
        graph=interview_graph,
        checkpointer=saver,
        ingestion=transcript_ingestion,
    )
    insight_graph = build_insight_graph(
        settings,
        dependencies=InsightGraphDependencies(
            checkpointer=saver,
            retrieval_service=retrieval,
            evidence_repository=evidence_repository,
            run_repository=insight_runs,
        ),
    )
    insight = InsightExecutionService(
        graph=insight_graph,
        repository=insight_runs,
        artifacts=agent_artifacts,
        settings=settings,
    )
    return ApplicationServices(
        settings=settings,
        database=database,
        access=access,
        reads=DomainReadRepository(database),
        ingestion=ingestion,
        transcript_repository=transcript_repository,
        interview=interview,
        insight=insight,
        insight_runs=insight_runs,
        evidence=evidence,
        source_artifacts=source_artifacts,
        agent_artifacts=agent_artifacts,
        retrieval=retrieval,
    )


@asynccontextmanager
async def open_application_services() -> AsyncIterator[ApplicationServices]:
    """Assemble the one production service graph for custom Agent Server routes."""
    settings, database, access = await asyncio.to_thread(
        lambda: (get_settings(), get_domain_database(), get_access_service())
    )

    async with open_sqlite_checkpointer(settings.checkpoint_database) as saver:
        services = await asyncio.to_thread(
            _assemble_application_services,
            settings,
            database,
            access,
            saver,
        )
        await services.initialize()
        yield services
