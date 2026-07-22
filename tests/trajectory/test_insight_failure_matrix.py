"""Verify that bounded insight failures stay visible and never create a report."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import pytest
from langchain.agents.middleware.model_call_limit import ModelCallLimitExceededError
from langchain.agents.middleware.tool_call_limit import ToolCallLimitExceededError
from langchain_core.messages import AIMessage, BaseMessage, ToolCall

from stakeholder_intelligence_agent.access import AccessService
from stakeholder_intelligence_agent.artifacts import ScopedArtifactStore
from stakeholder_intelligence_agent.contracts import InsightRuntimeContext
from stakeholder_intelligence_agent.errors import RetrievalExecutionError
from stakeholder_intelligence_agent.insight import (
    InsightExecutionService,
    InsightGraphDependencies,
    InsightRunRepository,
    build_insight_graph,
)
from stakeholder_intelligence_agent.persistence import DomainDatabase
from stakeholder_intelligence_agent.retrieval import HybridRetrievalService, RetrievalRepository
from tests.fakes import ToolCallingFakeModel

if TYPE_CHECKING:
    from stakeholder_intelligence_agent.config import Settings
    from stakeholder_intelligence_agent.contracts import AccessContext
    from stakeholder_intelligence_agent.retrieval.types import RetrievalResult

pytestmark = [pytest.mark.integration, pytest.mark.trajectory, pytest.mark.timeout(90)]
FailureCase = Literal[
    "provider_failure",
    "retrieval_call_limit",
    "invalid_structured_output",
    "model_call_limit",
    "unauthorized_evidence",
    "editor_no_report",
    "tool_call_limit",
]
RecoverableResearchCase = Literal["retrieval_failure", "researcher_artifact_failure"]
Scenario = FailureCase | RecoverableResearchCase
EXPECTED_FAILURE_CODES: dict[FailureCase, str] = {
    "provider_failure": "INSIGHT_EXECUTION_FAILED",
    "retrieval_call_limit": "COURSE_FIDELITY_FAILED",
    "invalid_structured_output": "REPORT_NOT_PRODUCED",
    "model_call_limit": "MODEL_CALL_LIMIT_EXCEEDED",
    "unauthorized_evidence": "EVIDENCE_POLICY_FAILED",
    "editor_no_report": "REPORT_NOT_PRODUCED",
    "tool_call_limit": "TOOL_CALL_LIMIT_EXCEEDED",
}


class FailingRetrievalService(HybridRetrievalService):
    """Raise at the real researcher retrieval-tool boundary."""

    def __init__(self) -> None:
        """Avoid constructing unused retrieval dependencies for this boundary double."""

    async def initialize(self) -> None:
        """Keep initialization deterministic for the injected boundary."""

    async def retrieve(self, access: AccessContext, query: str) -> RetrievalResult:
        """Fail without using caller-controlled scope or query details."""
        del access, query
        raise RetrievalExecutionError


def _tool_call(name: str, arguments: dict[str, Any], call_id: str) -> AIMessage:
    call: ToolCall = {
        "name": name,
        "args": arguments,
        "id": call_id,
        "type": "tool_call",
    }
    return AIMessage(content="", tool_calls=[call])


async def _runtime_context(
    settings: Settings,
    database: DomainDatabase,
    case: Scenario,
) -> tuple[InsightRuntimeContext, str]:
    access = AccessService(database, settings)
    await access.initialize()
    session = await access.activate_pm(settings.pm_bootstrap_token.get_secret_value())
    token = session.token.get_secret_value()
    engagement = await access.create_engagement(
        token,
        name=f"Failure matrix {case}",
        description="Synthetic safe-failure verification.",
        correlation_id=f"failure-{case}",
    )
    resolved = await access.resolve_pm_context(
        token,
        engagement.engagement_id,
        correlation_id=f"failure-context-{case}",
        required_permission="insight:run",
        thread_id=f"thread-{case}",
    )
    return (
        InsightRuntimeContext(
            access=resolved,
            run_id=f"run-{case}",
            question="Which supported operational finding can be reported?",
        ),
        token,
    )


def _primary_responses(case: Scenario) -> list[BaseMessage]:
    if case == "provider_failure":
        return []
    responses: list[BaseMessage] = [
        _tool_call(
            "write_todos",
            {
                "todos": [
                    {"content": "Research Failure Boundary", "status": "in_progress"},
                    {"content": "Edit the structured report", "status": "pending"},
                    {"content": "Validate the report", "status": "pending"},
                ]
            },
            f"{case}-todos",
        ),
        _tool_call(
            "create_research_plan",
            {
                "topics": [
                    {
                        "topic_id": "topic-failure",
                        "title": "Failure Boundary",
                        "objective": "Verify honest terminal behavior.",
                        "questions": ["Can current authorized evidence support a conclusion?"],
                        "required_source_types": ["document", "interview"],
                        "dependencies": [],
                        "priority": 1,
                    }
                ],
                "source_strategy": ["document", "interview"],
                "completion_criteria": ["Never produce an unsupported complete report."],
            },
            f"{case}-plan",
        ),
        _tool_call(
            "task",
            {
                "description": (
                    "topic_id=topic-failure Research only Failure Boundary and save artifacts."
                ),
                "subagent_type": "topic-researcher",
            },
            f"{case}-researcher",
        ),
    ]
    if case in {
        "retrieval_failure",
        "researcher_artifact_failure",
        "invalid_structured_output",
        "unauthorized_evidence",
        "editor_no_report",
    }:
        responses.extend(
            [
                _tool_call(
                    "write_todos",
                    {
                        "todos": [
                            {
                                "content": "Research Failure Boundary",
                                "status": "completed",
                            },
                            {
                                "content": "Edit the structured report",
                                "status": "in_progress",
                            },
                            {"content": "Validate the report", "status": "pending"},
                        ]
                    },
                    f"{case}-research-complete",
                ),
                _tool_call(
                    "task",
                    {
                        "description": "Load the completed research artifact and edit the report.",
                        "subagent_type": "report-editor",
                    },
                    f"{case}-editor",
                ),
                _tool_call(
                    "write_todos",
                    {
                        "todos": [
                            {
                                "content": "Research Failure Boundary",
                                "status": "completed",
                            },
                            {
                                "content": "Edit the structured report",
                                "status": "completed",
                            },
                            {"content": "Validate the report", "status": "completed"},
                        ]
                    },
                    f"{case}-workflow-complete",
                ),
            ]
        )
    else:
        responses.append(
            _tool_call(
                "task",
                {
                    "description": "Load the completed research artifact and edit the report.",
                    "subagent_type": "report-editor",
                },
                f"{case}-editor",
            )
        )
    responses.append(AIMessage(content="The bounded workflow has stopped."))
    return responses


def _researcher_responses(case: Scenario) -> list[BaseMessage]:
    if case == "provider_failure":
        return []
    if case == "researcher_artifact_failure":
        return [AIMessage(content="No research artifact was saved.")]
    responses: list[BaseMessage] = [
        _tool_call(
            "scoped_retrieve",
            {"topic_id": "topic-failure", "query": "current authorized evidence"},
            f"{case}-retrieve",
        ),
        _tool_call(
            "think_tool",
            {"reflection": "Check only the assigned evidence boundary."},
            f"{case}-think",
        ),
    ]
    if case == "retrieval_failure":
        responses.append(AIMessage(content="Retrieval failed and no artifact can be supported."))
        return responses
    if case == "retrieval_call_limit":
        responses.append(
            _tool_call(
                "scoped_retrieve",
                {"topic_id": "topic-failure", "query": "repeat authorized evidence search"},
                f"{case}-retrieve-above-limit",
            )
        )
    responses.extend(
        [
            _tool_call(
                "save_research_artifacts",
                {
                    "topic_id": "topic-failure",
                    "findings_markdown": "# Findings\n\nNo authorized evidence was available.",
                    "evidence_ids": [],
                },
                f"{case}-save-research",
            ),
            AIMessage(content="The insufficient-evidence artifact was saved."),
        ]
    )
    return responses


def _forged_report(context: InsightRuntimeContext) -> dict[str, Any]:
    return {
        "report_id": "forged-report",
        "engagement_id": context.access.engagement_id,
        "question": context.question,
        "status": "complete",
        "executive_summary": "A forged evidence reference must be rejected.",
        "researched_topics": [
            {
                "topic_id": "topic-failure",
                "title": "Failure Boundary",
                "status": "completed",
                "summary": "The model claimed evidence that was never registered.",
                "evidence_ids": ["forged-evidence"],
            }
        ],
        "findings": [
            {
                "claim_id": "forged-claim",
                "statement": "This unsupported claim must never persist.",
                "evidence_ids": ["forged-evidence"],
            }
        ],
        "responsibilities": [],
        "operational_risks": [],
        "buy_in_signals": [],
        "contradictions": [],
        "evidence_gaps": [],
        "open_questions": [],
        "follow_up_recommendations": [],
        "evidence_ids": ["forged-evidence"],
        "citations": [
            {
                "citation_id": "forged-citation",
                "evidence_id": "forged-evidence",
                "display_label": "Forged evidence",
                "source_location": {
                    "kind": "pdf_page",
                    "filename": "forged.pdf",
                    "page": 1,
                },
                "claim_ids": ["forged-claim"],
            }
        ],
        "run_metadata": {
            "run_id": context.run_id,
            "started_at": "2026-07-15T00:00:00Z",
            "completed_at": "2026-07-15T00:00:01Z",
            "primary_model_id": "gemini-test-primary",
            "fallback_model_id": "gemini-test-fallback",
            "topic_count": 1,
            "status_detail": "This payload is intentionally unauthorized.",
        },
    }


def _editor_responses(
    case: Scenario,
    context: InsightRuntimeContext,
) -> list[BaseMessage]:
    if case == "provider_failure":
        return [
            _tool_call("load_research_package", {}, f"{case}-load"),
            AIMessage(content="The editor cannot produce a validated report."),
        ]
    if case in {"researcher_artifact_failure", "retrieval_failure"}:
        return [
            _tool_call("load_research_package", {}, f"{case}-load"),
            _tool_call(
                "save_final_report",
                {
                    "report": {
                        "status": "insufficient_evidence",
                        "executive_summary": (
                            "The assigned topic reached an explicit operational failure, so no "
                            "supported business conclusion is available."
                        ),
                        "researched_topics": [
                            {
                                "topic_id": "topic-failure",
                                "status": "failed",
                                "summary": (
                                    "The bounded researcher did not produce authorized evidence."
                                ),
                            }
                        ],
                        "findings": [],
                        "responsibilities": [],
                        "operational_risks": [],
                        "buy_in_signals": [],
                        "contradictions": [],
                        "evidence_gaps": [
                            {
                                "topic": "Failure Boundary",
                                "description": (
                                    "The researcher failed before it could register evidence."
                                ),
                                "impact": (
                                    "The planned topic cannot support a business conclusion."
                                ),
                            }
                        ],
                        "open_questions": [
                            "Can the topic be researched after the operational issue is resolved?"
                        ],
                        "follow_up_recommendations": [],
                    }
                },
                f"{case}-save",
            ),
            AIMessage(content="The explicit insufficient-evidence report was saved."),
        ]
    if case == "invalid_structured_output":
        return [
            _tool_call("load_research_package", {}, f"{case}-load"),
            _tool_call(
                "save_final_report",
                {"report": {"unexpected": "invalid structured output"}},
                f"{case}-save",
            ),
            AIMessage(content="The invalid payload was rejected."),
        ]
    if case == "unauthorized_evidence":
        forged_report = _forged_report(context)
        return [
            _tool_call("load_research_package", {}, f"{case}-load"),
            _tool_call(
                "save_final_report",
                {"report": forged_report},
                f"{case}-save-1",
            ),
            _tool_call(
                "save_final_report",
                {"report": forged_report},
                f"{case}-save-2",
            ),
            _tool_call(
                "save_final_report",
                {"report": forged_report},
                f"{case}-save-3",
            ),
        ]
    return [
        _tool_call("load_research_package", {}, f"{case}-load"),
        AIMessage(content="The editor stopped without a report artifact."),
    ]


@pytest.mark.parametrize(
    "case",
    [
        "provider_failure",
        "retrieval_call_limit",
        "invalid_structured_output",
        "model_call_limit",
        "unauthorized_evidence",
        "editor_no_report",
        "tool_call_limit",
    ],
)
async def test_failure_never_persists_or_projects_a_complete_report(
    settings: Settings,
    case: FailureCase,
) -> None:
    overrides: dict[str, int] = {}
    if case == "model_call_limit":
        overrides["model_run_call_limit"] = 2
    if case == "tool_call_limit":
        overrides["tool_run_call_limit"] = 2
    if case == "retrieval_call_limit":
        overrides["retrieval_calls_per_researcher_limit"] = 1
    runtime_settings = settings.model_copy(update=overrides)
    database = DomainDatabase(runtime_settings.domain_database)
    context, raw_pm_token = await _runtime_context(runtime_settings, database, case)
    run_repository = InsightRunRepository(database)
    primary = ToolCallingFakeModel(responses=_primary_responses(case))
    fallback = ToolCallingFakeModel(responses=[])
    researcher = ToolCallingFakeModel(responses=_researcher_responses(case))
    editor = ToolCallingFakeModel(responses=_editor_responses(case, context))
    retrieval = None
    evidence_repository = RetrievalRepository(database)
    graph = build_insight_graph(
        runtime_settings,
        dependencies=InsightGraphDependencies(
            primary_model=primary,
            fallback_model=fallback,
            researcher_model=researcher,
            editor_model=editor,
            harness_provider="toolcallingfakemodel",
            retrieval_service=retrieval,
            evidence_repository=evidence_repository,
            run_repository=run_repository,
        ),
    )
    artifacts = ScopedArtifactStore(settings.agent_artifacts_root)
    execution = InsightExecutionService(
        graph=graph,
        repository=run_repository,
        artifacts=artifacts,
        settings=runtime_settings,
    )

    with pytest.raises(
        (
            RuntimeError,
            ValueError,
            IndexError,
            ModelCallLimitExceededError,
            ToolCallLimitExceededError,
        )
    ):
        await execution.execute(context)

    failed = await run_repository.load(context, now=datetime.now(UTC))
    events = await run_repository.events(context, now=datetime.now(UTC))
    metrics = await run_repository.metrics(context, now=datetime.now(UTC))
    measured_events = await run_repository.execution_events(context, now=datetime.now(UTC))
    assert failed.status == "failed"
    assert failed.report_id is None
    assert failed.failure_code == EXPECTED_FAILURE_CODES[case]
    assert failed.failure_message is not None
    assert metrics.status == "failed"
    assert metrics.failure_code == EXPECTED_FAILURE_CODES[case]
    assert metrics.model_calls >= 1
    assert metrics.model_failures <= metrics.model_calls
    assert metrics.tool_failures <= metrics.tool_calls
    assert metrics.max_concurrent_researchers <= runtime_settings.max_parallel_researchers
    assert all(event.run_id == context.run_id for event in measured_events)
    assert all(event.engagement_id == context.access.engagement_id for event in measured_events)
    assert all(event.thread_id == context.access.thread_id for event in measured_events)
    assert all(event.correlation_id == context.access.correlation_id for event in measured_events)
    assert any(event["action"] == "run_failed" for event in events)
    assert not artifacts.exists(context.access, "/report/insight_report.json")
    safe_projection = json.dumps(
        {
            "run": failed.model_dump(mode="json"),
            "metrics": metrics.model_dump(mode="json"),
            "events": events,
            "measured_events": [event.model_dump(mode="json") for event in measured_events],
        },
        default=str,
        sort_keys=True,
    )
    assert raw_pm_token not in safe_projection
    assert runtime_settings.google_api_key.get_secret_value() not in safe_projection
    assert "invalid structured output" not in safe_projection
    assert "forged-evidence" not in safe_projection
    assert "private reasoning" not in safe_projection.casefold()
    assert all(
        Path(str(event.get("artifact_name", "."))).is_absolute() is False for event in events
    )


@pytest.mark.parametrize("case", ["retrieval_failure", "researcher_artifact_failure"])
async def test_researcher_failure_becomes_explicit_insufficient_evidence(
    settings: Settings,
    case: RecoverableResearchCase,
) -> None:
    database = DomainDatabase(settings.domain_database)
    context, _ = await _runtime_context(settings, database, case)
    run_repository = InsightRunRepository(database)
    graph = build_insight_graph(
        settings,
        dependencies=InsightGraphDependencies(
            primary_model=ToolCallingFakeModel(responses=_primary_responses(case)),
            fallback_model=ToolCallingFakeModel(responses=[]),
            researcher_model=ToolCallingFakeModel(responses=_researcher_responses(case)),
            editor_model=ToolCallingFakeModel(responses=_editor_responses(case, context)),
            harness_provider="toolcallingfakemodel",
            retrieval_service=(FailingRetrievalService() if case == "retrieval_failure" else None),
            evidence_repository=RetrievalRepository(database),
            run_repository=run_repository,
        ),
    )
    artifacts = ScopedArtifactStore(settings.agent_artifacts_root)
    execution = InsightExecutionService(
        graph=graph,
        repository=run_repository,
        artifacts=artifacts,
        settings=settings,
    )

    result = await execution.execute(context)
    manifest = artifacts.read_json(context.access, "/research/topic-failure/sources.json")
    events = await run_repository.events(context, now=datetime.now(UTC))

    assert result.run.status == "insufficient_evidence"
    assert result.report.status == "insufficient_evidence"
    assert result.report.researched_topics[0].status == "failed"
    assert result.report.findings == ()
    assert manifest["status"] == "failed"
    assert artifacts.exists(context.access, "/report/insight_report.json")
    assert any(event["action"] == "research_topic_failed" for event in events)
    assert not any(event["action"] == "run_failed" for event in events)
