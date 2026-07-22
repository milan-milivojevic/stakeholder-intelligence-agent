"""Enforce server-resolved access context at graph execution boundaries."""

from __future__ import annotations

from typing import Any

from langchain.agents.middleware.types import AgentMiddleware, AgentState
from langgraph.config import get_config
from langgraph.runtime import Runtime

from stakeholder_intelligence_agent.artifacts import ScopedArtifactStore, activate_artifact_access
from stakeholder_intelligence_agent.contracts.access import (
    InsightRuntimeContext,
    InterviewRuntimeContext,
)
from stakeholder_intelligence_agent.contracts.common import utc_now
from stakeholder_intelligence_agent.errors import RuntimeScopeError


def _require_thread(runtime_context: InsightRuntimeContext | InterviewRuntimeContext) -> None:
    """Require persistent runtime thread identity to match trusted access context."""
    access = runtime_context.access
    access.require_active(utc_now())
    expected_thread = access.thread_id
    configurable = get_config().get("configurable", {})
    actual_thread = configurable.get("thread_id")
    if expected_thread is None or actual_thread != expected_thread:
        raise RuntimeScopeError


class InterviewRuntimeScopeMiddleware(
    AgentMiddleware[AgentState[Any], InterviewRuntimeContext, Any]
):
    """Validate interview scope before any agent work begins."""

    def before_agent(
        self,
        _state: AgentState[Any],
        runtime: Runtime[InterviewRuntimeContext],
    ) -> None:
        """Apply the synchronous scope check."""
        _require_thread(runtime.context)

    async def abefore_agent(
        self,
        _state: AgentState[Any],
        runtime: Runtime[InterviewRuntimeContext],
    ) -> None:
        """Apply the asynchronous scope check."""
        _require_thread(runtime.context)


class InsightRuntimeScopeMiddleware(AgentMiddleware[AgentState[Any], InsightRuntimeContext, Any]):
    """Validate PM insight scope before orchestrator or subagent work."""

    def __init__(self, artifacts: ScopedArtifactStore | None = None) -> None:
        super().__init__()
        self._artifacts = artifacts

    def before_agent(
        self,
        _state: AgentState[Any],
        runtime: Runtime[InsightRuntimeContext],
    ) -> None:
        """Apply the synchronous scope check."""
        _require_thread(runtime.context)
        activate_artifact_access(runtime.context.access)
        if self._artifacts is not None:
            self._artifacts.register_graph_scope(runtime.context.access)

    async def abefore_agent(
        self,
        _state: AgentState[Any],
        runtime: Runtime[InsightRuntimeContext],
    ) -> None:
        """Apply the asynchronous scope check."""
        _require_thread(runtime.context)
        activate_artifact_access(runtime.context.access)
        if self._artifacts is not None:
            self._artifacts.register_graph_scope(runtime.context.access)
