"""Server-measured insight metrics and no-private-content callback tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import uuid4

from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult

from stakeholder_intelligence_agent.insight.observability import InsightExecutionRecorder
from tests.helpers import insight_context

if TYPE_CHECKING:
    from stakeholder_intelligence_agent.config import Settings


def test_recorder_measures_retries_concurrency_usage_and_safe_retrieval_fields(
    settings: Settings,
) -> None:
    context = insight_context(run_id="run-observability", thread_id="thread-observability")
    recorder = InsightExecutionRecorder(context, settings)

    failed_model = uuid4()
    recorder.on_llm_start(
        {},
        ["SECRET PROMPT MUST NOT BE PERSISTED"],
        run_id=failed_model,
        metadata={"langgraph_node": "topic-researcher"},
    )
    recorder.on_llm_error(RuntimeError("sensitive provider detail"), run_id=failed_model)
    retried_model = uuid4()
    recorder.on_chat_model_start(
        {},
        [[AIMessage(content="SECRET MESSAGE MUST NOT BE PERSISTED")]],
        run_id=retried_model,
        metadata={"langgraph_node": "report-editor"},
    )
    recorder.on_llm_end(
        LLMResult(
            generations=[
                [
                    ChatGeneration(
                        message=AIMessage(
                            content="Synthetic response.",
                            usage_metadata={
                                "input_tokens": 11,
                                "output_tokens": 7,
                                "total_tokens": 18,
                            },
                        )
                    )
                ]
            ]
        ),
        run_id=retried_model,
    )

    failed_retrieval = uuid4()
    recorder.on_tool_start(
        {"name": "scoped_retrieve"},
        '{"query":"SECRET TOOL INPUT"}',
        run_id=failed_retrieval,
    )
    recorder.on_tool_error(RuntimeError("sensitive retrieval detail"), run_id=failed_retrieval)
    retried_retrieval = uuid4()
    recorder.on_tool_start(
        {"name": "scoped_retrieve"},
        '{"query":"SECRET TOOL RETRY"}',
        run_id=retried_retrieval,
    )
    recorder.on_tool_end(
        json.dumps(
            {
                "retrieval_metrics": {
                    "rerank_candidates": 4,
                    "total_latency_ms": 12.4,
                    "reranker_latency_ms": 3.2,
                },
                "results": [
                    {
                        "source_id": "source-safe",
                        "evidence_id": "evidence-safe",
                        "original_excerpt": "SECRET ORIGINAL CONTENT",
                    }
                ],
            }
        ),
        run_id=retried_retrieval,
    )

    researcher_tasks = [uuid4() for _ in range(3)]
    for task_run_id in researcher_tasks:
        recorder.on_tool_start(
            {"name": "task"},
            "SECRET TASK DESCRIPTION",
            run_id=task_run_id,
            inputs={"subagent_type": "topic-researcher"},
        )
    for task_run_id in researcher_tasks:
        recorder.on_tool_end("completed", run_id=task_run_id)

    metrics, events = recorder.snapshot(
        status="failed",
        completed_at=datetime.now(UTC),
        topic_count=3,
        failure_code="SYNTHETIC_FAILURE",
    )

    assert metrics.model_calls == 2
    assert metrics.model_failures == 1
    assert metrics.tool_calls == 5
    assert metrics.tool_failures == 1
    assert metrics.retrieval_calls == 2
    assert metrics.retry_count == 2
    assert metrics.researcher_calls == 3
    assert metrics.max_concurrent_researchers == 3
    assert metrics.rerank_candidates_total == 4
    assert metrics.max_rerank_candidates_per_call == 4
    assert metrics.retrieval_latency_ms == 12
    assert metrics.reranker_latency_ms == 3
    assert (metrics.input_tokens, metrics.output_tokens, metrics.total_tokens) == (11, 7, 18)
    assert metrics.source_ids == ("source-safe",)
    assert metrics.evidence_ids == ("evidence-safe",)
    assert len(events) == metrics.model_calls + metrics.tool_calls
    assert {event.actor for event in events} >= {
        "topic-researcher",
        "report-editor",
    }
    assert any(event.status == "failed" for event in events)
    persisted_projection = json.dumps(
        {
            "metrics": metrics.model_dump(mode="json"),
            "events": [event.model_dump(mode="json") for event in events],
        },
        sort_keys=True,
    )
    assert "SECRET" not in persisted_projection
    assert "sensitive" not in persisted_projection


def test_recorder_marks_in_flight_spans_as_timed_out(settings: Settings) -> None:
    context = insight_context(run_id="run-timeout-metrics", thread_id="thread-timeout")
    recorder = InsightExecutionRecorder(context, settings)
    model_run_id = uuid4()
    recorder.on_chat_model_start({}, [[AIMessage(content="private")]], run_id=model_run_id)

    recorder.mark_timeout()
    metrics, events = recorder.snapshot(
        status="failed",
        completed_at=datetime.now(UTC),
        topic_count=0,
        failure_code="INSIGHT_TIMEOUT",
    )

    assert metrics.timeout_count == 1
    assert metrics.model_calls == metrics.model_failures == 1
    assert len(events) == 1
    assert events[0].failure_code == "TIMEOUT"
