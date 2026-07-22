"""Transactional SQLite authority for exact interview transcripts and indexing."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import TYPE_CHECKING, Literal, cast

from pydantic import TypeAdapter

from stakeholder_intelligence_agent.contracts import (
    InterviewSession,
    Transcript,
    TranscriptIngestionVersion,
    TranscriptTurn,
)
from stakeholder_intelligence_agent.contracts.lifecycle import (
    validate_interview_transition,
    validate_transcript_transition,
    validate_transcript_version_transition,
)
from stakeholder_intelligence_agent.contracts.source import SearchChunk, SourceLocation
from stakeholder_intelligence_agent.errors import (
    AccessDeniedError,
    InterviewLifecycleError,
    TranscriptImmutableError,
    TranscriptIngestionError,
)
from stakeholder_intelligence_agent.ingestion.identity import stable_id
from stakeholder_intelligence_agent.interview.types import (
    FinalizationResult,
    StoredTranscriptTurn,
    TranscriptIngestionResult,
    TranscriptSnapshot,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    import aiosqlite

    from stakeholder_intelligence_agent.contracts import AccessContext
    from stakeholder_intelligence_agent.persistence import DomainDatabase

_LOCATION_ADAPTER: TypeAdapter[SourceLocation] = TypeAdapter(SourceLocation)


class TranscriptRepository:
    """Preserve raw turns and enforce the one-way finalization/READY lifecycle."""

    def __init__(self, database: DomainDatabase, *, lease_seconds: int = 300) -> None:
        self._database = database
        self._lease_seconds = lease_seconds

    async def initialize(self) -> None:
        """Apply all forward-only domain migrations."""
        await self._database.initialize()

    async def append_turn(
        self,
        access: AccessContext,
        *,
        speaker: Literal["stakeholder", "assistant"],
        original_text: str,
        checkpoint_message_id: str | None,
        now: datetime,
    ) -> StoredTranscriptTurn:
        """Append one exact turn, or resolve the same checkpoint message idempotently."""
        self._require_clock(now)
        async with self._database.transaction() as connection:
            session_row, stakeholder_row = await self._resolve_interview(
                connection,
                access,
                now,
                permission="interview:participate",
            )
            transcript_row = await self._fetchone(
                connection,
                "SELECT * FROM transcripts WHERE interview_session_id = ?",
                (access.interview_session_id,),
            )
            if transcript_row is None:
                if session_row["status"] != "draft":
                    raise TranscriptImmutableError
                transcript_id = stable_id("transcript", str(access.interview_session_id))
                await connection.execute(
                    """
                    INSERT INTO transcripts(
                        transcript_id, interview_session_id, engagement_id,
                        stakeholder_id, role, department, status,
                        language_observations_json, created_at, finalized_at, content_hash
                    ) VALUES (?, ?, ?, ?, ?, ?, 'draft', '[]', ?, NULL, NULL)
                    """,
                    (
                        transcript_id,
                        access.interview_session_id,
                        access.engagement_id,
                        access.stakeholder_id,
                        stakeholder_row["role"],
                        stakeholder_row["department"],
                        self._time(now),
                    ),
                )
                await connection.execute(
                    """
                    UPDATE interview_sessions SET transcript_id = ?
                    WHERE interview_session_id = ? AND status = 'draft'
                    """,
                    (transcript_id, access.interview_session_id),
                )
            else:
                transcript_id = str(transcript_row["transcript_id"])

            if checkpoint_message_id is not None:
                existing = await self._fetchone(
                    connection,
                    """
                    SELECT * FROM transcript_turns
                    WHERE transcript_id = ? AND checkpoint_message_id = ?
                    """,
                    (transcript_id, checkpoint_message_id),
                )
                if existing is not None:
                    stored = self._stored_turn(existing)
                    if (
                        stored.value.speaker != speaker
                        or stored.value.original_text != original_text
                    ):
                        raise InterviewLifecycleError
                    return stored

            if session_row["status"] != "draft":
                raise TranscriptImmutableError
            cursor = await connection.execute(
                """
                SELECT COALESCE(MAX(turn_index), -1) + 1 AS next_turn
                FROM transcript_turns WHERE transcript_id = ?
                """,
                (transcript_id,),
            )
            index_row = await cursor.fetchone()
            if index_row is None:
                raise InterviewLifecycleError
            turn = TranscriptTurn(
                turn_index=int(index_row["next_turn"]),
                speaker=speaker,
                original_text=original_text,
                created_at=now,
                checkpoint_message_id=checkpoint_message_id,
            )
            turn_id = stable_id("transcript-turn", transcript_id, str(turn.turn_index))
            await connection.execute(
                """
                INSERT INTO transcript_turns(
                    turn_id, transcript_id, turn_index, speaker, original_text,
                    created_at, checkpoint_message_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    turn_id,
                    transcript_id,
                    turn.turn_index,
                    turn.speaker,
                    turn.original_text,
                    self._time(turn.created_at),
                    turn.checkpoint_message_id,
                ),
            )
            await self._append_audit(
                connection,
                access,
                action="append_interview_turn",
                discriminator=turn_id,
                now=now,
            )
            return StoredTranscriptTurn(turn_id=turn_id, transcript_id=transcript_id, value=turn)

    async def snapshot(self, access: AccessContext, *, now: datetime) -> TranscriptSnapshot:
        """Load the complete exact transcript after rechecking persistent access."""
        self._require_clock(now)
        async with self._database.connection() as connection:
            await self._resolve_interview(
                connection,
                access,
                now,
                permission="interview:participate",
            )
            return await self._snapshot(connection, str(access.interview_session_id))

    async def list_turns(
        self,
        access: AccessContext,
        *,
        now: datetime,
    ) -> tuple[StoredTranscriptTurn, ...]:
        """Load exact turns without requiring a transcript to exist yet."""
        self._require_clock(now)
        async with self._database.connection() as connection:
            await self._resolve_interview(
                connection,
                access,
                now,
                permission="interview:participate",
            )
            transcript_row = await self._fetchone(
                connection,
                "SELECT transcript_id FROM transcripts WHERE interview_session_id = ?",
                (access.interview_session_id,),
            )
            if transcript_row is None:
                return ()
            cursor = await connection.execute(
                """
                SELECT * FROM transcript_turns
                WHERE transcript_id = ? ORDER BY turn_index
                """,
                (transcript_row["transcript_id"],),
            )
            turns = tuple(self._stored_turn(row) for row in await cursor.fetchall())
            if tuple(item.value.turn_index for item in turns) != tuple(range(len(turns))):
                raise InterviewLifecycleError
            return turns

    async def truncate_from_stakeholder_turn(
        self,
        access: AccessContext,
        *,
        turn_index: int,
        now: datetime,
    ) -> tuple[StoredTranscriptTurn, ...]:
        """Delete one draft answer and every later turn, returning the retained prefix."""
        self._require_clock(now)
        if turn_index < 0:
            raise InterviewLifecycleError
        async with self._database.transaction() as connection:
            session_row, _ = await self._resolve_interview(
                connection,
                access,
                now,
                permission="interview:participate",
            )
            if session_row["status"] != "draft":
                raise TranscriptImmutableError
            transcript_row = await self._fetchone(
                connection,
                """
                SELECT transcript_id, status FROM transcripts
                WHERE interview_session_id = ?
                """,
                (access.interview_session_id,),
            )
            if transcript_row is None or transcript_row["status"] != "draft":
                raise TranscriptImmutableError
            transcript_id = str(transcript_row["transcript_id"])
            target = await self._fetchone(
                connection,
                """
                SELECT turn_id, speaker FROM transcript_turns
                WHERE transcript_id = ? AND turn_index = ?
                """,
                (transcript_id, turn_index),
            )
            if target is None or target["speaker"] != "stakeholder":
                raise InterviewLifecycleError
            await connection.execute(
                """
                DELETE FROM transcript_turns
                WHERE transcript_id = ? AND turn_index >= ?
                """,
                (transcript_id, turn_index),
            )
            cursor = await connection.execute(
                """
                SELECT * FROM transcript_turns
                WHERE transcript_id = ? ORDER BY turn_index
                """,
                (transcript_id,),
            )
            retained = tuple(self._stored_turn(row) for row in await cursor.fetchall())
            if tuple(item.value.turn_index for item in retained) != tuple(range(len(retained))):
                raise InterviewLifecycleError
            await self._append_audit(
                connection,
                access,
                action="delete_draft_interview_answer",
                discriminator=f"{target['turn_id']}:{self._time(now)}",
                now=now,
            )
            return retained

    async def finalize(self, access: AccessContext, *, now: datetime) -> FinalizationResult:
        """Atomically freeze raw turns and allocate one retry-stable ingestion version."""
        self._require_clock(now)
        async with self._database.transaction() as connection:
            session_row, _ = await self._resolve_interview(
                connection,
                access,
                now,
                permission="interview:finalize",
            )
            previous_session = self._session(session_row)
            if previous_session.status in {"finalized", "ingesting", "ready"} or (
                previous_session.status == "failed" and previous_session.finalized_at is not None
            ):
                return await self._load_finalization(
                    connection,
                    str(access.interview_session_id),
                    idempotent=True,
                )
            if previous_session.status != "draft":
                raise InterviewLifecycleError

            snapshot = await self._snapshot(connection, str(access.interview_session_id))
            if not snapshot.turns or not any(
                item.value.speaker == "stakeholder" for item in snapshot.turns
            ):
                raise InterviewLifecycleError
            content_hash = self._content_hash(snapshot.turns)
            observations = (
                ("non_ascii_original_text_observed",)
                if any(
                    any(ord(character) > 127 for character in item.value.original_text)
                    for item in snapshot.turns
                )
                else ()
            )
            finalized_transcript = Transcript.model_validate(
                snapshot.transcript.model_dump()
                | {
                    "status": "finalized",
                    "language_observations": observations,
                    "finalized_at": now,
                    "content_hash": content_hash,
                }
            )
            validate_transcript_transition(snapshot.transcript, finalized_transcript)
            version_id = stable_id(
                "transcript-version",
                finalized_transcript.transcript_id,
                content_hash,
            )
            version = TranscriptIngestionVersion(
                transcript_ingestion_version_id=version_id,
                transcript_id=finalized_transcript.transcript_id,
                content_hash=content_hash,
                state="RECEIVED",
                is_active=False,
                created_at=now,
            )
            finalizing = InterviewSession.model_validate(
                previous_session.model_dump() | {"status": "finalizing"}
            )
            validate_interview_transition(previous_session, finalizing)
            finalized_session = InterviewSession.model_validate(
                finalizing.model_dump()
                | {
                    "status": "finalized",
                    "finalized_at": now,
                    "transcript_id": finalized_transcript.transcript_id,
                    "ingestion_version_id": version_id,
                }
            )
            validate_interview_transition(finalizing, finalized_session)

            await connection.execute(
                """
                UPDATE transcripts
                SET status = 'finalized', language_observations_json = ?,
                    finalized_at = ?, content_hash = ?
                WHERE transcript_id = ? AND status = 'draft'
                """,
                (
                    self._json(list(observations)),
                    self._time(now),
                    content_hash,
                    finalized_transcript.transcript_id,
                ),
            )
            await connection.execute(
                """
                INSERT INTO transcript_ingestion_versions(
                    transcript_ingestion_version_id, transcript_id, content_hash,
                    state, is_active, created_at, ready_at, failure_code,
                    failure_message, lease_token, lease_expires_at
                ) VALUES (?, ?, ?, 'RECEIVED', 0, ?, NULL, NULL, NULL, NULL, NULL)
                """,
                (version_id, version.transcript_id, content_hash, self._time(now)),
            )
            await connection.execute(
                """
                UPDATE interview_sessions
                SET status = 'finalized', finalized_at = ?, transcript_id = ?,
                    ingestion_version_id = ?, failure_code = NULL, failure_message = NULL
                WHERE interview_session_id = ? AND status = 'draft'
                """,
                (
                    self._time(now),
                    finalized_transcript.transcript_id,
                    version_id,
                    access.interview_session_id,
                ),
            )
            await self._append_version_event(
                connection,
                version_id=version_id,
                from_state=None,
                to_state="RECEIVED",
                access=access,
                now=now,
            )
            await self._append_audit(
                connection,
                access,
                action="finalize_interview",
                discriminator=version_id,
                now=now,
            )
            return FinalizationResult(
                session=finalized_session,
                snapshot=TranscriptSnapshot(finalized_transcript, snapshot.turns),
                version=version,
                idempotent=False,
            )

    async def begin_indexing(
        self,
        access: AccessContext,
        *,
        lease_token: str,
        now: datetime,
    ) -> TranscriptIngestionResult | FinalizationResult:
        """Claim the finalized version, or return its already-READY payload."""
        self._require_clock(now)
        async with self._database.transaction() as connection:
            session_row, _ = await self._resolve_interview(
                connection,
                access,
                now,
                permission="source:read",
            )
            session = self._session(session_row)
            loaded = await self._load_finalization(
                connection,
                str(access.interview_session_id),
                idempotent=True,
            )
            version = loaded.version
            if version.state == "READY":
                chunks = await self._load_chunks(
                    connection,
                    version.transcript_ingestion_version_id,
                )
                return TranscriptIngestionResult(
                    session=session,
                    snapshot=loaded.snapshot,
                    version=version,
                    chunks=chunks,
                    idempotent=True,
                )
            if version.state not in {"RECEIVED", "FAILED"}:
                raise TranscriptIngestionError
            proposed = TranscriptIngestionVersion.model_validate(
                version.model_dump()
                | {
                    "state": "INDEXING",
                    "failure_code": None,
                    "failure_message": None,
                }
            )
            validate_transcript_version_transition(version, proposed)
            if session.status not in {"finalized", "failed"}:
                raise TranscriptIngestionError
            ingesting = InterviewSession.model_validate(
                session.model_dump()
                | {"status": "ingesting", "failure_code": None, "failure_message": None}
            )
            validate_interview_transition(session, ingesting)
            await connection.execute(
                """
                UPDATE transcript_ingestion_versions
                SET state = 'INDEXING', failure_code = NULL, failure_message = NULL,
                    lease_token = ?, lease_expires_at = ?
                WHERE transcript_ingestion_version_id = ? AND state IN ('RECEIVED', 'FAILED')
                """,
                (
                    lease_token,
                    self._time(now + timedelta(seconds=self._lease_seconds)),
                    version.transcript_ingestion_version_id,
                ),
            )
            await connection.execute(
                """
                UPDATE interview_sessions
                SET status = 'ingesting', failure_code = NULL, failure_message = NULL
                WHERE interview_session_id = ? AND status IN ('finalized', 'failed')
                """,
                (access.interview_session_id,),
            )
            await self._append_version_event(
                connection,
                version_id=version.transcript_ingestion_version_id,
                from_state=version.state,
                to_state="INDEXING",
                access=access,
                now=now,
            )
            return FinalizationResult(
                session=ingesting,
                snapshot=loaded.snapshot,
                version=proposed,
                idempotent=False,
            )

    async def replace_chunks(
        self,
        access: AccessContext,
        *,
        version_id: str,
        lease_token: str,
        chunks: Sequence[SearchChunk],
        now: datetime,
    ) -> None:
        """Replace only a worker-owned pre-READY transcript chunk set."""
        if not chunks:
            raise TranscriptIngestionError
        async with self._database.transaction() as connection:
            await self._resolve_interview(connection, access, now, permission="source:read")
            await self._require_lease(connection, version_id, lease_token, now)
            if any(
                chunk.source_version_id != version_id
                or chunk.engagement_id != access.engagement_id
                or chunk.source_type != "interview"
                or chunk.doc_type != "transcript"
                or chunk.is_active_ready
                for chunk in chunks
            ):
                raise TranscriptIngestionError
            await connection.execute(
                "DELETE FROM transcript_search_chunks WHERE source_version_id = ?",
                (version_id,),
            )
            await connection.executemany(
                """
                INSERT INTO transcript_search_chunks(
                    chunk_id, engagement_id, source_id, source_version_id,
                    element_ids_json, text_for_retrieval, location_json,
                    stakeholder_id, role, department, doc_type, source_type,
                    dense_vector_json, sparse_vector_json, is_active_ready,
                    vector_stage_state
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'transcript', 'interview',
                    ?, ?, 0, 'STAGED')
                """,
                [
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
                        self._json(list(chunk.dense_vector)),
                        self._json(chunk.sparse_vector.model_dump(mode="json")),
                    )
                    for chunk in chunks
                ],
            )

    async def mark_vectors_prepared(
        self,
        *,
        version_id: str,
        lease_token: str,
        now: datetime,
    ) -> None:
        """Record that Qdrant has the complete inactive candidate set."""
        async with self._database.transaction() as connection:
            await self._require_lease(connection, version_id, lease_token, now)
            cursor = await connection.execute(
                """
                UPDATE transcript_search_chunks SET vector_stage_state = 'PREPARED'
                WHERE source_version_id = ? AND vector_stage_state = 'STAGED'
                """,
                (version_id,),
            )
            if cursor.rowcount <= 0:
                raise TranscriptIngestionError

    async def activate(
        self,
        access: AccessContext,
        *,
        version_id: str,
        lease_token: str,
        now: datetime,
    ) -> TranscriptIngestionResult:
        """Atomically make one complete finalized transcript searchable."""
        async with self._database.transaction() as connection:
            session_row, _ = await self._resolve_interview(
                connection,
                access,
                now,
                permission="source:read",
            )
            version_row = await self._require_lease(connection, version_id, lease_token, now)
            previous_version = self._version(version_row)
            cursor = await connection.execute(
                """
                SELECT COUNT(*) AS total,
                    SUM(CASE WHEN vector_stage_state = 'PREPARED' THEN 1 ELSE 0 END) AS prepared
                FROM transcript_search_chunks WHERE source_version_id = ?
                """,
                (version_id,),
            )
            counts = await cursor.fetchone()
            if (
                counts is None
                or int(counts["total"]) <= 0
                or int(counts["prepared"] or 0) != int(counts["total"])
            ):
                raise TranscriptIngestionError
            ready_version = TranscriptIngestionVersion.model_validate(
                previous_version.model_dump()
                | {"state": "READY", "is_active": True, "ready_at": now}
            )
            validate_transcript_version_transition(previous_version, ready_version)
            previous_session = self._session(session_row)
            ready_session = InterviewSession.model_validate(
                previous_session.model_dump() | {"status": "ready"}
            )
            validate_interview_transition(previous_session, ready_session)
            await connection.execute(
                """
                UPDATE transcript_ingestion_versions
                SET state = 'READY', is_active = 1, ready_at = ?,
                    lease_token = NULL, lease_expires_at = NULL
                WHERE transcript_ingestion_version_id = ? AND state = 'INDEXING'
                    AND lease_token = ?
                """,
                (self._time(now), version_id, lease_token),
            )
            await connection.execute(
                """
                UPDATE transcript_search_chunks
                SET is_active_ready = 1, vector_stage_state = 'ACTIVE'
                WHERE source_version_id = ? AND vector_stage_state = 'PREPARED'
                """,
                (version_id,),
            )
            await connection.execute(
                """
                UPDATE interview_sessions SET status = 'ready'
                WHERE interview_session_id = ? AND status = 'ingesting'
                """,
                (access.interview_session_id,),
            )
            await self._append_version_event(
                connection,
                version_id=version_id,
                from_state="INDEXING",
                to_state="READY",
                access=access,
                now=now,
            )
            await self._append_audit(
                connection,
                access,
                action="index_finalized_transcript",
                discriminator=version_id,
                now=now,
            )
            snapshot = await self._snapshot(connection, str(access.interview_session_id))
            chunks = await self._load_chunks(connection, version_id)
            return TranscriptIngestionResult(
                session=ready_session,
                snapshot=snapshot,
                version=ready_version,
                chunks=chunks,
                idempotent=False,
            )

    async def fail_indexing(
        self,
        access: AccessContext,
        *,
        version_id: str,
        lease_token: str,
        failure_code: str,
        now: datetime,
    ) -> None:
        """Persist only stable failure detail while preserving the finalized raw record."""
        async with self._database.transaction() as connection:
            await self._resolve_interview(connection, access, now, permission="source:read")
            row = await self._fetchone(
                connection,
                """
                SELECT * FROM transcript_ingestion_versions
                WHERE transcript_ingestion_version_id = ? AND state = 'INDEXING'
                    AND lease_token = ?
                """,
                (version_id, lease_token),
            )
            if row is None:
                return
            message = "Finalized transcript indexing could not be completed."
            await connection.execute(
                """
                UPDATE transcript_ingestion_versions
                SET state = 'FAILED', is_active = 0, failure_code = ?,
                    failure_message = ?, lease_token = NULL, lease_expires_at = NULL
                WHERE transcript_ingestion_version_id = ? AND lease_token = ?
                """,
                (failure_code, message, version_id, lease_token),
            )
            await connection.execute(
                """
                UPDATE interview_sessions
                SET status = 'failed', failure_code = ?, failure_message = ?
                WHERE interview_session_id = ? AND status = 'ingesting'
                """,
                (failure_code, message, access.interview_session_id),
            )
            await connection.execute(
                """
                UPDATE transcript_search_chunks
                SET is_active_ready = 0, vector_stage_state = 'STAGED'
                WHERE source_version_id = ?
                """,
                (version_id,),
            )
            await self._append_version_event(
                connection,
                version_id=version_id,
                from_state="INDEXING",
                to_state="FAILED",
                access=access,
                now=now,
            )

    async def _load_finalization(
        self,
        connection: aiosqlite.Connection,
        interview_session_id: str,
        *,
        idempotent: bool,
    ) -> FinalizationResult:
        session_row = await self._fetchone(
            connection,
            "SELECT * FROM interview_sessions WHERE interview_session_id = ?",
            (interview_session_id,),
        )
        if session_row is None or session_row["ingestion_version_id"] is None:
            raise InterviewLifecycleError
        version_row = await self._fetchone(
            connection,
            """
            SELECT * FROM transcript_ingestion_versions
            WHERE transcript_ingestion_version_id = ?
            """,
            (session_row["ingestion_version_id"],),
        )
        if version_row is None:
            raise InterviewLifecycleError
        return FinalizationResult(
            session=self._session(session_row),
            snapshot=await self._snapshot(connection, interview_session_id),
            version=self._version(version_row),
            idempotent=idempotent,
        )

    async def _snapshot(
        self,
        connection: aiosqlite.Connection,
        interview_session_id: str,
    ) -> TranscriptSnapshot:
        transcript_row = await self._fetchone(
            connection,
            "SELECT * FROM transcripts WHERE interview_session_id = ?",
            (interview_session_id,),
        )
        if transcript_row is None:
            raise InterviewLifecycleError
        cursor = await connection.execute(
            """
            SELECT * FROM transcript_turns
            WHERE transcript_id = ? ORDER BY turn_index
            """,
            (transcript_row["transcript_id"],),
        )
        turns = tuple(self._stored_turn(row) for row in await cursor.fetchall())
        if tuple(item.value.turn_index for item in turns) != tuple(range(len(turns))):
            raise InterviewLifecycleError
        return TranscriptSnapshot(self._transcript(transcript_row), turns)

    async def _load_chunks(
        self,
        connection: aiosqlite.Connection,
        version_id: str,
    ) -> tuple[SearchChunk, ...]:
        cursor = await connection.execute(
            """
            SELECT * FROM transcript_search_chunks
            WHERE source_version_id = ? ORDER BY chunk_id
            """,
            (version_id,),
        )
        chunks: list[SearchChunk] = []
        for row in await cursor.fetchall():
            sparse = cast("dict[str, object]", json.loads(row["sparse_vector_json"]))
            chunks.append(
                SearchChunk.model_validate(
                    {
                        "chunk_id": row["chunk_id"],
                        "engagement_id": row["engagement_id"],
                        "source_id": row["source_id"],
                        "source_version_id": row["source_version_id"],
                        "element_ids": json.loads(row["element_ids_json"]),
                        "text_for_retrieval": row["text_for_retrieval"],
                        "location": _LOCATION_ADAPTER.validate_python(
                            json.loads(row["location_json"])
                        ),
                        "stakeholder_id": row["stakeholder_id"],
                        "role": row["role"],
                        "department": row["department"],
                        "doc_type": row["doc_type"],
                        "source_type": row["source_type"],
                        "dense_vector": json.loads(row["dense_vector_json"]),
                        "sparse_vector": sparse,
                        "is_active_ready": bool(row["is_active_ready"]),
                    }
                )
            )
        return tuple(chunks)

    async def _resolve_interview(
        self,
        connection: aiosqlite.Connection,
        access: AccessContext,
        now: datetime,
        *,
        permission: str,
    ) -> tuple[aiosqlite.Row, aiosqlite.Row]:
        try:
            access.require_permission(permission)
            access.require_active(now)
        except (PermissionError, ValueError) as error:
            raise AccessDeniedError from error
        if (
            access.principal_type != "stakeholder"
            or access.stakeholder_id is None
            or access.interview_session_id is None
            or access.thread_id is None
            or access.principal_id != access.stakeholder_id
        ):
            raise AccessDeniedError
        row = await self._fetchone(
            connection,
            """
            SELECT i.*, s.role AS stakeholder_role,
                s.department AS stakeholder_department
            FROM interview_sessions AS i
            JOIN access_sessions AS a
                ON a.interview_session_id = i.interview_session_id
                AND a.engagement_id = i.engagement_id
                AND a.stakeholder_id = i.stakeholder_id
                AND a.thread_id = i.thread_id
            JOIN stakeholders AS s
                ON s.stakeholder_id = i.stakeholder_id
                AND s.engagement_id = i.engagement_id
            JOIN engagements AS e ON e.engagement_id = i.engagement_id
            WHERE i.interview_session_id = ? AND i.engagement_id = ?
                AND i.stakeholder_id = ? AND i.thread_id = ?
                AND a.principal_type = 'stakeholder' AND a.principal_id = ?
                AND a.revoked_at IS NULL AND a.expires_at > ?
                AND s.status = 'active' AND e.status = 'active'
            LIMIT 1
            """,
            (
                access.interview_session_id,
                access.engagement_id,
                access.stakeholder_id,
                access.thread_id,
                access.principal_id,
                self._time(now),
            ),
        )
        if row is None:
            raise AccessDeniedError
        stakeholder = {
            "role": row["stakeholder_role"],
            "department": row["stakeholder_department"],
        }
        return row, cast("aiosqlite.Row", stakeholder)

    async def _require_lease(
        self,
        connection: aiosqlite.Connection,
        version_id: str,
        lease_token: str,
        now: datetime,
    ) -> aiosqlite.Row:
        row = await self._fetchone(
            connection,
            """
            SELECT * FROM transcript_ingestion_versions
            WHERE transcript_ingestion_version_id = ? AND state = 'INDEXING'
                AND lease_token = ? AND lease_expires_at > ?
            """,
            (version_id, lease_token, self._time(now)),
        )
        if row is None:
            raise TranscriptIngestionError
        return row

    async def _append_version_event(
        self,
        connection: aiosqlite.Connection,
        *,
        version_id: str,
        from_state: str | None,
        to_state: str,
        access: AccessContext,
        now: datetime,
    ) -> None:
        event_id = stable_id("transcript-version-event", version_id, to_state)
        await connection.execute(
            """
            INSERT OR IGNORE INTO transcript_version_events(
                event_id, transcript_ingestion_version_id, from_state,
                to_state, occurred_at, correlation_id
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                version_id,
                from_state,
                to_state,
                self._time(now),
                access.correlation_id,
            ),
        )

    async def _append_audit(
        self,
        connection: aiosqlite.Connection,
        access: AccessContext,
        *,
        action: str,
        discriminator: str,
        now: datetime,
    ) -> None:
        await connection.execute(
            """
            INSERT OR IGNORE INTO operational_audit_events(
                event_id, occurred_at, run_id, engagement_id, thread_id,
                actor, action, status, duration_ms, source_ids_json,
                evidence_ids_json, retry_count, failure_code, correlation_id
            ) VALUES (?, ?, NULL, ?, ?, 'interview_service', ?, 'succeeded',
                NULL, '[]', '[]', NULL, NULL, ?)
            """,
            (
                stable_id("audit", action, discriminator),
                self._time(now),
                access.engagement_id,
                access.thread_id,
                action,
                access.correlation_id,
            ),
        )

    @staticmethod
    def _content_hash(turns: Sequence[StoredTranscriptTurn]) -> str:
        payload = [
            {
                "turn_index": item.value.turn_index,
                "speaker": item.value.speaker,
                "original_text": item.value.original_text,
            }
            for item in turns
        ]
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return sha256(encoded).hexdigest()

    @staticmethod
    def _session(row: aiosqlite.Row) -> InterviewSession:
        return InterviewSession(
            interview_session_id=row["interview_session_id"],
            engagement_id=row["engagement_id"],
            stakeholder_id=row["stakeholder_id"],
            invitation_id=row["invitation_id"],
            thread_id=row["thread_id"],
            status=row["status"],
            started_at=TranscriptRepository._parse_time(row["started_at"]),
            finalized_at=TranscriptRepository._parse_optional_time(row["finalized_at"]),
            transcript_id=row["transcript_id"],
            ingestion_version_id=row["ingestion_version_id"],
            failure_code=row["failure_code"],
            failure_message=row["failure_message"],
        )

    @staticmethod
    def _transcript(row: aiosqlite.Row) -> Transcript:
        return Transcript(
            transcript_id=row["transcript_id"],
            interview_session_id=row["interview_session_id"],
            engagement_id=row["engagement_id"],
            stakeholder_id=row["stakeholder_id"],
            role=row["role"],
            department=row["department"],
            status=row["status"],
            language_observations=tuple(json.loads(row["language_observations_json"])),
            finalized_at=TranscriptRepository._parse_optional_time(row["finalized_at"]),
            content_hash=row["content_hash"],
        )

    @staticmethod
    def _stored_turn(row: aiosqlite.Row) -> StoredTranscriptTurn:
        return StoredTranscriptTurn(
            turn_id=row["turn_id"],
            transcript_id=row["transcript_id"],
            value=TranscriptTurn(
                turn_index=row["turn_index"],
                speaker=row["speaker"],
                original_text=row["original_text"],
                created_at=TranscriptRepository._parse_time(row["created_at"]),
                checkpoint_message_id=row["checkpoint_message_id"],
            ),
        )

    @staticmethod
    def _version(row: aiosqlite.Row) -> TranscriptIngestionVersion:
        return TranscriptIngestionVersion(
            transcript_ingestion_version_id=row["transcript_ingestion_version_id"],
            transcript_id=row["transcript_id"],
            content_hash=row["content_hash"],
            state=row["state"],
            is_active=bool(row["is_active"]),
            created_at=TranscriptRepository._parse_time(row["created_at"]),
            ready_at=TranscriptRepository._parse_optional_time(row["ready_at"]),
            failure_code=row["failure_code"],
            failure_message=row["failure_message"],
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
    def _parse_optional_time(value: object) -> datetime | None:
        return None if value is None else TranscriptRepository._parse_time(value)

    @staticmethod
    def _require_clock(value: datetime) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Interview persistence requires an aware timestamp.")
