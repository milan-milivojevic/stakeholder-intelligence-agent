"""SQLite authority for active retrieval versions and immutable evidence records."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from hashlib import sha256
from math import isclose
from typing import TYPE_CHECKING, cast

from pydantic import TypeAdapter, ValidationError

from stakeholder_intelligence_agent.contracts.evidence import EvidenceRecord
from stakeholder_intelligence_agent.contracts.source import SourceLocation
from stakeholder_intelligence_agent.errors import (
    AccessDeniedError,
    EvidenceRegistrationError,
)
from stakeholder_intelligence_agent.ingestion.identity import stable_id
from stakeholder_intelligence_agent.retrieval.types import (
    RetrievedItem,
    SourceArtifactReference,
    StakeholderFilterCandidate,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    import aiosqlite

    from stakeholder_intelligence_agent.contracts.access import AccessContext
    from stakeholder_intelligence_agent.contracts.retrieval import RetrievalFilterInput
    from stakeholder_intelligence_agent.persistence.domain import DomainDatabase

_LOCATION_ADAPTER: TypeAdapter[SourceLocation] = TypeAdapter(SourceLocation)
_BOUNDING_BOX_ABS_TOLERANCE = 1e-9
_BOUNDING_BOX_COORDINATES = ("x0", "y0", "x1", "y1")


class RetrievalRepository:
    """Recheck access and keep SQLite the active-version and evidence authority."""

    def __init__(self, database: DomainDatabase) -> None:
        self._database = database

    async def initialize(self) -> None:
        """Apply the evidence-registry migration idempotently."""
        await self._database.initialize()

    async def active_ready_version_ids(
        self,
        access: AccessContext,
        filters: RetrievalFilterInput,
        *,
        now: datetime,
    ) -> tuple[str, ...]:
        """Return only authoritative active READY versions in the caller's scope."""
        self._require_clock(now)
        document_parameters: tuple[object, ...] = (
            access.engagement_id,
            filters.stakeholder_id,
            filters.stakeholder_id,
            filters.role,
            filters.role,
            filters.department,
            filters.department,
            filters.doc_type,
            filters.doc_type,
            filters.source_type,
            filters.source_type,
        )
        transcript_parameters: tuple[object, ...] = (
            access.engagement_id,
            filters.stakeholder_id,
            filters.stakeholder_id,
            filters.role,
            filters.role,
            filters.department,
            filters.department,
            filters.doc_type,
            filters.doc_type,
            filters.source_type,
            filters.source_type,
        )
        async with self._database.connection() as connection:
            await self._resolve_access(connection, access, now)
            document_cursor = await connection.execute(
                """
                SELECT DISTINCT v.document_version_id
                FROM document_versions AS v
                JOIN document_sources AS s ON s.document_id = v.document_id
                JOIN search_chunks AS c
                    ON c.source_version_id = v.document_version_id
                WHERE s.engagement_id = ?
                    AND v.state = 'READY' AND v.is_active = 1
                    AND c.is_active_ready = 1 AND c.vector_stage_state = 'ACTIVE'
                    AND (? IS NULL OR s.stakeholder_id = ?)
                    AND (? IS NULL OR s.role = ?)
                    AND (? IS NULL OR s.department = ?)
                    AND (? IS NULL OR s.doc_type = ?)
                    AND (? IS NULL OR s.source_type = ?)
                ORDER BY v.document_version_id
                """,
                document_parameters,
            )
            transcript_cursor = await connection.execute(
                """
                SELECT DISTINCT v.transcript_ingestion_version_id AS version_id
                FROM transcript_ingestion_versions AS v
                JOIN transcripts AS t ON t.transcript_id = v.transcript_id
                JOIN transcript_search_chunks AS c
                    ON c.source_version_id = v.transcript_ingestion_version_id
                WHERE t.engagement_id = ? AND t.status = 'finalized'
                    AND v.state = 'READY' AND v.is_active = 1
                    AND c.is_active_ready = 1 AND c.vector_stage_state = 'ACTIVE'
                    AND (? IS NULL OR t.stakeholder_id = ?)
                    AND (? IS NULL OR t.role = ?)
                    AND (? IS NULL OR t.department = ?)
                    AND (? IS NULL OR ? = 'transcript')
                    AND (? IS NULL OR ? = 'interview')
                ORDER BY v.transcript_ingestion_version_id
                """,
                transcript_parameters,
            )
            document_ids = tuple(
                str(row["document_version_id"]) for row in await document_cursor.fetchall()
            )
            transcript_ids = tuple(
                str(row["version_id"]) for row in await transcript_cursor.fetchall()
            )
            return tuple(sorted((*document_ids, *transcript_ids)))

    async def stakeholder_filter_candidates(
        self,
        access: AccessContext,
        *,
        now: datetime,
    ) -> tuple[StakeholderFilterCandidate, ...]:
        """Resolve a same-engagement name directory without widening stakeholder access."""
        self._require_clock(now)
        async with self._database.connection() as connection:
            await self._resolve_access(connection, access, now)
            if access.principal_type == "pm":
                parameters: tuple[object, ...] = (access.engagement_id,)
                statement = """
                    SELECT stakeholder_id, display_name, role, department
                    FROM stakeholders
                    WHERE engagement_id = ? AND status = 'active'
                    ORDER BY display_name, stakeholder_id
                """
            else:
                if access.stakeholder_id is None:
                    raise AccessDeniedError
                parameters = (access.engagement_id, access.stakeholder_id)
                statement = """
                    SELECT stakeholder_id, display_name, role, department
                    FROM stakeholders
                    WHERE engagement_id = ? AND status = 'active'
                        AND stakeholder_id = ?
                    ORDER BY display_name, stakeholder_id
                """
            cursor = await connection.execute(
                statement,
                parameters,
            )
            return tuple(
                StakeholderFilterCandidate(
                    stakeholder_id=str(row["stakeholder_id"]),
                    display_name=str(row["display_name"]),
                    role=None if row["role"] is None else str(row["role"]),
                    department=(None if row["department"] is None else str(row["department"])),
                )
                for row in await cursor.fetchall()
            )

    async def register_evidence(
        self,
        access: AccessContext,
        *,
        run_id: str,
        topic_id: str,
        researcher_id: str,
        item: RetrievedItem,
        now: datetime,
    ) -> EvidenceRecord:
        """Register an item only if its exact chunk remains permitted and active."""
        self._require_clock(now)
        async with self._database.transaction() as connection:
            await self._resolve_access(connection, access, now)
            row = await self._active_chunk(connection, access, item.candidate.chunk_id)
            authoritative_location = self._match_item(row, item)
            content_hash = sha256(item.original_excerpt.encode("utf-8")).hexdigest()
            evidence_id = stable_id(
                "evidence",
                run_id,
                topic_id,
                researcher_id,
                item.candidate.chunk_id,
                content_hash,
            )
            record = EvidenceRecord(
                evidence_id=evidence_id,
                run_id=run_id,
                engagement_id=access.engagement_id,
                topic_id=topic_id,
                source_id=item.source_id,
                source_version_id=item.source_version_id,
                source_type=item.candidate.metadata.source_type,
                stakeholder_id=item.candidate.metadata.stakeholder_id,
                location=authoritative_location,
                original_excerpt=item.original_excerpt,
                english_interpretation=None,
                content_hash=content_hash,
                researcher_id=researcher_id,
                created_at=now,
            )
            await connection.execute(
                """
                INSERT OR IGNORE INTO evidence_records(
                    evidence_id, run_id, engagement_id, topic_id, researcher_id,
                    chunk_id, source_id, source_version_id, source_type,
                    stakeholder_id, location_json, original_excerpt,
                    english_interpretation, content_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.evidence_id,
                    record.run_id,
                    record.engagement_id,
                    record.topic_id,
                    record.researcher_id,
                    item.candidate.chunk_id,
                    record.source_id,
                    record.source_version_id,
                    record.source_type,
                    record.stakeholder_id,
                    self._json(record.location.model_dump(mode="json")),
                    record.original_excerpt,
                    record.english_interpretation,
                    record.content_hash,
                    self._time(record.created_at),
                ),
            )
            stored_row = await self._fetchone(
                connection,
                "SELECT * FROM evidence_records WHERE evidence_id = ?",
                (evidence_id,),
            )
            if stored_row is None:
                raise EvidenceRegistrationError
            stored = self._evidence(stored_row)
            if stored.model_dump(exclude={"created_at"}) != record.model_dump(
                exclude={"created_at"}
            ):
                raise EvidenceRegistrationError
            await self._append_audit(connection, access, record, now)
            return stored

    async def load_evidence(
        self,
        access: AccessContext,
        evidence_id: str,
        *,
        now: datetime,
    ) -> EvidenceRecord:
        """Load one same-engagement record only while its source remains active."""
        self._require_clock(now)
        async with self._database.connection() as connection:
            await self._resolve_access(connection, access, now)
            row = await self._fetchone(
                connection,
                """
                SELECT e.*
                FROM evidence_records AS e
                JOIN search_chunks AS c ON c.chunk_id = e.chunk_id
                JOIN document_versions AS v
                    ON v.document_version_id = e.source_version_id
                WHERE e.evidence_id = ? AND e.engagement_id = ?
                    AND c.engagement_id = e.engagement_id
                    AND c.source_id = e.source_id
                    AND c.source_version_id = e.source_version_id
                    AND c.is_active_ready = 1 AND c.vector_stage_state = 'ACTIVE'
                    AND v.state = 'READY' AND v.is_active = 1
                """,
                (evidence_id, access.engagement_id),
            )
            if row is None:
                row = await self._fetchone(
                    connection,
                    """
                    SELECT e.*
                    FROM evidence_records AS e
                    JOIN transcript_search_chunks AS c ON c.chunk_id = e.chunk_id
                    JOIN transcript_ingestion_versions AS v
                        ON v.transcript_ingestion_version_id = e.source_version_id
                    JOIN transcripts AS t ON t.transcript_id = e.source_id
                    WHERE e.evidence_id = ? AND e.engagement_id = ?
                        AND e.source_type = 'interview'
                        AND c.engagement_id = e.engagement_id
                        AND c.source_id = e.source_id
                        AND c.source_version_id = e.source_version_id
                        AND c.is_active_ready = 1
                        AND c.vector_stage_state = 'ACTIVE'
                        AND v.state = 'READY' AND v.is_active = 1
                        AND t.status = 'finalized'
                    """,
                    (evidence_id, access.engagement_id),
                )
            if row is None:
                raise EvidenceRegistrationError
            return self._evidence(row)

    async def source_artifacts(
        self,
        access: AccessContext,
        evidence_id: str,
        *,
        now: datetime,
    ) -> tuple[EvidenceRecord, SourceArtifactReference, tuple[SourceArtifactReference, ...]]:
        """Return virtual artifact references after the same active-source validation."""
        evidence = await self.load_evidence(access, evidence_id, now=now)
        if evidence.source_type == "interview":
            async with self._database.connection() as connection:
                await self._resolve_access(connection, access, now)
                row = await self._fetchone(
                    connection,
                    """
                    SELECT content_hash FROM transcripts
                    WHERE transcript_id = ? AND engagement_id = ?
                        AND status = 'finalized'
                    """,
                    (evidence.source_id, access.engagement_id),
                )
            if row is None or row["content_hash"] is None:
                raise EvidenceRegistrationError
            original = SourceArtifactReference(
                artifact_id=stable_id("transcript-source", evidence.source_id),
                artifact_kind="raw_transcript",
                virtual_path=f"/transcripts/{evidence.source_id}",
                media_type="application/vnd.stakeholder-intelligence.transcript+json",
                content_hash=str(row["content_hash"]),
            )
            return evidence, original, ()
        async with self._database.connection() as connection:
            await self._resolve_access(connection, access, now)
            cursor = await connection.execute(
                """
                SELECT artifact_id, artifact_kind, virtual_path, media_type, content_hash
                FROM ingestion_artifacts
                WHERE engagement_id = ? AND document_version_id = ?
                ORDER BY CASE WHEN artifact_kind = 'original' THEN 0 ELSE 1 END,
                    artifact_kind, artifact_id
                """,
                (access.engagement_id, evidence.source_version_id),
            )
            references = tuple(self._artifact(row) for row in await cursor.fetchall())
        originals = tuple(item for item in references if item.artifact_kind == "original")
        if len(originals) != 1:
            raise EvidenceRegistrationError
        return (
            evidence,
            originals[0],
            tuple(item for item in references if item.artifact_kind != "original"),
        )

    async def _resolve_access(
        self,
        connection: aiosqlite.Connection,
        access: AccessContext,
        now: datetime,
    ) -> None:
        try:
            access.require_permission("source:read")
            access.require_active(now)
        except (PermissionError, ValueError) as error:
            raise AccessDeniedError from error
        timestamp = self._time(now)
        if access.principal_type == "pm":
            if access.stakeholder_id is not None:
                raise AccessDeniedError
            row = await self._fetchone(
                connection,
                """
                SELECT 1
                FROM pm_access AS p
                JOIN access_sessions AS a
                    ON a.principal_type = 'pm' AND a.principal_id = p.pm_access_id
                JOIN engagements AS e ON e.engagement_id = a.engagement_id
                WHERE p.pm_access_id = ? AND p.status = 'active'
                    AND a.engagement_id = ? AND a.revoked_at IS NULL
                    AND a.expires_at > ? AND e.status = 'active'
                LIMIT 1
                """,
                (access.principal_id, access.engagement_id, timestamp),
            )
            if row is None:
                raise AccessDeniedError
            return
        if (
            access.stakeholder_id is None
            or access.interview_session_id is None
            or access.thread_id is None
            or access.principal_id != access.stakeholder_id
        ):
            raise AccessDeniedError
        row = await self._fetchone(
            connection,
            """
            SELECT 1
            FROM access_sessions AS a
            JOIN stakeholders AS s
                ON s.stakeholder_id = a.stakeholder_id
                AND s.engagement_id = a.engagement_id
            JOIN engagements AS e ON e.engagement_id = a.engagement_id
            JOIN interview_sessions AS i
                ON i.interview_session_id = a.interview_session_id
                AND i.stakeholder_id = a.stakeholder_id
                AND i.engagement_id = a.engagement_id
            WHERE a.principal_type = 'stakeholder' AND a.principal_id = ?
                AND a.engagement_id = ? AND a.stakeholder_id = ?
                AND a.interview_session_id = ? AND a.thread_id = ?
                AND a.revoked_at IS NULL AND a.expires_at > ?
                AND s.status = 'active' AND e.status = 'active'
                AND i.status IN ('draft', 'finalizing', 'finalized', 'ingesting', 'ready')
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
    async def _active_chunk(
        connection: aiosqlite.Connection,
        access: AccessContext,
        chunk_id: str,
    ) -> aiosqlite.Row:
        row = await RetrievalRepository._fetchone(
            connection,
            """
            SELECT c.*, v.state AS version_state, v.is_active AS version_active
            FROM search_chunks AS c
            JOIN document_versions AS v
                ON v.document_version_id = c.source_version_id
            JOIN document_sources AS s ON s.document_id = c.source_id
            WHERE c.chunk_id = ? AND c.engagement_id = ?
                AND s.engagement_id = c.engagement_id
                AND v.state = 'READY' AND v.is_active = 1
                AND c.is_active_ready = 1 AND c.vector_stage_state = 'ACTIVE'
            """,
            (chunk_id, access.engagement_id),
        )
        if row is None:
            row = await RetrievalRepository._fetchone(
                connection,
                """
                SELECT c.*, v.state AS version_state, v.is_active AS version_active
                FROM transcript_search_chunks AS c
                JOIN transcript_ingestion_versions AS v
                    ON v.transcript_ingestion_version_id = c.source_version_id
                JOIN transcripts AS t ON t.transcript_id = c.source_id
                WHERE c.chunk_id = ? AND c.engagement_id = ?
                    AND t.engagement_id = c.engagement_id
                    AND t.status = 'finalized'
                    AND v.state = 'READY' AND v.is_active = 1
                    AND c.is_active_ready = 1 AND c.vector_stage_state = 'ACTIVE'
                """,
                (chunk_id, access.engagement_id),
            )
        if row is None:
            raise EvidenceRegistrationError
        return row

    @staticmethod
    def _match_item(row: aiosqlite.Row, item: RetrievedItem) -> SourceLocation:
        try:
            location = _LOCATION_ADAPTER.validate_python(json.loads(row["location_json"]))
            element_ids = tuple(cast("list[str]", json.loads(row["element_ids_json"])))
        except (TypeError, ValueError, ValidationError) as error:
            raise EvidenceRegistrationError from error
        candidate = item.candidate
        expected = (
            row["chunk_id"],
            row["source_id"],
            row["source_version_id"],
            element_ids,
            row["text_for_retrieval"],
            row["engagement_id"],
            row["stakeholder_id"],
            row["role"],
            row["department"],
            row["doc_type"],
            row["source_type"],
        )
        supplied = (
            candidate.chunk_id,
            item.source_id,
            item.source_version_id,
            item.element_ids,
            item.original_excerpt,
            candidate.metadata.engagement_id,
            candidate.metadata.stakeholder_id,
            candidate.metadata.role,
            candidate.metadata.department,
            candidate.metadata.doc_type,
            candidate.metadata.source_type,
        )
        if expected != supplied or not RetrievalRepository._locations_match(
            location,
            candidate.location,
        ):
            raise EvidenceRegistrationError
        return location

    @staticmethod
    def _locations_match(expected: SourceLocation, supplied: SourceLocation) -> bool:
        """Accept only serialization-scale bounding-box drift from the vector store."""
        expected_payload = expected.model_dump(mode="python")
        supplied_payload = supplied.model_dump(mode="python")
        expected_box = expected_payload.pop("bounding_box", None)
        supplied_box = supplied_payload.pop("bounding_box", None)
        if expected_payload != supplied_payload:
            return False
        if expected_box is None or supplied_box is None:
            return expected_box is supplied_box
        if not isinstance(expected_box, dict) or not isinstance(supplied_box, dict):
            return False
        if expected_box.get("coordinate_space") != supplied_box.get("coordinate_space"):
            return False
        try:
            return all(
                isclose(
                    float(expected_box[key]),
                    float(supplied_box[key]),
                    rel_tol=0.0,
                    abs_tol=_BOUNDING_BOX_ABS_TOLERANCE,
                )
                for key in _BOUNDING_BOX_COORDINATES
            )
        except (KeyError, TypeError, ValueError):
            return False

    @staticmethod
    async def _append_audit(
        connection: aiosqlite.Connection,
        access: AccessContext,
        record: EvidenceRecord,
        now: datetime,
    ) -> None:
        event_id = stable_id("audit", "register-evidence", record.evidence_id)
        await connection.execute(
            """
            INSERT OR IGNORE INTO operational_audit_events(
                event_id, occurred_at, run_id, engagement_id, thread_id,
                actor, action, status, duration_ms, source_ids_json,
                evidence_ids_json, retry_count, failure_code, correlation_id
            ) VALUES (?, ?, ?, ?, ?, 'retrieval_service', 'register_evidence',
                'succeeded', NULL, ?, ?, NULL, NULL, ?)
            """,
            (
                event_id,
                RetrievalRepository._time(now),
                record.run_id,
                record.engagement_id,
                access.thread_id,
                RetrievalRepository._json([record.source_id]),
                RetrievalRepository._json([record.evidence_id]),
                access.correlation_id,
            ),
        )

    @staticmethod
    def _evidence(row: aiosqlite.Row) -> EvidenceRecord:
        try:
            location = _LOCATION_ADAPTER.validate_python(json.loads(row["location_json"]))
        except (TypeError, ValueError, ValidationError) as error:
            raise EvidenceRegistrationError from error
        return EvidenceRecord(
            evidence_id=row["evidence_id"],
            run_id=row["run_id"],
            engagement_id=row["engagement_id"],
            topic_id=row["topic_id"],
            source_id=row["source_id"],
            source_version_id=row["source_version_id"],
            source_type=row["source_type"],
            stakeholder_id=row["stakeholder_id"],
            location=location,
            original_excerpt=row["original_excerpt"],
            english_interpretation=row["english_interpretation"],
            content_hash=row["content_hash"],
            researcher_id=row["researcher_id"],
            created_at=RetrievalRepository._parse_time(row["created_at"]),
        )

    @staticmethod
    def _artifact(row: aiosqlite.Row) -> SourceArtifactReference:
        return SourceArtifactReference(
            artifact_id=str(row["artifact_id"]),
            artifact_kind=str(row["artifact_kind"]),
            virtual_path=str(row["virtual_path"]),
            media_type=str(row["media_type"]),
            content_hash=str(row["content_hash"]),
        )

    @staticmethod
    async def _fetchone(
        connection: aiosqlite.Connection,
        sql: str,
        parameters: Sequence[object],
    ) -> aiosqlite.Row | None:
        cursor = await connection.execute(sql, tuple(parameters))
        return await cursor.fetchone()

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
    def _require_clock(value: datetime) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Retrieval requires an aware timestamp.")
