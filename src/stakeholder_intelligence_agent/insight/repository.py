"""SQLite authority for PM insight-run states, trajectory facts, and reports."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from stakeholder_intelligence_agent.contracts import (
    InsightExecutionEvent,
    InsightExecutionMetrics,
    InsightReport,
    InsightRun,
)
from stakeholder_intelligence_agent.contracts.lifecycle import validate_insight_run_transition
from stakeholder_intelligence_agent.errors import (
    AccessDeniedError,
    DomainConflictError,
    DomainPersistenceError,
)
from stakeholder_intelligence_agent.ingestion.identity import stable_id

if TYPE_CHECKING:
    from collections.abc import Sequence

    import aiosqlite

    from stakeholder_intelligence_agent.contracts import InsightRuntimeContext
    from stakeholder_intelligence_agent.persistence import DomainDatabase

_TERMINAL = frozenset({"complete", "partial", "insufficient_evidence", "failed"})


class InsightRunRepository:
    """Persist an inspectable run lifecycle without storing private model reasoning."""

    def __init__(self, database: DomainDatabase) -> None:
        self._database = database

    async def initialize(self) -> None:
        """Apply the run/report migration idempotently."""
        await self._database.initialize()

    async def start(self, context: InsightRuntimeContext, *, now: datetime) -> InsightRun:
        """Create one queued run or reuse the same active PM question atomically."""
        self._require_clock(now)
        async with self._database.transaction() as connection:
            await self._resolve_access(connection, context, now)
            row = await self._fetchone(
                connection,
                "SELECT * FROM insight_runs WHERE run_id = ?",
                (context.run_id,),
            )
            if row is not None:
                run = self._run(row)
                expected = (
                    context.access.engagement_id,
                    context.access.thread_id,
                    context.question,
                    context.access.principal_id,
                )
                actual = (
                    run.engagement_id,
                    run.thread_id,
                    run.requested_question,
                    row["requested_by_pm_access_id"],
                )
                if actual != expected:
                    raise DomainConflictError
                return run
            row = await self._fetchone(
                connection,
                """
                SELECT * FROM insight_runs
                WHERE engagement_id = ? AND requested_by_pm_access_id = ?
                    AND requested_question = ?
                    AND status IN ('queued', 'planning', 'researching', 'editing', 'validating')
                ORDER BY started_at DESC
                LIMIT 1
                """,
                (
                    context.access.engagement_id,
                    context.access.principal_id,
                    context.question,
                ),
            )
            if row is not None:
                return self._run(row)
            thread_id = context.access.thread_id
            if thread_id is None:
                raise AccessDeniedError
            run = InsightRun(
                run_id=context.run_id,
                engagement_id=context.access.engagement_id,
                thread_id=thread_id,
                status="queued",
                requested_question=context.question,
                started_at=now,
            )
            await connection.execute(
                """
                INSERT INTO insight_runs(
                    run_id, engagement_id, thread_id, requested_by_pm_access_id,
                    status, requested_question, plan_id, report_id, failure_code,
                    failure_message, started_at, completed_at
                ) VALUES (?, ?, ?, ?, 'queued', ?, NULL, NULL, NULL, NULL, ?, NULL)
                """,
                (
                    run.run_id,
                    run.engagement_id,
                    run.thread_id,
                    context.access.principal_id,
                    run.requested_question,
                    self._time(run.started_at),
                ),
            )
            await self._append_event(
                connection,
                context,
                actor="insight_service",
                action="run_queued",
                from_status=None,
                to_status="queued",
                now=now,
            )
            return run

    async def transition(
        self,
        context: InsightRuntimeContext,
        status: str,
        *,
        now: datetime,
        plan_id: str | None = None,
        actor: str = "insight_service",
    ) -> InsightRun:
        """Apply one canonical non-terminal state transition idempotently."""
        self._require_clock(now)
        async with self._database.transaction() as connection:
            await self._resolve_access(connection, context, now)
            row = await self._require_run(connection, context)
            previous = self._run(row)
            if previous.status == status:
                if plan_id is not None and previous.plan_id != plan_id:
                    raise DomainConflictError
                return previous
            update: dict[str, object] = {"status": status}
            if plan_id is not None:
                update["plan_id"] = plan_id
            proposed = InsightRun.model_validate(previous.model_copy(update=update).model_dump())
            validate_insight_run_transition(previous, proposed)
            await connection.execute(
                """
                UPDATE insight_runs SET status = ?, plan_id = ?
                WHERE run_id = ? AND engagement_id = ? AND status = ?
                """,
                (
                    proposed.status,
                    proposed.plan_id,
                    proposed.run_id,
                    proposed.engagement_id,
                    previous.status,
                ),
            )
            await self._append_event(
                connection,
                context,
                actor=actor,
                action=f"run_{status}",
                from_status=previous.status,
                to_status=status,
                now=now,
            )
            return proposed

    async def complete(
        self,
        context: InsightRuntimeContext,
        report: InsightReport,
        *,
        virtual_path: str,
        content_hash: str,
        now: datetime,
    ) -> InsightRun:
        """Register the immutable validated report and terminal run state atomically."""
        self._require_clock(now)
        async with self._database.transaction() as connection:
            await self._resolve_access(connection, context, now)
            row = await self._require_run(connection, context)
            previous = self._run(row)
            if previous.status in {"complete", "partial", "insufficient_evidence"}:
                if previous.report_id != report.report_id or previous.status != report.status:
                    raise DomainConflictError
                return previous
            proposed = InsightRun.model_validate(
                previous.model_copy(
                    update={
                        "status": report.status,
                        "report_id": report.report_id,
                        "completed_at": now,
                    }
                ).model_dump()
            )
            validate_insight_run_transition(previous, proposed)
            if report.engagement_id != context.access.engagement_id:
                raise AccessDeniedError
            await connection.execute(
                """
                INSERT INTO insight_report_records(
                    report_id, run_id, engagement_id, status, virtual_path,
                    content_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    report.report_id,
                    context.run_id,
                    context.access.engagement_id,
                    report.status,
                    virtual_path,
                    content_hash,
                    self._time(now),
                ),
            )
            await connection.execute(
                """
                UPDATE insight_runs
                SET status = ?, report_id = ?, completed_at = ?
                WHERE run_id = ? AND engagement_id = ? AND status = 'validating'
                """,
                (
                    proposed.status,
                    proposed.report_id,
                    self._time(now),
                    proposed.run_id,
                    proposed.engagement_id,
                ),
            )
            await self._append_event(
                connection,
                context,
                actor="report-editor",
                action="report_validated",
                from_status=previous.status,
                to_status=proposed.status,
                now=now,
                evidence_ids=report.evidence_ids,
                artifact_name=virtual_path.removeprefix("/"),
            )
            return proposed

    async def fail(
        self,
        context: InsightRuntimeContext,
        *,
        failure_code: str,
        failure_message: str,
        now: datetime,
    ) -> InsightRun:
        """Expose a safe terminal failure without overwriting an existing report."""
        self._require_clock(now)
        async with self._database.transaction() as connection:
            await self._resolve_access(connection, context, now)
            row = await self._require_run(connection, context)
            previous = self._run(row)
            if previous.status in _TERMINAL:
                return previous
            proposed = InsightRun.model_validate(
                previous.model_copy(
                    update={
                        "status": "failed",
                        "failure_code": failure_code,
                        "failure_message": failure_message,
                        "completed_at": now,
                    }
                ).model_dump()
            )
            validate_insight_run_transition(previous, proposed)
            await connection.execute(
                """
                UPDATE insight_runs
                SET status = 'failed', failure_code = ?, failure_message = ?,
                    completed_at = ?
                WHERE run_id = ? AND engagement_id = ?
                    AND status NOT IN ('complete', 'partial', 'insufficient_evidence', 'failed')
                """,
                (
                    failure_code,
                    failure_message,
                    self._time(now),
                    context.run_id,
                    context.access.engagement_id,
                ),
            )
            await self._append_event(
                connection,
                context,
                actor="insight_service",
                action="run_failed",
                from_status=previous.status,
                to_status="failed",
                now=now,
                failure_code=failure_code,
            )
            return proposed

    async def record_activity(
        self,
        context: InsightRuntimeContext,
        *,
        actor: str,
        action: str,
        now: datetime,
        topic_id: str | None = None,
        source_ids: Sequence[str] = (),
        evidence_ids: Sequence[str] = (),
        artifact_name: str | None = None,
    ) -> None:
        """Append an allowlisted trajectory fact without model prompts or reasoning."""
        async with self._database.transaction() as connection:
            await self._resolve_access(connection, context, now)
            await self._require_run(connection, context)
            await self._append_event(
                connection,
                context,
                actor=actor,
                action=action,
                from_status=None,
                to_status=None,
                now=now,
                topic_id=topic_id,
                source_ids=source_ids,
                evidence_ids=evidence_ids,
                artifact_name=artifact_name,
            )

    async def load(self, context: InsightRuntimeContext, *, now: datetime) -> InsightRun:
        """Load a run only after a fresh PM session and scope check."""
        async with self._database.connection() as connection:
            await self._resolve_access(connection, context, now)
            return self._run(await self._require_run(connection, context))

    async def events(
        self,
        context: InsightRuntimeContext,
        *,
        now: datetime,
    ) -> tuple[dict[str, object], ...]:
        """Return ordered safe trajectory rows for verification and route projection."""
        async with self._database.connection() as connection:
            await self._resolve_access(connection, context, now)
            await self._require_run(connection, context)
            cursor = await connection.execute(
                """
                SELECT * FROM insight_run_events
                WHERE run_id = ? AND engagement_id = ?
                ORDER BY occurred_at, event_id
                """,
                (context.run_id, context.access.engagement_id),
            )
            return tuple(dict(row) for row in await cursor.fetchall())

    async def record_execution(
        self,
        context: InsightRuntimeContext,
        metrics: InsightExecutionMetrics,
        events: Sequence[InsightExecutionEvent],
        *,
        now: datetime,
    ) -> InsightExecutionMetrics:
        """Atomically persist immutable server measurements and a canonical audit summary."""
        self._require_clock(now)
        expected_scope = (
            context.run_id,
            context.access.engagement_id,
            context.access.thread_id,
            context.access.correlation_id,
        )
        actual_scope = (
            metrics.run_id,
            metrics.engagement_id,
            metrics.thread_id,
            metrics.correlation_id,
        )
        if actual_scope != expected_scope:
            raise AccessDeniedError
        async with self._database.transaction() as connection:
            await self._resolve_access(connection, context, now)
            run = self._run(await self._require_run(connection, context))
            if run.status != metrics.status or run.status not in _TERMINAL:
                raise DomainConflictError
            existing = await self._fetchone(
                connection,
                "SELECT * FROM insight_execution_metrics WHERE run_id = ?",
                (context.run_id,),
            )
            if existing is not None:
                persisted = self._execution_metrics(existing)
                if persisted != metrics:
                    raise DomainConflictError
                return persisted
            await connection.execute(
                """
                INSERT INTO insight_execution_metrics(
                    run_id, engagement_id, thread_id, started_at, completed_at,
                    status, duration_ms, topic_count, researcher_calls,
                    max_concurrent_researchers, model_calls, model_failures,
                    tool_calls, tool_failures, retrieval_calls, retry_count,
                    timeout_count, rerank_candidates_total,
                    max_rerank_candidates_per_call, retrieval_latency_ms,
                    reranker_latency_ms, input_tokens, output_tokens, total_tokens,
                    configured_topic_limit, configured_parallel_researcher_limit,
                    configured_model_call_limit, configured_tool_call_limit,
                    configured_retrieval_calls_per_researcher_limit,
                    configured_rerank_candidate_limit,
                    configured_provider_timeout_seconds,
                    configured_run_timeout_seconds, source_ids_json,
                    evidence_ids_json, tool_names_json, failure_code, correlation_id
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    metrics.run_id,
                    metrics.engagement_id,
                    metrics.thread_id,
                    self._time(metrics.started_at),
                    self._time(metrics.completed_at),
                    metrics.status,
                    metrics.duration_ms,
                    metrics.topic_count,
                    metrics.researcher_calls,
                    metrics.max_concurrent_researchers,
                    metrics.model_calls,
                    metrics.model_failures,
                    metrics.tool_calls,
                    metrics.tool_failures,
                    metrics.retrieval_calls,
                    metrics.retry_count,
                    metrics.timeout_count,
                    metrics.rerank_candidates_total,
                    metrics.max_rerank_candidates_per_call,
                    metrics.retrieval_latency_ms,
                    metrics.reranker_latency_ms,
                    metrics.input_tokens,
                    metrics.output_tokens,
                    metrics.total_tokens,
                    metrics.configured_topic_limit,
                    metrics.configured_parallel_researcher_limit,
                    metrics.configured_model_call_limit,
                    metrics.configured_tool_call_limit,
                    metrics.configured_retrieval_calls_per_researcher_limit,
                    metrics.configured_rerank_candidate_limit,
                    metrics.configured_provider_timeout_seconds,
                    metrics.configured_run_timeout_seconds,
                    self._json(metrics.source_ids),
                    self._json(metrics.evidence_ids),
                    self._json(metrics.tool_names),
                    metrics.failure_code,
                    metrics.correlation_id,
                ),
            )
            for event in events:
                if (
                    event.run_id,
                    event.engagement_id,
                    event.thread_id,
                    event.correlation_id,
                ) != expected_scope:
                    raise AccessDeniedError
                await connection.execute(
                    """
                    INSERT INTO insight_execution_events(
                        event_id, occurred_at, run_id, engagement_id, thread_id,
                        actor, operation_type, tool_name, status, duration_ms,
                        source_ids_json, evidence_ids_json, retry_count,
                        failure_code, correlation_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event.event_id,
                        self._time(event.occurred_at),
                        event.run_id,
                        event.engagement_id,
                        event.thread_id,
                        event.actor,
                        event.operation_type,
                        event.tool_name,
                        event.status,
                        event.duration_ms,
                        self._json(event.source_ids),
                        self._json(event.evidence_ids),
                        event.retry_count,
                        event.failure_code,
                        event.correlation_id,
                    ),
                )
            await connection.execute(
                """
                INSERT OR IGNORE INTO operational_audit_events(
                    event_id, occurred_at, run_id, engagement_id, thread_id,
                    actor, action, status, duration_ms, source_ids_json,
                    evidence_ids_json, retry_count, failure_code, correlation_id
                ) VALUES (?, ?, ?, ?, ?, 'insight_service', 'insight_execution',
                    ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    stable_id("insight-operational", context.run_id),
                    self._time(metrics.completed_at),
                    metrics.run_id,
                    metrics.engagement_id,
                    metrics.thread_id,
                    "failed" if metrics.status == "failed" else "succeeded",
                    metrics.duration_ms,
                    self._json(metrics.source_ids),
                    self._json(metrics.evidence_ids),
                    metrics.retry_count,
                    metrics.failure_code,
                    metrics.correlation_id,
                ),
            )
            return metrics

    async def metrics(
        self,
        context: InsightRuntimeContext,
        *,
        now: datetime,
    ) -> InsightExecutionMetrics:
        """Load the immutable server-measured execution totals for one scoped run."""
        async with self._database.connection() as connection:
            await self._resolve_access(connection, context, now)
            await self._require_run(connection, context)
            row = await self._fetchone(
                connection,
                "SELECT * FROM insight_execution_metrics WHERE run_id = ?",
                (context.run_id,),
            )
            if row is None:
                raise DomainPersistenceError
            return self._execution_metrics(row)

    async def execution_events(
        self,
        context: InsightRuntimeContext,
        *,
        now: datetime,
    ) -> tuple[InsightExecutionEvent, ...]:
        """Load ordered safe measured spans without prompts, inputs, or outputs."""
        async with self._database.connection() as connection:
            await self._resolve_access(connection, context, now)
            await self._require_run(connection, context)
            cursor = await connection.execute(
                """
                SELECT * FROM insight_execution_events
                WHERE run_id = ? AND engagement_id = ?
                ORDER BY occurred_at, event_id
                """,
                (context.run_id, context.access.engagement_id),
            )
            return tuple(self._execution_event(row) for row in await cursor.fetchall())

    async def _resolve_access(
        self,
        connection: aiosqlite.Connection,
        context: InsightRuntimeContext,
        now: datetime,
    ) -> None:
        access = context.access
        try:
            access.require_permission("insight:run")
            access.require_active(now)
        except (PermissionError, ValueError) as error:
            raise AccessDeniedError from error
        if access.principal_type != "pm" or access.thread_id is None:
            raise AccessDeniedError
        row = await self._fetchone(
            connection,
            """
            SELECT 1 FROM access_sessions AS a
            JOIN pm_access AS p ON p.pm_access_id = a.principal_id
            JOIN engagements AS e ON e.engagement_id = a.engagement_id
            WHERE a.principal_type = 'pm' AND a.principal_id = ?
                AND a.engagement_id = ? AND a.revoked_at IS NULL
                AND a.expires_at > ? AND p.status = 'active' AND e.status = 'active'
            LIMIT 1
            """,
            (access.principal_id, access.engagement_id, self._time(now)),
        )
        if row is None:
            raise AccessDeniedError

    async def _require_run(
        self,
        connection: aiosqlite.Connection,
        context: InsightRuntimeContext,
    ) -> aiosqlite.Row:
        row = await self._fetchone(
            connection,
            """
            SELECT * FROM insight_runs
            WHERE run_id = ? AND engagement_id = ? AND thread_id = ?
                AND requested_by_pm_access_id = ? AND requested_question = ?
            """,
            (
                context.run_id,
                context.access.engagement_id,
                context.access.thread_id,
                context.access.principal_id,
                context.question,
            ),
        )
        if row is None:
            raise AccessDeniedError
        return row

    async def _append_event(
        self,
        connection: aiosqlite.Connection,
        context: InsightRuntimeContext,
        *,
        actor: str,
        action: str,
        from_status: str | None,
        to_status: str | None,
        now: datetime,
        topic_id: str | None = None,
        source_ids: Sequence[str] = (),
        evidence_ids: Sequence[str] = (),
        artifact_name: str | None = None,
        failure_code: str | None = None,
    ) -> None:
        cursor = await connection.execute(
            "SELECT COUNT(*) AS count FROM insight_run_events WHERE run_id = ?",
            (context.run_id,),
        )
        count_row = await cursor.fetchone()
        if count_row is None:
            raise DomainPersistenceError
        event_id = stable_id(
            "insight-event",
            context.run_id,
            str(count_row["count"]),
            action,
        )
        await connection.execute(
            """
            INSERT INTO insight_run_events(
                event_id, run_id, engagement_id, occurred_at, actor, action,
                from_status, to_status, topic_id, source_ids_json,
                evidence_ids_json, artifact_name, failure_code, correlation_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                context.run_id,
                context.access.engagement_id,
                self._time(now),
                actor,
                action,
                from_status,
                to_status,
                topic_id,
                self._json(source_ids),
                self._json(evidence_ids),
                artifact_name,
                failure_code,
                context.access.correlation_id,
            ),
        )

    @staticmethod
    def _run(row: aiosqlite.Row) -> InsightRun:
        return InsightRun(
            run_id=row["run_id"],
            engagement_id=row["engagement_id"],
            thread_id=row["thread_id"],
            status=row["status"],
            requested_question=row["requested_question"],
            plan_id=row["plan_id"],
            report_id=row["report_id"],
            failure_code=row["failure_code"],
            failure_message=row["failure_message"],
            started_at=InsightRunRepository._parse_time(row["started_at"]),
            completed_at=InsightRunRepository._parse_optional_time(row["completed_at"]),
        )

    @staticmethod
    def _execution_metrics(row: aiosqlite.Row) -> InsightExecutionMetrics:
        payload = dict(row)
        payload["source_ids"] = json.loads(payload.pop("source_ids_json"))
        payload["evidence_ids"] = json.loads(payload.pop("evidence_ids_json"))
        payload["tool_names"] = json.loads(payload.pop("tool_names_json"))
        return InsightExecutionMetrics.model_validate(payload)

    @staticmethod
    def _execution_event(row: aiosqlite.Row) -> InsightExecutionEvent:
        payload = dict(row)
        payload["source_ids"] = json.loads(payload.pop("source_ids_json"))
        payload["evidence_ids"] = json.loads(payload.pop("evidence_ids_json"))
        return InsightExecutionEvent.model_validate(payload)

    @staticmethod
    async def _fetchone(
        connection: aiosqlite.Connection,
        sql: str,
        parameters: Sequence[object],
    ) -> aiosqlite.Row | None:
        cursor = await connection.execute(sql, tuple(parameters))
        return await cursor.fetchone()

    @staticmethod
    def _json(value: Sequence[str]) -> str:
        return json.dumps(list(value), ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _time(value: datetime) -> str:
        return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")

    @staticmethod
    def _parse_time(value: object) -> datetime:
        return datetime.fromisoformat(str(value)).astimezone(UTC)

    @staticmethod
    def _parse_optional_time(value: object) -> datetime | None:
        return None if value is None else InsightRunRepository._parse_time(value)

    @staticmethod
    def _require_clock(value: datetime) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Insight persistence requires an aware timestamp.")
