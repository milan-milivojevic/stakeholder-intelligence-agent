"""Typed checkpoint state for inspectable stakeholder interview progress."""

from __future__ import annotations

from typing import Annotated, NotRequired

from langchain.agents.middleware.types import AgentState, OmitFromInput


class InterviewAgentState(AgentState[None]):
    """Keep structured progress separate from the authoritative raw transcript."""

    topics_covered: Annotated[NotRequired[tuple[str, ...]], OmitFromInput]
    evidence_gaps: Annotated[NotRequired[tuple[str, ...]], OmitFromInput]
    retrieved_chunk_ids: Annotated[NotRequired[tuple[str, ...]], OmitFromInput]
