"""Typed internal boundaries for interview turns, finalization, and indexing."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from stakeholder_intelligence_agent.contracts import (
        AccessContext,
        InterviewSession,
        Transcript,
        TranscriptIngestionVersion,
        TranscriptTurn,
    )
    from stakeholder_intelligence_agent.contracts.source import SearchChunk
    from stakeholder_intelligence_agent.retrieval.types import RetrievalResult


@dataclass(frozen=True, slots=True)
class StoredTranscriptTurn:
    """One persisted exact turn plus its stable lineage identity."""

    turn_id: str
    transcript_id: str
    value: TranscriptTurn


@dataclass(frozen=True, slots=True)
class TranscriptSnapshot:
    """A transcript and all exact raw turns in canonical order."""

    transcript: Transcript
    turns: tuple[StoredTranscriptTurn, ...]


@dataclass(frozen=True, slots=True)
class FinalizationResult:
    """Atomic finalization output with stable retry identity."""

    session: InterviewSession
    snapshot: TranscriptSnapshot
    version: TranscriptIngestionVersion
    idempotent: bool


@dataclass(frozen=True, slots=True)
class TranscriptIngestionResult:
    """Searchable transcript output after the READY activation boundary."""

    session: InterviewSession
    snapshot: TranscriptSnapshot
    version: TranscriptIngestionVersion
    chunks: tuple[SearchChunk, ...]
    idempotent: bool


@dataclass(frozen=True, slots=True)
class InterviewTurnResult:
    """One completed participant-to-agent exchange."""

    stakeholder_turn: StoredTranscriptTurn
    assistant_turn: StoredTranscriptTurn
    assistant_text: str
    graph_state: dict[str, object]


@dataclass(frozen=True, slots=True)
class InterviewTokenChunk:
    """One PII-scrubbed assistant text delta emitted by the graph stream."""

    sequence: int
    delta: str


@dataclass(frozen=True, slots=True)
class InterviewStartResult:
    """One persisted assistant opening question with idempotency state."""

    opening_turn: StoredTranscriptTurn
    idempotent: bool


class InterviewRetriever(Protocol):
    """Read-only retrieval boundary available to the interview agent."""

    async def initialize(self) -> None:
        """Initialize persistence needed by retrieval."""
        ...

    async def retrieve(self, access: AccessContext, query: str) -> RetrievalResult:
        """Retrieve only current authorized engagement evidence."""
        ...
