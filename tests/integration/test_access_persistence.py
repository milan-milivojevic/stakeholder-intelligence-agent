"""Migrated domain persistence, restart, audit, and setup-flow integration tests."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from pydantic import SecretStr

from stakeholder_intelligence_agent.access import AccessService
from stakeholder_intelligence_agent.errors import AccessDeniedError, DomainConflictError
from stakeholder_intelligence_agent.persistence import DomainDatabase

if TYPE_CHECKING:
    from stakeholder_intelligence_agent.config import Settings

pytestmark = pytest.mark.integration


@dataclass
class MutableClock:
    """Deterministic aware clock for persistence-bound lifecycle tests."""

    value: datetime

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **kwargs: float) -> None:
        self.value += timedelta(**kwargs)


async def _initialized_service(
    settings: Settings,
    clock: MutableClock,
) -> tuple[DomainDatabase, AccessService, str]:
    database = DomainDatabase(settings.domain_database)
    service = AccessService(database, settings, clock=clock)
    await service.initialize()
    session = await service.activate_pm(settings.pm_bootstrap_token.get_secret_value())
    return database, service, session.token.get_secret_value()


async def test_migrations_are_idempotent_and_domain_database_is_separate(
    settings: Settings,
) -> None:
    database = DomainDatabase(settings.domain_database)
    service = AccessService(database, settings)

    first = await service.initialize()
    second = await service.initialize()

    assert first == second
    assert await database.migration_versions() == (1, 2, 3, 4, 5, 6, 7, 8, 9, 10)
    assert settings.domain_database.exists()
    assert settings.domain_database != settings.checkpoint_database
    assert not settings.checkpoint_database.exists()
    async with database.connection() as connection:
        cursor = await connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        )
        tables = {str(row["name"]) for row in await cursor.fetchall()}
        assert {
            "access_sessions",
            "document_sources",
            "document_versions",
            "evidence_records",
            "engagements",
            "ingestion_artifacts",
            "ingestion_attempts",
            "insight_report_records",
            "insight_execution_events",
            "insight_execution_metrics",
            "insight_run_events",
            "insight_runs",
            "interview_sessions",
            "invitation_tokens",
            "operational_audit_events",
            "pm_access",
            "schema_migrations",
            "search_chunks",
            "source_elements",
            "stakeholders",
            "transcript_ingestion_versions",
            "transcript_search_chunks",
            "transcript_turns",
            "transcript_version_events",
            "transcripts",
        } <= tables
        with pytest.raises(sqlite3.IntegrityError):
            await connection.execute(
                """
                INSERT INTO stakeholders(
                    stakeholder_id, engagement_id, display_name, role, department,
                    status, created_at, updated_at
                ) VALUES ('invalid-status', 'missing', 'Invalid', NULL, NULL,
                    'invited', '2026-07-15T00:00:00Z', '2026-07-15T00:00:00Z')
                """
            )


async def test_changed_bootstrap_secret_does_not_silently_add_another_pm(
    settings: Settings,
) -> None:
    database = DomainDatabase(settings.domain_database)
    await AccessService(database, settings).initialize()
    changed = settings.model_copy(update={"pm_bootstrap_token": SecretStr("q" * 32)})

    with pytest.raises(DomainConflictError):
        await AccessService(database, changed).initialize()

    async with database.connection() as connection:
        cursor = await connection.execute("SELECT COUNT(*) AS count FROM pm_access")
        row = await cursor.fetchone()
        assert row is not None
        assert int(row["count"]) == 1


async def test_invalid_pm_secret_and_revoked_session_share_safe_denial(
    settings: Settings,
) -> None:
    database = DomainDatabase(settings.domain_database)
    service = AccessService(database, settings)
    await service.initialize()

    with pytest.raises(AccessDeniedError) as invalid:
        await service.activate_pm("invalid-synthetic-value")
    issued = await service.activate_pm(settings.pm_bootstrap_token.get_secret_value())
    raw_session = issued.token.get_secret_value()
    await service.revoke_session(raw_session)
    with pytest.raises(AccessDeniedError) as revoked:
        await service.create_engagement(
            raw_session,
            name="Denied",
            description=None,
            correlation_id="correlation-denied",
        )

    assert str(invalid.value) == str(revoked.value) == "Access is not authorized."


async def test_setup_records_sessions_and_audit_survive_service_restart(
    settings: Settings,
) -> None:
    clock = MutableClock(datetime(2026, 7, 15, 4, 0, tzinfo=UTC))
    database, service, pm_session = await _initialized_service(settings, clock)
    engagement = await service.create_engagement(
        pm_session,
        name="Transformation Alpha",
        description="Synthetic engagement for persistence verification.",
        correlation_id="correlation-engagement",
    )
    stakeholder = await service.create_stakeholder(
        pm_session,
        engagement.engagement_id,
        display_name="Alex Morgan",
        role="Operations manager",
        department="Operations",
        correlation_id="correlation-stakeholder",
    )
    assert stakeholder.status == "active"
    issued = await service.issue_invitation(
        pm_session,
        engagement.engagement_id,
        stakeholder.stakeholder_id,
        correlation_id="correlation-invitation",
    )

    restarted = AccessService(DomainDatabase(settings.domain_database), settings, clock=clock)
    await restarted.initialize()
    activated = await restarted.activate_invitation(
        issued.token.get_secret_value(),
        correlation_id="correlation-activation",
    )
    restarted_again = AccessService(
        DomainDatabase(settings.domain_database),
        settings,
        clock=clock,
    )
    context = await restarted_again.resolve_stakeholder_context(
        activated.access_session.token.get_secret_value(),
        correlation_id="correlation-resume",
        requested_engagement_id=engagement.engagement_id,
        requested_interview_session_id=activated.interview_session.interview_session_id,
        requested_thread_id=activated.interview_session.thread_id,
        required_permission="interview:participate",
    )

    assert context.engagement_id == engagement.engagement_id
    assert context.stakeholder_id == stakeholder.stakeholder_id
    assert context.thread_id == activated.interview_session.thread_id
    events = await restarted_again.list_audit_events(
        pm_session,
        engagement.engagement_id,
        correlation_id="correlation-audit",
    )
    assert {event.action for event in events} >= {
        "activate_invitation",
        "create_engagement",
        "create_stakeholder",
        "issue_invitation",
    }

    async with database.connection() as connection:
        with pytest.raises(sqlite3.IntegrityError):
            await connection.execute("UPDATE operational_audit_events SET actor = 'changed'")
        with pytest.raises(sqlite3.IntegrityError):
            await connection.execute("DELETE FROM operational_audit_events")


async def test_stakeholder_session_resolves_in_a_fresh_python_process(
    settings: Settings,
) -> None:
    clock = MutableClock(datetime(2026, 7, 15, 4, 0, tzinfo=UTC))
    _, service, pm_session = await _initialized_service(settings, clock)
    engagement = await service.create_engagement(
        pm_session,
        name="Process restart",
        description=None,
        correlation_id="correlation-engagement",
    )
    stakeholder = await service.create_stakeholder(
        pm_session,
        engagement.engagement_id,
        display_name="Process Restart Stakeholder",
        role=None,
        department=None,
        correlation_id="correlation-stakeholder",
    )
    invitation = await service.issue_invitation(
        pm_session,
        engagement.engagement_id,
        stakeholder.stakeholder_id,
        correlation_id="correlation-invitation",
    )
    activated = await service.activate_invitation(
        invitation.token.get_secret_value(),
        correlation_id="correlation-activation",
    )
    payload = {
        "settings": {
            "environment": "test",
            "google_api_key": settings.google_api_key.get_secret_value(),
            "gemini_primary_chat_model": settings.gemini_primary_chat_model,
            "gemini_fallback_chat_model": settings.gemini_fallback_chat_model,
            "gemini_vision_model": settings.gemini_vision_model,
            "gemini_embedding_model": settings.gemini_embedding_model,
            "pm_bootstrap_token": settings.pm_bootstrap_token.get_secret_value(),
            "token_pepper": settings.token_pepper.get_secret_value(),
            "data_root": str(settings.data_root),
            "domain_database": str(settings.domain_database),
            "checkpoint_database": str(settings.checkpoint_database),
            "originals_root": str(settings.originals_root),
            "derived_root": str(settings.derived_root),
            "agent_artifacts_root": str(settings.agent_artifacts_root),
            "audit_root": str(settings.audit_root),
        },
        "session_token": activated.access_session.token.get_secret_value(),
        "engagement_id": engagement.engagement_id,
        "interview_session_id": activated.interview_session.interview_session_id,
        "thread_id": activated.interview_session.thread_id,
        "clock_at": clock.value.isoformat(),
    }
    probe = Path(__file__).parents[1] / "probes" / "resolve_access_context.py"

    completed = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, str(probe)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
        cwd=probe.parents[2],
    )

    assert completed.returncode == 0, completed.stderr
    resolved = json.loads(completed.stdout)
    assert resolved == {
        "engagement_id": engagement.engagement_id,
        "stakeholder_id": stakeholder.stakeholder_id,
        "interview_session_id": activated.interview_session.interview_session_id,
        "thread_id": activated.interview_session.thread_id,
    }


async def test_pm_selection_is_server_state_and_browser_scope_drift_is_denied(
    settings: Settings,
) -> None:
    clock = MutableClock(datetime(2026, 7, 15, 4, 0, tzinfo=UTC))
    _, service, pm_session = await _initialized_service(settings, clock)
    alpha = await service.create_engagement(
        pm_session,
        name="Alpha",
        description=None,
        correlation_id="correlation-alpha",
    )
    beta = await service.create_engagement(
        pm_session,
        name="Beta",
        description=None,
        correlation_id="correlation-beta",
    )

    with pytest.raises(PermissionError, match="Access is not authorized"):
        await service.resolve_pm_context(
            pm_session,
            alpha.engagement_id,
            correlation_id="correlation-forged-selection",
        )
    selected = await service.select_engagement(
        pm_session,
        alpha.engagement_id,
        correlation_id="correlation-select-alpha",
    )
    assert selected.engagement_id == alpha.engagement_id
    with pytest.raises(PermissionError, match="Access is not authorized"):
        await service.resolve_pm_context(
            pm_session,
            beta.engagement_id,
            correlation_id="correlation-stale-beta",
        )


async def test_pm_session_expiration_is_enforced_from_persisted_time(
    settings: Settings,
) -> None:
    clock = MutableClock(datetime(2026, 7, 15, 4, 0, tzinfo=UTC))
    _, service, pm_session = await _initialized_service(settings, clock)
    engagement = await service.create_engagement(
        pm_session,
        name="Expiring PM session",
        description=None,
        correlation_id="correlation-create",
    )
    clock.advance(minutes=settings.pm_session_ttl_minutes, seconds=1)

    with pytest.raises(PermissionError, match="Access is not authorized"):
        await service.resolve_pm_context(
            pm_session,
            engagement.engagement_id,
            correlation_id="correlation-expired",
        )
