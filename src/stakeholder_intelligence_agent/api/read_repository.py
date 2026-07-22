"""Read-only projections for approved custom routes with fresh scope checks."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal, cast

from pydantic import TypeAdapter

from stakeholder_intelligence_agent.contracts import (
    Engagement,
    InsightRun,
    InterviewSession,
    Stakeholder,
    Transcript,
    TranscriptIngestionVersion,
    TranscriptTurn,
)
from stakeholder_intelligence_agent.contracts.source import (
    DocumentSource,
    DocumentVersion,
    DocumentVersionState,
    ElementType,
    SourceLocation,
)
from stakeholder_intelligence_agent.errors import AccessDeniedError
from stakeholder_intelligence_agent.ingestion.types import ArtifactKind, StoredArtifact

if TYPE_CHECKING:
    import aiosqlite

    from stakeholder_intelligence_agent.contracts import AccessContext
    from stakeholder_intelligence_agent.persistence import DomainDatabase

_LOCATION_ADAPTER: TypeAdapter[SourceLocation] = TypeAdapter(SourceLocation)
_ELEMENT_PREVIEW_LIMIT = 24
_ARTIFACT_PREVIEW_LIMIT = 24
_TEXT_PREVIEW_LIMIT = 2_000
_MAX_INSIGHT_HISTORY_LIMIT = 100


@dataclass(frozen=True, slots=True)
class InvitationSummaryRecord:
    """Invitation lifecycle fields safe for an authorized PM response."""

    invitation_id: str
    engagement_id: str
    stakeholder_id: str
    status: Literal["active", "activated", "expired", "revoked"]
    created_at: datetime
    expires_at: datetime
    activated_at: datetime | None
    revoked_at: datetime | None


@dataclass(frozen=True, slots=True)
class DocumentSummaryRecord:
    """One source and its latest immutable processing version."""

    source: DocumentSource
    version: DocumentVersion


@dataclass(frozen=True, slots=True)
class DocumentLifecycleEventRecord:
    """One safe append-only document lifecycle transition."""

    event_id: str
    from_state: DocumentVersionState | None
    to_state: DocumentVersionState
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class DocumentElementPreviewRecord:
    """One bounded extracted-element preview without vectors or internal prompts."""

    element_id: str
    document_version_id: str
    element_type: ElementType
    location: SourceLocation
    extraction_method: str
    content_preview: str | None
    english_interpretation: str | None


@dataclass(frozen=True, slots=True)
class DocumentProcessingRecord:
    """Authorized processing facts and bounded preview data for one document."""

    summary: DocumentSummaryRecord
    lifecycle_events: tuple[DocumentLifecycleEventRecord, ...]
    element_counts: tuple[tuple[ElementType, int], ...]
    chunk_count: int
    artifact_counts: tuple[tuple[ArtifactKind, int], ...]
    artifacts: tuple[StoredArtifact, ...]
    element_previews: tuple[DocumentElementPreviewRecord, ...]


@dataclass(frozen=True, slots=True)
class InterviewStatusRecord:
    """Current interview, optional transcript/version, and exact authorized turns."""

    session: InterviewSession
    transcript: Transcript | None
    version: TranscriptIngestionVersion | None
    turns: tuple[TranscriptTurn, ...]
    turn_count: int


@dataclass(frozen=True, slots=True)
class FinalizedInterviewRecord:
    """One finalized transcript and its exact turns for an authorized PM."""

    session: InterviewSession
    transcript: Transcript
    turns: tuple[TranscriptTurn, ...]


class DomainReadRepository:
    """Query permitted views without using browser identifiers as authorization."""

    def __init__(self, database: DomainDatabase) -> None:
        self._database = database

    async def initialize(self) -> None:
        """Apply the shared forward-only domain migrations."""
        await self._database.initialize()

    async def engagement(self, access: AccessContext, *, now: datetime) -> Engagement:
        """Return only the active engagement bound to the resolved context."""
        async with self._database.connection() as connection:
            permission = "session:read" if access.principal_type == "pm" else "source:read"
            await self._resolve_access(connection, access, now, permission=permission)
            row = await self._fetchone(
                connection,
                "SELECT * FROM engagements WHERE engagement_id = ? AND status = 'active'",
                (access.engagement_id,),
            )
            if row is None:
                raise AccessDeniedError
            return self._engagement(row)

    async def stakeholders(
        self,
        access: AccessContext,
        *,
        now: datetime,
    ) -> tuple[Stakeholder, ...]:
        """List engagement stakeholders for the selected PM session."""
        if access.principal_type != "pm":
            raise AccessDeniedError
        async with self._database.connection() as connection:
            await self._resolve_access(connection, access, now, permission="session:read")
            cursor = await connection.execute(
                """
                SELECT * FROM stakeholders
                WHERE engagement_id = ?
                ORDER BY created_at, stakeholder_id
                """,
                (access.engagement_id,),
            )
            return tuple(self._stakeholder(row) for row in await cursor.fetchall())

    async def stakeholder_profile(
        self,
        access: AccessContext,
        *,
        now: datetime,
    ) -> Stakeholder:
        """Load the stakeholder fixed to the current limited session."""
        if access.principal_type != "stakeholder" or access.stakeholder_id is None:
            raise AccessDeniedError
        async with self._database.connection() as connection:
            await self._resolve_access(
                connection,
                access,
                now,
                permission="interview:participate",
            )
            row = await self._fetchone(
                connection,
                """
                SELECT * FROM stakeholders
                WHERE stakeholder_id = ? AND engagement_id = ? AND status = 'active'
                """,
                (access.stakeholder_id, access.engagement_id),
            )
            if row is None:
                raise AccessDeniedError
            return self._stakeholder(row)

    async def invitations(
        self,
        access: AccessContext,
        *,
        now: datetime,
    ) -> tuple[InvitationSummaryRecord, ...]:
        """List hash-free invitation status for the selected PM engagement."""
        if access.principal_type != "pm":
            raise AccessDeniedError
        async with self._database.connection() as connection:
            await self._resolve_access(
                connection,
                access,
                now,
                permission="invitation:manage",
            )
            cursor = await connection.execute(
                """
                SELECT invitation_id, engagement_id, stakeholder_id, status,
                    created_at, expires_at, activated_at, revoked_at
                FROM invitation_tokens
                WHERE engagement_id = ?
                ORDER BY created_at, invitation_id
                """,
                (access.engagement_id,),
            )
            return tuple(self._invitation(row) for row in await cursor.fetchall())

    async def documents(
        self,
        access: AccessContext,
        *,
        now: datetime,
    ) -> tuple[DocumentSummaryRecord, ...]:
        """List latest document versions inside the caller's permitted source scope."""
        async with self._database.connection() as connection:
            await self._resolve_access(connection, access, now, permission="source:read")
            parameters: tuple[object, ...]
            if access.principal_type == "stakeholder":
                query = """
                    SELECT s.*,
                        v.document_version_id, v.version_number, v.content_hash,
                        v.state, v.is_active, v.original_artifact_id, v.ingestion_key,
                        v.created_at AS version_created_at, v.ready_at,
                        v.superseded_at, v.failure_code, v.failure_message
                    FROM document_sources AS s
                    JOIN document_versions AS v ON v.document_version_id = (
                        SELECT latest.document_version_id
                        FROM document_versions AS latest
                        WHERE latest.document_id = s.document_id
                        ORDER BY latest.version_number DESC
                        LIMIT 1
                    )
                    WHERE s.engagement_id = ? AND s.stakeholder_id = ?
                        AND s.deleted_at IS NULL
                    ORDER BY s.created_at, s.document_id
                """
                parameters = (access.engagement_id, access.stakeholder_id)
            else:
                query = """
                    SELECT s.*,
                        v.document_version_id, v.version_number, v.content_hash,
                        v.state, v.is_active, v.original_artifact_id, v.ingestion_key,
                        v.created_at AS version_created_at, v.ready_at,
                        v.superseded_at, v.failure_code, v.failure_message
                    FROM document_sources AS s
                    JOIN document_versions AS v ON v.document_version_id = (
                        SELECT latest.document_version_id
                        FROM document_versions AS latest
                        WHERE latest.document_id = s.document_id
                        ORDER BY latest.version_number DESC
                        LIMIT 1
                    )
                    WHERE s.engagement_id = ? AND s.deleted_at IS NULL
                        AND (
                            s.source_type = 'engagement_document'
                            OR EXISTS (
                                SELECT 1
                                FROM interview_sessions AS i
                                WHERE i.engagement_id = s.engagement_id
                                    AND i.stakeholder_id = s.stakeholder_id
                                    AND i.finalized_at IS NOT NULL
                            )
                        )
                    ORDER BY s.created_at, s.document_id
                """
                parameters = (access.engagement_id,)
            cursor = await connection.execute(query, parameters)
            return tuple(self._document(row) for row in await cursor.fetchall())

    async def document(
        self,
        access: AccessContext,
        document_id: str,
        *,
        now: datetime,
    ) -> DocumentSummaryRecord:
        """Return one latest document version without protected existence disclosure."""
        documents = await self.documents(access, now=now)
        result = next((item for item in documents if item.source.document_id == document_id), None)
        if result is None:
            raise AccessDeniedError
        return result

    async def document_processing(
        self,
        access: AccessContext,
        document_id: str,
        *,
        now: datetime,
    ) -> DocumentProcessingRecord:
        """Return bounded processing facts for one authorized latest document version."""
        summary = await self.document(access, document_id, now=now)
        version_id = summary.version.document_version_id
        async with self._database.connection() as connection:
            await self._resolve_access(connection, access, now, permission="source:read")

            event_cursor = await connection.execute(
                """
                SELECT event_id, from_state, to_state, occurred_at
                FROM document_version_events
                WHERE document_version_id = ?
                ORDER BY occurred_at, event_id
                """,
                (version_id,),
            )
            lifecycle_events = tuple(
                DocumentLifecycleEventRecord(
                    event_id=str(row["event_id"]),
                    from_state=(
                        None
                        if row["from_state"] is None
                        else cast("DocumentVersionState", str(row["from_state"]))
                    ),
                    to_state=cast("DocumentVersionState", str(row["to_state"])),
                    occurred_at=self._parse_time(row["occurred_at"]),
                )
                for row in await event_cursor.fetchall()
            )

            element_count_cursor = await connection.execute(
                """
                SELECT element_type, COUNT(*) AS count
                FROM source_elements
                WHERE document_version_id = ?
                GROUP BY element_type
                ORDER BY element_type
                """,
                (version_id,),
            )
            element_counts = tuple(
                (cast("ElementType", str(row["element_type"])), int(row["count"]))
                for row in await element_count_cursor.fetchall()
            )

            chunk_row = await self._fetchone(
                connection,
                """
                SELECT COUNT(*) AS count
                FROM search_chunks
                WHERE engagement_id = ? AND source_version_id = ?
                """,
                (access.engagement_id, version_id),
            )
            if chunk_row is None:
                raise AccessDeniedError

            artifact_count_cursor = await connection.execute(
                """
                SELECT artifact_kind, COUNT(*) AS count
                FROM ingestion_artifacts
                WHERE engagement_id = ? AND document_version_id = ?
                GROUP BY artifact_kind
                ORDER BY artifact_kind
                """,
                (access.engagement_id, version_id),
            )
            artifact_counts = tuple(
                (cast("ArtifactKind", str(row["artifact_kind"])), int(row["count"]))
                for row in await artifact_count_cursor.fetchall()
            )

            artifact_cursor = await connection.execute(
                """
                SELECT artifact_id, engagement_id, document_version_id,
                    artifact_kind, virtual_path, media_type, content_hash
                FROM ingestion_artifacts
                WHERE engagement_id = ? AND document_version_id = ?
                ORDER BY CASE WHEN artifact_kind = 'original' THEN 0 ELSE 1 END,
                    artifact_kind, artifact_id
                LIMIT ?
                """,
                (access.engagement_id, version_id, _ARTIFACT_PREVIEW_LIMIT),
            )
            artifacts = tuple(
                StoredArtifact(
                    artifact_id=str(row["artifact_id"]),
                    engagement_id=str(row["engagement_id"]),
                    document_version_id=str(row["document_version_id"]),
                    artifact_kind=cast("ArtifactKind", str(row["artifact_kind"])),
                    virtual_path=str(row["virtual_path"]),
                    media_type=str(row["media_type"]),
                    content_hash=str(row["content_hash"]),
                )
                for row in await artifact_cursor.fetchall()
            )

            preview_cursor = await connection.execute(
                """
                SELECT element_id, document_version_id, element_type,
                    original_content, english_interpretation, location_json,
                    extraction_method
                FROM source_elements
                WHERE document_version_id = ?
                    AND element_type NOT IN ('image', 'chart')
                ORDER BY element_order
                LIMIT ?
                """,
                (version_id, _ELEMENT_PREVIEW_LIMIT),
            )
            element_previews = tuple(
                DocumentElementPreviewRecord(
                    element_id=str(row["element_id"]),
                    document_version_id=str(row["document_version_id"]),
                    element_type=cast("ElementType", str(row["element_type"])),
                    location=_LOCATION_ADAPTER.validate_json(str(row["location_json"])),
                    extraction_method=str(row["extraction_method"]),
                    content_preview=self._bounded_preview(row["original_content"]),
                    english_interpretation=self._bounded_preview(row["english_interpretation"]),
                )
                for row in await preview_cursor.fetchall()
            )

        return DocumentProcessingRecord(
            summary=summary,
            lifecycle_events=lifecycle_events,
            element_counts=element_counts,
            chunk_count=int(chunk_row["count"]),
            artifact_counts=artifact_counts,
            artifacts=artifacts,
            element_previews=element_previews,
        )

    async def interview_sessions(
        self,
        access: AccessContext,
        *,
        now: datetime,
    ) -> tuple[InterviewSession, ...]:
        """List engagement sessions for PM or the one session fixed to a stakeholder."""
        async with self._database.connection() as connection:
            permission = (
                "session:read" if access.principal_type == "pm" else "interview:participate"
            )
            await self._resolve_access(connection, access, now, permission=permission)
            parameters: tuple[object, ...]
            if access.principal_type == "stakeholder":
                query = """
                    SELECT * FROM interview_sessions
                    WHERE engagement_id = ? AND interview_session_id = ?
                        AND stakeholder_id = ?
                    ORDER BY started_at, interview_session_id
                """
                parameters = (
                    access.engagement_id,
                    access.interview_session_id,
                    access.stakeholder_id,
                )
            else:
                query = """
                    SELECT * FROM interview_sessions
                    WHERE engagement_id = ?
                    ORDER BY started_at, interview_session_id
                """
                parameters = (access.engagement_id,)
            cursor = await connection.execute(query, parameters)
            return tuple(self._session(row) for row in await cursor.fetchall())

    async def interview_status(
        self,
        access: AccessContext,
        *,
        now: datetime,
    ) -> InterviewStatusRecord:
        """Return the stakeholder's fixed interview lifecycle without widening scope."""
        if access.principal_type != "stakeholder":
            raise AccessDeniedError
        async with self._database.connection() as connection:
            await self._resolve_access(
                connection,
                access,
                now,
                permission="interview:participate",
            )
            row = await self._fetchone(
                connection,
                """
                SELECT i.*,
                    t.transcript_id AS stored_transcript_id,
                    t.role AS transcript_role,
                    t.department AS transcript_department,
                    t.status AS transcript_status,
                    t.language_observations_json,
                    t.finalized_at AS transcript_finalized_at,
                    t.content_hash AS transcript_content_hash,
                    v.transcript_ingestion_version_id,
                    v.content_hash AS version_content_hash,
                    v.state AS version_state,
                    v.is_active AS version_is_active,
                    v.created_at AS version_created_at,
                    v.ready_at AS version_ready_at,
                    v.failure_code AS version_failure_code,
                    v.failure_message AS version_failure_message,
                    (SELECT COUNT(*) FROM transcript_turns AS tt
                        WHERE tt.transcript_id = t.transcript_id) AS turn_count
                FROM interview_sessions AS i
                LEFT JOIN transcripts AS t
                    ON t.interview_session_id = i.interview_session_id
                LEFT JOIN transcript_ingestion_versions AS v
                    ON v.transcript_ingestion_version_id = i.ingestion_version_id
                WHERE i.interview_session_id = ? AND i.engagement_id = ?
                    AND i.stakeholder_id = ? AND i.thread_id = ?
                """,
                (
                    access.interview_session_id,
                    access.engagement_id,
                    access.stakeholder_id,
                    access.thread_id,
                ),
            )
            if row is None:
                raise AccessDeniedError
            transcript = self._transcript(row) if row["stored_transcript_id"] else None
            version = (
                self._transcript_version(row) if row["transcript_ingestion_version_id"] else None
            )
            turns: tuple[TranscriptTurn, ...] = ()
            if row["stored_transcript_id"]:
                cursor = await connection.execute(
                    """
                    SELECT * FROM transcript_turns
                    WHERE transcript_id = ? ORDER BY turn_index
                    """,
                    (row["stored_transcript_id"],),
                )
                turns = tuple(self._transcript_turn(item) for item in await cursor.fetchall())
                if tuple(item.turn_index for item in turns) != tuple(range(len(turns))):
                    raise AccessDeniedError
            return InterviewStatusRecord(
                session=self._session(row),
                transcript=transcript,
                version=version,
                turns=turns,
                turn_count=int(row["turn_count"] or 0),
            )

    async def finalized_interview(
        self,
        access: AccessContext,
        interview_session_id: str,
        *,
        now: datetime,
    ) -> FinalizedInterviewRecord:
        """Return one finalized transcript without widening the PM's engagement scope."""
        if access.principal_type != "pm":
            raise AccessDeniedError
        async with self._database.connection() as connection:
            await self._resolve_access(connection, access, now, permission="session:read")
            row = await self._fetchone(
                connection,
                """
                SELECT i.*,
                    t.transcript_id AS stored_transcript_id,
                    t.role AS transcript_role,
                    t.department AS transcript_department,
                    t.status AS transcript_status,
                    t.language_observations_json,
                    t.finalized_at AS transcript_finalized_at,
                    t.content_hash AS transcript_content_hash
                FROM interview_sessions AS i
                INNER JOIN transcripts AS t
                    ON t.interview_session_id = i.interview_session_id
                WHERE i.interview_session_id = ? AND i.engagement_id = ?
                    AND i.finalized_at IS NOT NULL
                    AND t.status = 'finalized' AND t.finalized_at IS NOT NULL
                """,
                (interview_session_id, access.engagement_id),
            )
            if row is None:
                raise AccessDeniedError
            transcript = self._transcript(row)
            cursor = await connection.execute(
                """
                SELECT * FROM transcript_turns
                WHERE transcript_id = ? ORDER BY turn_index
                """,
                (transcript.transcript_id,),
            )
            turns = tuple(self._transcript_turn(item) for item in await cursor.fetchall())
            if tuple(item.turn_index for item in turns) != tuple(range(len(turns))):
                raise AccessDeniedError
            return FinalizedInterviewRecord(
                session=self._session(row),
                transcript=transcript,
                turns=turns,
            )

    async def insight_run(
        self,
        access: AccessContext,
        run_id: str,
        *,
        now: datetime,
    ) -> InsightRun:
        """Resolve one PM-owned run before rebuilding its exact runtime context."""
        if access.principal_type != "pm":
            raise AccessDeniedError
        async with self._database.connection() as connection:
            await self._resolve_access(connection, access, now, permission="insight:run")
            row = await self._fetchone(
                connection,
                """
                SELECT * FROM insight_runs
                WHERE run_id = ? AND engagement_id = ?
                    AND requested_by_pm_access_id = ?
                """,
                (run_id, access.engagement_id, access.principal_id),
            )
            if row is None:
                raise AccessDeniedError
            return self._run(row)

    async def insight_runs(
        self,
        access: AccessContext,
        *,
        now: datetime,
        limit: int = 50,
    ) -> tuple[InsightRun, ...]:
        """List recent runs owned by this PM in the selected engagement."""
        if access.principal_type != "pm" or not 1 <= limit <= _MAX_INSIGHT_HISTORY_LIMIT:
            raise AccessDeniedError
        async with self._database.connection() as connection:
            await self._resolve_access(connection, access, now, permission="insight:run")
            cursor = await connection.execute(
                """
                SELECT * FROM insight_runs
                WHERE engagement_id = ? AND requested_by_pm_access_id = ?
                    AND status IN ('complete', 'partial', 'insufficient_evidence')
                    AND report_id IS NOT NULL
                ORDER BY started_at DESC, run_id DESC
                LIMIT ?
                """,
                (access.engagement_id, access.principal_id, limit),
            )
            return tuple(self._run(row) for row in await cursor.fetchall())

    async def _resolve_access(
        self,
        connection: aiosqlite.Connection,
        access: AccessContext,
        now: datetime,
        *,
        permission: str,
    ) -> None:
        self._require_clock(now)
        try:
            access.require_permission(permission)
            access.require_active(now)
        except (PermissionError, ValueError) as error:
            raise AccessDeniedError from error
        timestamp = self._time(now)
        if access.principal_type == "pm":
            row = await self._fetchone(
                connection,
                """
                SELECT 1
                FROM access_sessions AS a
                JOIN pm_access AS p ON p.pm_access_id = a.principal_id
                JOIN engagements AS e ON e.engagement_id = a.engagement_id
                WHERE a.principal_type = 'pm' AND a.principal_id = ?
                    AND a.engagement_id = ? AND a.revoked_at IS NULL
                    AND a.expires_at > ? AND p.status = 'active'
                    AND e.status = 'active'
                LIMIT 1
                """,
                (access.principal_id, access.engagement_id, timestamp),
            )
        else:
            row = await self._fetchone(
                connection,
                """
                SELECT 1
                FROM access_sessions AS a
                JOIN stakeholders AS s
                    ON s.stakeholder_id = a.stakeholder_id
                    AND s.engagement_id = a.engagement_id
                JOIN interview_sessions AS i
                    ON i.interview_session_id = a.interview_session_id
                    AND i.stakeholder_id = a.stakeholder_id
                    AND i.engagement_id = a.engagement_id
                JOIN engagements AS e ON e.engagement_id = a.engagement_id
                WHERE a.principal_type = 'stakeholder' AND a.principal_id = ?
                    AND a.engagement_id = ? AND a.stakeholder_id = ?
                    AND a.interview_session_id = ? AND a.thread_id = ?
                    AND a.revoked_at IS NULL AND a.expires_at > ?
                    AND s.status = 'active' AND e.status = 'active'
                LIMIT 1
                """,
                (
                    access.principal_id,
                    access.engagement_id,
                    access.stakeholder_id,
                    access.interview_session_id,
                    access.thread_id,
                    timestamp,
                ),
            )
        if row is None:
            raise AccessDeniedError

    @staticmethod
    async def _fetchone(
        connection: aiosqlite.Connection,
        query: str,
        parameters: tuple[object, ...],
    ) -> aiosqlite.Row | None:
        cursor = await connection.execute(query, parameters)
        return await cursor.fetchone()

    @staticmethod
    def _bounded_preview(value: object) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None
        if len(text) <= _TEXT_PREVIEW_LIMIT:
            return text
        return f"{text[: _TEXT_PREVIEW_LIMIT - 3].rstrip()}..."

    @staticmethod
    def _engagement(row: aiosqlite.Row) -> Engagement:
        return Engagement(
            engagement_id=row["engagement_id"],
            name=row["name"],
            description=row["description"],
            status=row["status"],
            created_at=DomainReadRepository._parse_time(row["created_at"]),
            updated_at=DomainReadRepository._parse_time(row["updated_at"]),
        )

    @staticmethod
    def _stakeholder(row: aiosqlite.Row) -> Stakeholder:
        return Stakeholder(
            stakeholder_id=row["stakeholder_id"],
            engagement_id=row["engagement_id"],
            display_name=row["display_name"],
            role=row["role"],
            department=row["department"],
            status=row["status"],
            created_at=DomainReadRepository._parse_time(row["created_at"]),
            updated_at=DomainReadRepository._parse_time(row["updated_at"]),
        )

    @staticmethod
    def _invitation(row: aiosqlite.Row) -> InvitationSummaryRecord:
        return InvitationSummaryRecord(
            invitation_id=row["invitation_id"],
            engagement_id=row["engagement_id"],
            stakeholder_id=row["stakeholder_id"],
            status=row["status"],
            created_at=DomainReadRepository._parse_time(row["created_at"]),
            expires_at=DomainReadRepository._parse_time(row["expires_at"]),
            activated_at=DomainReadRepository._parse_optional_time(row["activated_at"]),
            revoked_at=DomainReadRepository._parse_optional_time(row["revoked_at"]),
        )

    @staticmethod
    def _document(row: aiosqlite.Row) -> DocumentSummaryRecord:
        return DocumentSummaryRecord(
            source=DocumentSource(
                document_id=row["document_id"],
                engagement_id=row["engagement_id"],
                stakeholder_id=row["stakeholder_id"],
                role=row["role"],
                department=row["department"],
                doc_type=row["doc_type"],
                source_type=row["source_type"],
                original_filename=row["original_filename"],
                media_type=row["media_type"],
                created_at=DomainReadRepository._parse_time(row["created_at"]),
            ),
            version=DocumentVersion(
                document_version_id=row["document_version_id"],
                document_id=row["document_id"],
                version_number=row["version_number"],
                content_hash=row["content_hash"],
                state=row["state"],
                is_active=bool(row["is_active"]),
                original_artifact_id=row["original_artifact_id"],
                ingestion_key=row["ingestion_key"],
                created_at=DomainReadRepository._parse_time(row["version_created_at"]),
                ready_at=DomainReadRepository._parse_optional_time(row["ready_at"]),
                superseded_at=DomainReadRepository._parse_optional_time(row["superseded_at"]),
                failure_code=row["failure_code"],
                failure_message=row["failure_message"],
            ),
        )

    @staticmethod
    def _session(row: aiosqlite.Row) -> InterviewSession:
        return InterviewSession(
            interview_session_id=row["interview_session_id"],
            engagement_id=row["engagement_id"],
            stakeholder_id=row["stakeholder_id"],
            invitation_id=row["invitation_id"],
            thread_id=row["thread_id"],
            status=row["status"],
            started_at=DomainReadRepository._parse_time(row["started_at"]),
            finalized_at=DomainReadRepository._parse_optional_time(row["finalized_at"]),
            transcript_id=row["transcript_id"],
            ingestion_version_id=row["ingestion_version_id"],
            failure_code=row["failure_code"],
            failure_message=row["failure_message"],
        )

    @staticmethod
    def _transcript(row: aiosqlite.Row) -> Transcript:
        return Transcript(
            transcript_id=row["stored_transcript_id"],
            interview_session_id=row["interview_session_id"],
            engagement_id=row["engagement_id"],
            stakeholder_id=row["stakeholder_id"],
            role=row["transcript_role"],
            department=row["transcript_department"],
            status=row["transcript_status"],
            language_observations=tuple(json.loads(row["language_observations_json"])),
            finalized_at=DomainReadRepository._parse_optional_time(row["transcript_finalized_at"]),
            content_hash=row["transcript_content_hash"],
        )

    @staticmethod
    def _transcript_version(row: aiosqlite.Row) -> TranscriptIngestionVersion:
        return TranscriptIngestionVersion(
            transcript_ingestion_version_id=row["transcript_ingestion_version_id"],
            transcript_id=row["stored_transcript_id"],
            content_hash=row["version_content_hash"],
            state=row["version_state"],
            is_active=bool(row["version_is_active"]),
            created_at=DomainReadRepository._parse_time(row["version_created_at"]),
            ready_at=DomainReadRepository._parse_optional_time(row["version_ready_at"]),
            failure_code=row["version_failure_code"],
            failure_message=row["version_failure_message"],
        )

    @staticmethod
    def _transcript_turn(row: aiosqlite.Row) -> TranscriptTurn:
        return TranscriptTurn(
            turn_index=row["turn_index"],
            speaker=row["speaker"],
            original_text=row["original_text"],
            created_at=DomainReadRepository._parse_time(row["created_at"]),
            checkpoint_message_id=row["checkpoint_message_id"],
        )

    @staticmethod
    def _run(row: aiosqlite.Row) -> InsightRun:
        return InsightRun(
            run_id=row["run_id"],
            engagement_id=row["engagement_id"],
            thread_id=row["thread_id"],
            status=row["status"],
            requested_question=row["requested_question"],
            plan_id=row["plan_id"],
            report_id=row["report_id"],
            failure_code=row["failure_code"],
            failure_message=row["failure_message"],
            started_at=DomainReadRepository._parse_time(row["started_at"]),
            completed_at=DomainReadRepository._parse_optional_time(row["completed_at"]),
        )

    @staticmethod
    def _time(value: datetime) -> str:
        return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")

    @staticmethod
    def _parse_time(value: object) -> datetime:
        return datetime.fromisoformat(str(value)).astimezone(UTC)

    @staticmethod
    def _parse_optional_time(value: object) -> datetime | None:
        return None if value is None else DomainReadRepository._parse_time(value)

    @staticmethod
    def _require_clock(value: datetime) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError
