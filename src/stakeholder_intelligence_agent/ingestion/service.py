"""One authorized ingestion service shared by PM and stakeholder upload contexts."""

from __future__ import annotations

import asyncio
import contextlib
import json
import secrets
import sqlite3
from hashlib import sha256
from importlib.metadata import version as package_version
from typing import TYPE_CHECKING

from stakeholder_intelligence_agent.contracts.common import utc_now
from stakeholder_intelligence_agent.errors import (
    AccessDeniedError,
    CorruptSourceError,
    EnrichmentFailedError,
    ExtractionFailedError,
    IndexingFailedError,
    IngestionAuthorizationError,
    IngestionError,
    MandatoryContentMissingError,
    StakeholderIntelligenceError,
)
from stakeholder_intelligence_agent.ingestion.identity import stable_document_key, stable_id
from stakeholder_intelligence_agent.ingestion.normalization import (
    build_chunk_seeds,
    materialize_chunks,
    materialize_elements,
)
from stakeholder_intelligence_agent.ingestion.types import (
    ArtifactDraft,
    ElementDraft,
    ExtractionBundle,
    IngestionResult,
    StoredArtifact,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from stakeholder_intelligence_agent.config import Settings
    from stakeholder_intelligence_agent.contracts.access import AccessContext
    from stakeholder_intelligence_agent.ingestion.repository import IngestionRepository
    from stakeholder_intelligence_agent.ingestion.storage import IngestionArtifactStore
    from stakeholder_intelligence_agent.ingestion.types import (
        DocumentExtractor,
        ValidatedUpload,
        Vectorizer,
        VectorStager,
        VisionEnricher,
    )
    from stakeholder_intelligence_agent.ingestion.validation import UploadValidator


class IngestionService:
    """Coordinate validation, conversion, enrichment, vectors, and atomic activation."""

    def __init__(
        self,
        *,
        settings: Settings,
        repository: IngestionRepository,
        validator: UploadValidator,
        artifacts: IngestionArtifactStore,
        extractor: DocumentExtractor,
        vision: VisionEnricher,
        vectorizer: Vectorizer,
        vector_stager: VectorStager,
        clock: Callable[[], datetime] = utc_now,
        lease_factory: Callable[[], str] | None = None,
    ) -> None:
        self._settings = settings
        self._repository = repository
        self._validator = validator
        self._artifacts = artifacts
        self._extractor = extractor
        self._vision = vision
        self._vectorizer = vectorizer
        self._vector_stager = vector_stager
        self._clock = clock
        self._lease_factory = lease_factory or (lambda: secrets.token_urlsafe(32))
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    @classmethod
    def from_settings(cls, settings: Settings) -> IngestionService:
        """Construct the production Gemini, Docling, SQLite, filesystem, and Qdrant path."""
        from stakeholder_intelligence_agent.ingestion.adapters import (
            GeminiBm25Vectorizer,
            GeminiVisionEnricher,
        )
        from stakeholder_intelligence_agent.ingestion.docling_adapter import DoclingExtractor
        from stakeholder_intelligence_agent.ingestion.qdrant import QdrantVectorStager
        from stakeholder_intelligence_agent.ingestion.repository import IngestionRepository
        from stakeholder_intelligence_agent.ingestion.storage import IngestionArtifactStore
        from stakeholder_intelligence_agent.ingestion.validation import UploadValidator
        from stakeholder_intelligence_agent.persistence import DomainDatabase

        return cls(
            settings=settings,
            repository=IngestionRepository(
                DomainDatabase(settings.domain_database),
                lease_seconds=settings.ingestion_lease_seconds,
            ),
            validator=UploadValidator(settings),
            artifacts=IngestionArtifactStore(settings.originals_root, settings.derived_root),
            extractor=DoclingExtractor(settings),
            vision=GeminiVisionEnricher(settings),
            vectorizer=GeminiBm25Vectorizer(settings),
            vector_stager=QdrantVectorStager(settings),
        )

    async def initialize(self) -> None:
        """Initialize domain migrations and the one approved Qdrant collection."""
        await self._repository.initialize()
        await self._vector_stager.initialize()

    async def ingest(
        self,
        access: AccessContext,
        *,
        filename: str,
        declared_media_type: str,
        content: bytes,
    ) -> IngestionResult:
        """Ingest one immutable upload through the shared authorized pipeline."""
        upload = self._validator.validate_envelope(
            filename=filename,
            declared_media_type=declared_media_type,
            content=content,
        )
        source_type = (
            "stakeholder_document"
            if access.principal_type == "stakeholder"
            else "engagement_document"
        )
        document_key = stable_document_key(
            access.engagement_id,
            access.stakeholder_id or "-",
            source_type,
            upload.normalized_filename,
        )
        lock = await self._lock_for(stable_id("document", document_key))
        async with lock:
            return await self._ingest_locked(access, upload)

    async def delete_stakeholder_document(
        self,
        access: AccessContext,
        document_id: str,
    ) -> None:
        """Withdraw one owned document from inventories and active retrieval."""
        lock = await self._lock_for(document_id)
        async with lock:
            active_version_ids = await self._repository.withdraw_stakeholder_document(
                access,
                document_id,
                now=self._clock(),
            )
            for version_id in active_version_ids:
                with contextlib.suppress(IngestionError):
                    await self._vector_stager.deactivate(version_id)

    async def delete_pm_document(
        self,
        access: AccessContext,
        document_id: str,
    ) -> None:
        """Withdraw one PM-owned engagement document from inventories and retrieval."""
        lock = await self._lock_for(document_id)
        async with lock:
            active_version_ids = await self._repository.withdraw_pm_document(
                access,
                document_id,
                now=self._clock(),
            )
            for version_id in active_version_ids:
                with contextlib.suppress(IngestionError):
                    await self._vector_stager.deactivate(version_id)

    async def _ingest_locked(
        self,
        access: AccessContext,
        upload: ValidatedUpload,
    ) -> IngestionResult:
        lease_token = self._lease_factory()
        start = await self._repository.start(
            access,
            upload,
            lease_token=lease_token,
            now=self._clock(),
        )
        if start.idempotent_ready:
            source, version, elements, chunks = await self._repository.load_ready_result(
                access,
                start.version.document_version_id,
                now=self._clock(),
            )
            superseded_ids = await self._repository.superseded_version_ids(
                access,
                source.document_id,
                now=self._clock(),
            )
            await self._deactivate_versions(superseded_ids)
            return IngestionResult(
                source=source,
                version=version,
                elements=elements,
                chunks=chunks,
                attempt_id=None,
                idempotent=True,
            )
        if start.attempt_id is None or start.lease_token is None:
            raise RuntimeError("A claimed ingestion version is missing its attempt lease.")

        version_id = start.version.document_version_id
        attempt_id = start.attempt_id
        phase = "extracting"
        staged = False
        new_prepared = False
        prior_active_ids: tuple[str, ...] = ()
        try:
            original, source_path = await asyncio.to_thread(
                self._artifacts.write_original,
                access,
                document_id=start.source.document_id,
                document_version_id=version_id,
                artifact_id=start.version.original_artifact_id,
                upload=upload,
            )
            await self._repository.record_artifact(
                access,
                version_id=version_id,
                lease_token=lease_token,
                artifact=original,
                now=self._clock(),
            )
            await self._repository.transition(
                access,
                version_id=version_id,
                attempt_id=attempt_id,
                lease_token=lease_token,
                state="VALIDATING",
                now=self._clock(),
            )
            self._validator.validate_structure(upload)
            await self._repository.transition(
                access,
                version_id=version_id,
                attempt_id=attempt_id,
                lease_token=lease_token,
                state="EXTRACTING",
                now=self._clock(),
            )
            bundle = await asyncio.to_thread(self._extractor.extract, source_path, upload)

            phase = "enriching"
            await self._repository.transition(
                access,
                version_id=version_id,
                attempt_id=attempt_id,
                lease_token=lease_token,
                state="ENRICHING",
                now=self._clock(),
            )
            bundle = self._with_extraction_manifest(bundle, upload)
            derived = await self._persist_derived(
                access,
                document_id=start.source.document_id,
                version_id=version_id,
                drafts=bundle.artifacts,
            )
            enriched_drafts = await self._enrich_visuals(
                upload,
                bundle.elements,
                bundle.artifacts,
            )
            elements = materialize_elements(
                start.version,
                enriched_drafts,
                original_artifact=original,
                derived_artifacts=derived,
            )

            phase = "indexing"
            await self._repository.transition(
                access,
                version_id=version_id,
                attempt_id=attempt_id,
                lease_token=lease_token,
                state="INDEXING",
                now=self._clock(),
            )
            seeds = build_chunk_seeds(
                start.version,
                elements,
                chunk_characters=self._settings.ingestion_chunk_characters,
                overlap=self._settings.ingestion_chunk_overlap,
            )
            vectors = await self._vectorizer.vectorize(tuple(seed.text for seed in seeds))
            chunks = materialize_chunks(start.source, start.version, seeds, vectors)
            all_artifacts = (original, *derived.values())
            await self._repository.replace_payload(
                access,
                version_id=version_id,
                lease_token=lease_token,
                artifacts=all_artifacts,
                elements=elements,
                chunks=chunks,
                now=self._clock(),
            )
            await self._vector_stager.initialize()
            await self._vector_stager.stage(chunks)
            staged = True
            await self._vector_stager.verify(
                version_id,
                tuple(chunk.chunk_id for chunk in chunks),
            )
            prior_active_ids = await self._repository.active_version_ids(
                access,
                start.source.document_id,
                now=self._clock(),
            )
            await self._vector_stager.prepare_activation(version_id)
            new_prepared = True
            await self._repository.mark_vectors_prepared(
                version_id=version_id,
                lease_token=lease_token,
                now=self._clock(),
            )
            activation = await self._repository.activate(
                access,
                version_id=version_id,
                attempt_id=attempt_id,
                lease_token=lease_token,
                now=self._clock(),
            )
            await self._deactivate_versions(activation.superseded_version_ids)
            return IngestionResult(
                source=start.source,
                version=activation.version,
                elements=elements,
                chunks=activation.chunks,
                attempt_id=attempt_id,
                idempotent=False,
            )
        except AccessDeniedError:
            authorization_failure = IngestionAuthorizationError()
            await self._compensate_vectors(
                version_id,
                staged=staged,
                new_prepared=new_prepared,
                prior_active_ids=prior_active_ids,
            )
            await self._record_failure(
                access,
                version_id=version_id,
                attempt_id=attempt_id,
                lease_token=lease_token,
                failure=authorization_failure,
            )
            raise
        except IngestionError as error:
            await self._compensate_vectors(
                version_id,
                staged=staged,
                new_prepared=new_prepared,
                prior_active_ids=prior_active_ids,
            )
            await self._record_failure(
                access,
                version_id=version_id,
                attempt_id=attempt_id,
                lease_token=lease_token,
                failure=error,
            )
            raise
        except Exception as error:
            failure = self._phase_failure(phase)
            await self._compensate_vectors(
                version_id,
                staged=staged,
                new_prepared=new_prepared,
                prior_active_ids=prior_active_ids,
            )
            await self._record_failure(
                access,
                version_id=version_id,
                attempt_id=attempt_id,
                lease_token=lease_token,
                failure=failure,
            )
            raise failure from error

    async def _persist_derived(
        self,
        access: AccessContext,
        *,
        document_id: str,
        version_id: str,
        drafts: tuple[ArtifactDraft, ...],
    ) -> dict[str, StoredArtifact]:
        if len({draft.key for draft in drafts}) != len(drafts):
            raise MandatoryContentMissingError
        stored: dict[str, StoredArtifact] = {}
        for draft in drafts:
            digest = sha256(draft.content).hexdigest()
            artifact_id = stable_id("artifact", version_id, draft.key, digest)
            stored[draft.key] = await asyncio.to_thread(
                self._artifacts.write_derived,
                access,
                document_id=document_id,
                document_version_id=version_id,
                artifact_id=artifact_id,
                draft=draft,
            )
        return stored

    async def _enrich_visuals(
        self,
        upload: ValidatedUpload,
        elements: tuple[ElementDraft, ...],
        artifacts: tuple[ArtifactDraft, ...],
    ) -> tuple[ElementDraft, ...]:
        artifact_map = {artifact.key: artifact for artifact in artifacts}
        enriched = list(elements)
        for element in elements:
            if element.element_type not in {"image", "chart"}:
                continue
            if element.artifact_key == "$original":
                content = upload.content
                media_type = upload.media_type
            else:
                artifact = artifact_map.get(element.artifact_key or "")
                if artifact is None:
                    raise MandatoryContentMissingError
                content = artifact.content
                media_type = artifact.media_type
            description = await self._vision.describe(
                content=content,
                media_type=media_type,
                filename=upload.filename,
                location=element.location,
            )
            enriched.append(
                ElementDraft(
                    key=f"{element.key}:vision",
                    element_type="vision_description",
                    original_content=description,
                    location=element.location,
                    extraction_method="gemini_vision_v1",
                    parent_key=element.key,
                )
            )
        return tuple(enriched)

    @staticmethod
    def _with_extraction_manifest(
        bundle: ExtractionBundle,
        upload: ValidatedUpload,
    ) -> ExtractionBundle:
        payload = {
            "content_hash": upload.content_hash,
            "document_type": upload.document_type,
            "docling_version": package_version("docling"),
            "capability_facts": list(bundle.capability_facts),
        }
        manifest = ArtifactDraft(
            key="extraction-capability-manifest",
            artifact_kind="extraction_manifest",
            media_type="application/json",
            suffix=".json",
            content=(json.dumps(payload, indent=2, sort_keys=True) + "\n").encode(),
        )
        return ExtractionBundle(
            elements=bundle.elements,
            artifacts=(*bundle.artifacts, manifest),
            capability_facts=bundle.capability_facts,
        )

    async def _compensate_vectors(
        self,
        version_id: str,
        *,
        staged: bool,
        new_prepared: bool,
        prior_active_ids: tuple[str, ...],
    ) -> None:
        if staged or new_prepared:
            with contextlib.suppress(IngestionError):
                await self._vector_stager.deactivate(version_id)
        for active_version_id in prior_active_ids:
            try:
                await self._vector_stager.prepare_activation(active_version_id)
            except IngestionError:
                continue

    async def _deactivate_versions(self, version_ids: tuple[str, ...]) -> None:
        """Best-effort cleanup after SQLite has made obsolete versions unreachable."""
        for version_id in version_ids:
            with contextlib.suppress(IngestionError):
                await self._vector_stager.deactivate(version_id)

    async def _record_failure(
        self,
        access: AccessContext,
        *,
        version_id: str,
        attempt_id: str,
        lease_token: str,
        failure: IngestionError,
    ) -> None:
        try:
            await self._repository.fail(
                access,
                version_id=version_id,
                attempt_id=attempt_id,
                lease_token=lease_token,
                failure=failure,
                now=self._clock(),
            )
        except (
            OSError,
            RuntimeError,
            StakeholderIntelligenceError,
            ValueError,
            sqlite3.Error,
        ):
            return

    async def _lock_for(self, key: str) -> asyncio.Lock:
        async with self._locks_guard:
            return self._locks.setdefault(key, asyncio.Lock())

    @staticmethod
    def _phase_failure(phase: str) -> IngestionError:
        if phase == "enriching":
            return EnrichmentFailedError()
        if phase == "indexing":
            return IndexingFailedError()
        if phase == "validating":
            return CorruptSourceError()
        return ExtractionFailedError()
