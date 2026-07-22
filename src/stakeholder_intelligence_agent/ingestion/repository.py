"""Transactional SQLite repository for versioned document ingestion."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final, cast

from pydantic import TypeAdapter

from stakeholder_intelligence_agent.contracts.lifecycle import (
    validate_document_version_transition,
)
from stakeholder_intelligence_agent.contracts.source import (
    DocumentSource,
    DocumentVersion,
    SearchChunk,
    SourceElement,
    SourceLocation,
)
from stakeholder_intelligence_agent.errors import (
    AccessDeniedError,
    DomainConflictError,
    IngestionError,
    IngestionInProgressError,
    TranscriptImmutableError,
)
from stakeholder_intelligence_agent.ingestion.identity import stable_document_key, stable_id
from stakeholder_intelligence_agent.ingestion.types import (
    ActivationResult,
    IngestionStart,
    StoredArtifact,
    UploadScope,
    ValidatedUpload,
)

if TYPE_CHECKING:
    import aiosqlite

    from stakeholder_intelligence_agent.contracts.access import AccessContext
    from stakeholder_intelligence_agent.persistence.domain import DomainDatabase

_LOCATION_ADAPTER: TypeAdapter[SourceLocation] = TypeAdapter(SourceLocation)
_ACTIVE_STATES: Final[tuple[str, ...]] = (
    "draft",
    "finalizing",
    "finalized",
    "ingesting",
    "ready",
)
type SqlParams = tuple[object, ...]


class IngestionRepository:
    """Enforce upload scope, leases, lifecycle, lineage, and atomic READY selection."""

    def __init__(self, database: DomainDatabase, *, lease_seconds: int) -> None:
        self._database = database
        self._lease_seconds = lease_seconds

    async def initialize(self) -> None:
        """Apply the ingestion migration inventory."""
        await self._database.initialize()

    async def start(
        self,
        access: AccessContext,
        upload: ValidatedUpload,
        *,
        lease_token: str,
        now: datetime,
    ) -> IngestionStart:
        """Resolve the logical source and exclusively claim one stable content version."""
        self._require_clock(now)
        async with self._database.transaction() as connection:
            scope = await self._resolve_scope(connection, access, now)
            document_key = stable_document_key(
                scope.engagement_id,
                scope.stakeholder_id or "-",
                scope.source_type,
                upload.normalized_filename,
            )
            source_row = await self._fetchone(
                connection,
                "SELECT * FROM document_sources WHERE document_key = ?",
                (document_key,),
            )
            if source_row is None:
                document_id = stable_id("document", document_key)
                source = DocumentSource(
                    document_id=document_id,
                    engagement_id=scope.engagement_id,
                    stakeholder_id=scope.stakeholder_id,
                    role=scope.role,
                    department=scope.department,
                    doc_type=upload.document_type,
                    source_type=scope.source_type,
                    original_filename=upload.filename,
                    media_type=upload.media_type,
                    created_at=now,
                )
                await connection.execute(
                    """
                    INSERT INTO document_sources(
                        document_id, document_key, engagement_id, stakeholder_id,
                        role, department, doc_type, source_type, original_filename,
                        normalized_filename, media_type, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        source.document_id,
                        document_key,
                        source.engagement_id,
                        source.stakeholder_id,
                        source.role,
                        source.department,
                        source.doc_type,
                        source.source_type,
                        source.original_filename,
                        upload.normalized_filename,
                        source.media_type,
                        self._time(source.created_at),
                    ),
                )
            else:
                source = self._source(source_row)
                if (
                    source.doc_type != upload.document_type
                    or source.media_type != upload.media_type
                    or source.engagement_id != scope.engagement_id
                    or source.stakeholder_id != scope.stakeholder_id
                ):
                    raise AccessDeniedError
                if source_row["deleted_at"] is not None:
                    await connection.execute(
                        "UPDATE document_sources SET deleted_at = NULL WHERE document_id = ?",
                        (source.document_id,),
                    )

            version_row = await self._fetchone(
                connection,
                """
                SELECT * FROM document_versions
                WHERE document_id = ? AND content_hash = ? AND state != 'SUPERSEDED'
                ORDER BY version_number DESC LIMIT 1
                """,
                (source.document_id, upload.content_hash),
            )
            if version_row is not None:
                version = self._version(version_row)
                if version.state == "READY":
                    return IngestionStart(
                        source=source,
                        version=version,
                        attempt_id=None,
                        lease_token=None,
                        idempotent_ready=True,
                    )
                await self._claim_existing(
                    connection,
                    version,
                    version_row,
                    access=access,
                    lease_token=lease_token,
                    now=now,
                )
            else:
                cursor = await connection.execute(
                    """
                    SELECT COALESCE(MAX(version_number), 0) + 1 AS next_version
                    FROM document_versions WHERE document_id = ?
                    """,
                    (source.document_id,),
                )
                number_row = await cursor.fetchone()
                if number_row is None:
                    raise RuntimeError("Version allocation failed.")
                version_number = int(number_row["next_version"])
                version_id = stable_id(
                    "version",
                    source.document_id,
                    str(version_number),
                    upload.content_hash,
                )
                version = DocumentVersion(
                    document_version_id=version_id,
                    document_id=source.document_id,
                    version_number=version_number,
                    content_hash=upload.content_hash,
                    state="RECEIVED",
                    is_active=False,
                    original_artifact_id=stable_id("artifact", version_id, "original"),
                    ingestion_key=stable_id("ingestion", version_id),
                    created_at=now,
                )
                await connection.execute(
                    """
                    INSERT INTO document_versions(
                        document_version_id, document_id, version_number, content_hash,
                        state, is_active, original_artifact_id, ingestion_key, created_at,
                        ready_at, superseded_at, failure_code, failure_message,
                        lease_token, lease_expires_at
                    ) VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, NULL, NULL, NULL, NULL, ?, ?)
                    """,
                    (
                        version.document_version_id,
                        version.document_id,
                        version.version_number,
                        version.content_hash,
                        version.state,
                        version.original_artifact_id,
                        version.ingestion_key,
                        self._time(version.created_at),
                        lease_token,
                        self._time(now + timedelta(seconds=self._lease_seconds)),
                    ),
                )
                await self._append_event(
                    connection,
                    version_id=version.document_version_id,
                    from_state=None,
                    to_state="RECEIVED",
                    attempt_id=None,
                    access=access,
                    now=now,
                )

            attempt_number = await self._next_attempt_number(
                connection,
                version.document_version_id,
            )
            attempt_id = stable_id(
                "attempt",
                version.document_version_id,
                str(attempt_number),
            )
            await connection.execute(
                """
                INSERT INTO ingestion_attempts(
                    attempt_id, document_version_id, attempt_number, status,
                    started_at, finished_at, failure_code, correlation_id
                ) VALUES (?, ?, ?, 'started', ?, NULL, NULL, ?)
                """,
                (
                    attempt_id,
                    version.document_version_id,
                    attempt_number,
                    self._time(now),
                    access.correlation_id,
                ),
            )
            await self._append_audit(
                connection,
                access=access,
                action="ingest_document",
                status="started",
                now=now,
                event_discriminator=attempt_id,
            )
            return IngestionStart(
                source=source,
                version=version,
                attempt_id=attempt_id,
                lease_token=lease_token,
                idempotent_ready=False,
            )

    async def withdraw_stakeholder_document(
        self,
        access: AccessContext,
        document_id: str,
        *,
        now: datetime,
    ) -> tuple[str, ...]:
        """Withdraw one owned document while its interview transcript remains mutable."""
        self._require_clock(now)
        async with self._database.transaction() as connection:
            scope = await self._resolve_scope(connection, access, now)
            if scope.source_type != "stakeholder_document" or scope.stakeholder_id is None:
                raise AccessDeniedError
            source_row = await self._fetchone(
                connection,
                """
                SELECT s.* FROM document_sources AS s
                WHERE s.document_id = ? AND s.engagement_id = ?
                    AND s.stakeholder_id = ?
                    AND s.source_type = 'stakeholder_document'
                    AND s.deleted_at IS NULL
                """,
                (document_id, scope.engagement_id, scope.stakeholder_id),
            )
            if source_row is None:
                raise AccessDeniedError
            session_row = await self._fetchone(
                connection,
                """
                SELECT status FROM interview_sessions
                WHERE interview_session_id = ? AND engagement_id = ?
                    AND stakeholder_id = ? AND thread_id = ?
                """,
                (
                    access.interview_session_id,
                    access.engagement_id,
                    access.stakeholder_id,
                    access.thread_id,
                ),
            )
            if session_row is None:
                raise AccessDeniedError
            if session_row["status"] != "draft":
                raise TranscriptImmutableError
            return await self._withdraw_document_versions(
                connection,
                access,
                document_id,
                audit_action="withdraw_stakeholder_document",
                now=now,
            )

    async def withdraw_pm_document(
        self,
        access: AccessContext,
        document_id: str,
        *,
        now: datetime,
    ) -> tuple[str, ...]:
        """Withdraw one PM-owned engagement document from active retrieval."""
        self._require_clock(now)
        async with self._database.transaction() as connection:
            scope = await self._resolve_scope(connection, access, now)
            if scope.source_type != "engagement_document" or scope.stakeholder_id is not None:
                raise AccessDeniedError
            source_row = await self._fetchone(
                connection,
                """
                SELECT s.document_id FROM document_sources AS s
                WHERE s.document_id = ? AND s.engagement_id = ?
                    AND s.stakeholder_id IS NULL
                    AND s.source_type = 'engagement_document'
                    AND s.deleted_at IS NULL
                """,
                (document_id, scope.engagement_id),
            )
            if source_row is None:
                raise AccessDeniedError
            return await self._withdraw_document_versions(
                connection,
                access,
                document_id,
                audit_action="withdraw_pm_document",
                now=now,
            )

    async def _withdraw_document_versions(
        self,
        connection: aiosqlite.Connection,
        access: AccessContext,
        document_id: str,
        *,
        audit_action: str,
        now: datetime,
    ) -> tuple[str, ...]:
        cursor = await connection.execute(
            """
            SELECT * FROM document_versions
            WHERE document_id = ? ORDER BY version_number
            """,
            (document_id,),
        )
        version_rows = await cursor.fetchall()
        if not version_rows or any(
            row["state"] not in {"READY", "FAILED", "SUPERSEDED"} for row in version_rows
        ):
            raise DomainConflictError
        active_version_ids: list[str] = []
        for row in version_rows:
            version = self._version(row)
            if version.state != "READY" or not version.is_active:
                continue
            superseded = DocumentVersion.model_validate(
                version.model_copy(
                    update={
                        "state": "SUPERSEDED",
                        "is_active": False,
                        "superseded_at": now,
                    }
                ).model_dump()
            )
            validate_document_version_transition(version, superseded)
            await connection.execute(
                """
                UPDATE document_versions
                SET state = 'SUPERSEDED', is_active = 0, superseded_at = ?,
                    lease_token = NULL, lease_expires_at = NULL
                WHERE document_version_id = ? AND state = 'READY' AND is_active = 1
                """,
                (self._time(now), version.document_version_id),
            )
            await connection.execute(
                """
                UPDATE search_chunks
                SET is_active_ready = 0, vector_stage_state = 'STAGED'
                WHERE source_version_id = ?
                """,
                (version.document_version_id,),
            )
            await self._append_event(
                connection,
                version_id=version.document_version_id,
                from_state="READY",
                to_state="SUPERSEDED",
                attempt_id=None,
                access=access,
                now=now,
            )
            active_version_ids.append(version.document_version_id)
        await connection.execute(
            "UPDATE document_sources SET deleted_at = ? WHERE document_id = ?",
            (self._time(now), document_id),
        )
        await self._append_audit(
            connection,
            access=access,
            action=audit_action,
            status="succeeded",
            now=now,
            event_discriminator=f"{document_id}:{self._time(now)}",
        )
        return tuple(active_version_ids)

    async def transition(
        self,
        access: AccessContext,
        *,
        version_id: str,
        attempt_id: str,
        lease_token: str,
        state: str,
        now: datetime,
    ) -> DocumentVersion:
        """Apply one permitted document-version transition under its worker lease."""
        async with self._database.transaction() as connection:
            await self._resolve_scope(connection, access, now)
            row = await self._require_leased_version(
                connection,
                version_id,
                lease_token,
                now,
            )
            previous = self._version(row)
            proposed = previous.model_copy(
                update={
                    "state": state,
                    "failure_code": None,
                    "failure_message": None,
                }
            )
            proposed = DocumentVersion.model_validate(proposed.model_dump())
            validate_document_version_transition(previous, proposed)
            await connection.execute(
                """
                UPDATE document_versions
                SET state = ?, failure_code = NULL, failure_message = NULL
                WHERE document_version_id = ? AND lease_token = ?
                """,
                (state, version_id, lease_token),
            )
            await self._append_event(
                connection,
                version_id=version_id,
                from_state=previous.state,
                to_state=state,
                attempt_id=attempt_id,
                access=access,
                now=now,
            )
            return proposed

    async def record_artifact(
        self,
        access: AccessContext,
        *,
        version_id: str,
        lease_token: str,
        artifact: StoredArtifact,
        now: datetime,
    ) -> None:
        """Persist original-artifact lineage immediately, including for later failures."""
        async with self._database.transaction() as connection:
            await self._resolve_scope(connection, access, now)
            await self._require_leased_version(connection, version_id, lease_token, now)
            await connection.execute(
                """
                INSERT INTO ingestion_artifacts(
                    artifact_id, engagement_id, document_version_id, artifact_kind,
                    virtual_path, media_type, content_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(artifact_id) DO NOTHING
                """,
                (
                    artifact.artifact_id,
                    artifact.engagement_id,
                    artifact.document_version_id,
                    artifact.artifact_kind,
                    artifact.virtual_path,
                    artifact.media_type,
                    artifact.content_hash,
                    self._time(now),
                ),
            )

    async def replace_payload(
        self,
        access: AccessContext,
        *,
        version_id: str,
        lease_token: str,
        artifacts: tuple[StoredArtifact, ...],
        elements: tuple[SourceElement, ...],
        chunks: tuple[SearchChunk, ...],
        now: datetime,
    ) -> None:
        """Replace retry residue with one complete deterministic staged payload."""
        async with self._database.transaction() as connection:
            await self._resolve_scope(connection, access, now)
            await self._require_leased_version(connection, version_id, lease_token, now)
            await connection.execute(
                "DELETE FROM search_chunks WHERE source_version_id = ?",
                (version_id,),
            )
            await connection.execute(
                """
                UPDATE source_elements SET parent_element_id = NULL
                WHERE document_version_id = ?
                """,
                (version_id,),
            )
            await connection.execute(
                "DELETE FROM source_elements WHERE document_version_id = ?",
                (version_id,),
            )
            await connection.execute(
                """
                DELETE FROM ingestion_artifacts
                WHERE document_version_id = ? AND artifact_kind != 'original'
                """,
                (version_id,),
            )
            for artifact in artifacts:
                await connection.execute(
                    """
                    INSERT INTO ingestion_artifacts(
                        artifact_id, engagement_id, document_version_id, artifact_kind,
                        virtual_path, media_type, content_hash, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(artifact_id) DO UPDATE SET
                        virtual_path = excluded.virtual_path,
                        media_type = excluded.media_type,
                        content_hash = excluded.content_hash
                    """,
                    (
                        artifact.artifact_id,
                        artifact.engagement_id,
                        artifact.document_version_id,
                        artifact.artifact_kind,
                        artifact.virtual_path,
                        artifact.media_type,
                        artifact.content_hash,
                        self._time(now),
                    ),
                )
            for order, element in enumerate(elements):
                await connection.execute(
                    """
                    INSERT INTO source_elements(
                        element_id, document_version_id, element_order, element_type,
                        original_content, english_interpretation, location_json,
                        parent_element_id, artifact_id, content_hash, extraction_method
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        element.element_id,
                        element.document_version_id,
                        order,
                        element.element_type,
                        element.original_content,
                        element.english_interpretation,
                        self._json(element.location.model_dump(mode="json")),
                        element.parent_element_id,
                        element.artifact_id,
                        element.content_hash,
                        element.extraction_method,
                    ),
                )
            for chunk in chunks:
                await connection.execute(
                    """
                    INSERT INTO search_chunks(
                        chunk_id, engagement_id, source_id, source_version_id,
                        element_ids_json, text_for_retrieval, location_json,
                        stakeholder_id, role, department, doc_type, source_type,
                        dense_vector_json, sparse_vector_json, is_active_ready,
                        vector_stage_state
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'STAGED')
                    """,
                    (
                        chunk.chunk_id,
                        chunk.engagement_id,
                        chunk.source_id,
                        chunk.source_version_id,
                        self._json(list(chunk.element_ids)),
                        chunk.text_for_retrieval,
                        self._json(chunk.location.model_dump(mode="json")),
                        chunk.stakeholder_id,
                        chunk.role,
                        chunk.department,
                        chunk.doc_type,
                        chunk.source_type,
                        self._json(list(chunk.dense_vector)),
                        self._json(chunk.sparse_vector.model_dump(mode="json")),
                    ),
                )
            await self._validate_completeness(connection, version_id)

    async def mark_vectors_prepared(
        self,
        *,
        version_id: str,
        lease_token: str,
        now: datetime,
    ) -> None:
        """Record successful external-vector preparation without exposing chunks."""
        async with self._database.transaction() as connection:
            await self._require_leased_version(connection, version_id, lease_token, now)
            await self._validate_completeness(connection, version_id)
            await connection.execute(
                """
                UPDATE search_chunks SET vector_stage_state = 'PREPARED'
                WHERE source_version_id = ?
                """,
                (version_id,),
            )

    async def activate(
        self,
        access: AccessContext,
        *,
        version_id: str,
        attempt_id: str,
        lease_token: str,
        now: datetime,
    ) -> ActivationResult:
        """Atomically select exactly one complete READY version in authoritative SQLite."""
        async with self._database.transaction() as connection:
            await self._resolve_scope(connection, access, now)
            row = await self._require_leased_version(
                connection,
                version_id,
                lease_token,
                now,
            )
            previous = self._version(row)
            if previous.state != "INDEXING":
                raise RuntimeError("Only an INDEXING version can be activated.")
            await self._validate_completeness(connection, version_id, require_prepared=True)
            cursor = await connection.execute(
                """
                SELECT * FROM document_versions
                WHERE document_id = ? AND is_active = 1 AND document_version_id != ?
                ORDER BY version_number
                """,
                (previous.document_id, version_id),
            )
            active_rows = await cursor.fetchall()
            superseded_ids: list[str] = []
            for active_row in active_rows:
                active = self._version(active_row)
                superseded = DocumentVersion.model_validate(
                    active.model_copy(
                        update={
                            "state": "SUPERSEDED",
                            "is_active": False,
                            "superseded_at": now,
                        }
                    ).model_dump()
                )
                validate_document_version_transition(active, superseded)
                await connection.execute(
                    """
                    UPDATE document_versions
                    SET state = 'SUPERSEDED', is_active = 0, superseded_at = ?
                    WHERE document_version_id = ? AND is_active = 1
                    """,
                    (self._time(now), active.document_version_id),
                )
                await connection.execute(
                    """
                    UPDATE search_chunks SET is_active_ready = 0
                    WHERE source_version_id = ?
                    """,
                    (active.document_version_id,),
                )
                await self._append_event(
                    connection,
                    version_id=active.document_version_id,
                    from_state="READY",
                    to_state="SUPERSEDED",
                    attempt_id=attempt_id,
                    access=access,
                    now=now,
                )
                superseded_ids.append(active.document_version_id)

            ready = DocumentVersion.model_validate(
                previous.model_copy(
                    update={
                        "state": "READY",
                        "is_active": True,
                        "ready_at": now,
                        "failure_code": None,
                        "failure_message": None,
                    }
                ).model_dump()
            )
            validate_document_version_transition(previous, ready)
            await connection.execute(
                """
                UPDATE document_versions
                SET state = 'READY', is_active = 1, ready_at = ?,
                    lease_token = NULL, lease_expires_at = NULL
                WHERE document_version_id = ? AND lease_token = ?
                """,
                (self._time(now), version_id, lease_token),
            )
            await connection.execute(
                """
                UPDATE search_chunks
                SET is_active_ready = 1, vector_stage_state = 'ACTIVE'
                WHERE source_version_id = ?
                """,
                (version_id,),
            )
            await connection.execute(
                """
                UPDATE ingestion_attempts
                SET status = 'succeeded', finished_at = ?
                WHERE attempt_id = ? AND status = 'started'
                """,
                (self._time(now), attempt_id),
            )
            await self._append_event(
                connection,
                version_id=version_id,
                from_state="INDEXING",
                to_state="READY",
                attempt_id=attempt_id,
                access=access,
                now=now,
            )
            await self._append_audit(
                connection,
                access=access,
                action="ingest_document",
                status="succeeded",
                now=now,
                event_discriminator=attempt_id,
            )
            chunks = await self._load_chunks(connection, version_id)
            return ActivationResult(
                version=ready,
                chunks=chunks,
                superseded_version_ids=tuple(superseded_ids),
            )

    async def fail(
        self,
        access: AccessContext,
        *,
        version_id: str,
        attempt_id: str,
        lease_token: str,
        failure: IngestionError,
        now: datetime,
    ) -> DocumentVersion | None:
        """Persist a safe non-searchable failure without superseding an older READY version."""
        async with self._database.transaction() as connection:
            row = await self._fetchone(
                connection,
                "SELECT * FROM document_versions WHERE document_version_id = ?",
                (version_id,),
            )
            if row is None or row["lease_token"] != lease_token:
                return None
            previous = self._version(row)
            if previous.state in {"READY", "SUPERSEDED"}:
                return previous
            failed = DocumentVersion.model_validate(
                previous.model_copy(
                    update={
                        "state": "FAILED",
                        "is_active": False,
                        "failure_code": failure.code,
                        "failure_message": str(failure),
                    }
                ).model_dump()
            )
            if previous.state != "FAILED":
                validate_document_version_transition(previous, failed)
                await self._append_event(
                    connection,
                    version_id=version_id,
                    from_state=previous.state,
                    to_state="FAILED",
                    attempt_id=attempt_id,
                    access=access,
                    now=now,
                )
            await connection.execute(
                """
                UPDATE document_versions
                SET state = 'FAILED', is_active = 0, failure_code = ?,
                    failure_message = ?, lease_token = NULL, lease_expires_at = NULL
                WHERE document_version_id = ? AND lease_token = ?
                """,
                (failure.code, str(failure), version_id, lease_token),
            )
            await connection.execute(
                """
                UPDATE search_chunks SET is_active_ready = 0
                WHERE source_version_id = ?
                """,
                (version_id,),
            )
            await connection.execute(
                """
                UPDATE ingestion_attempts
                SET status = 'failed', finished_at = ?, failure_code = ?
                WHERE attempt_id = ? AND status = 'started'
                """,
                (self._time(now), failure.code, attempt_id),
            )
            await self._append_audit(
                connection,
                access=access,
                action="ingest_document",
                status="failed",
                now=now,
                failure_code=failure.code,
                event_discriminator=attempt_id,
            )
            return failed

    async def load_ready_result(
        self,
        access: AccessContext,
        version_id: str,
        *,
        now: datetime,
    ) -> tuple[DocumentSource, DocumentVersion, tuple[SourceElement, ...], tuple[SearchChunk, ...]]:
        """Load one authorized complete READY result for idempotent return or inspection."""
        async with self._database.connection() as connection:
            await self._resolve_scope(connection, access, now)
            row = await self._fetchone(
                connection,
                """
                SELECT v.*, s.engagement_id AS source_engagement_id
                FROM document_versions AS v
                JOIN document_sources AS s ON s.document_id = v.document_id
                WHERE v.document_version_id = ? AND v.state = 'READY'
                    AND v.is_active = 1 AND s.engagement_id = ?
                """,
                (version_id, access.engagement_id),
            )
            if row is None:
                raise AccessDeniedError
            version = self._version(row)
            source_row = await self._fetchone(
                connection,
                "SELECT * FROM document_sources WHERE document_id = ?",
                (version.document_id,),
            )
            if source_row is None:
                raise RuntimeError("READY source is missing.")
            return (
                self._source(source_row),
                version,
                await self._load_elements(connection, version_id),
                await self._load_chunks(connection, version_id),
            )

    async def active_version_ids(
        self,
        access: AccessContext,
        document_id: str,
        *,
        now: datetime,
    ) -> tuple[str, ...]:
        """Return the authorized SQLite-active versions used by the activation protocol."""
        async with self._database.connection() as connection:
            await self._resolve_scope(connection, access, now)
            cursor = await connection.execute(
                """
                SELECT v.document_version_id
                FROM document_versions AS v
                JOIN document_sources AS s ON s.document_id = v.document_id
                WHERE v.document_id = ? AND v.is_active = 1 AND v.state = 'READY'
                    AND s.engagement_id = ?
                ORDER BY v.version_number
                """,
                (document_id, access.engagement_id),
            )
            return tuple(str(row["document_version_id"]) for row in await cursor.fetchall())

    async def superseded_version_ids(
        self,
        access: AccessContext,
        document_id: str,
        *,
        now: datetime,
    ) -> tuple[str, ...]:
        """Return authorized obsolete versions for idempotent vector reconciliation."""
        async with self._database.connection() as connection:
            await self._resolve_scope(connection, access, now)
            cursor = await connection.execute(
                """
                SELECT v.document_version_id
                FROM document_versions AS v
                JOIN document_sources AS s ON s.document_id = v.document_id
                WHERE v.document_id = ? AND v.state = 'SUPERSEDED'
                    AND v.is_active = 0 AND s.engagement_id = ?
                ORDER BY v.version_number
                """,
                (document_id, access.engagement_id),
            )
            return tuple(str(row["document_version_id"]) for row in await cursor.fetchall())

    async def _claim_existing(
        self,
        connection: aiosqlite.Connection,
        version: DocumentVersion,
        row: aiosqlite.Row,
        *,
        access: AccessContext,
        lease_token: str,
        now: datetime,
    ) -> None:
        lease_expires = self._optional_time(row["lease_expires_at"])
        if row["lease_token"] is not None and lease_expires is not None and lease_expires > now:
            raise IngestionInProgressError
        if version.state != "FAILED":
            cursor = await connection.execute(
                """
                SELECT attempt_id, correlation_id
                FROM ingestion_attempts
                WHERE document_version_id = ? AND status = 'started'
                ORDER BY attempt_number
                """,
                (version.document_version_id,),
            )
            interrupted_attempts = tuple(await cursor.fetchall())
            interrupted = DocumentVersion.model_validate(
                version.model_copy(
                    update={
                        "state": "FAILED",
                        "failure_code": "INTERRUPTED_ATTEMPT",
                        "failure_message": "A previous ingestion attempt was interrupted.",
                    }
                ).model_dump()
            )
            validate_document_version_transition(version, interrupted)
            await connection.execute(
                """
                UPDATE document_versions
                SET state = 'FAILED', failure_code = 'INTERRUPTED_ATTEMPT',
                    failure_message = 'A previous ingestion attempt was interrupted.',
                    lease_token = NULL, lease_expires_at = NULL
                WHERE document_version_id = ?
                """,
                (version.document_version_id,),
            )
            await self._append_event(
                connection,
                version_id=version.document_version_id,
                from_state=version.state,
                to_state="FAILED",
                attempt_id=None,
                access=access,
                now=now,
            )
            await connection.execute(
                """
                UPDATE ingestion_attempts
                SET status = 'failed', finished_at = ?,
                    failure_code = 'INTERRUPTED_ATTEMPT'
                WHERE document_version_id = ? AND status = 'started'
                """,
                (self._time(now), version.document_version_id),
            )
            for interrupted_attempt in interrupted_attempts:
                await self._append_audit(
                    connection,
                    access=access,
                    action="ingest_document",
                    status="failed",
                    now=now,
                    failure_code="INTERRUPTED_ATTEMPT",
                    event_discriminator=str(interrupted_attempt["attempt_id"]),
                    correlation_id=str(interrupted_attempt["correlation_id"]),
                )
        await connection.execute(
            """
            UPDATE document_versions
            SET lease_token = ?, lease_expires_at = ?
            WHERE document_version_id = ?
            """,
            (
                lease_token,
                self._time(now + timedelta(seconds=self._lease_seconds)),
                version.document_version_id,
            ),
        )

    async def _resolve_scope(
        self,
        connection: aiosqlite.Connection,
        access: AccessContext,
        now: datetime,
    ) -> UploadScope:
        try:
            access.require_permission("document:upload")
            access.require_active(now)
        except (PermissionError, ValueError) as error:
            raise AccessDeniedError from error
        if access.principal_type == "pm":
            row = await self._fetchone(
                connection,
                """
                SELECT p.pm_access_id
                FROM pm_access AS p
                JOIN engagements AS e ON e.engagement_id = ?
                WHERE p.pm_access_id = ? AND p.status = 'active' AND e.status = 'active'
                """,
                (access.engagement_id, access.principal_id),
            )
            if row is None or access.stakeholder_id is not None:
                raise AccessDeniedError
            return UploadScope(
                engagement_id=access.engagement_id,
                stakeholder_id=None,
                role=None,
                department=None,
                source_type="engagement_document",
            )
        if (
            access.stakeholder_id is None
            or access.interview_session_id is None
            or access.thread_id is None
        ):
            raise AccessDeniedError
        row = await self._fetchone(
            connection,
            """
            SELECT h.role, h.department
            FROM stakeholders AS h
            JOIN engagements AS e ON e.engagement_id = h.engagement_id
            JOIN interview_sessions AS i
                ON i.engagement_id = h.engagement_id
                AND i.stakeholder_id = h.stakeholder_id
            WHERE h.stakeholder_id = ? AND h.engagement_id = ?
                AND h.status = 'active' AND e.status = 'active'
                AND i.interview_session_id = ? AND i.thread_id = ?
                AND i.status IN (?, ?, ?, ?, ?)
            """,
            (
                access.stakeholder_id,
                access.engagement_id,
                access.interview_session_id,
                access.thread_id,
                *_ACTIVE_STATES,
            ),
        )
        if row is None or access.principal_id != access.stakeholder_id:
            raise AccessDeniedError
        return UploadScope(
            engagement_id=access.engagement_id,
            stakeholder_id=access.stakeholder_id,
            role=cast("str | None", row["role"]),
            department=cast("str | None", row["department"]),
            source_type="stakeholder_document",
        )

    async def _require_leased_version(
        self,
        connection: aiosqlite.Connection,
        version_id: str,
        lease_token: str,
        now: datetime,
    ) -> aiosqlite.Row:
        row = await self._fetchone(
            connection,
            """
            SELECT * FROM document_versions
            WHERE document_version_id = ? AND lease_token = ? AND lease_expires_at > ?
            """,
            (version_id, lease_token, self._time(now)),
        )
        if row is None:
            raise IngestionInProgressError
        return row

    async def _validate_completeness(
        self,
        connection: aiosqlite.Connection,
        version_id: str,
        *,
        require_prepared: bool = False,
    ) -> None:
        cursor = await connection.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM source_elements
                    WHERE document_version_id = ?) AS element_count,
                (SELECT COUNT(*) FROM search_chunks
                    WHERE source_version_id = ?) AS chunk_count,
                (SELECT COUNT(*) FROM search_chunks
                    WHERE source_version_id = ?
                    AND (dense_vector_json = '[]' OR sparse_vector_json = '{}')) AS bad_vectors,
                (SELECT COUNT(*) FROM source_elements
                    WHERE document_version_id = ?
                    AND element_type IN ('image', 'chart') AND artifact_id IS NULL) AS bad_visuals,
                (SELECT COUNT(*) FROM source_elements
                    WHERE document_version_id = ?
                    AND element_type IN ('ocr_text', 'vision_description')
                    AND parent_element_id IS NULL) AS bad_derivatives,
                (SELECT COUNT(*) FROM search_chunks
                    WHERE source_version_id = ? AND vector_stage_state != 'PREPARED')
                    AS not_prepared
            """,
            (version_id, version_id, version_id, version_id, version_id, version_id),
        )
        row = await cursor.fetchone()
        if row is None:
            raise RuntimeError("Ingestion completeness query failed.")
        if (
            int(row["element_count"]) < 1
            or int(row["chunk_count"]) < 1
            or int(row["bad_vectors"]) != 0
            or int(row["bad_visuals"]) != 0
            or int(row["bad_derivatives"]) != 0
            or (require_prepared and int(row["not_prepared"]) != 0)
        ):
            raise RuntimeError("The staged ingestion payload is incomplete.")

    async def _load_elements(
        self,
        connection: aiosqlite.Connection,
        version_id: str,
    ) -> tuple[SourceElement, ...]:
        cursor = await connection.execute(
            """
            SELECT * FROM source_elements
            WHERE document_version_id = ? ORDER BY element_order
            """,
            (version_id,),
        )
        return tuple(
            SourceElement.model_validate(
                {
                    "element_id": row["element_id"],
                    "document_version_id": row["document_version_id"],
                    "element_type": row["element_type"],
                    "original_content": row["original_content"],
                    "english_interpretation": row["english_interpretation"],
                    "location": _LOCATION_ADAPTER.validate_json(str(row["location_json"])),
                    "parent_element_id": row["parent_element_id"],
                    "artifact_id": row["artifact_id"],
                    "content_hash": row["content_hash"],
                    "extraction_method": row["extraction_method"],
                }
            )
            for row in await cursor.fetchall()
        )

    async def _load_chunks(
        self,
        connection: aiosqlite.Connection,
        version_id: str,
    ) -> tuple[SearchChunk, ...]:
        cursor = await connection.execute(
            """
            SELECT * FROM search_chunks
            WHERE source_version_id = ? ORDER BY chunk_id
            """,
            (version_id,),
        )
        return tuple(
            (
                SearchChunk.model_validate(
                    {
                        "chunk_id": row["chunk_id"],
                        "engagement_id": row["engagement_id"],
                        "source_id": row["source_id"],
                        "source_version_id": row["source_version_id"],
                        "element_ids": json.loads(str(row["element_ids_json"])),
                        "text_for_retrieval": row["text_for_retrieval"],
                        "location": json.loads(str(row["location_json"])),
                        "stakeholder_id": row["stakeholder_id"],
                        "role": row["role"],
                        "department": row["department"],
                        "doc_type": row["doc_type"],
                        "source_type": row["source_type"],
                        "dense_vector": json.loads(str(row["dense_vector_json"])),
                        "sparse_vector": json.loads(str(row["sparse_vector_json"])),
                        "is_active_ready": bool(row["is_active_ready"]),
                    }
                )
            )
            for row in await cursor.fetchall()
        )

    async def _next_attempt_number(
        self,
        connection: aiosqlite.Connection,
        version_id: str,
    ) -> int:
        cursor = await connection.execute(
            """
            SELECT COALESCE(MAX(attempt_number), 0) + 1 AS next_attempt
            FROM ingestion_attempts WHERE document_version_id = ?
            """,
            (version_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            raise RuntimeError("Attempt allocation failed.")
        return int(row["next_attempt"])

    async def _append_event(
        self,
        connection: aiosqlite.Connection,
        *,
        version_id: str,
        from_state: str | None,
        to_state: str,
        attempt_id: str | None,
        access: AccessContext,
        now: datetime,
    ) -> None:
        event_id = stable_id(
            "version-event",
            version_id,
            from_state or "none",
            to_state,
            attempt_id or self._time(now),
        )
        await connection.execute(
            """
            INSERT INTO document_version_events(
                event_id, document_version_id, from_state, to_state,
                occurred_at, attempt_id, correlation_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                version_id,
                from_state,
                to_state,
                self._time(now),
                attempt_id,
                access.correlation_id,
            ),
        )

    async def _append_audit(
        self,
        connection: aiosqlite.Connection,
        *,
        access: AccessContext,
        action: str,
        status: str,
        now: datetime,
        event_discriminator: str,
        failure_code: str | None = None,
        correlation_id: str | None = None,
    ) -> None:
        event_id = stable_id("audit", event_discriminator, status)
        await connection.execute(
            """
            INSERT INTO operational_audit_events(
                event_id, occurred_at, run_id, engagement_id, thread_id,
                actor, action, status, duration_ms, source_ids_json,
                evidence_ids_json, retry_count, failure_code, correlation_id
            ) VALUES (?, ?, NULL, ?, ?, 'ingestion_service', ?, ?, NULL,
                '[]', '[]', NULL, ?, ?)
            """,
            (
                event_id,
                self._time(now),
                access.engagement_id,
                access.thread_id,
                action,
                status,
                failure_code,
                correlation_id or access.correlation_id,
            ),
        )

    @staticmethod
    async def _fetchone(
        connection: aiosqlite.Connection,
        sql: str,
        parameters: SqlParams,
    ) -> aiosqlite.Row | None:
        cursor = await connection.execute(sql, parameters)
        return await cursor.fetchone()

    @staticmethod
    def _source(row: aiosqlite.Row) -> DocumentSource:
        return DocumentSource.model_validate(
            {
                "document_id": row["document_id"],
                "engagement_id": row["engagement_id"],
                "stakeholder_id": row["stakeholder_id"],
                "role": row["role"],
                "department": row["department"],
                "doc_type": row["doc_type"],
                "source_type": row["source_type"],
                "original_filename": row["original_filename"],
                "media_type": row["media_type"],
                "created_at": IngestionRepository._parse_time(row["created_at"]),
            }
        )

    @staticmethod
    def _version(row: aiosqlite.Row) -> DocumentVersion:
        return DocumentVersion.model_validate(
            {
                "document_version_id": row["document_version_id"],
                "document_id": row["document_id"],
                "version_number": row["version_number"],
                "content_hash": row["content_hash"],
                "state": row["state"],
                "is_active": bool(row["is_active"]),
                "original_artifact_id": row["original_artifact_id"],
                "ingestion_key": row["ingestion_key"],
                "created_at": IngestionRepository._parse_time(row["created_at"]),
                "ready_at": IngestionRepository._optional_time(row["ready_at"]),
                "superseded_at": IngestionRepository._optional_time(row["superseded_at"]),
                "failure_code": row["failure_code"],
                "failure_message": row["failure_message"],
            }
        )

    @staticmethod
    def _json(value: object) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)

    @staticmethod
    def _time(value: datetime) -> str:
        return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")

    @staticmethod
    def _parse_time(value: object) -> datetime:
        return datetime.fromisoformat(str(value)).astimezone(UTC)

    @staticmethod
    def _optional_time(value: object) -> datetime | None:
        return None if value is None else IngestionRepository._parse_time(value)

    @staticmethod
    def _require_clock(value: datetime) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Ingestion requires an aware timestamp.")
