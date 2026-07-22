"""Single authorized application entry point for every PM Deep Agent insight run."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from langchain_core.messages import HumanMessage
from pydantic import ValidationError

from stakeholder_intelligence_agent.contracts import (
    InsightExecutionMetrics,
    InsightReport,
    InsightRun,
    ResearchPlan,
)
from stakeholder_intelligence_agent.errors import (
    AccessDeniedError,
    DomainConflictError,
    ReportNotProducedError,
    StakeholderIntelligenceError,
)
from stakeholder_intelligence_agent.ingestion.identity import stable_id
from stakeholder_intelligence_agent.insight.observability import InsightExecutionRecorder

if TYPE_CHECKING:
    from collections.abc import Callable

    from langchain_core.runnables import RunnableConfig
    from langgraph.pregel import Pregel

    from stakeholder_intelligence_agent.artifacts import ScopedArtifactStore
    from stakeholder_intelligence_agent.config import Settings
    from stakeholder_intelligence_agent.contracts import InsightRuntimeContext
    from stakeholder_intelligence_agent.insight.repository import InsightRunRepository


@dataclass(frozen=True, slots=True)
class InsightExecutionResult:
    """Terminal validated report, run state, graph state, and safe trajectory facts."""

    run: InsightRun
    report: InsightReport
    metrics: InsightExecutionMetrics
    graph_state: dict[str, object]
    events: tuple[dict[str, object], ...]
    idempotent: bool


class InsightExecutionService:
    """Prevent direct report paths by wrapping the one required Deep Agent graph."""

    def __init__(
        self,
        *,
        graph: Pregel[Any, Any, Any, Any],
        repository: InsightRunRepository,
        artifacts: ScopedArtifactStore,
        settings: Settings,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._graph = graph
        self._repository = repository
        self._artifacts = artifacts
        self._settings = settings
        self._clock = clock or (lambda: datetime.now(UTC))

    async def initialize(self) -> None:
        """Prepare the persistent run/report lifecycle."""
        await self._repository.initialize()

    async def execute(self, context: InsightRuntimeContext) -> InsightExecutionResult:
        """Run or idempotently load the sole Deep Agent report path."""
        recorder = InsightExecutionRecorder(context, self._settings, clock=self._clock)
        await self.initialize()
        run = await self._repository.start(context, now=self._clock())
        if run.status in {"complete", "partial", "insufficient_evidence"}:
            metrics = await self._repository.metrics(context, now=self._clock())
            return await self._terminal_result(
                context,
                run,
                metrics,
                {},
                idempotent=True,
            )
        if run.status == "failed":
            raise DomainConflictError
        if run.status == "queued":
            await self._repository.transition(context, "planning", now=self._clock())
        thread_id = context.access.thread_id
        if thread_id is None:
            raise AccessDeniedError
        config: RunnableConfig = {
            "configurable": {"thread_id": thread_id},
            "callbacks": [recorder],
        }
        message_id = stable_id("insight-question", context.run_id, context.question)
        try:
            async with asyncio.timeout(float(self._settings.insight_run_timeout_seconds)):
                graph_state = await self._consume_graph_stream(
                    context,
                    config,
                    message_id,
                )
            current = await self._repository.load(context, now=self._clock())
            if current.status == "validating":
                report = await asyncio.to_thread(self._candidate_report, context)
                report_content = await self._artifacts.aread_text(
                    context.access,
                    "/report/insight_report.json",
                )
                terminal = await self._repository.complete(
                    context,
                    report,
                    virtual_path="/report/insight_report.json",
                    content_hash=hashlib.sha256(report_content.encode("utf-8")).hexdigest(),
                    now=self._clock(),
                )
            elif current.status in {"complete", "partial", "insufficient_evidence"}:
                terminal = current
            else:
                raise ReportNotProducedError
            metrics = await self._persist_measurements(context, terminal, recorder)
            return await self._terminal_result(
                context,
                terminal,
                metrics,
                graph_state,
                idempotent=False,
            )
        except AccessDeniedError:
            raise
        except Exception as error:
            if isinstance(error, TimeoutError):
                recorder.mark_timeout()
            safe_code = self._safe_failure_code(error)
            safe_message = (
                "The insight workflow exceeded its time limit."
                if isinstance(error, TimeoutError)
                else (
                    str(error)
                    if isinstance(error, StakeholderIntelligenceError)
                    else "The insight workflow could not be completed."
                )
            )
            failed = await self._repository.fail(
                context,
                failure_code=safe_code,
                failure_message=safe_message[:500],
                now=self._clock(),
            )
            await self._persist_measurements(context, failed, recorder)
            raise

    @staticmethod
    def _safe_failure_code(error: Exception) -> str:
        """Classify known bounded failures without retaining provider or payload details."""
        if isinstance(error, TimeoutError):
            return "INSIGHT_TIMEOUT"
        code = getattr(error, "code", None)
        if isinstance(code, str):
            return code
        if isinstance(error, ValidationError):
            return "REPORT_VALIDATION_FAILED"
        failure_types = {
            "ModelCallLimitExceededError": "MODEL_CALL_LIMIT_EXCEEDED",
            "ToolCallLimitExceededError": "TOOL_CALL_LIMIT_EXCEEDED",
        }
        classified = failure_types.get(type(error).__name__)
        if classified is not None:
            return classified
        current: BaseException | None = error
        visited: set[int] = set()
        while current is not None and id(current) not in visited:
            visited.add(id(current))
            provider_identity = f"{type(current).__module__}.{type(current).__name__}".casefold()
            if "google" in provider_identity or "generative" in provider_identity:
                return "PROVIDER_EXECUTION_FAILED"
            current = current.__cause__ or current.__context__
        return "INSIGHT_EXECUTION_FAILED"

    async def _consume_graph_stream(
        self,
        context: InsightRuntimeContext,
        config: RunnableConfig,
        message_id: str,
    ) -> dict[str, object]:
        """Consume real graph updates and return only a complete terminal state."""
        graph_state: dict[str, object] | None = None
        try:
            async for mode, update in self._graph.astream(
                {"messages": [HumanMessage(content=context.question, id=message_id)]},
                config=config,
                context=context,
                stream_mode=["updates", "values"],
            ):
                if mode == "updates":
                    await self._record_safe_graph_update(context, update)
                elif mode == "values" and isinstance(update, dict):
                    graph_state = cast("dict[str, object]", update)
        finally:
            self._artifacts.unregister_graph_scope(context.access)
        if graph_state is None or not self._todos_are_complete(graph_state):
            raise ReportNotProducedError
        return graph_state

    async def _record_safe_graph_update(
        self,
        context: InsightRuntimeContext,
        update: object,
    ) -> None:
        """Project an actual LangGraph update without retaining its state payload."""
        if not isinstance(update, dict):
            return
        safe_nodes = {
            "model": "orchestrator_model",
            "tools": "orchestrator_tools",
            "topic-researcher": "researcher_subagent",
            "report-editor": "editor_subagent",
        }
        for node_name in sorted(str(name) for name in update):
            actor = safe_nodes.get(node_name)
            if actor is None:
                continue
            await self._repository.record_activity(
                context,
                actor=actor,
                action="langgraph_update_streamed",
                now=self._clock(),
            )

    @staticmethod
    def _todos_are_complete(graph_state: dict[str, object]) -> bool:
        todos = graph_state.get("todos")
        return (
            isinstance(todos, list)
            and bool(todos)
            and all(isinstance(item, dict) and item.get("status") == "completed" for item in todos)
        )

    async def load_report(
        self,
        context: InsightRuntimeContext,
    ) -> tuple[InsightRun, InsightReport, InsightExecutionMetrics]:
        """Load a terminal report together with its authoritative measured execution."""
        await self.initialize()
        run = await self._repository.load(context, now=self._clock())
        if run.status not in {"complete", "partial", "insufficient_evidence"}:
            raise DomainConflictError
        report = await asyncio.to_thread(self._validated_report, context, run)
        metrics = await self._repository.metrics(context, now=self._clock())
        return run, report, metrics

    async def _terminal_result(
        self,
        context: InsightRuntimeContext,
        run: InsightRun,
        metrics: InsightExecutionMetrics,
        graph_state: dict[str, object],
        *,
        idempotent: bool,
    ) -> InsightExecutionResult:
        report = await asyncio.to_thread(self._validated_report, context, run)
        return InsightExecutionResult(
            run=run,
            report=report,
            metrics=metrics,
            graph_state=graph_state,
            events=await self._repository.events(context, now=self._clock()),
            idempotent=idempotent,
        )

    def _validated_report(
        self,
        context: InsightRuntimeContext,
        run: InsightRun,
    ) -> InsightReport:
        payload = self._artifacts.read_json(context.access, "/report/insight_report.json")
        report = InsightReport.model_validate(payload)
        if (
            report.report_id != run.report_id
            or report.status != run.status
            or report.engagement_id != run.engagement_id
            or report.run_metadata.run_id != run.run_id
        ):
            raise DomainConflictError
        return report

    def _candidate_report(self, context: InsightRuntimeContext) -> InsightReport:
        """Validate the editor artifact before making its run terminal and immutable."""
        payload = self._artifacts.read_json(context.access, "/report/insight_report.json")
        report = InsightReport.model_validate(payload)
        if (
            report.report_id != stable_id("report", context.run_id)
            or report.engagement_id != context.access.engagement_id
            or report.question != context.question
            or report.run_metadata.run_id != context.run_id
        ):
            raise DomainConflictError
        return report

    async def _persist_measurements(
        self,
        context: InsightRuntimeContext,
        run: InsightRun,
        recorder: InsightExecutionRecorder,
    ) -> InsightExecutionMetrics:
        """Persist measured totals plus safe spans after the domain run is terminal."""
        if run.status not in {"complete", "partial", "insufficient_evidence", "failed"}:
            raise DomainConflictError
        trajectory = await self._repository.events(context, now=self._clock())
        source_ids, evidence_ids = self._trajectory_ids(trajectory)
        topic_count = await asyncio.to_thread(self._topic_count, context)
        metrics, events = recorder.snapshot(
            status=run.status,
            completed_at=run.completed_at or self._clock(),
            topic_count=topic_count,
            failure_code=run.failure_code,
            source_ids=source_ids,
            evidence_ids=evidence_ids,
        )
        return await self._repository.record_execution(
            context,
            metrics,
            events,
            now=self._clock(),
        )

    def _topic_count(self, context: InsightRuntimeContext) -> int:
        """Read the validated plan count when planning reached durable artifacts."""
        if not self._artifacts.exists(context.access, "/research_plan.json"):
            return 0
        try:
            plan = ResearchPlan.model_validate(
                self._artifacts.read_json(context.access, "/research_plan.json")
            )
        except (StakeholderIntelligenceError, ValidationError):
            return 0
        return len(plan.topics)

    @staticmethod
    def _trajectory_ids(
        trajectory: tuple[dict[str, object], ...],
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Aggregate only opaque source/evidence IDs from safe trajectory rows."""
        source_ids: set[str] = set()
        evidence_ids: set[str] = set()
        for row in trajectory:
            for key, target in (
                ("source_ids_json", source_ids),
                ("evidence_ids_json", evidence_ids),
            ):
                raw = row.get(key)
                if not isinstance(raw, str):
                    continue
                try:
                    values = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(values, list):
                    target.update(value for value in values if isinstance(value, str))
        return tuple(sorted(source_ids)), tuple(sorted(evidence_ids))
