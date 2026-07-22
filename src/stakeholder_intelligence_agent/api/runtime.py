"""Lightweight custom-route runtime contract without production adapter imports."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from stakeholder_intelligence_agent.access.tokens import generate_opaque_id

if TYPE_CHECKING:
    from collections.abc import Callable

    from stakeholder_intelligence_agent.access import AccessService
    from stakeholder_intelligence_agent.api.read_repository import DomainReadRepository
    from stakeholder_intelligence_agent.artifacts import ScopedArtifactStore
    from stakeholder_intelligence_agent.config import Settings
    from stakeholder_intelligence_agent.ingestion import IngestionService
    from stakeholder_intelligence_agent.ingestion.storage import IngestionArtifactStore
    from stakeholder_intelligence_agent.insight import (
        InsightExecutionService,
        InsightRunRepository,
    )
    from stakeholder_intelligence_agent.interview import (
        InterviewConversationService,
        TranscriptRepository,
    )
    from stakeholder_intelligence_agent.persistence import DomainDatabase
    from stakeholder_intelligence_agent.retrieval import (
        EvidenceRegistry,
        HybridRetrievalService,
    )


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class ApplicationServices:
    """One dependency set shared by every approved custom route."""

    settings: Settings
    database: DomainDatabase
    access: AccessService
    reads: DomainReadRepository
    ingestion: IngestionService
    transcript_repository: TranscriptRepository
    interview: InterviewConversationService
    insight: InsightExecutionService
    insight_runs: InsightRunRepository
    evidence: EvidenceRegistry
    source_artifacts: IngestionArtifactStore
    agent_artifacts: ScopedArtifactStore
    retrieval: HybridRetrievalService
    id_factory: Callable[[str], str] = generate_opaque_id
    clock: Callable[[], datetime] = _utc_now

    async def initialize(self) -> None:
        """Prepare all route-owned persistence and shared service boundaries."""
        await self.access.initialize()
        await self.reads.initialize()
        await self.ingestion.initialize()
        await self.retrieval.initialize()
        await self.interview.initialize()
        await self.insight.initialize()
