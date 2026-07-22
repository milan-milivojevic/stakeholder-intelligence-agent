"""Deep Agent state extensions required for inspectable planning."""

from __future__ import annotations

from typing import Annotated, Any, NotRequired

from deepagents.graph import DeepAgentState
from langchain.agents.middleware.types import OmitFromInput


class InsightAgentState(DeepAgentState):
    """Persist the canonical plan alongside Deep Agents TODO and messages."""

    research_plan: Annotated[NotRequired[dict[str, Any]], OmitFromInput]
