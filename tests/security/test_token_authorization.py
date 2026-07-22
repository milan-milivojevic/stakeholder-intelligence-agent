"""Invitation and session security, isolation, concurrency, and disclosure tests."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from stakeholder_intelligence_agent.access import AccessService, ActivatedInterview
from stakeholder_intelligence_agent.errors import AccessDeniedError, DomainConflictError
from stakeholder_intelligence_agent.persistence import DomainDatabase

if TYPE_CHECKING:
    from collections.abc import Awaitable

    from stakeholder_intelligence_agent.config import Settings

pytestmark = [pytest.mark.integration, pytest.mark.security]


@dataclass
class MutableClock:
    value: datetime

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **kwargs: float) -> None:
        self.value += timedelta(**kwargs)


@dataclass(frozen=True, slots=True)
class InvitationFixture:
    service: AccessService
    database: DomainDatabase
    pm_session: str
    engagement_id: str
    stakeholder_id: str
    invitation_id: str
    invitation_token: str


async def _invitation_fixture(
    settings: Settings,
    clock: MutableClock,
    *,
    engagement_name: str = "Security Alpha",
) -> InvitationFixture:
    database = DomainDatabase(settings.domain_database)
    service = AccessService(database, settings, clock=clock)
    await service.initialize()
    pm_issued = await service.activate_pm(settings.pm_bootstrap_token.get_secret_value())
    pm_session = pm_issued.token.get_secret_value()
    engagement = await service.create_engagement(
        pm_session,
        name=engagement_name,
        description=None,
        correlation_id="correlation-engagement",
    )
    stakeholder = await service.create_stakeholder(
        pm_session,
        engagement.engagement_id,
        display_name="Jordan Lee",
        role="Delivery lead",
        department="Delivery",
        correlation_id="correlation-stakeholder",
    )
    invitation = await service.issue_invitation(
        pm_session,
        engagement.engagement_id,
        stakeholder.stakeholder_id,
        correlation_id="correlation-invitation",
    )
    return InvitationFixture(
        service=service,
        database=database,
        pm_session=pm_session,
        engagement_id=engagement.engagement_id,
        stakeholder_id=stakeholder.stakeholder_id,
        invitation_id=invitation.invitation.invitation_id,
        invitation_token=invitation.token.get_secret_value(),
    )


async def _assert_denied(awaitable: Awaitable[object]) -> None:
    with pytest.raises(AccessDeniedError) as captured:
        await awaitable
    assert captured.value.code == "ACCESS_DENIED"
    assert str(captured.value) == "Access is not authorized."


async def test_concurrent_link_opens_resume_one_interview_mapping(
    settings: Settings,
) -> None:
    clock = MutableClock(datetime(2026, 7, 15, 5, 0, tzinfo=UTC))
    fixture = await _invitation_fixture(settings, clock)

    outcomes = await asyncio.gather(
        fixture.service.activate_invitation(
            fixture.invitation_token,
            correlation_id="correlation-first",
        ),
        fixture.service.activate_invitation(
            fixture.invitation_token,
            correlation_id="correlation-second",
        ),
        return_exceptions=True,
    )

    assert all(isinstance(outcome, ActivatedInterview) for outcome in outcomes)
    activations = [outcome for outcome in outcomes if isinstance(outcome, ActivatedInterview)]
    assert len({item.interview_session.interview_session_id for item in activations}) == 1
    assert len({item.interview_session.thread_id for item in activations}) == 1
    async with fixture.database.connection() as connection:
        cursor = await connection.execute("SELECT COUNT(*) AS count FROM interview_sessions")
        row = await cursor.fetchone()
        assert row is not None
        assert int(row["count"]) == 1
        cursor = await connection.execute(
            "SELECT COUNT(*) AS count FROM access_sessions WHERE principal_type = 'stakeholder'"
        )
        row = await cursor.fetchone()
        assert row is not None
        assert int(row["count"]) == 2


async def test_generating_again_returns_the_current_invitation_link(
    settings: Settings,
) -> None:
    clock = MutableClock(datetime(2026, 7, 15, 5, 0, tzinfo=UTC))
    fixture = await _invitation_fixture(settings, clock)

    current = await fixture.service.issue_invitation(
        fixture.pm_session,
        fixture.engagement_id,
        fixture.stakeholder_id,
        correlation_id="correlation-duplicate",
    )

    assert current.invitation.invitation_id == fixture.invitation_id
    assert current.token.get_secret_value() == fixture.invitation_token


async def test_forged_and_cross_scope_fail_while_valid_link_resumes_same_interview(
    settings: Settings,
) -> None:
    clock = MutableClock(datetime(2026, 7, 15, 5, 0, tzinfo=UTC))
    fixture = await _invitation_fixture(settings, clock)

    await _assert_denied(
        fixture.service.activate_invitation(
            "forged-token-that-does-not-exist",
            correlation_id="correlation-forged",
        )
    )
    activated = await fixture.service.activate_invitation(
        fixture.invitation_token,
        correlation_id="correlation-valid",
    )
    resumed = await fixture.service.activate_invitation(
        fixture.invitation_token,
        correlation_id="correlation-replay",
    )
    assert (
        resumed.interview_session.interview_session_id
        == activated.interview_session.interview_session_id
    )
    assert resumed.interview_session.thread_id == activated.interview_session.thread_id
    session_token = activated.access_session.token.get_secret_value()
    await _assert_denied(
        fixture.service.resolve_stakeholder_context(
            session_token,
            correlation_id="correlation-cross-engagement",
            requested_engagement_id="engagement-foreign",
        )
    )
    await _assert_denied(
        fixture.service.resolve_stakeholder_context(
            session_token,
            correlation_id="correlation-cross-session",
            requested_interview_session_id="interview-foreign",
        )
    )
    with pytest.raises(DomainConflictError):
        await fixture.service.revoke_invitation(
            fixture.pm_session,
            fixture.engagement_id,
            fixture.invitation_id,
            correlation_id="correlation-revoke",
        )
    context = await fixture.service.resolve_stakeholder_context(
        session_token,
        correlation_id="correlation-session-still-active",
    )
    assert context.interview_session_id == activated.interview_session.interview_session_id


async def test_two_real_engagement_canaries_cannot_cross_session_boundaries(
    settings: Settings,
) -> None:
    clock = MutableClock(datetime(2026, 7, 15, 5, 0, tzinfo=UTC))
    alpha = await _invitation_fixture(settings, clock, engagement_name="Canary Alpha")
    beta = await _invitation_fixture(settings, clock, engagement_name="Canary Beta")
    alpha_activation = await alpha.service.activate_invitation(
        alpha.invitation_token,
        correlation_id="correlation-activate-alpha",
    )
    beta_activation = await beta.service.activate_invitation(
        beta.invitation_token,
        correlation_id="correlation-activate-beta",
    )

    alpha_token = alpha_activation.access_session.token.get_secret_value()
    beta_token = beta_activation.access_session.token.get_secret_value()
    alpha_context = await alpha.service.resolve_stakeholder_context(
        alpha_token,
        correlation_id="correlation-alpha",
        requested_engagement_id=alpha.engagement_id,
        requested_interview_session_id=(alpha_activation.interview_session.interview_session_id),
        requested_thread_id=alpha_activation.interview_session.thread_id,
    )
    beta_context = await beta.service.resolve_stakeholder_context(
        beta_token,
        correlation_id="correlation-beta",
        requested_engagement_id=beta.engagement_id,
        requested_interview_session_id=beta_activation.interview_session.interview_session_id,
        requested_thread_id=beta_activation.interview_session.thread_id,
    )
    assert alpha_context.engagement_id != beta_context.engagement_id
    assert alpha_context.thread_id != beta_context.thread_id

    await _assert_denied(
        alpha.service.resolve_stakeholder_context(
            alpha_token,
            correlation_id="correlation-alpha-to-beta",
            requested_engagement_id=beta.engagement_id,
            requested_interview_session_id=(beta_activation.interview_session.interview_session_id),
            requested_thread_id=beta_activation.interview_session.thread_id,
        )
    )
    await _assert_denied(
        beta.service.resolve_stakeholder_context(
            beta_token,
            correlation_id="correlation-beta-to-alpha",
            requested_engagement_id=alpha.engagement_id,
            requested_interview_session_id=(
                alpha_activation.interview_session.interview_session_id
            ),
            requested_thread_id=alpha_activation.interview_session.thread_id,
        )
    )


async def test_expired_invitation_is_persisted_and_never_activates(
    settings: Settings,
) -> None:
    clock = MutableClock(datetime(2026, 7, 15, 5, 0, tzinfo=UTC))
    fixture = await _invitation_fixture(settings, clock)
    clock.advance(minutes=settings.invitation_ttl_minutes, seconds=1)

    await _assert_denied(
        fixture.service.activate_invitation(
            fixture.invitation_token,
            correlation_id="correlation-expired",
        )
    )
    async with fixture.database.connection() as connection:
        cursor = await connection.execute(
            "SELECT status FROM invitation_tokens WHERE invitation_id = ?",
            (fixture.invitation_id,),
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row["status"] == "expired"
        cursor = await connection.execute("SELECT COUNT(*) AS count FROM interview_sessions")
        row = await cursor.fetchone()
        assert row is not None
        assert int(row["count"]) == 0
        cursor = await connection.execute(
            """
            SELECT engagement_id, actor, action, status, failure_code, correlation_id
            FROM operational_audit_events
            WHERE action = 'activate_invitation' AND correlation_id = ?
            """,
            ("correlation-expired",),
        )
        denied = await cursor.fetchone()
        assert denied is not None
        assert dict(denied) == {
            "engagement_id": fixture.engagement_id,
            "actor": "access_service",
            "action": "activate_invitation",
            "status": "denied",
            "failure_code": "ACCESS_DENIED",
            "correlation_id": "correlation-expired",
        }


async def test_pre_activation_revocation_blocks_link_without_disclosure(
    settings: Settings,
) -> None:
    clock = MutableClock(datetime(2026, 7, 15, 5, 0, tzinfo=UTC))
    fixture = await _invitation_fixture(settings, clock)
    await fixture.service.revoke_invitation(
        fixture.pm_session,
        fixture.engagement_id,
        fixture.invitation_id,
        correlation_id="correlation-revoke",
    )

    await _assert_denied(
        fixture.service.activate_invitation(
            fixture.invitation_token,
            correlation_id="correlation-revoked",
        )
    )


async def test_stakeholder_session_expiration_is_checked_after_restart(
    settings: Settings,
) -> None:
    clock = MutableClock(datetime(2026, 7, 15, 5, 0, tzinfo=UTC))
    fixture = await _invitation_fixture(settings, clock)
    activated = await fixture.service.activate_invitation(
        fixture.invitation_token,
        correlation_id="correlation-activate",
    )
    clock.advance(minutes=settings.stakeholder_session_ttl_minutes, seconds=1)
    restarted = AccessService(
        DomainDatabase(settings.domain_database),
        settings,
        clock=clock,
    )

    await _assert_denied(
        restarted.resolve_stakeholder_context(
            activated.access_session.token.get_secret_value(),
            correlation_id="correlation-expired-session",
        )
    )


async def test_expired_interview_session_can_receive_a_new_link_for_the_same_interview(
    settings: Settings,
) -> None:
    clock = MutableClock(datetime(2026, 7, 15, 5, 0, tzinfo=UTC))
    fixture = await _invitation_fixture(settings, clock)
    activated = await fixture.service.activate_invitation(
        fixture.invitation_token,
        correlation_id="correlation-activate",
    )
    first_expiry = activated.access_session.expires_at
    assert first_expiry == clock.value + timedelta(hours=8)

    clock.advance(hours=7)
    late_resume = await fixture.service.activate_invitation(
        fixture.invitation_token,
        correlation_id="correlation-late-resume",
    )
    assert late_resume.access_session.expires_at == first_expiry

    clock.advance(hours=1, seconds=1)
    await _assert_denied(
        fixture.service.activate_invitation(
            fixture.invitation_token,
            correlation_id="correlation-expired-link",
        )
    )
    reissued = await fixture.service.issue_invitation(
        fixture.pm_session,
        fixture.engagement_id,
        fixture.stakeholder_id,
        correlation_id="correlation-reissue",
    )
    assert reissued.invitation.invitation_id != fixture.invitation_id
    copied = await fixture.service.get_invitation_link(
        fixture.pm_session,
        fixture.engagement_id,
        reissued.invitation.invitation_id,
        correlation_id="correlation-copy",
    )
    assert copied.token.get_secret_value() == reissued.token.get_secret_value()

    resumed = await fixture.service.activate_invitation(
        reissued.token.get_secret_value(),
        correlation_id="correlation-resume",
    )
    assert (
        resumed.interview_session.interview_session_id
        == activated.interview_session.interview_session_id
    )
    assert resumed.interview_session.thread_id == activated.interview_session.thread_id
    async with fixture.database.connection() as connection:
        cursor = await connection.execute("SELECT COUNT(*) AS count FROM interview_sessions")
        row = await cursor.fetchone()
        assert row is not None
        assert int(row["count"]) == 1


async def test_submitted_interview_blocks_new_or_copied_invitation_links(
    settings: Settings,
) -> None:
    clock = MutableClock(datetime(2026, 7, 15, 5, 0, tzinfo=UTC))
    fixture = await _invitation_fixture(settings, clock)
    activated = await fixture.service.activate_invitation(
        fixture.invitation_token,
        correlation_id="correlation-activate",
    )
    async with fixture.database.transaction() as connection:
        await connection.execute(
            "UPDATE interview_sessions SET status = 'finalizing' WHERE interview_session_id = ?",
            (activated.interview_session.interview_session_id,),
        )

    with pytest.raises(DomainConflictError):
        await fixture.service.issue_invitation(
            fixture.pm_session,
            fixture.engagement_id,
            fixture.stakeholder_id,
            correlation_id="correlation-issue-after-submit",
        )
    with pytest.raises(DomainConflictError):
        await fixture.service.get_invitation_link(
            fixture.pm_session,
            fixture.engagement_id,
            fixture.invitation_id,
            correlation_id="correlation-copy-after-submit",
        )


async def test_raw_tokens_never_enter_sqlite_files_or_serializable_domain_records(
    settings: Settings,
) -> None:
    clock = MutableClock(datetime(2026, 7, 15, 5, 0, tzinfo=UTC))
    fixture = await _invitation_fixture(settings, clock)
    activated = await fixture.service.activate_invitation(
        fixture.invitation_token,
        correlation_id="correlation-activate",
    )
    raw_values = {
        settings.pm_bootstrap_token.get_secret_value(),
        fixture.pm_session,
        fixture.invitation_token,
        activated.access_session.token.get_secret_value(),
    }

    assert all(raw not in repr(activated) for raw in raw_values)
    async with fixture.database.connection() as connection:
        await connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        cursor = await connection.execute(
            """
            SELECT token_hash FROM pm_access
            UNION ALL SELECT token_hash FROM invitation_tokens
            UNION ALL SELECT token_hash FROM access_sessions
            """
        )
        hashes = {str(row["token_hash"]) for row in await cursor.fetchall()}
        assert hashes
        assert all(len(value) == 64 for value in hashes)
        assert raw_values.isdisjoint(hashes)

    database_files = tuple(
        path
        for path in settings.domain_database.parent.glob(f"{settings.domain_database.name}*")
        if path.is_file()
    )
    assert database_files
    for path in database_files:
        content = await asyncio.to_thread(path.read_bytes)
        for raw in raw_values:
            assert raw.encode("utf-8") not in content
