"""Immutable evidence registration and authorized original-source drill-down."""

from __future__ import annotations

import asyncio
from hashlib import sha256
from typing import TYPE_CHECKING

from stakeholder_intelligence_agent.errors import EvidenceRegistrationError
from stakeholder_intelligence_agent.retrieval.types import SourceDrillDown

if TYPE_CHECKING:
    from datetime import datetime

    from stakeholder_intelligence_agent.contracts.access import AccessContext
    from stakeholder_intelligence_agent.contracts.evidence import EvidenceRecord
    from stakeholder_intelligence_agent.ingestion.storage import IngestionArtifactStore
    from stakeholder_intelligence_agent.retrieval.repository import RetrievalRepository
    from stakeholder_intelligence_agent.retrieval.types import RetrievedItem


class EvidenceRegistry:
    """Bind retrieved chunks to immutable evidence and controlled source artifacts."""

    def __init__(
        self,
        repository: RetrievalRepository,
        artifacts: IngestionArtifactStore,
    ) -> None:
        self._repository = repository
        self._artifacts = artifacts

    async def register(
        self,
        access: AccessContext,
        *,
        run_id: str,
        topic_id: str,
        researcher_id: str,
        item: RetrievedItem,
        now: datetime,
    ) -> EvidenceRecord:
        """Persist one exact currently active candidate idempotently."""
        return await self._repository.register_evidence(
            access,
            run_id=run_id,
            topic_id=topic_id,
            researcher_id=researcher_id,
            item=item,
            now=now,
        )

    async def drill_down(
        self,
        access: AccessContext,
        evidence_id: str,
        *,
        now: datetime,
    ) -> SourceDrillDown:
        """Resolve and hash-check permitted virtual artifacts without exposing host paths."""
        evidence, original, related = await self._repository.source_artifacts(
            access,
            evidence_id,
            now=now,
        )
        if evidence.source_type != "interview":
            for reference in (original, *related):
                path = await asyncio.to_thread(
                    self._artifacts.resolve_virtual,
                    access,
                    reference.virtual_path,
                )
                content = await asyncio.to_thread(path.read_bytes)
                digest = sha256(content).hexdigest()
                if digest != reference.content_hash:
                    raise EvidenceRegistrationError
        return SourceDrillDown(
            evidence=evidence,
            original=original,
            related_artifacts=related,
        )
