"""Safe in-process measurement for the one authorized insight graph execution."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Lock
from time import perf_counter
from typing import TYPE_CHECKING, Any, override

from langchain_core.callbacks import BaseCallbackHandler

from stakeholder_intelligence_agent.contracts import (
    InsightExecutionEvent,
    InsightExecutionMetrics,
)
from stakeholder_intelligence_agent.ingestion.identity import stable_id

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from typing import Literal
    from uuid import UUID

    from langchain_core.messages import BaseMessage
    from langchain_core.outputs import LLMResult

    from stakeholder_intelligence_agent.config import Settings
    from stakeholder_intelligence_agent.contracts import InsightRuntimeContext
    from stakeholder_intelligence_agent.contracts.execution import TerminalInsightRunStatus


_TOOL_ACTORS = {
    "create_research_plan": "insight-orchestrator",
    "write_todos": "insight-orchestrator",
    "scoped_retrieve": "topic-researcher",
    "think_tool": "topic-researcher",
    "save_research_artifacts": "topic-researcher",
    "load_research_package": "report-editor",
    "save_final_report": "report-editor",
}


@dataclass(frozen=True, slots=True)
class _ActiveSpan:
    started: float
    actor: str
    tool_name: str | None
    retry_count: int
    researcher_task: bool = False


class InsightExecutionRecorder(BaseCallbackHandler):
    """Count real callback spans and retain only allowlisted operational facts."""

    def __init__(
        self,
        context: InsightRuntimeContext,
        settings: Settings,
        *,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] = perf_counter,
    ) -> None:
        self._context = context
        self._settings = settings
        self._clock = clock or (lambda: datetime.now(UTC))
        self._monotonic = monotonic
        self._started_at = self._clock()
        self._started = self._monotonic()
        self._lock = Lock()
        self._model_spans: dict[UUID, _ActiveSpan] = {}
        self._tool_spans: dict[UUID, _ActiveSpan] = {}
        self._pending_model_retries = 0
        self._pending_tool_retries: dict[str, int] = {}
        self._events: list[InsightExecutionEvent] = []
        self._model_calls = 0
        self._model_failures = 0
        self._tool_calls = 0
        self._tool_failures = 0
        self._retrieval_calls = 0
        self._retry_count = 0
        self._timeout_count = 0
        self._researcher_calls = 0
        self._active_researchers = 0
        self._max_concurrent_researchers = 0
        self._rerank_candidates_total = 0
        self._max_rerank_candidates = 0
        self._retrieval_latency_ms = 0
        self._reranker_latency_ms = 0
        self._input_tokens = 0
        self._output_tokens = 0
        self._total_tokens = 0
        self._tool_names: set[str] = set()
        self._source_ids: set[str] = set()
        self._evidence_ids: set[str] = set()

    @override
    def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[BaseMessage]],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        """Start one chat-model span without retaining messages."""
        del serialized, messages, parent_run_id, kwargs
        self._start_model(run_id, tags=tags, metadata=metadata)
        return None

    @override
    def on_llm_start(
        self,
        serialized: dict[str, Any],
        prompts: list[str],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        """Start one non-chat model span without retaining prompts."""
        del serialized, prompts, parent_run_id, kwargs
        self._start_model(run_id, tags=tags, metadata=metadata)
        return None

    @override
    def on_llm_end(
        self,
        response: LLMResult,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        **kwargs: Any,
    ) -> Any:
        """Finish a successful model span and aggregate provider usage metadata."""
        del parent_run_id, tags, kwargs
        usage = self._usage(response)
        with self._lock:
            self._input_tokens += usage[0]
            self._output_tokens += usage[1]
            self._total_tokens += usage[2]
            self._finish_span(self._model_spans, run_id, status="succeeded")
        return None

    @override
    def on_llm_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        **kwargs: Any,
    ) -> Any:
        """Finish a failed model span with a class-derived safe failure code."""
        del parent_run_id, tags, kwargs
        with self._lock:
            self._model_failures += 1
            self._pending_model_retries += 1
            self._finish_span(
                self._model_spans,
                run_id,
                status="failed",
                failure_code=self._failure_code(error),
            )
        return None

    @override
    def on_tool_start(
        self,
        serialized: dict[str, Any],
        input_str: str,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        inputs: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        """Start one named tool span while discarding all tool inputs."""
        del parent_run_id, tags, metadata
        raw_name = serialized.get("name") or kwargs.get("name") or "unknown_tool"
        tool_name = str(raw_name)[:500]
        parsed_inputs = inputs or self._parse_mapping(input_str)
        subagent = parsed_inputs.get("subagent_type")
        actor = _TOOL_ACTORS.get(tool_name, "insight-orchestrator")
        if tool_name == "task" and subagent in {"topic-researcher", "report-editor"}:
            actor = str(subagent)
        researcher_task = tool_name == "task" and subagent == "topic-researcher"
        with self._lock:
            if run_id in self._tool_spans:
                return None
            retry_count = min(self._pending_tool_retries.get(tool_name, 0), 1)
            if retry_count:
                self._pending_tool_retries[tool_name] -= 1
                self._retry_count += retry_count
            self._tool_calls += 1
            self._tool_names.add(tool_name)
            if tool_name == "scoped_retrieve":
                self._retrieval_calls += 1
            if researcher_task:
                self._researcher_calls += 1
                self._active_researchers += 1
                self._max_concurrent_researchers = max(
                    self._max_concurrent_researchers,
                    self._active_researchers,
                )
            self._tool_spans[run_id] = _ActiveSpan(
                started=self._monotonic(),
                actor=actor,
                tool_name=tool_name,
                retry_count=retry_count,
                researcher_task=researcher_task,
            )
        return None

    @override
    def on_tool_end(
        self,
        output: Any,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> Any:
        """Finish a successful tool span and extract only safe retrieval counters."""
        del parent_run_id, kwargs
        payload = self._tool_output_mapping(output)
        with self._lock:
            span = self._tool_spans.get(run_id)
            source_ids: tuple[str, ...] = ()
            evidence_ids: tuple[str, ...] = ()
            if span is not None and span.tool_name == "scoped_retrieve":
                source_ids, evidence_ids = self._capture_retrieval(payload)
            self._finish_span(
                self._tool_spans,
                run_id,
                status="succeeded",
                source_ids=source_ids,
                evidence_ids=evidence_ids,
            )
        return None

    @override
    def on_tool_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> Any:
        """Finish a failed tool span and make one subsequent retry observable."""
        del parent_run_id, kwargs
        with self._lock:
            span = self._tool_spans.get(run_id)
            if span is not None and span.tool_name is not None:
                self._pending_tool_retries[span.tool_name] = (
                    self._pending_tool_retries.get(span.tool_name, 0) + 1
                )
            self._tool_failures += 1
            self._finish_span(
                self._tool_spans,
                run_id,
                status="failed",
                failure_code=self._failure_code(error),
            )
        return None

    def mark_timeout(self) -> None:
        """Record one run timeout and close any callback spans left in flight."""
        with self._lock:
            self._timeout_count += 1
            self._model_failures += len(self._model_spans)
            self._tool_failures += len(self._tool_spans)
            for run_id in tuple(self._model_spans):
                self._finish_span(
                    self._model_spans,
                    run_id,
                    status="failed",
                    failure_code="TIMEOUT",
                )
            for run_id in tuple(self._tool_spans):
                self._finish_span(
                    self._tool_spans,
                    run_id,
                    status="failed",
                    failure_code="TIMEOUT",
                )

    def snapshot(
        self,
        *,
        status: TerminalInsightRunStatus,
        completed_at: datetime,
        topic_count: int,
        failure_code: str | None,
        source_ids: Sequence[str] = (),
        evidence_ids: Sequence[str] = (),
    ) -> tuple[InsightExecutionMetrics, tuple[InsightExecutionEvent, ...]]:
        """Freeze the measured aggregate and safe event list for persistence."""
        with self._lock:
            all_sources = tuple(sorted(self._source_ids | set(source_ids)))
            all_evidence = tuple(sorted(self._evidence_ids | set(evidence_ids)))
            metrics = InsightExecutionMetrics(
                run_id=self._context.run_id,
                engagement_id=self._context.access.engagement_id,
                thread_id=self._context.access.thread_id or "missing-thread",
                started_at=self._started_at,
                completed_at=completed_at,
                status=status,
                duration_ms=self._duration(self._started),
                topic_count=topic_count,
                researcher_calls=self._researcher_calls,
                max_concurrent_researchers=self._max_concurrent_researchers,
                model_calls=self._model_calls,
                model_failures=self._model_failures,
                tool_calls=self._tool_calls,
                tool_failures=self._tool_failures,
                retrieval_calls=self._retrieval_calls,
                retry_count=self._retry_count,
                timeout_count=self._timeout_count,
                rerank_candidates_total=self._rerank_candidates_total,
                max_rerank_candidates_per_call=self._max_rerank_candidates,
                retrieval_latency_ms=self._retrieval_latency_ms,
                reranker_latency_ms=self._reranker_latency_ms,
                input_tokens=self._input_tokens,
                output_tokens=self._output_tokens,
                total_tokens=self._total_tokens,
                configured_topic_limit=self._settings.max_research_topics,
                configured_parallel_researcher_limit=self._settings.max_parallel_researchers,
                configured_model_call_limit=self._settings.model_run_call_limit,
                configured_tool_call_limit=self._settings.tool_run_call_limit,
                configured_retrieval_calls_per_researcher_limit=(
                    self._settings.retrieval_calls_per_researcher_limit
                ),
                configured_rerank_candidate_limit=self._settings.max_rerank_candidates,
                configured_provider_timeout_seconds=self._settings.provider_timeout_seconds,
                configured_run_timeout_seconds=self._settings.insight_run_timeout_seconds,
                source_ids=all_sources,
                evidence_ids=all_evidence,
                tool_names=tuple(sorted(self._tool_names)),
                failure_code=failure_code,
                correlation_id=self._context.access.correlation_id,
            )
            return metrics, tuple(self._events)

    def _start_model(
        self,
        run_id: UUID,
        *,
        tags: Sequence[str] | None,
        metadata: Mapping[str, Any] | None,
    ) -> None:
        with self._lock:
            if run_id in self._model_spans:
                return
            retry_count = min(self._pending_model_retries, 1)
            if retry_count:
                self._pending_model_retries -= 1
                self._retry_count += retry_count
            self._model_calls += 1
            self._model_spans[run_id] = _ActiveSpan(
                started=self._monotonic(),
                actor=self._model_actor(tags, metadata),
                tool_name=None,
                retry_count=retry_count,
            )

    def _finish_span(
        self,
        spans: dict[UUID, _ActiveSpan],
        run_id: UUID,
        *,
        status: Literal["succeeded", "failed"],
        failure_code: str | None = None,
        source_ids: Sequence[str] = (),
        evidence_ids: Sequence[str] = (),
    ) -> None:
        span = spans.pop(run_id, None)
        if span is None:
            return
        if span.researcher_task:
            self._active_researchers = max(0, self._active_researchers - 1)
        self._source_ids.update(source_ids)
        self._evidence_ids.update(evidence_ids)
        sequence = len(self._events)
        self._events.append(
            InsightExecutionEvent(
                event_id=stable_id(
                    "insight-execution-event",
                    self._context.run_id,
                    str(sequence),
                    span.tool_name or "model",
                ),
                occurred_at=self._clock(),
                run_id=self._context.run_id,
                engagement_id=self._context.access.engagement_id,
                thread_id=self._context.access.thread_id or "missing-thread",
                actor=span.actor,
                operation_type="tool" if span.tool_name is not None else "model",
                tool_name=span.tool_name,
                status=status,
                duration_ms=self._duration(span.started),
                source_ids=tuple(sorted(set(source_ids))),
                evidence_ids=tuple(sorted(set(evidence_ids))),
                retry_count=span.retry_count,
                failure_code=failure_code,
                correlation_id=self._context.access.correlation_id,
            )
        )

    def _capture_retrieval(
        self,
        payload: Mapping[str, Any],
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        raw_metrics = payload.get("retrieval_metrics")
        if isinstance(raw_metrics, dict):
            candidates = self._non_negative_int(raw_metrics.get("rerank_candidates"))
            self._rerank_candidates_total += candidates
            self._max_rerank_candidates = max(self._max_rerank_candidates, candidates)
            self._retrieval_latency_ms += self._non_negative_int(
                raw_metrics.get("total_latency_ms")
            )
            self._reranker_latency_ms += self._non_negative_int(
                raw_metrics.get("reranker_latency_ms")
            )
        raw_results = payload.get("results")
        if not isinstance(raw_results, list):
            return (), ()
        sources: set[str] = set()
        evidence: set[str] = set()
        for item in raw_results:
            if not isinstance(item, dict):
                continue
            source_id = item.get("source_id")
            evidence_id = item.get("evidence_id")
            if isinstance(source_id, str):
                sources.add(source_id)
            if isinstance(evidence_id, str):
                evidence.add(evidence_id)
        return tuple(sorted(sources)), tuple(sorted(evidence))

    def _duration(self, started: float) -> int:
        return max(0, round((self._monotonic() - started) * 1_000))

    @staticmethod
    def _parse_mapping(value: str) -> dict[str, Any]:
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    @classmethod
    def _tool_output_mapping(cls, output: Any) -> dict[str, Any]:
        content = getattr(output, "content", output)
        return cls._parse_mapping(content) if isinstance(content, str) else {}

    @staticmethod
    def _model_actor(
        tags: Sequence[str] | None,
        metadata: Mapping[str, Any] | None,
    ) -> str:
        safe_context = " ".join(
            [*(tags or ()), *(str(value) for value in (metadata or {}).values())]
        ).casefold()
        if "topic-researcher" in safe_context or "topic_researcher" in safe_context:
            return "topic-researcher"
        if "report-editor" in safe_context or "report_editor" in safe_context:
            return "report-editor"
        return "insight-orchestrator"

    @staticmethod
    def _failure_code(error: BaseException) -> str:
        name = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", type(error).__name__).upper()
        safe = re.sub(r"[^A-Z0-9_]", "_", name).strip("_")
        if not safe or not safe[0].isalpha():
            safe = f"EXECUTION_{safe}" if safe else "EXECUTION_FAILED"
        return safe[:64]

    @staticmethod
    def _non_negative_int(value: Any) -> int:
        if not isinstance(value, int | float) or isinstance(value, bool):
            return 0
        return max(0, round(value))

    @classmethod
    def _usage(cls, response: LLMResult) -> tuple[int, int, int]:
        input_tokens = 0
        output_tokens = 0
        total_tokens = 0
        found_message_usage = False
        for generations in response.generations:
            for generation in generations:
                message = getattr(generation, "message", None)
                usage = getattr(message, "usage_metadata", None)
                if not isinstance(usage, dict):
                    continue
                found_message_usage = True
                input_tokens += cls._non_negative_int(usage.get("input_tokens"))
                output_tokens += cls._non_negative_int(usage.get("output_tokens"))
                total_tokens += cls._non_negative_int(usage.get("total_tokens"))
        if not found_message_usage and isinstance(response.llm_output, dict):
            raw_usage = response.llm_output.get("token_usage") or response.llm_output.get(
                "usage_metadata"
            )
            if isinstance(raw_usage, dict):
                input_tokens = cls._non_negative_int(
                    raw_usage.get("input_tokens", raw_usage.get("prompt_tokens"))
                )
                output_tokens = cls._non_negative_int(
                    raw_usage.get("output_tokens", raw_usage.get("completion_tokens"))
                )
                total_tokens = cls._non_negative_int(raw_usage.get("total_tokens"))
        return input_tokens, output_tokens, max(total_tokens, input_tokens + output_tokens)
