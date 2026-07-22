"""Idempotent indexing of immutable finalized interview transcripts."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import TYPE_CHECKING

from stakeholder_intelligence_agent.access.tokens import generate_opaque_id
from stakeholder_intelligence_agent.contracts.source import (
    SearchChunk,
    TranscriptTurnsLocation,
)
from stakeholder_intelligence_agent.errors import (
    AccessDeniedError,
    StakeholderIntelligenceError,
    TranscriptIngestionError,
)
from stakeholder_intelligence_agent.ingestion.identity import stable_id
from stakeholder_intelligence_agent.interview.types import (
    FinalizationResult,
    TranscriptIngestionResult,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from stakeholder_intelligence_agent.config import Settings
    from stakeholder_intelligence_agent.contracts import AccessContext
    from stakeholder_intelligence_agent.ingestion.types import Vectorizer, VectorPair, VectorStager
    from stakeholder_intelligence_agent.interview.repository import TranscriptRepository
    from stakeholder_intelligence_agent.interview.types import (
        StoredTranscriptTurn,
        TranscriptSnapshot,
    )


@dataclass(frozen=True, slots=True)
class _TranscriptChunkSeed:
    """Bounded retrieval text and exact raw-turn lineage before vectorization."""

    ordinal: int
    text: str
    turn_start: int
    turn_end: int
    turn_ids: tuple[str, ...]


def _turn_entries(
    turns: Sequence[StoredTranscriptTurn],
    *,
    chunk_characters: int,
) -> tuple[tuple[str, int, str], ...]:
    entries: list[tuple[str, int, str]] = []
    for stored in turns:
        turn = stored.value
        speaker = "Stakeholder" if turn.speaker == "stakeholder" else "Interviewer"
        prefix = f"Turn {turn.turn_index} — {speaker}:\n"
        available = max(1, chunk_characters - len(prefix))
        for offset in range(0, len(turn.original_text), available):
            fragment = turn.original_text[offset : offset + available]
            entries.append((stored.turn_id, turn.turn_index, prefix + fragment))
    return tuple(entries)


def build_transcript_chunk_seeds(
    snapshot: TranscriptSnapshot,
    *,
    chunk_characters: int,
) -> tuple[_TranscriptChunkSeed, ...]:
    """Group exact turn text without allowing any retrieval unit to exceed its bound."""
    entries = _turn_entries(snapshot.turns, chunk_characters=chunk_characters)
    seeds: list[_TranscriptChunkSeed] = []
    current: list[tuple[str, int, str]] = []
    current_length = 0

    def flush() -> None:
        nonlocal current, current_length
        if not current:
            return
        turn_ids = tuple(dict.fromkeys(item[0] for item in current))
        indices = tuple(item[1] for item in current)
        seeds.append(
            _TranscriptChunkSeed(
                ordinal=len(seeds),
                text="\n\n".join(item[2] for item in current),
                turn_start=min(indices),
                turn_end=max(indices),
                turn_ids=turn_ids,
            )
        )
        current = []
        current_length = 0

    for entry in entries:
        separator = 2 if current else 0
        if current and current_length + separator + len(entry[2]) > chunk_characters:
            flush()
            separator = 0
        current.append(entry)
        current_length += separator + len(entry[2])
    flush()
    if not seeds:
        raise TranscriptIngestionError
    return tuple(seeds)


def materialize_transcript_chunks(
    finalization: FinalizationResult,
    seeds: Sequence[_TranscriptChunkSeed],
    vectors: Sequence[VectorPair],
) -> tuple[SearchChunk, ...]:
    """Create stable interview SearchChunk contracts from complete vector pairs."""
    if len(seeds) != len(vectors):
        raise TranscriptIngestionError
    transcript = finalization.snapshot.transcript
    version = finalization.version
    chunks: list[SearchChunk] = []
    for seed, pair in zip(seeds, vectors, strict=True):
        text_hash = sha256(seed.text.encode("utf-8")).hexdigest()
        chunks.append(
            SearchChunk(
                chunk_id=stable_id(
                    "transcript-chunk",
                    version.transcript_ingestion_version_id,
                    str(seed.ordinal),
                    text_hash,
                ),
                engagement_id=transcript.engagement_id,
                source_id=transcript.transcript_id,
                source_version_id=version.transcript_ingestion_version_id,
                element_ids=seed.turn_ids,
                text_for_retrieval=seed.text,
                location=TranscriptTurnsLocation(
                    stakeholder_id=transcript.stakeholder_id,
                    transcript_id=transcript.transcript_id,
                    turn_start=seed.turn_start,
                    turn_end=seed.turn_end,
                ),
                stakeholder_id=transcript.stakeholder_id,
                role=transcript.role,
                department=transcript.department,
                doc_type="transcript",
                source_type="interview",
                dense_vector=pair.dense,
                sparse_vector=pair.sparse,
                is_active_ready=False,
            )
        )
    return tuple(chunks)


class TranscriptIngestionService:
    """Stage both vector channels before the SQLite READY activation boundary."""

    def __init__(
        self,
        *,
        settings: Settings,
        repository: TranscriptRepository,
        vectorizer: Vectorizer,
        vector_stager: VectorStager,
        clock: Callable[[], datetime] | None = None,
        lease_factory: Callable[[], str] | None = None,
    ) -> None:
        self._settings = settings
        self._repository = repository
        self._vectorizer = vectorizer
        self._vector_stager = vector_stager
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lease_factory = lease_factory or (lambda: generate_opaque_id("transcript_lease"))
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    async def initialize(self) -> None:
        """Initialize transcript persistence and the shared Qdrant collection."""
        await self._repository.initialize()
        await self._vector_stager.initialize()

    async def ingest(self, access: AccessContext) -> TranscriptIngestionResult:
        """Index the caller's finalized transcript exactly once across safe retries."""
        session_id = access.interview_session_id
        if session_id is None:
            raise AccessDeniedError
        lock = await self._lock_for(session_id)
        async with lock:
            return await self._ingest_locked(access)

    async def _ingest_locked(self, access: AccessContext) -> TranscriptIngestionResult:
        lease_token = self._lease_factory()
        claimed = await self._repository.begin_indexing(
            access,
            lease_token=lease_token,
            now=self._clock(),
        )
        if isinstance(claimed, TranscriptIngestionResult):
            return claimed
        version_id = claimed.version.transcript_ingestion_version_id
        staged = False
        try:
            seeds = build_transcript_chunk_seeds(
                claimed.snapshot,
                chunk_characters=self._settings.ingestion_chunk_characters,
            )
            vectors = await self._vectorizer.vectorize(tuple(seed.text for seed in seeds))
            chunks = materialize_transcript_chunks(claimed, seeds, vectors)
            await self._repository.replace_chunks(
                access,
                version_id=version_id,
                lease_token=lease_token,
                chunks=chunks,
                now=self._clock(),
            )
            await self._vector_stager.stage(chunks)
            staged = True
            await self._vector_stager.verify(
                version_id,
                tuple(chunk.chunk_id for chunk in chunks),
            )
            await self._vector_stager.prepare_activation(version_id)
            await self._repository.mark_vectors_prepared(
                version_id=version_id,
                lease_token=lease_token,
                now=self._clock(),
            )
            return await self._repository.activate(
                access,
                version_id=version_id,
                lease_token=lease_token,
                now=self._clock(),
            )
        except AccessDeniedError:
            if staged:
                await self._vector_stager.deactivate(version_id)
            raise
        except Exception as error:
            if staged:
                await self._vector_stager.deactivate(version_id)
            raw_code = getattr(error, "code", None)
            code = (
                raw_code
                if isinstance(error, StakeholderIntelligenceError) and isinstance(raw_code, str)
                else TranscriptIngestionError.code
            )
            await self._repository.fail_indexing(
                access,
                version_id=version_id,
                lease_token=lease_token,
                failure_code=code,
                now=self._clock(),
            )
            if isinstance(error, TranscriptIngestionError):
                raise
            raise TranscriptIngestionError from error

    async def _lock_for(self, key: str) -> asyncio.Lock:
        async with self._locks_guard:
            return self._locks.setdefault(key, asyncio.Lock())
