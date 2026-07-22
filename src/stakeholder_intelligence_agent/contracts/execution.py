"""Canonical insight-run, local-audit, and safe progress-event contracts."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import Field, StringConstraints, field_validator, model_validator

from stakeholder_intelligence_agent.contracts.common import (
    CanonicalModel,
    FailureCode,
    NonEmptyText,
    OpaqueId,
    ShortText,
    UtcDatetime,
)

InsightRunStatus = Literal[
    "queued",
    "planning",
    "researching",
    "editing",
    "validating",
    "complete",
    "partial",
    "insufficient_evidence",
    "failed",
]
TerminalInsightRunStatus = Literal[
    "complete",
    "partial",
    "insufficient_evidence",
    "failed",
]
AuditStatus = Literal["started", "succeeded", "failed", "retried", "denied"]
StableAction = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=3,
        max_length=100,
        pattern=r"^[a-z][a-z0-9_]*$",
    ),
]


class InsightRun(CanonicalModel):
    """Operational run state kept distinct from report evidence status."""

    run_id: OpaqueId
    engagement_id: OpaqueId
    thread_id: OpaqueId
    status: InsightRunStatus
    requested_question: NonEmptyText
    plan_id: OpaqueId | None = None
    report_id: OpaqueId | None = None
    failure_code: FailureCode | None = None
    failure_message: ShortText | None = None
    started_at: UtcDatetime
    completed_at: UtcDatetime | None = None

    @model_validator(mode="after")
    def validate_run_state(self) -> Self:
        if self.completed_at is not None and self.completed_at < self.started_at:
            raise ValueError("completed_at must not precede started_at.")

        terminal_report_states = {"complete", "partial", "insufficient_evidence"}
        if (
            self.status in {"researching", "editing", "validating", *terminal_report_states}
            and self.plan_id is None
        ):
            raise ValueError("Research and later run states require plan_id.")
        if self.status in terminal_report_states:
            if self.report_id is None or self.completed_at is None:
                raise ValueError("Terminal report states require report_id and completed_at.")
        elif self.report_id is not None:
            raise ValueError("Only terminal report states may carry report_id.")

        has_failure = self.failure_code is not None or self.failure_message is not None
        if self.status == "failed":
            if (
                self.failure_code is None
                or self.failure_message is None
                or self.completed_at is None
            ):
                raise ValueError(
                    "Failed insight runs require safe failure detail and completed_at."
                )
        elif has_failure:
            raise ValueError("Only failed insight runs may carry failure detail.")

        if self.status not in {*terminal_report_states, "failed"} and self.completed_at is not None:
            raise ValueError("Non-terminal insight runs cannot have completed_at.")
        return self


class OperationalAuditEvent(CanonicalModel):
    """Local, structured operational evidence without private reasoning."""

    event_id: OpaqueId
    occurred_at: UtcDatetime
    run_id: OpaqueId | None = None
    engagement_id: OpaqueId
    thread_id: OpaqueId | None = None
    actor: ShortText
    action: StableAction
    status: AuditStatus
    duration_ms: int | None = Field(default=None, ge=0)
    source_ids: tuple[OpaqueId, ...] = ()
    evidence_ids: tuple[OpaqueId, ...] = ()
    retry_count: int | None = Field(default=None, ge=0)
    failure_code: FailureCode | None = None
    correlation_id: OpaqueId

    @model_validator(mode="after")
    def validate_event_state(self) -> Self:
        if self.status in {"failed", "denied"}:
            if self.failure_code is None:
                raise ValueError("Failed or denied audit events require failure_code.")
        elif self.failure_code is not None:
            raise ValueError("Only failed or denied audit events may carry failure_code.")
        if self.status == "retried" and self.retry_count is None:
            raise ValueError("Retried audit events require retry_count.")
        return self


class InsightExecutionEvent(CanonicalModel):
    """One measured model or tool span without prompts, inputs, or private reasoning."""

    event_id: OpaqueId
    occurred_at: UtcDatetime
    run_id: OpaqueId
    engagement_id: OpaqueId
    thread_id: OpaqueId
    actor: ShortText
    operation_type: Literal["model", "tool"]
    tool_name: ShortText | None = None
    status: Literal["succeeded", "failed"]
    duration_ms: int = Field(ge=0)
    source_ids: tuple[OpaqueId, ...] = ()
    evidence_ids: tuple[OpaqueId, ...] = ()
    retry_count: int = Field(default=0, ge=0)
    failure_code: FailureCode | None = None
    correlation_id: OpaqueId

    @model_validator(mode="after")
    def validate_execution_event(self) -> Self:
        """Keep tool identity and safe failure state structurally consistent."""
        if (self.operation_type == "tool") != (self.tool_name is not None):
            raise ValueError("Only tool events may carry tool_name.")
        if (self.status == "failed") != (self.failure_code is not None):
            raise ValueError("Only failed execution events require failure_code.")
        return self


class InsightExecutionMetrics(CanonicalModel):
    """Server-measured bounded execution totals, separate from compact report metadata."""

    run_id: OpaqueId
    engagement_id: OpaqueId
    thread_id: OpaqueId
    started_at: UtcDatetime
    completed_at: UtcDatetime
    status: TerminalInsightRunStatus
    duration_ms: int = Field(ge=0)
    topic_count: int = Field(ge=0, le=5)
    researcher_calls: int = Field(ge=0)
    max_concurrent_researchers: int = Field(ge=0)
    model_calls: int = Field(ge=0)
    model_failures: int = Field(ge=0)
    tool_calls: int = Field(ge=0)
    tool_failures: int = Field(ge=0)
    retrieval_calls: int = Field(ge=0)
    retry_count: int = Field(ge=0)
    timeout_count: int = Field(ge=0)
    rerank_candidates_total: int = Field(ge=0)
    max_rerank_candidates_per_call: int = Field(ge=0)
    retrieval_latency_ms: int = Field(ge=0)
    reranker_latency_ms: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    configured_topic_limit: int = Field(ge=1, le=5)
    configured_parallel_researcher_limit: int = Field(ge=1, le=3)
    configured_model_call_limit: int = Field(ge=1)
    configured_tool_call_limit: int = Field(ge=1)
    configured_retrieval_calls_per_researcher_limit: int = Field(ge=1)
    configured_rerank_candidate_limit: int = Field(ge=1)
    configured_provider_timeout_seconds: int = Field(ge=1)
    configured_run_timeout_seconds: int = Field(ge=1)
    source_ids: tuple[OpaqueId, ...] = ()
    evidence_ids: tuple[OpaqueId, ...] = ()
    tool_names: tuple[ShortText, ...] = ()
    failure_code: FailureCode | None = None
    correlation_id: OpaqueId

    @model_validator(mode="after")
    def validate_execution_metrics(self) -> Self:
        """Enforce timing, terminal failure, and configured runtime bounds."""
        if self.completed_at < self.started_at:
            raise ValueError("completed_at must not precede started_at.")
        if (self.status == "failed") != (self.failure_code is not None):
            raise ValueError("Only failed execution metrics require failure_code.")
        if self.topic_count > self.configured_topic_limit:
            raise ValueError("Measured topic count exceeds the configured bound.")
        if self.max_concurrent_researchers > self.configured_parallel_researcher_limit:
            raise ValueError("Measured researcher concurrency exceeds the configured bound.")
        if self.max_rerank_candidates_per_call > self.configured_rerank_candidate_limit:
            raise ValueError("Measured rerank candidates exceed the configured bound.")
        if self.model_failures > self.model_calls or self.tool_failures > self.tool_calls:
            raise ValueError("Failure counts cannot exceed measured call counts.")
        if self.retrieval_calls > self.tool_calls:
            raise ValueError("Retrieval calls must be a subset of tool calls.")
        return self


class SafeProgressEvent(CanonicalModel):
    """Allowlisted UI projection of operational facts only."""

    event_id: OpaqueId
    occurred_at: UtcDatetime
    engagement_id: OpaqueId
    run_id: OpaqueId | None = None
    thread_id: OpaqueId | None = None
    stage: Literal[
        "queued",
        "interview",
        "finalization",
        "ingestion",
        "planning",
        "researching",
        "editing",
        "validating",
        "report",
    ]
    status: Literal[
        "started",
        "in_progress",
        "succeeded",
        "failed",
        "retried",
        "denied",
        "complete",
        "partial",
        "insufficient_evidence",
    ]
    todo_id: OpaqueId | None = None
    todo_status: Literal["pending", "in_progress", "completed"] | None = None
    subagent: Literal["topic-researcher", "report-editor"] | None = None
    tool_name: ShortText | None = None
    artifact_name: ShortText | None = None
    source_ids: tuple[OpaqueId, ...] = ()
    evidence_ids: tuple[OpaqueId, ...] = ()
    duration_ms: int | None = Field(default=None, ge=0)
    retry_count: int | None = Field(default=None, ge=0)
    failure_code: FailureCode | None = None
    failure_message: ShortText | None = None
    correlation_id: OpaqueId

    @field_validator("artifact_name")
    @classmethod
    def require_virtual_artifact_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if "\\" in value or value.startswith("/") or ".." in value.split("/"):
            raise ValueError("Progress events may expose only safe virtual artifact names.")
        return value

    @model_validator(mode="after")
    def validate_progress_state(self) -> Self:
        if (self.todo_id is None) != (self.todo_status is None):
            raise ValueError("TODO identity and status must appear together.")
        has_failure = self.failure_code is not None or self.failure_message is not None
        if self.status in {"failed", "denied"}:
            if self.failure_code is None or self.failure_message is None:
                raise ValueError("Failed progress events require safe failure detail.")
        elif has_failure:
            raise ValueError("Only failed progress events may carry failure detail.")
        if self.status == "retried" and self.retry_count is None:
            raise ValueError("Retried progress events require retry_count.")
        return self
