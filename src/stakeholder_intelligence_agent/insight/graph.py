"""Required create_deep_agent graph with researchers, editor, and scoped artifacts."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from deepagents import create_deep_agent
from deepagents.middleware.filesystem import FilesystemPermission
from deepagents.middleware.subagents import CompiledSubAgent
from deepagents.profiles import (
    GeneralPurposeSubagentProfile,
    HarnessProfile,
    register_harness_profile,
)
from langchain.agents import create_agent
from langchain.agents.middleware import (
    ModelCallLimitMiddleware,
    ModelFallbackMiddleware,
    PIIMiddleware,
    ToolCallLimitMiddleware,
)
from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.language_models import BaseChatModel
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.pregel import Pregel

from stakeholder_intelligence_agent.artifacts import (
    ScopedArtifactStore,
    ScopedFilesystemBackend,
)
from stakeholder_intelligence_agent.config import Settings, get_settings
from stakeholder_intelligence_agent.contracts import InsightRuntimeContext
from stakeholder_intelligence_agent.errors import ProviderPolicyError
from stakeholder_intelligence_agent.gemini_runtime import get_shared_gemini_rate_limiter
from stakeholder_intelligence_agent.insight.prompts import (
    EDITOR_SYSTEM_PROMPT,
    ORCHESTRATOR_SYSTEM_PROMPT,
    RESEARCHER_SYSTEM_PROMPT,
)
from stakeholder_intelligence_agent.insight.repository import InsightRunRepository
from stakeholder_intelligence_agent.insight.state import InsightAgentState
from stakeholder_intelligence_agent.insight.tools import (
    build_editor_tools,
    build_orchestrator_tools,
    build_researcher_tools,
)
from stakeholder_intelligence_agent.middleware import (
    CourseFidelityGuardMiddleware,
    GeminiQuotaRetryMiddleware,
    InsightRuntimeScopeMiddleware,
    OrderedSubagentToolMiddleware,
    ResearcherLoopMiddleware,
)
from stakeholder_intelligence_agent.models import create_primary_and_fallback

if TYPE_CHECKING:
    from stakeholder_intelligence_agent.retrieval.repository import RetrievalRepository
    from stakeholder_intelligence_agent.retrieval.service import HybridRetrievalService

_PROFILE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


@dataclass(frozen=True, slots=True)
class InsightGraphDependencies:
    """Injectable runtime dependencies used by offline trajectory tests."""

    primary_model: BaseChatModel | None = None
    fallback_model: BaseChatModel | None = None
    researcher_model: BaseChatModel | None = None
    editor_model: BaseChatModel | None = None
    checkpointer: BaseCheckpointSaver[Any] | None = None
    harness_provider: str = "google_genai"
    retrieval_service: HybridRetrievalService | None = None
    evidence_repository: RetrievalRepository | None = None
    run_repository: InsightRunRepository | None = None


def _disable_general_purpose_subagent(provider_key: str) -> None:
    """Disable Deep Agents' unapproved automatic general-purpose subagent."""
    if _PROFILE_KEY.fullmatch(provider_key) is None:
        raise ProviderPolicyError
    register_harness_profile(
        provider_key,
        HarnessProfile(general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False)),
    )


def _filesystem_permissions() -> list[FilesystemPermission]:
    """Allow required reads and history offload while denying arbitrary writes."""
    return [
        FilesystemPermission(
            operations=["read"],
            paths=[
                "/research_plan.json",
                "/research_plan.md",
                "/research/**",
                "/report/**",
                "/conversation_history/**",
            ],
            mode="allow",
        ),
        FilesystemPermission(
            operations=["write"],
            paths=["/conversation_history/**"],
            mode="allow",
        ),
        FilesystemPermission(
            operations=["read", "write"],
            paths=["/**"],
            mode="deny",
        ),
    ]


def _bounded_middleware(  # noqa: PLR0913
    settings: Settings,
    fallback_model: BaseChatModel | None,
    *,
    artifacts: ScopedArtifactStore | None = None,
    ordered_tools: tuple[str, ...] | None = None,
    model_run_limit: int | None = None,
    tool_run_limits: tuple[tuple[str, int], ...] = (),
) -> list[AgentMiddleware[Any, Any, Any]]:
    """Create a fresh Gemini fallback, call-bound, PII, and scope stack."""
    middleware: list[AgentMiddleware[Any, Any, Any]] = [
        InsightRuntimeScopeMiddleware(artifacts),
    ]
    if ordered_tools is not None:
        middleware.append(OrderedSubagentToolMiddleware(ordered_tools))
    middleware.append(GeminiQuotaRetryMiddleware(get_shared_gemini_rate_limiter(settings)))
    if fallback_model is not None:
        middleware.append(ModelFallbackMiddleware(fallback_model))
    middleware.extend(
        [
            ModelCallLimitMiddleware(
                thread_limit=settings.model_thread_call_limit,
                run_limit=min(
                    settings.model_run_call_limit,
                    model_run_limit or settings.model_run_call_limit,
                ),
                exit_behavior="error",
            ),
            ToolCallLimitMiddleware(
                thread_limit=settings.tool_thread_call_limit,
                run_limit=settings.tool_run_call_limit,
                exit_behavior="error",
            ),
            ToolCallLimitMiddleware(
                tool_name="scoped_retrieve",
                run_limit=settings.retrieval_calls_per_researcher_limit,
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
        ]
    )
    middleware.extend(
        ToolCallLimitMiddleware(
            tool_name=tool_name,
            run_limit=run_limit,
            exit_behavior="error",
        )
        for tool_name, run_limit in tool_run_limits
    )
    return middleware


def build_insight_graph(
    settings: Settings,
    *,
    dependencies: InsightGraphDependencies | None = None,
) -> Pregel[Any, InsightRuntimeContext, Any, Any]:
    """Build the real PM Deep Agent graph without a direct-answer shortcut."""
    dependencies = dependencies or InsightGraphDependencies()
    primary_model = dependencies.primary_model
    fallback_model = dependencies.fallback_model
    if primary_model is None or fallback_model is None:
        configured_primary, configured_fallback = create_primary_and_fallback(settings)
        primary_model = primary_model or configured_primary
        fallback_model = fallback_model or configured_fallback
    researcher_model = dependencies.researcher_model or primary_model
    editor_model = dependencies.editor_model or primary_model
    _disable_general_purpose_subagent(dependencies.harness_provider)

    artifacts = ScopedArtifactStore(settings.agent_artifacts_root)
    permissions = _filesystem_permissions()
    backend = ScopedFilesystemBackend(artifacts)
    researcher_middleware = _bounded_middleware(
        settings,
        fallback_model,
        artifacts=artifacts,
        model_run_limit=(2 * settings.retrieval_calls_per_researcher_limit) + 4,
    )
    researcher_middleware.insert(
        1,
        ResearcherLoopMiddleware(
            max_retrievals=settings.retrieval_calls_per_researcher_limit,
        ),
    )

    researcher_agent = create_agent(
        model=researcher_model,
        tools=build_researcher_tools(
            artifacts,
            retrieval_service=dependencies.retrieval_service,
            evidence_repository=dependencies.evidence_repository,
            run_repository=dependencies.run_repository,
        ),
        system_prompt=RESEARCHER_SYSTEM_PROMPT,
        middleware=researcher_middleware,
        state_schema=InsightAgentState,
        context_schema=InsightRuntimeContext,
        name="topic-researcher",
    )
    researcher: CompiledSubAgent = {
        "name": "topic-researcher",
        "description": "Research exactly one planned topic using only scoped evidence tools.",
        "runnable": researcher_agent,
    }
    editor_agent = create_agent(
        model=editor_model,
        tools=build_editor_tools(
            artifacts,
            settings=settings,
            evidence_repository=dependencies.evidence_repository,
            run_repository=dependencies.run_repository,
        ),
        system_prompt=EDITOR_SYSTEM_PROMPT,
        middleware=_bounded_middleware(
            settings,
            fallback_model,
            artifacts=artifacts,
            ordered_tools=("load_research_package", "save_final_report"),
            model_run_limit=5,
            tool_run_limits=(("save_final_report", 3),),
        ),
        state_schema=InsightAgentState,
        context_schema=InsightRuntimeContext,
        name="report-editor",
    )
    editor: CompiledSubAgent = {
        "name": "report-editor",
        "description": "Edit completed researcher artifacts into the one strict InsightReport.",
        "runnable": editor_agent,
    }

    main_middleware = _bounded_middleware(
        settings,
        fallback_model,
        artifacts=artifacts,
    )
    main_middleware.append(
        CourseFidelityGuardMiddleware(
            artifacts,
            max_parallel_researchers=settings.max_parallel_researchers,
            run_repository=dependencies.run_repository,
        )
    )
    return create_deep_agent(
        model=primary_model,
        tools=build_orchestrator_tools(
            artifacts,
            max_topics=settings.max_research_topics,
            run_repository=dependencies.run_repository,
        ),
        system_prompt=ORCHESTRATOR_SYSTEM_PROMPT,
        middleware=main_middleware,
        subagents=[researcher, editor],
        permissions=permissions,
        backend=backend,
        state_schema=InsightAgentState,
        context_schema=InsightRuntimeContext,
        checkpointer=dependencies.checkpointer,
        name="stakeholder_insight",
    )


def _build_server_graph() -> Pregel[Any, InsightRuntimeContext, Any, Any]:
    """Build the configured graph outside the Agent Server event loop."""
    from stakeholder_intelligence_agent.persistence import DomainDatabase
    from stakeholder_intelligence_agent.retrieval.factory import (
        build_production_retrieval,
    )
    from stakeholder_intelligence_agent.retrieval.repository import RetrievalRepository

    settings = get_settings()
    database = DomainDatabase(settings.domain_database)
    return build_insight_graph(
        settings,
        dependencies=InsightGraphDependencies(
            retrieval_service=build_production_retrieval(settings),
            evidence_repository=RetrievalRepository(database),
            run_repository=InsightRunRepository(database),
        ),
    )


async def make_graph() -> Pregel[Any, InsightRuntimeContext, Any, Any]:
    """Agent Server factory that keeps synchronous dependency assembly off-loop."""
    return await asyncio.to_thread(_build_server_graph)
