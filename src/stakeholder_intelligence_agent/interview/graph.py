"""Stakeholder create_agent graph with Gemini fallback and bounded middleware."""

from __future__ import annotations

import asyncio
from typing import Any

from langchain.agents import create_agent
from langchain.agents.middleware import (
    ModelCallLimitMiddleware,
    ModelFallbackMiddleware,
    PIIMiddleware,
    SummarizationMiddleware,
    ToolCallLimitMiddleware,
)
from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.language_models import BaseChatModel
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.pregel import Pregel

from stakeholder_intelligence_agent.config import Settings, get_settings
from stakeholder_intelligence_agent.contracts import InterviewRuntimeContext
from stakeholder_intelligence_agent.gemini_runtime import get_shared_gemini_rate_limiter
from stakeholder_intelligence_agent.interview.middleware import (
    InterviewProgressMiddleware,
    interview_context_prompt,
)
from stakeholder_intelligence_agent.interview.prompts import INTERVIEW_SYSTEM_PROMPT
from stakeholder_intelligence_agent.interview.state import InterviewAgentState
from stakeholder_intelligence_agent.interview.tools import build_interview_tools
from stakeholder_intelligence_agent.interview.types import InterviewRetriever
from stakeholder_intelligence_agent.middleware import (
    GeminiQuotaRetryMiddleware,
    InterviewRuntimeScopeMiddleware,
)
from stakeholder_intelligence_agent.models import create_primary_and_fallback


def build_interview_graph(
    settings: Settings,
    *,
    primary_model: BaseChatModel | None = None,
    fallback_model: BaseChatModel | None = None,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
    retrieval_service: InterviewRetriever | None = None,
) -> Pregel[Any, InterviewRuntimeContext, Any, Any]:
    """Build the persistent stakeholder interview agent."""
    if primary_model is None or fallback_model is None:
        configured_primary, configured_fallback = create_primary_and_fallback(settings)
        primary_model = primary_model or configured_primary
        fallback_model = fallback_model or configured_fallback
    middleware: list[AgentMiddleware[Any, Any, Any]] = [
        InterviewRuntimeScopeMiddleware(),
        interview_context_prompt,
        GeminiQuotaRetryMiddleware(get_shared_gemini_rate_limiter(settings)),
    ]
    if fallback_model is not None:
        middleware.append(ModelFallbackMiddleware(fallback_model))
    middleware.extend(
        [
            SummarizationMiddleware(
                primary_model,
                trigger=("tokens", settings.summary_trigger_tokens),
                keep=("messages", settings.summary_keep_messages),
            ),
            ModelCallLimitMiddleware(
                thread_limit=settings.model_thread_call_limit,
                run_limit=settings.model_run_call_limit,
                exit_behavior="error",
            ),
            ToolCallLimitMiddleware(
                thread_limit=settings.tool_thread_call_limit,
                run_limit=settings.tool_run_call_limit,
                exit_behavior="error",
            ),
            PIIMiddleware(
                "email",
                strategy="mask",
                apply_to_input=True,
                apply_to_output=True,
                apply_to_tool_results=True,
            ),
            PIIMiddleware(
                "credit_card",
                strategy="block",
                apply_to_input=True,
                apply_to_output=True,
                apply_to_tool_results=True,
            ),
            InterviewProgressMiddleware(),
        ]
    )
    return create_agent(
        model=primary_model,
        tools=build_interview_tools(retrieval_service),
        system_prompt=INTERVIEW_SYSTEM_PROMPT,
        middleware=middleware,
        state_schema=InterviewAgentState,
        context_schema=InterviewRuntimeContext,
        checkpointer=checkpointer,
        name="stakeholder_interview",
    )


def _build_server_graph() -> Pregel[Any, InterviewRuntimeContext, Any, Any]:
    """Build the configured graph outside the Agent Server event loop."""
    from stakeholder_intelligence_agent.retrieval.factory import (
        build_production_retrieval,
    )

    settings = get_settings()
    return build_interview_graph(
        settings,
        retrieval_service=build_production_retrieval(settings),
    )


async def make_graph() -> Pregel[Any, InterviewRuntimeContext, Any, Any]:
    """Agent Server factory that keeps synchronous dependency assembly off-loop."""
    return await asyncio.to_thread(_build_server_graph)
