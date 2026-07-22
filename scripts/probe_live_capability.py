"""Run one sanitized retained probe against a configured live Gemini capability."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import re
import time
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from langchain_core.messages import AIMessage, HumanMessage
from pydantic import SecretStr

from stakeholder_intelligence_agent.config import Settings
from stakeholder_intelligence_agent.contracts import (
    AccessContext,
    InsightReport,
    InsightRuntimeContext,
    InterviewRuntimeContext,
)
from stakeholder_intelligence_agent.contracts.retrieval import RetrievalFilter
from stakeholder_intelligence_agent.gemini_runtime import safe_gemini_quota_observation
from stakeholder_intelligence_agent.ingestion.adapters import GeminiBm25Vectorizer
from stakeholder_intelligence_agent.insight.graph import (
    InsightGraphDependencies,
    build_insight_graph,
)
from stakeholder_intelligence_agent.interview.graph import build_interview_graph
from stakeholder_intelligence_agent.models import safe_gemini_runtime_summary
from stakeholder_intelligence_agent.persistence.checkpointer import open_sqlite_checkpointer
from stakeholder_intelligence_agent.retrieval.filters import GeminiFilterExtractor
from stakeholder_intelligence_agent.retrieval.types import RetrievalResult, RetrievalTrace

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig

PROJECT_ROOT = Path(__file__).resolve().parents[1]
HTTP_STATUS_MIN = 100
HTTP_STATUS_MAX = 599
PROBE_TIMEOUT_SECONDS = 300
OUT_OF_SCOPE_OUTPUT = "Probe output must remain inside the project."
CAPABILITIES = ("embedding", "structured_output", "interview", "insight")


class ProbeValidationError(RuntimeError):
    """Represent a sanitized capability-contract failure."""


def _require(condition: bool) -> None:
    if not condition:
        raise ProbeValidationError


def _safe_exception_chain(error: BaseException) -> list[dict[str, Any]]:
    chain: list[dict[str, Any]] = []
    current: BaseException | None = error
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        item: dict[str, Any] = {
            "module": type(current).__module__,
            "type": type(current).__name__,
        }
        statuses: list[str | int] = []
        for attribute in ("status_code", "code"):
            value = getattr(current, attribute, None)
            if isinstance(value, int) and HTTP_STATUS_MIN <= value <= HTTP_STATUS_MAX:
                statuses.append(value)
            elif isinstance(value, Enum):
                statuses.append(value.name)
            elif re.fullmatch(r"[1-5][0-9]{2}", str(value)):
                statuses.append(int(str(value)))
        if statuses:
            item["statuses"] = statuses
        chain.append(item)
        current = current.__cause__ or current.__context__
    return chain


def _settings(runtime_root: Path) -> Settings:
    return Settings(
        environment="test",
        pm_bootstrap_token=SecretStr("retained-live-local-bootstrap-token-0001"),
        token_pepper=SecretStr("retained-live-local-token-pepper-000001"),
        data_root=runtime_root,
        domain_database=runtime_root / "domain.sqlite3",
        checkpoint_database=runtime_root / "checkpoints.sqlite3",
        originals_root=runtime_root / "originals",
        derived_root=runtime_root / "derived",
        agent_artifacts_root=runtime_root / "agent-artifacts",
        audit_root=runtime_root / "audit",
    )


def _pm_access(*, engagement_id: str, thread_id: str) -> AccessContext:
    issued = datetime.now(UTC)
    return AccessContext(
        principal_type="pm",
        principal_id="pm-live-probe",
        engagement_id=engagement_id,
        stakeholder_id=None,
        interview_session_id=None,
        thread_id=thread_id,
        permissions=frozenset({"insight:run"}),
        issued_at=issued,
        expires_at=issued + timedelta(hours=1),
        correlation_id="retained-live-insight-correlation",
    )


def _insight_context(
    *,
    engagement_id: str,
    thread_id: str,
    run_id: str,
    question: str,
) -> InsightRuntimeContext:
    return InsightRuntimeContext(
        access=_pm_access(engagement_id=engagement_id, thread_id=thread_id),
        run_id=run_id,
        question=question,
    )


def _stakeholder_access(*, engagement_id: str, thread_id: str) -> AccessContext:
    issued = datetime.now(UTC)
    stakeholder_id = "stakeholder-live-probe"
    return AccessContext(
        principal_type="stakeholder",
        principal_id=stakeholder_id,
        engagement_id=engagement_id,
        stakeholder_id=stakeholder_id,
        interview_session_id="interview-session-live-probe",
        thread_id=thread_id,
        permissions=frozenset(
            {
                "document:upload",
                "interview:finalize",
                "interview:participate",
                "source:read",
            }
        ),
        issued_at=issued,
        expires_at=issued + timedelta(hours=1),
        correlation_id="retained-live-interview-correlation",
    )


def _interview_context(*, engagement_id: str, thread_id: str) -> InterviewRuntimeContext:
    return InterviewRuntimeContext(
        access=_stakeholder_access(engagement_id=engagement_id, thread_id=thread_id),
        role="Operations manager",
        department="Operations",
    )


async def _probe_embedding(settings: Settings) -> dict[str, Any]:
    pair = await GeminiBm25Vectorizer(settings).vectorize_query(
        "Which current evidence identifies an operational risk owner?"
    )
    dense_finite = all(math.isfinite(value) for value in pair.dense)
    sparse_pair_complete = len(pair.sparse.indices) == len(pair.sparse.values)
    sparse_indices_ordered = pair.sparse.indices == tuple(sorted(pair.sparse.indices))
    _require(len(pair.dense) == settings.gemini_embedding_dimension)
    _require(dense_finite)
    _require(bool(pair.sparse.indices))
    _require(sparse_pair_complete)
    _require(sparse_indices_ordered)
    return {
        "dense_dimensions": len(pair.dense),
        "dense_values_finite": dense_finite,
        "sparse_entries": len(pair.sparse.indices),
        "sparse_pair_complete": sparse_pair_complete,
        "sparse_indices_ordered": sparse_indices_ordered,
    }


async def _probe_structured_output(settings: Settings) -> dict[str, Any]:
    extracted = await GeminiFilterExtractor(settings).extract(
        "Find Operations department documents about approval handoff risks."
    )
    payload = extracted.model_dump(mode="json")
    expected_fields = {
        "stakeholder_id",
        "role",
        "department",
        "doc_type",
        "source_type",
    }
    values_typed = all(value is None or isinstance(value, str) for value in payload.values())
    _require(set(payload) == expected_fields)
    _require("engagement_id" not in payload)
    _require(values_typed)
    return {
        "schema_fields": sorted(payload),
        "server_owned_engagement_scope_absent": "engagement_id" not in payload,
        "values_typed": values_typed,
        "non_null_field_count": sum(value is not None for value in payload.values()),
    }


@dataclass(slots=True)
class _EmptyScopedRetriever:
    calls: list[str] = field(default_factory=list)

    async def initialize(self) -> None:
        """Satisfy the production interview retriever boundary."""

    async def retrieve(self, access: AccessContext, query: str) -> RetrievalResult:
        self.calls.append(access.engagement_id)
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


async def _probe_interview(settings: Settings, runtime_root: Path) -> dict[str, Any]:
    context = _interview_context(
        engagement_id="engagement-a",
        thread_id="retained-live-interview-thread",
    )
    retriever = _EmptyScopedRetriever()
    config: RunnableConfig = {"configurable": {"thread_id": "retained-live-interview-thread"}}
    async with open_sqlite_checkpointer(runtime_root / "interview.sqlite3") as saver:
        graph = build_interview_graph(
            settings,
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

    messages = cast("list[Any]", snapshot.values["messages"])
    response_present = isinstance(messages[-1], AIMessage) and bool(messages[-1].text.strip())
    scope_preserved = bool(retriever.calls) and all(
        engagement_id == context.access.engagement_id for engagement_id in retriever.calls
    )
    _require(bool(updates))
    _require(checkpoint is not None)
    _require(scope_preserved)
    _require(response_present)
    return {
        "stream_update_count": len(updates),
        "checkpoint_present": checkpoint is not None,
        "scoped_retrieval_call_count": len(retriever.calls),
        "engagement_scope_preserved": scope_preserved,
        "assistant_response_present": response_present,
    }


async def _probe_insight(settings: Settings, runtime_root: Path) -> dict[str, Any]:
    context = _insight_context(
        engagement_id="engagement-a",
        thread_id="retained-live-insight-thread",
        run_id="retained-live-insight-run",
        question="Which operational risks and responsibilities are supported?",
    )
    config: RunnableConfig = {"configurable": {"thread_id": "retained-live-insight-thread"}}
    async with open_sqlite_checkpointer(runtime_root / "insight.sqlite3") as saver:
        graph = build_insight_graph(
            settings,
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
    planning_ordered = [call["name"] for call in tool_calls[:2]] == [
        "write_todos",
        "create_research_plan",
    ]
    task_types = [call["args"]["subagent_type"] for call in tool_calls if call["name"] == "task"]
    researcher_task_count = task_types.count("topic-researcher")
    editor_last = bool(task_types) and task_types[-1] == "report-editor"
    scope_root = (
        settings.agent_artifacts_root
        / context.access.engagement_id
        / "retained-live-insight-thread"
    )
    plan_present = (scope_root / "research_plan.md").is_file()
    findings_count = len(tuple((scope_root / "research").glob("*/findings.md")))
    report_path = scope_root / "report" / "insight_report.json"
    report = InsightReport.model_validate_json(report_path.read_text(encoding="utf-8"))
    report_scope_valid = (
        report.run_metadata.run_id == context.run_id
        and report.engagement_id == context.access.engagement_id
        and report.question == context.question
    )
    _require(planning_ordered)
    _require(researcher_task_count >= 1)
    _require(editor_last)
    _require(plan_present)
    _require(findings_count >= 1)
    _require(report_scope_valid)
    _require(report.status == "insufficient_evidence")
    _require(not report.evidence_ids)
    return {
        "planning_tools_ordered": planning_ordered,
        "researcher_task_count": researcher_task_count,
        "editor_task_last": editor_last,
        "research_plan_present": plan_present,
        "research_findings_count": findings_count,
        "report_schema_valid": True,
        "report_scope_valid": report_scope_valid,
        "report_status": report.status,
        "evidence_count": len(report.evidence_ids),
    }


async def _execute(capability: str, settings: Settings, runtime_root: Path) -> dict[str, Any]:
    async with asyncio.timeout(PROBE_TIMEOUT_SECONDS):
        if capability == "embedding":
            return await _probe_embedding(settings)
        if capability == "structured_output":
            return await _probe_structured_output(settings)
        if capability == "interview":
            return await _probe_interview(settings, runtime_root)
        if capability == "insight":
            return await _probe_insight(settings, runtime_root)
    raise ValueError(capability)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--capability", choices=CAPABILITIES, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    output = arguments.output.resolve()
    try:
        output.relative_to(PROJECT_ROOT)
    except ValueError as error:
        raise SystemExit(OUT_OF_SCOPE_OUTPUT) from error

    runtime_root = PROJECT_ROOT / ".cache" / "live-capability-probes" / output.stem
    runtime_root.mkdir(parents=True, exist_ok=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    logging.disable(logging.CRITICAL)
    runtime_summary: dict[str, object] | None = None
    try:
        with (
            Path(os.devnull).open("w", encoding="utf-8") as sink,
            redirect_stdout(sink),
            redirect_stderr(sink),
        ):
            settings = _settings(runtime_root)
            runtime_summary = safe_gemini_runtime_summary(settings)
            details = asyncio.run(_execute(arguments.capability, settings, runtime_root))
    except BaseException as error:  # noqa: BLE001
        quota_observation = safe_gemini_quota_observation(error)
        result: dict[str, Any] = {
            "capability": arguments.capability,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "exception_chain": _safe_exception_chain(error),
            "executed_at_utc": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "result": "FAIL",
        }
        if quota_observation is not None:
            result["quota_observation"] = quota_observation.as_dict()
        if runtime_summary is not None:
            result["runtime"] = runtime_summary
    else:
        result = {
            "capability": arguments.capability,
            "details": details,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "executed_at_utc": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "result": "PASS",
            "runtime": runtime_summary,
        }

    output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Live capability probe result: {result['result']}")
    print(f"Evidence: {output.relative_to(PROJECT_ROOT)}")
    return 0 if result["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
