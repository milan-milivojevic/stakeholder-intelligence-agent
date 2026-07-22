"""Offline graph-construction checks for both registered Agent Server graphs."""

from deepagents.backends import BackendProtocol
from langchain_core.messages import AIMessage

from stakeholder_intelligence_agent.artifacts import (
    ScopedArtifactStore,
    ScopedFilesystemBackend,
    activate_artifact_access,
)
from stakeholder_intelligence_agent.config import Settings
from stakeholder_intelligence_agent.insight.graph import (
    InsightGraphDependencies,
    build_insight_graph,
)
from stakeholder_intelligence_agent.interview.graph import build_interview_graph
from tests.fakes import ToolCallingFakeModel
from tests.helpers import insight_context


def _model() -> ToolCallingFakeModel:
    return ToolCallingFakeModel(responses=[AIMessage(content="Offline response.")])


def test_interview_graph_constructs_without_provider_call(settings: Settings) -> None:
    graph = build_interview_graph(
        settings,
        primary_model=_model(),
        fallback_model=_model(),
    )

    assert graph.name == "stakeholder_interview"
    assert "model" in graph.nodes


def test_insight_graph_constructs_with_only_approved_subagent_profile(
    settings: Settings,
) -> None:
    graph = build_insight_graph(
        settings,
        dependencies=InsightGraphDependencies(
            primary_model=_model(),
            fallback_model=_model(),
            researcher_model=_model(),
            editor_model=_model(),
            harness_provider="toolcallingfakemodel",
        ),
    )

    assert graph.name == "stakeholder_insight"
    assert "model" in graph.nodes


def test_concrete_scoped_backend_routes_each_active_context_to_isolated_filesystem(
    settings: Settings,
) -> None:
    artifacts = ScopedArtifactStore(settings.agent_artifacts_root)
    backend = ScopedFilesystemBackend(artifacts)
    first_context = insight_context(thread_id="thread-backend-a")
    second_context = insight_context(thread_id="thread-backend-b", run_id="run-backend-b")

    assert isinstance(backend, BackendProtocol)
    assert not callable(backend)
    activate_artifact_access(first_context.access)
    assert backend.write("/research/finding.md", "first").error is None
    activate_artifact_access(second_context.access)
    assert backend.read("/research/finding.md").error is not None
    assert backend.write("/research/finding.md", "second").error is None
    activate_artifact_access(first_context.access)
    assert backend.read("/research/finding.md", limit=10).file_data == {
        "content": "first",
        "encoding": "utf-8",
    }
