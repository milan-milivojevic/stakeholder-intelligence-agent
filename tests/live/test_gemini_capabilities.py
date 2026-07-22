"""Five credential-gated smoke tests against the real configured Gemini capabilities."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import SecretStr, ValidationError

from stakeholder_intelligence_agent.config import Settings
from stakeholder_intelligence_agent.contracts import InsightReport
from stakeholder_intelligence_agent.contracts.retrieval import RetrievalFilter
from stakeholder_intelligence_agent.contracts.source import ImageRegionLocation
from stakeholder_intelligence_agent.ingestion.adapters import (
    GeminiBm25Vectorizer,
    GeminiVisionEnricher,
)
from stakeholder_intelligence_agent.insight.graph import (
    InsightGraphDependencies,
    build_insight_graph,
)
from stakeholder_intelligence_agent.interview.graph import build_interview_graph
from stakeholder_intelligence_agent.persistence.checkpointer import open_sqlite_checkpointer
from stakeholder_intelligence_agent.retrieval.filters import GeminiFilterExtractor
from stakeholder_intelligence_agent.retrieval.types import RetrievalResult, RetrievalTrace
from tests.helpers import insight_context, interview_context

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig

    from stakeholder_intelligence_agent.contracts import AccessContext

pytestmark = [pytest.mark.live, pytest.mark.timeout(300)]

FIXTURES = Path(__file__).parents[1] / "fixtures" / "ingestion"
_LIVE_ENVIRONMENT_BY_FIELD = {
    "GOOGLE_API_KEY": "GOOGLE_API_KEY",
    "google_api_key": "GOOGLE_API_KEY",
    "gemini_primary_chat_model": "STAKEHOLDER_AI_GEMINI_PRIMARY_CHAT_MODEL",
    "gemini_fallback_chat_model": "STAKEHOLDER_AI_GEMINI_FALLBACK_CHAT_MODEL",
    "gemini_vision_model": "STAKEHOLDER_AI_GEMINI_VISION_MODEL",
    "gemini_embedding_model": "STAKEHOLDER_AI_GEMINI_EMBEDDING_MODEL",
}


def _unavailable_live_environment(error: ValidationError) -> tuple[str, ...]:
    """Return only provider prerequisites that are genuinely absent or blank."""
    unavailable: list[str] = []
    for issue in error.errors(include_url=False):
        location = issue.get("loc")
        field_name = str(location[0]) if location else ""
        environment_name = _LIVE_ENVIRONMENT_BY_FIELD.get(field_name)
        if environment_name is None or issue.get("type") not in {
            "missing",
            "string_too_short",
            "too_short",
        }:
            return ()
        if environment_name not in unavailable:
            unavailable.append(environment_name)
    return tuple(unavailable)


@pytest.fixture
def live_settings(tmp_path: Path) -> Settings:
    """Load the documented validated environment and isolate all local persistence."""
    data_root = tmp_path / "live-data"
    try:
        return Settings(
            environment="test",
            pm_bootstrap_token=SecretStr("live-smoke-local-bootstrap-token-0001"),
            token_pepper=SecretStr("live-smoke-local-token-pepper-000001"),
            data_root=data_root,
            domain_database=data_root / "domain.sqlite3",
            checkpoint_database=data_root / "checkpoints.sqlite3",
            originals_root=data_root / "originals",
            derived_root=data_root / "derived",
            agent_artifacts_root=data_root / "agent-artifacts",
            audit_root=data_root / "audit",
        )
    except ValidationError as error:
        unavailable = _unavailable_live_environment(error)
        if unavailable:
            pytest.skip("Live Gemini prerequisites are unavailable: " + ", ".join(unavailable))
        raise


@pytest.mark.integration
async def test_live_gemini_embedding_returns_complete_dense_and_sparse_pair(
    live_settings: Settings,
) -> None:
    """LIVE-GEM-01: call the production query-embedding adapter."""
    pair = await GeminiBm25Vectorizer(live_settings).vectorize_query(
        "Which current evidence identifies an operational risk owner?"
    )

    assert len(pair.dense) == live_settings.gemini_embedding_dimension
    assert all(math.isfinite(value) for value in pair.dense)
    assert pair.sparse.indices
    assert len(pair.sparse.indices) == len(pair.sparse.values)


@pytest.mark.integration
async def test_live_gemini_vision_describes_preserved_fixture(
    live_settings: Settings,
) -> None:
    """LIVE-GEM-02: call the production multimodal adapter on a preserved fixture."""
    fixture = FIXTURES / "alpha-organization-chart.png"
    description = await GeminiVisionEnricher(live_settings).describe(
        content=fixture.read_bytes(),
        media_type="image/png",
        filename=fixture.name,
        location=ImageRegionLocation(
            filename=fixture.name,
            image_index=1,
            region="whole_image",
        ),
    )

    assert description.strip()
    assert len(description) <= 200_000
    assert fixture.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")


@pytest.mark.integration
async def test_live_gemini_structured_filter_validates_project_schema(
    live_settings: Settings,
) -> None:
    """LIVE-GEM-03: execute the production Gemini structured-output boundary."""
    extracted = await GeminiFilterExtractor(live_settings).extract(
        "Find Operations department documents about approval handoff risks."
    )
    payload = extracted.model_dump(mode="json")

    assert set(payload) == {
        "stakeholder_id",
        "role",
        "department",
        "doc_type",
        "source_type",
    }
    assert "engagement_id" not in payload
    assert all(value is None or isinstance(value, str) for value in payload.values())


@dataclass(slots=True)
class EmptyScopedRetriever:
    """Return no evidence while proving that real Gemini uses the scoped tool boundary."""

    calls: list[tuple[str, str]] = field(default_factory=list)

    async def initialize(self) -> None:
        """Satisfy the production interview retriever boundary."""

    async def retrieve(self, access: AccessContext, query: str) -> RetrievalResult:
        """Record scope and return a valid empty retrieval result."""
        self.calls.append((access.engagement_id, query))
        return RetrievalResult(
            query=query,
            retrieval_filter=RetrievalFilter(engagement_id=access.engagement_id),
            items=(),
            trace=RetrievalTrace(
                rrf_chunk_ids=(),
                reranked_chunk_ids=(),
                fusion_method="qdrant_native_rrf",
                filter_extraction_degraded=False,
                optional_filters_relaxed=False,
                reranker_model=None,
                reranker_device=None,
                hybrid_latency_ms=0.0,
                reranker_latency_ms=0.0,
                total_latency_ms=0.0,
            ),
        )


@pytest.mark.integration
async def test_live_gemini_interview_streams_checkpoints_and_uses_scoped_tool(
    live_settings: Settings,
    tmp_path: Path,
) -> None:
    """LIVE-GEM-04: run the real create_agent path with stream and checkpoint proof."""
    context = interview_context(thread_id="live-interview-thread")
    retriever = EmptyScopedRetriever()
    config: RunnableConfig = {"configurable": {"thread_id": "live-interview-thread"}}
    async with open_sqlite_checkpointer(tmp_path / "live-interview.sqlite3") as saver:
        graph = build_interview_graph(
            live_settings,
            checkpointer=saver,
            retrieval_service=retriever,
        )
        updates = [
            cast("dict[str, Any]", update)
            async for update in graph.astream(
                {
                    "messages": [
                        HumanMessage(
                            content=(
                                "I own the approval handoff. Consult authorized process evidence "
                                "before asking one concise follow-up question."
                            )
                        )
                    ]
                },
                config=config,
                context=context,
                stream_mode="updates",
            )
        ]
        checkpoint = await saver.aget_tuple(config)
        snapshot = await graph.aget_state(config)

    assert updates
    assert checkpoint is not None
    assert retriever.calls
    assert all(call[0] == context.access.engagement_id for call in retriever.calls)
    messages = cast("list[Any]", snapshot.values["messages"])
    assert isinstance(messages[-1], AIMessage)
    assert messages[-1].text.strip()


@pytest.mark.integration
@pytest.mark.trajectory
async def test_live_gemini_deep_agent_produces_plan_research_and_strict_report(
    live_settings: Settings,
    tmp_path: Path,
) -> None:
    """LIVE-GEM-05: run the real Gemini create_deep_agent workflow end to end."""
    context = insight_context(
        thread_id="live-insight-thread",
        run_id="live-insight-run",
        question="Which operational risks and responsibilities are supported?",
    )
    config: RunnableConfig = {"configurable": {"thread_id": "live-insight-thread"}}
    async with open_sqlite_checkpointer(tmp_path / "live-insight.sqlite3") as saver:
        graph = build_insight_graph(
            live_settings,
            dependencies=InsightGraphDependencies(checkpointer=saver),
        )
        raw = await graph.ainvoke(
            {"messages": [HumanMessage(content=context.question)]},
            config=config,
            context=context,
        )

    result = cast("dict[str, Any]", raw)
    messages = cast("list[Any]", result["messages"])
    tool_calls = [
        call
        for message in messages
        if isinstance(message, AIMessage)
        for call in message.tool_calls
    ]
    assert [call["name"] for call in tool_calls[:2]] == [
        "write_todos",
        "create_research_plan",
    ]
    task_types = [call["args"]["subagent_type"] for call in tool_calls if call["name"] == "task"]
    assert "topic-researcher" in task_types
    assert task_types[-1] == "report-editor"
    scope_root = (
        live_settings.agent_artifacts_root / context.access.engagement_id / "live-insight-thread"
    )
    assert (scope_root / "research_plan.md").is_file()
    assert tuple((scope_root / "research").glob("*/findings.md"))
    report_path = scope_root / "report" / "insight_report.json"
    report = InsightReport.model_validate_json(report_path.read_text(encoding="utf-8"))
    assert report.run_metadata.run_id == context.run_id
    assert report.engagement_id == context.access.engagement_id
    assert report.question == context.question
    assert report.status == "insufficient_evidence"
    assert not report.evidence_ids
