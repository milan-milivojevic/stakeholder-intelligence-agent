"""Persistent measured insight timeout, audit, and immutability verification."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

import pytest
from langchain_core.messages import AIMessage, ToolCall

from stakeholder_intelligence_agent.access import AccessService
from stakeholder_intelligence_agent.artifacts import ScopedArtifactStore
from stakeholder_intelligence_agent.contracts import (
    InsightExecutionEvent,
    InsightExecutionMetrics,
    InsightReport,
    InsightRuntimeContext,
)
from stakeholder_intelligence_agent.errors import ReportNotProducedError
from stakeholder_intelligence_agent.insight import (
    InsightExecutionService,
    InsightGraphDependencies,
    InsightRunRepository,
    build_insight_graph,
)
from stakeholder_intelligence_agent.persistence import DomainDatabase
from tests.fakes import ToolCallingFakeModel

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from typing import Literal

    from langgraph.pregel import Pregel

    from stakeholder_intelligence_agent.config import Settings

pytestmark = [pytest.mark.integration, pytest.mark.timeout(10)]


class SlowGraph:
    """Graph boundary double that exceeds the configured whole-run deadline."""

    async def astream(
        self,
        *_args: object,
        **_kwargs: object,
    ) -> AsyncIterator[tuple[str, object]]:
        """Wait long enough for the service-owned timeout to cancel this stream."""
        await asyncio.sleep(2)
        yield "values", {}


def _tool_call(name: str, arguments: dict[str, Any], call_id: str) -> ToolCall:
    return {
        "name": name,
        "args": arguments,
        "id": call_id,
        "type": "tool_call",
    }


def _terminal_report(
    context: InsightRuntimeContext,
    status: Literal["partial", "insufficient_evidence"],
    now: datetime,
) -> InsightReport:
    supported = status == "partial"
    payload: dict[str, Any] = {
        "report_id": f"report-{status}",
        "engagement_id": context.access.engagement_id,
        "question": context.question,
        "status": status,
        "executive_summary": (
            "One bounded finding is supported and one material evidence gap remains."
            if supported
            else "No authorized current evidence supports a responsible conclusion."
        ),
        "researched_topics": [
            {
                "topic_id": "topic-supported" if supported else "topic-gap",
                "title": "Supported handoff" if supported else "Evidence gap",
                "status": "completed" if supported else "insufficient_evidence",
                "summary": (
                    "Registered evidence supports one handoff finding."
                    if supported
                    else "No READY source was available."
                ),
                "evidence_ids": ["evidence-safe"] if supported else [],
            },
            *(
                [
                    {
                        "topic_id": "topic-gap",
                        "title": "Ownership gap",
                        "status": "insufficient_evidence",
                        "summary": "No READY ownership source was available.",
                        "evidence_ids": [],
                    }
                ]
                if supported
                else []
            ),
        ],
        "findings": (
            [
                {
                    "claim_id": "claim-safe",
                    "statement": "The current handoff is described in the registered source.",
                    "evidence_ids": ["evidence-safe"],
                }
            ]
            if supported
            else []
        ),
        "responsibilities": [],
        "operational_risks": [],
        "buy_in_signals": [],
        "contradictions": [],
        "evidence_gaps": [
            {
                "topic": "Ownership gap" if supported else "Evidence gap",
                "description": "No READY source establishes accountable ownership.",
                "impact": "The report cannot assign responsibility from current evidence.",
            }
        ],
        "open_questions": ["Which READY source establishes accountable ownership?"],
        "follow_up_recommendations": [],
        "evidence_ids": ["evidence-safe"] if supported else [],
        "citations": (
            [
                {
                    "citation_id": "citation-safe",
                    "evidence_id": "evidence-safe",
                    "display_label": "Synthetic brief, page 1",
                    "source_location": {
                        "kind": "pdf_page",
                        "filename": "synthetic-brief.pdf",
                        "page": 1,
                    },
                    "claim_ids": ["claim-safe"],
                }
            ]
            if supported
            else []
        ),
        "run_metadata": {
            "run_id": context.run_id,
            "started_at": now,
            "completed_at": now,
            "primary_model_id": "gemini-test-primary",
            "fallback_model_id": "gemini-test-fallback",
            "topic_count": 2 if supported else 1,
            "status_detail": "The bounded synthetic persistence flow completed.",
        },
    }
    return InsightReport.model_validate(payload)


async def test_timeout_is_failed_measured_audited_and_immutable(settings: Settings) -> None:
    runtime_settings = settings.model_copy(update={"insight_run_timeout_seconds": 1})
    database = DomainDatabase(runtime_settings.domain_database)
    access = AccessService(database, runtime_settings)
    await access.initialize()
    session = await access.activate_pm(runtime_settings.pm_bootstrap_token.get_secret_value())
    token = session.token.get_secret_value()
    engagement = await access.create_engagement(
        token,
        name="Timeout observability",
        description="Synthetic bounded-time verification.",
        correlation_id="timeout-engagement",
    )
    pm_access = await access.resolve_pm_context(
        token,
        engagement.engagement_id,
        correlation_id="timeout-correlation",
        thread_id="thread-timeout-observability",
        required_permission="insight:run",
    )
    context = InsightRuntimeContext(
        access=pm_access,
        run_id="run-timeout-observability",
        question="Which evidence-backed result can be produced before the deadline?",
    )
    repository = InsightRunRepository(database)
    execution = InsightExecutionService(
        graph=cast("Pregel[Any, Any, Any, Any]", SlowGraph()),
        repository=repository,
        artifacts=ScopedArtifactStore(runtime_settings.agent_artifacts_root),
        settings=runtime_settings,
    )

    with pytest.raises(TimeoutError):
        await execution.execute(context)

    run = await repository.load(context, now=datetime.now(UTC))
    metrics = await repository.metrics(context, now=datetime.now(UTC))
    assert run.status == "failed"
    assert run.failure_code == "INSIGHT_TIMEOUT"
    assert metrics.status == "failed"
    assert metrics.failure_code == "INSIGHT_TIMEOUT"
    assert metrics.timeout_count == 1
    assert metrics.duration_ms >= 900
    assert metrics.configured_run_timeout_seconds == 1
    assert metrics.model_calls == metrics.tool_calls == 0
    assert await repository.execution_events(context, now=datetime.now(UTC)) == ()

    async with database.connection() as connection:
        cursor = await connection.execute(
            """
            SELECT run_id, engagement_id, thread_id, actor, action, status,
                duration_ms, retry_count, failure_code, correlation_id
            FROM operational_audit_events
            WHERE run_id = ? AND action = 'insight_execution'
            """,
            (context.run_id,),
        )
        audit = await cursor.fetchone()
        assert audit is not None
        assert dict(audit) == {
            "run_id": context.run_id,
            "engagement_id": context.access.engagement_id,
            "thread_id": context.access.thread_id,
            "actor": "insight_service",
            "action": "insight_execution",
            "status": "failed",
            "duration_ms": metrics.duration_ms,
            "retry_count": 0,
            "failure_code": "INSIGHT_TIMEOUT",
            "correlation_id": context.access.correlation_id,
        }
        with pytest.raises(sqlite3.IntegrityError):
            await connection.execute(
                "UPDATE insight_execution_metrics SET duration_ms = 0 WHERE run_id = ?",
                (context.run_id,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            await connection.execute(
                "DELETE FROM insight_execution_metrics WHERE run_id = ?",
                (context.run_id,),
            )


async def test_same_active_pm_question_reuses_one_run_atomically(settings: Settings) -> None:
    database = DomainDatabase(settings.domain_database)
    access = AccessService(database, settings)
    await access.initialize()
    session = await access.activate_pm(settings.pm_bootstrap_token.get_secret_value())
    token = session.token.get_secret_value()
    engagement = await access.create_engagement(
        token,
        name="Duplicate insight protection",
        description="Synthetic active-run idempotency verification.",
        correlation_id="duplicate-engagement",
    )
    first_access = await access.resolve_pm_context(
        token,
        engagement.engagement_id,
        correlation_id="duplicate-first",
        thread_id="thread-duplicate-first",
        required_permission="insight:run",
    )
    second_access = await access.resolve_pm_context(
        token,
        engagement.engagement_id,
        correlation_id="duplicate-second",
        thread_id="thread-duplicate-second",
        required_permission="insight:run",
    )
    question = "What current evidence supports the shared operational process?"
    first_context = InsightRuntimeContext(
        access=first_access,
        run_id="run-duplicate-first",
        question=question,
    )
    second_context = InsightRuntimeContext(
        access=second_access,
        run_id="run-duplicate-second",
        question=question,
    )
    repository = InsightRunRepository(database)
    now = datetime.now(UTC)

    first, second = await asyncio.gather(
        repository.start(first_context, now=now),
        repository.start(second_context, now=now),
    )

    assert first.run_id == second.run_id
    assert first.requested_question == question
    async with database.connection() as connection:
        cursor = await connection.execute(
            "SELECT COUNT(*) AS total FROM insight_runs WHERE engagement_id = ?",
            (engagement.engagement_id,),
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row["total"] == 1


@pytest.mark.parametrize("status", ["partial", "insufficient_evidence"])
async def test_non_complete_report_outcomes_keep_complete_observability_fields(
    settings: Settings,
    status: Literal["partial", "insufficient_evidence"],
) -> None:
    database = DomainDatabase(settings.domain_database)
    access = AccessService(database, settings)
    await access.initialize()
    session = await access.activate_pm(settings.pm_bootstrap_token.get_secret_value())
    token = session.token.get_secret_value()
    engagement = await access.create_engagement(
        token,
        name=f"Observed {status}",
        description="Synthetic terminal-status persistence verification.",
        correlation_id=f"{status}-engagement",
    )
    pm_access = await access.resolve_pm_context(
        token,
        engagement.engagement_id,
        correlation_id=f"{status}-correlation",
        thread_id=f"thread-{status}",
        required_permission="insight:run",
    )
    context = InsightRuntimeContext(
        access=pm_access,
        run_id=f"run-{status}",
        question="What conclusion is justified by the current bounded evidence?",
    )
    repository = InsightRunRepository(database)
    now = datetime.now(UTC)
    await repository.start(context, now=now)
    await repository.transition(context, "planning", now=now)
    await repository.transition(context, "researching", plan_id="plan-safe", now=now)
    await repository.transition(context, "editing", actor="report-editor", now=now)
    await repository.transition(context, "validating", actor="report-editor", now=now)
    report = _terminal_report(context, status, now)
    artifacts = ScopedArtifactStore(settings.agent_artifacts_root)
    artifacts.write_json(
        context.access,
        "/report/insight_report.json",
        report.model_dump(mode="json"),
    )
    terminal = await repository.complete(
        context,
        report,
        virtual_path="/report/insight_report.json",
        content_hash="a" * 64,
        now=now,
    )
    execution = InsightExecutionService(
        graph=cast("Pregel[Any, Any, Any, Any]", SlowGraph()),
        repository=repository,
        artifacts=artifacts,
        settings=settings,
    )
    source_ids = ("source-safe",) if status == "partial" else ()
    evidence_ids = ("evidence-safe",) if status == "partial" else ()
    metrics = InsightExecutionMetrics(
        run_id=context.run_id,
        engagement_id=context.access.engagement_id,
        thread_id=context.access.thread_id or "missing-thread",
        started_at=now,
        completed_at=now,
        status=status,
        duration_ms=25,
        topic_count=report.run_metadata.topic_count,
        researcher_calls=2 if status == "partial" else 1,
        max_concurrent_researchers=1,
        model_calls=4,
        model_failures=0,
        tool_calls=5,
        tool_failures=0,
        retrieval_calls=1,
        retry_count=0,
        timeout_count=0,
        rerank_candidates_total=3 if status == "partial" else 0,
        max_rerank_candidates_per_call=3 if status == "partial" else 0,
        retrieval_latency_ms=8,
        reranker_latency_ms=2 if status == "partial" else 0,
        input_tokens=20,
        output_tokens=10,
        total_tokens=30,
        configured_topic_limit=settings.max_research_topics,
        configured_parallel_researcher_limit=settings.max_parallel_researchers,
        configured_model_call_limit=settings.model_run_call_limit,
        configured_tool_call_limit=settings.tool_run_call_limit,
        configured_retrieval_calls_per_researcher_limit=(
            settings.retrieval_calls_per_researcher_limit
        ),
        configured_rerank_candidate_limit=settings.max_rerank_candidates,
        configured_provider_timeout_seconds=settings.provider_timeout_seconds,
        configured_run_timeout_seconds=settings.insight_run_timeout_seconds,
        source_ids=source_ids,
        evidence_ids=evidence_ids,
        tool_names=("save_final_report",),
        correlation_id=context.access.correlation_id,
    )
    event = InsightExecutionEvent(
        event_id=f"event-{status}",
        occurred_at=now,
        run_id=context.run_id,
        engagement_id=context.access.engagement_id,
        thread_id=context.access.thread_id or "missing-thread",
        actor="report-editor",
        operation_type="tool",
        tool_name="save_final_report",
        status="succeeded",
        duration_ms=5,
        source_ids=source_ids,
        evidence_ids=evidence_ids,
        correlation_id=context.access.correlation_id,
    )

    persisted = await repository.record_execution(
        context,
        metrics,
        (event,),
        now=now,
    )
    loaded_run, loaded_report, loaded_metrics = await execution.load_report(context)

    assert terminal.status == status
    assert loaded_run == terminal
    assert loaded_report == report
    assert loaded_metrics == persisted
    assert persisted.status == status
    assert persisted.thread_id == context.access.thread_id
    assert persisted.source_ids == source_ids
    assert persisted.evidence_ids == evidence_ids
    measured_events = await repository.execution_events(context, now=now)
    assert measured_events == (event,)
    async with database.connection() as connection:
        cursor = await connection.execute(
            """
            SELECT status, duration_ms, retry_count, failure_code
            FROM operational_audit_events
            WHERE run_id = ? AND action = 'insight_execution'
            """,
            (context.run_id,),
        )
        audit = await cursor.fetchone()
        assert audit is not None
        assert dict(audit) == {
            "status": "succeeded",
            "duration_ms": 25,
            "retry_count": 0,
            "failure_code": None,
        }


async def test_real_deep_agent_observes_three_parallel_researchers(settings: Settings) -> None:
    database = DomainDatabase(settings.domain_database)
    access = AccessService(database, settings)
    await access.initialize()
    session = await access.activate_pm(settings.pm_bootstrap_token.get_secret_value())
    token = session.token.get_secret_value()
    engagement = await access.create_engagement(
        token,
        name="Parallel researcher observation",
        description="Synthetic three-task concurrency measurement.",
        correlation_id="parallel-engagement",
    )
    pm_access = await access.resolve_pm_context(
        token,
        engagement.engagement_id,
        correlation_id="parallel-correlation",
        thread_id="thread-parallel-observability",
        required_permission="insight:run",
    )
    context = InsightRuntimeContext(
        access=pm_access,
        run_id="run-parallel-observability",
        question="Which three bounded topics require separate researchers?",
    )
    topics: list[dict[str, Any]] = [
        {
            "topic_id": f"topic-{index}",
            "title": f"Research Topic {index}",
            "objective": f"Inspect bounded topic {index}.",
            "questions": [f"What supports topic {index}?"],
            "required_source_types": ["document", "interview"],
            "dependencies": [],
            "priority": index,
        }
        for index in range(1, 4)
    ]
    primary = ToolCallingFakeModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    _tool_call(
                        "write_todos",
                        {
                            "todos": [
                                *[
                                    {
                                        "content": topic["title"],
                                        "status": "in_progress",
                                    }
                                    for topic in topics
                                ],
                                {"content": "Edit the report", "status": "pending"},
                                {"content": "Validate the report", "status": "pending"},
                            ]
                        },
                        "parallel-todos",
                    )
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    _tool_call(
                        "create_research_plan",
                        {
                            "topics": topics,
                            "source_strategy": ["document", "interview"],
                            "completion_criteria": [
                                "Each topic must produce a scoped research artifact."
                            ],
                        },
                        "parallel-plan",
                    )
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    _tool_call(
                        "task",
                        {
                            "description": (
                                f"topic_id=topic-{index} Research only Research Topic {index}."
                            ),
                            "subagent_type": "topic-researcher",
                        },
                        f"parallel-researcher-{index}",
                    )
                    for index in range(1, 4)
                ],
            ),
            AIMessage(content="Researchers returned without durable artifacts."),
        ]
    )
    repository = InsightRunRepository(database)
    graph = build_insight_graph(
        settings,
        dependencies=InsightGraphDependencies(
            primary_model=primary,
            fallback_model=ToolCallingFakeModel(
                responses=[AIMessage(content="Fallback response.")]
            ),
            researcher_model=ToolCallingFakeModel(
                responses=[
                    AIMessage(content="No artifact was saved."),
                    AIMessage(content="No artifact was saved."),
                    AIMessage(content="No artifact was saved."),
                ]
            ),
            editor_model=ToolCallingFakeModel(
                responses=[AIMessage(content="Editor was not invoked.")]
            ),
            harness_provider="toolcallingfakemodel",
            run_repository=repository,
        ),
    )
    execution = InsightExecutionService(
        graph=graph,
        repository=repository,
        artifacts=ScopedArtifactStore(settings.agent_artifacts_root),
        settings=settings,
    )

    with pytest.raises(ReportNotProducedError):
        await execution.execute(context)

    metrics = await repository.metrics(context, now=datetime.now(UTC))
    measured_events = await repository.execution_events(context, now=datetime.now(UTC))
    task_events = [event for event in measured_events if event.tool_name == "task"]
    assert metrics.status == "failed"
    assert metrics.topic_count == 3
    assert metrics.researcher_calls == 3
    assert metrics.max_concurrent_researchers == 3
    assert len(task_events) == 3
    assert all(event.actor == "topic-researcher" for event in task_events)
