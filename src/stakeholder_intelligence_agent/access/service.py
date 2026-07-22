"""Server-side PM, invitation, limited-session, and AccessContext service."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final, Literal

if TYPE_CHECKING:
    from collections.abc import Callable

    import aiosqlite
    from pydantic import SecretStr

    from stakeholder_intelligence_agent.config import Settings
    from stakeholder_intelligence_agent.persistence.domain import DomainDatabase

from stakeholder_intelligence_agent.access.tokens import (
    IssuedBearerToken,
    decrypt_invitation_token,
    encrypt_invitation_token,
    generate_bearer_token,
    generate_opaque_id,
    token_digest,
)
from stakeholder_intelligence_agent.contracts import (
    AccessContext,
    Engagement,
    InterviewSession,
    InvitationToken,
    OperationalAuditEvent,
    PMAccess,
    Stakeholder,
)
from stakeholder_intelligence_agent.contracts.common import utc_now
from stakeholder_intelligence_agent.contracts.lifecycle import validate_invitation_transition
from stakeholder_intelligence_agent.errors import (
    AccessClockError,
    AccessDeniedError,
    DomainConflictError,
    DomainPersistenceError,
)

PM_PERMISSIONS: Final[frozenset[str]] = frozenset(
    {
        "document:upload",
        "engagement:manage",
        "engagement:select",
        "insight:run",
        "invitation:manage",
        "session:read",
        "source:read",
        "stakeholder:manage",
    }
)
STAKEHOLDER_PERMISSIONS: Final[frozenset[str]] = frozenset(
    {
        "document:upload",
        "interview:finalize",
        "interview:participate",
        "source:read",
    }
)


@dataclass(frozen=True, slots=True)
class IssuedInvitation:
    """Invitation metadata plus a raw link returned only to an authorized PM."""

    invitation: InvitationToken
    token: SecretStr


@dataclass(frozen=True, slots=True)
class ActivatedInterview:
    """Atomic invitation activation result and limited session credential."""

    interview_session: InterviewSession
    access_session: IssuedBearerToken


@dataclass(frozen=True, slots=True)
class ResolvedAccessSession:
    """Safe server-resolved session metadata that never contains the bearer value."""

    principal_type: Literal["pm", "stakeholder"]
    access_session_id: str
    expires_at: datetime
    engagement_id: str | None
    stakeholder_id: str | None
    interview_session_id: str | None
    thread_id: str | None


class AccessService:
    """Resolve every access boundary from persisted trusted state."""

    def __init__(
        self,
        database: DomainDatabase,
        settings: Settings,
        *,
        clock: Callable[[], datetime] = utc_now,
        token_factory: Callable[[], SecretStr] = generate_bearer_token,
        id_factory: Callable[[str], str] = generate_opaque_id,
    ) -> None:
        self._database = database
        self._settings = settings
        self._clock = clock
        self._token_factory = token_factory
        self._id_factory = id_factory

    async def initialize(self) -> PMAccess:
        """Apply migrations and ensure exactly one record for the configured PM secret."""
        await self._database.initialize()
        digest = token_digest(self._settings.pm_bootstrap_token, self._settings.token_pepper)
        now = self._now()
        async with self._database.transaction() as connection:
            row = await self._fetchone(
                connection,
                "SELECT * FROM pm_access WHERE token_hash = ?",
                (digest,),
            )
            if row is None:
                existing = await self._fetchone(
                    connection,
                    "SELECT pm_access_id FROM pm_access LIMIT 1",
                    (),
                )
                if existing is not None:
                    raise DomainConflictError
                pm_access = PMAccess(
                    pm_access_id=self._id_factory("pm"),
                    token_hash=digest,
                    status="active",
                    created_at=now,
                )
                await connection.execute(
                    """
                    INSERT INTO pm_access(
                        pm_access_id, token_hash, status, created_at, revoked_at
                    ) VALUES (?, ?, ?, ?, NULL)
                    """,
                    (
                        pm_access.pm_access_id,
                        pm_access.token_hash,
                        pm_access.status,
                        self._time(pm_access.created_at),
                    ),
                )
                return pm_access
            return self._pm_access(row)

    async def activate_pm(self, bootstrap_token: str) -> IssuedBearerToken:
        """Exchange the configured PM secret for a hash-stored, expiring session."""
        now = self._now()
        digest = token_digest(bootstrap_token, self._settings.token_pepper)
        async with self._database.transaction() as connection:
            row = await self._fetchone(
                connection,
                "SELECT * FROM pm_access WHERE token_hash = ? AND status = 'active'",
                (digest,),
            )
            if row is None:
                raise AccessDeniedError
            pm_access = self._pm_access(row)
            return await self._issue_access_session(
                connection,
                principal_type="pm",
                principal_id=pm_access.pm_access_id,
                engagement_id=None,
                stakeholder_id=None,
                interview_session_id=None,
                thread_id=None,
                now=now,
                ttl_minutes=self._settings.pm_session_ttl_minutes,
            )

    async def inspect_access_session(
        self,
        session_token: str,
        *,
        correlation_id: str,
    ) -> ResolvedAccessSession:
        """Resolve safe current session metadata without returning or widening its credential."""
        now = self._now()
        digest = token_digest(session_token, self._settings.token_pepper)
        async with self._database.connection() as connection:
            row = await self._fetchone(
                connection,
                """
                SELECT * FROM access_sessions
                WHERE token_hash = ? AND revoked_at IS NULL AND expires_at > ?
                """,
                (digest, self._time(now)),
            )
            if row is None:
                raise AccessDeniedError
            principal_type = str(row["principal_type"])
            if principal_type == "pm":
                resolved_principal_type: Literal["pm", "stakeholder"] = "pm"
                session = await self._require_pm_session(connection, session_token, now)
                engagement_id = session["engagement_id"]
                if engagement_id is not None:
                    await self._require_active_engagement(connection, str(engagement_id))
            elif principal_type == "stakeholder":
                resolved_principal_type = "stakeholder"
            else:
                raise AccessDeniedError

        if principal_type == "stakeholder":
            await self.resolve_stakeholder_context(
                session_token,
                correlation_id=correlation_id,
            )

        return ResolvedAccessSession(
            principal_type=resolved_principal_type,
            access_session_id=str(row["access_session_id"]),
            expires_at=self._parse_time(row["expires_at"]),
            engagement_id=None if row["engagement_id"] is None else str(row["engagement_id"]),
            stakeholder_id=(None if row["stakeholder_id"] is None else str(row["stakeholder_id"])),
            interview_session_id=(
                None if row["interview_session_id"] is None else str(row["interview_session_id"])
            ),
            thread_id=None if row["thread_id"] is None else str(row["thread_id"]),
        )

    async def list_engagements(self, pm_session_token: str) -> tuple[Engagement, ...]:
        """List active engagements after rechecking the live PM bearer session."""
        now = self._now()
        async with self._database.connection() as connection:
            await self._require_pm_session(connection, pm_session_token, now)
            cursor = await connection.execute(
                """
                SELECT * FROM engagements
                WHERE status = 'active'
                ORDER BY created_at, engagement_id
                """
            )
            return tuple(self._engagement(row) for row in await cursor.fetchall())

    async def create_engagement(
        self,
        pm_session_token: str,
        *,
        name: str,
        description: str | None,
        correlation_id: str,
    ) -> Engagement:
        """Create and atomically select one server-generated engagement scope."""
        now = self._now()
        async with self._database.transaction() as connection:
            session = await self._require_pm_session(connection, pm_session_token, now)
            engagement = Engagement(
                engagement_id=self._id_factory("engagement"),
                name=name,
                description=description,
                status="active",
                created_at=now,
                updated_at=now,
            )
            creation_context = self._pm_context(
                session,
                engagement.engagement_id,
                correlation_id,
                now=now,
            )
            creation_context.require_permission("engagement:manage")
            await connection.execute(
                """
                INSERT INTO engagements(
                    engagement_id, name, description, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    engagement.engagement_id,
                    engagement.name,
                    engagement.description,
                    engagement.status,
                    self._time(engagement.created_at),
                    self._time(engagement.updated_at),
                ),
            )
            await connection.execute(
                "UPDATE access_sessions SET engagement_id = ? WHERE access_session_id = ?",
                (engagement.engagement_id, session["access_session_id"]),
            )
            await self._append_audit(
                connection,
                engagement_id=engagement.engagement_id,
                actor="access_service",
                action="create_engagement",
                status="succeeded",
                correlation_id=correlation_id,
                occurred_at=now,
            )
            return engagement

    async def select_engagement(
        self,
        pm_session_token: str,
        engagement_id: str,
        *,
        correlation_id: str,
    ) -> AccessContext:
        """Select an active engagement in trusted server-side PM session state."""
        now = self._now()
        async with self._database.transaction() as connection:
            session = await self._require_pm_session(connection, pm_session_token, now)
            await self._require_active_engagement(connection, engagement_id)
            await connection.execute(
                "UPDATE access_sessions SET engagement_id = ? WHERE access_session_id = ?",
                (engagement_id, session["access_session_id"]),
            )
            await self._append_audit(
                connection,
                engagement_id=engagement_id,
                actor="access_service",
                action="select_engagement",
                status="succeeded",
                correlation_id=correlation_id,
                occurred_at=now,
            )
            context = self._pm_context(session, engagement_id, correlation_id, now=now)
            context.require_permission("engagement:select")
            return context

    async def resolve_pm_context(
        self,
        pm_session_token: str,
        engagement_id: str,
        *,
        correlation_id: str,
        thread_id: str | None = None,
        required_permission: str | None = None,
    ) -> AccessContext:
        """Resolve PM scope and reject browser-supplied selection drift."""
        now = self._now()
        async with self._database.connection() as connection:
            session = await self._require_pm_session(connection, pm_session_token, now)
            if session["engagement_id"] != engagement_id:
                raise AccessDeniedError
            await self._require_active_engagement(connection, engagement_id)
            context = self._pm_context(
                session,
                engagement_id,
                correlation_id,
                now=now,
                thread_id=thread_id,
            )
            if required_permission is not None:
                context.require_permission(required_permission)
            return context

    async def create_stakeholder(
        self,
        pm_session_token: str,
        engagement_id: str,
        *,
        display_name: str,
        role: str | None,
        department: str | None,
        correlation_id: str,
    ) -> Stakeholder:
        """Create one stakeholder inside the selected PM engagement."""
        context = await self.resolve_pm_context(
            pm_session_token,
            engagement_id,
            correlation_id=correlation_id,
            required_permission="stakeholder:manage",
        )
        now = self._now()
        stakeholder = Stakeholder(
            stakeholder_id=self._id_factory("stakeholder"),
            engagement_id=context.engagement_id,
            display_name=display_name,
            role=role,
            department=department,
            status="active",
            created_at=now,
            updated_at=now,
        )
        async with self._database.transaction() as connection:
            await self._recheck_pm_session_selection(
                connection,
                pm_session_token,
                engagement_id,
                now,
            )
            await connection.execute(
                """
                INSERT INTO stakeholders(
                    stakeholder_id, engagement_id, display_name, role, department,
                    status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    stakeholder.stakeholder_id,
                    stakeholder.engagement_id,
                    stakeholder.display_name,
                    stakeholder.role,
                    stakeholder.department,
                    stakeholder.status,
                    self._time(stakeholder.created_at),
                    self._time(stakeholder.updated_at),
                ),
            )
            await self._append_audit(
                connection,
                engagement_id=engagement_id,
                actor="access_service",
                action="create_stakeholder",
                status="succeeded",
                correlation_id=correlation_id,
                occurred_at=now,
            )
        return stakeholder

    async def issue_invitation(
        self,
        pm_session_token: str,
        engagement_id: str,
        stakeholder_id: str,
        *,
        correlation_id: str,
    ) -> IssuedInvitation:
        """Create or return the current interview invitation link for one stakeholder."""
        await self.resolve_pm_context(
            pm_session_token,
            engagement_id,
            correlation_id=correlation_id,
            required_permission="invitation:manage",
        )
        now = self._now()
        async with self._database.transaction() as connection:
            session = await self._recheck_pm_session_selection(
                connection,
                pm_session_token,
                engagement_id,
                now,
            )
            stakeholder_row = await self._fetchone(
                connection,
                "SELECT * FROM stakeholders WHERE stakeholder_id = ? AND engagement_id = ?",
                (stakeholder_id, engagement_id),
            )
            if stakeholder_row is None:
                raise AccessDeniedError
            if stakeholder_row["status"] == "revoked":
                raise DomainConflictError

            interview_row = await self._fetchone(
                connection,
                """
                SELECT * FROM interview_sessions
                WHERE stakeholder_id = ? AND engagement_id = ?
                ORDER BY started_at DESC, interview_session_id DESC
                LIMIT 1
                """,
                (stakeholder_id, engagement_id),
            )
            if interview_row is not None and interview_row["status"] != "draft":
                raise DomainConflictError

            current = await self._fetchone(
                connection,
                """
                SELECT * FROM invitation_tokens
                WHERE stakeholder_id = ? AND engagement_id = ?
                    AND status IN ('active', 'activated')
                ORDER BY created_at DESC, invitation_id DESC
                LIMIT 1
                """,
                (stakeholder_id, engagement_id),
            )
            if current is not None:
                current_invitation = self._invitation(current)
                if now < current_invitation.expires_at:
                    return IssuedInvitation(
                        invitation=current_invitation,
                        token=self._recover_invitation_token(current),
                    )
                await connection.execute(
                    "UPDATE invitation_tokens SET status = 'expired' WHERE invitation_id = ?",
                    (current_invitation.invitation_id,),
                )

            interview_session_id = (
                None if interview_row is None else str(interview_row["interview_session_id"])
            )

            raw_token = self._token_factory()
            invitation = InvitationToken(
                invitation_id=self._id_factory("invitation"),
                engagement_id=engagement_id,
                stakeholder_id=stakeholder_id,
                token_hash=token_digest(raw_token, self._settings.token_pepper),
                status="active",
                created_at=now,
                expires_at=now + timedelta(minutes=self._settings.invitation_ttl_minutes),
                created_by_pm_access_id=str(session["principal_id"]),
            )
            try:
                await connection.execute(
                    """
                    INSERT INTO invitation_tokens(
                        invitation_id, engagement_id, stakeholder_id, token_hash, status,
                        created_at, expires_at, activated_at, revoked_at,
                        created_by_pm_access_id, token_ciphertext, interview_session_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?)
                    """,
                    (
                        invitation.invitation_id,
                        invitation.engagement_id,
                        invitation.stakeholder_id,
                        invitation.token_hash,
                        invitation.status,
                        self._time(invitation.created_at),
                        self._time(invitation.expires_at),
                        invitation.created_by_pm_access_id,
                        encrypt_invitation_token(raw_token, self._settings.token_pepper),
                        interview_session_id,
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise DomainConflictError from error
            await self._append_audit(
                connection,
                engagement_id=engagement_id,
                actor="access_service",
                action="issue_invitation",
                status="succeeded",
                correlation_id=correlation_id,
                occurred_at=now,
            )
        return IssuedInvitation(invitation=invitation, token=raw_token)

    async def get_invitation_link(
        self,
        pm_session_token: str,
        engagement_id: str,
        invitation_id: str,
        *,
        correlation_id: str,
    ) -> IssuedInvitation:
        """Return a current invitation link for an authorized PM copy action."""
        await self.resolve_pm_context(
            pm_session_token,
            engagement_id,
            correlation_id=correlation_id,
            required_permission="invitation:manage",
        )
        now = self._now()
        unavailable = False
        result: IssuedInvitation | None = None
        async with self._database.transaction() as connection:
            await self._recheck_pm_session_selection(
                connection,
                pm_session_token,
                engagement_id,
                now,
            )
            row = await self._fetchone(
                connection,
                """
                SELECT * FROM invitation_tokens
                WHERE invitation_id = ? AND engagement_id = ?
                """,
                (invitation_id, engagement_id),
            )
            if row is None:
                raise AccessDeniedError
            invitation = self._invitation(row)
            interview_session_id = row["interview_session_id"]
            if interview_session_id is not None:
                interview_row = await self._fetchone(
                    connection,
                    """
                    SELECT status FROM interview_sessions
                    WHERE interview_session_id = ? AND engagement_id = ?
                        AND stakeholder_id = ?
                    """,
                    (interview_session_id, engagement_id, invitation.stakeholder_id),
                )
                if interview_row is None:
                    raise AccessDeniedError
                if interview_row["status"] != "draft":
                    raise DomainConflictError
            if invitation.status not in {"active", "activated"} or now >= invitation.expires_at:
                if invitation.status in {"active", "activated"}:
                    await connection.execute(
                        "UPDATE invitation_tokens SET status = 'expired' WHERE invitation_id = ?",
                        (invitation_id,),
                    )
                unavailable = True
            else:
                result = IssuedInvitation(
                    invitation=invitation,
                    token=self._recover_invitation_token(row),
                )
        if unavailable or result is None:
            raise DomainConflictError
        return result

    async def activate_invitation(
        self,
        invitation_token: str,
        *,
        correlation_id: str,
    ) -> ActivatedInterview:
        """Activate a link or return to its still-valid interview session."""
        now = self._now()
        digest = token_digest(invitation_token, self._settings.token_pepper)
        denied = False
        result: ActivatedInterview | None = None
        async with self._database.transaction() as connection:
            row = await self._fetchone(
                connection,
                "SELECT * FROM invitation_tokens WHERE token_hash = ?",
                (digest,),
            )
            if row is None:
                raise AccessDeniedError
            invitation = self._invitation(row)
            if invitation.status in {"active", "activated"} and now >= invitation.expires_at:
                expired = InvitationToken.model_validate(
                    invitation.model_dump() | {"status": "expired"}
                )
                validate_invitation_transition(invitation, expired)
                await connection.execute(
                    "UPDATE invitation_tokens SET status = 'expired' WHERE invitation_id = ?",
                    (invitation.invitation_id,),
                )
                invitation = expired
            if invitation.status == "activated":
                result = await self._resume_interview_from_invitation(
                    connection,
                    invitation,
                    interview_session_id=row["interview_session_id"],
                    correlation_id=correlation_id,
                    now=now,
                )
            elif invitation.status == "active":
                result = await self._activate_valid_invitation(
                    connection,
                    invitation,
                    interview_session_id=row["interview_session_id"],
                    correlation_id=correlation_id,
                    now=now,
                )
            else:
                await self._append_audit(
                    connection,
                    engagement_id=invitation.engagement_id,
                    actor="access_service",
                    action="activate_invitation",
                    status="denied",
                    correlation_id=correlation_id,
                    occurred_at=now,
                    failure_code="ACCESS_DENIED",
                )
                denied = True
        if denied or result is None:
            raise AccessDeniedError
        return result

    async def resolve_stakeholder_context(
        self,
        session_token: str,
        *,
        correlation_id: str,
        requested_engagement_id: str | None = None,
        requested_interview_session_id: str | None = None,
        requested_thread_id: str | None = None,
        required_permission: str | None = None,
    ) -> AccessContext:
        """Resolve a fixed stakeholder, engagement, interview, and thread mapping."""
        now = self._now()
        digest = token_digest(session_token, self._settings.token_pepper)
        async with self._database.connection() as connection:
            row = await self._fetchone(
                connection,
                """
                SELECT s.*
                FROM access_sessions AS s
                JOIN engagements AS e ON e.engagement_id = s.engagement_id
                JOIN stakeholders AS h ON h.stakeholder_id = s.stakeholder_id
                JOIN interview_sessions AS i
                    ON i.interview_session_id = s.interview_session_id
                WHERE s.token_hash = ? AND s.principal_type = 'stakeholder'
                    AND s.revoked_at IS NULL AND s.expires_at > ?
                    AND e.status = 'active' AND h.status = 'active'
                    AND i.engagement_id = s.engagement_id
                    AND i.stakeholder_id = s.stakeholder_id
                    AND i.thread_id = s.thread_id
                """,
                (digest, self._time(now)),
            )
            if row is None:
                raise AccessDeniedError
            if (
                requested_engagement_id is not None
                and row["engagement_id"] != requested_engagement_id
            ):
                raise AccessDeniedError
            if (
                requested_interview_session_id is not None
                and row["interview_session_id"] != requested_interview_session_id
            ):
                raise AccessDeniedError
            if requested_thread_id is not None and row["thread_id"] != requested_thread_id:
                raise AccessDeniedError
            context = AccessContext(
                principal_type="stakeholder",
                principal_id=str(row["principal_id"]),
                engagement_id=str(row["engagement_id"]),
                stakeholder_id=str(row["stakeholder_id"]),
                interview_session_id=str(row["interview_session_id"]),
                thread_id=str(row["thread_id"]),
                permissions=STAKEHOLDER_PERMISSIONS,
                issued_at=self._parse_time(row["issued_at"]),
                expires_at=self._parse_time(row["expires_at"]),
                correlation_id=correlation_id,
            )
            if required_permission is not None:
                context.require_permission(required_permission)
            return context

    async def revoke_invitation(
        self,
        pm_session_token: str,
        engagement_id: str,
        invitation_id: str,
        *,
        correlation_id: str,
    ) -> InvitationToken:
        """Revoke an invitation that the stakeholder has not opened."""
        await self.resolve_pm_context(
            pm_session_token,
            engagement_id,
            correlation_id=correlation_id,
            required_permission="invitation:manage",
        )
        now = self._now()
        async with self._database.transaction() as connection:
            await self._recheck_pm_session_selection(
                connection,
                pm_session_token,
                engagement_id,
                now,
            )
            row = await self._fetchone(
                connection,
                """
                SELECT * FROM invitation_tokens
                WHERE invitation_id = ? AND engagement_id = ?
                """,
                (invitation_id, engagement_id),
            )
            if row is None:
                raise AccessDeniedError
            invitation = self._invitation(row)
            if invitation.status != "active":
                raise DomainConflictError
            revoked = InvitationToken.model_validate(
                invitation.model_dump() | {"status": "revoked", "revoked_at": now}
            )
            validate_invitation_transition(invitation, revoked)
            await connection.execute(
                """
                UPDATE invitation_tokens
                SET status = 'revoked', revoked_at = ?
                WHERE invitation_id = ?
                """,
                (self._time(now), invitation_id),
            )
            await connection.execute(
                """
                UPDATE access_sessions
                SET revoked_at = ?
                WHERE interview_session_id IN (
                    SELECT interview_session_id FROM interview_sessions
                    WHERE invitation_id = ?
                ) AND revoked_at IS NULL
                """,
                (self._time(now), invitation_id),
            )
            await self._append_audit(
                connection,
                engagement_id=engagement_id,
                actor="access_service",
                action="revoke_invitation",
                status="succeeded",
                correlation_id=correlation_id,
                occurred_at=now,
            )
            return revoked

    async def revoke_session(self, session_token: str) -> None:
        """Revoke one known access session without revealing whether it existed."""
        digest = token_digest(session_token, self._settings.token_pepper)
        async with self._database.transaction() as connection:
            await connection.execute(
                """
                UPDATE access_sessions SET revoked_at = ?
                WHERE token_hash = ? AND revoked_at IS NULL
                """,
                (self._time(self._now()), digest),
            )

    async def list_audit_events(
        self,
        pm_session_token: str,
        engagement_id: str,
        *,
        correlation_id: str,
    ) -> tuple[OperationalAuditEvent, ...]:
        """Return only audit events for the server-selected PM engagement."""
        await self.resolve_pm_context(
            pm_session_token,
            engagement_id,
            correlation_id=correlation_id,
            required_permission="session:read",
        )
        async with self._database.connection() as connection:
            cursor = await connection.execute(
                """
                SELECT * FROM operational_audit_events
                WHERE engagement_id = ? ORDER BY occurred_at, event_id
                """,
                (engagement_id,),
            )
            return tuple(self._audit_event(row) for row in await cursor.fetchall())

    async def _activate_valid_invitation(
        self,
        connection: aiosqlite.Connection,
        invitation: InvitationToken,
        *,
        interview_session_id: str | None,
        correlation_id: str,
        now: datetime,
    ) -> ActivatedInterview:
        if interview_session_id is not None:
            return await self._resume_interview_from_invitation(
                connection,
                invitation,
                interview_session_id=interview_session_id,
                correlation_id=correlation_id,
                now=now,
            )
        stakeholder_row = await self._fetchone(
            connection,
            """
            SELECT h.* FROM stakeholders AS h
            JOIN engagements AS e ON e.engagement_id = h.engagement_id
            WHERE h.stakeholder_id = ? AND h.engagement_id = ?
                AND h.status = 'active' AND e.status = 'active'
            """,
            (invitation.stakeholder_id, invitation.engagement_id),
        )
        if stakeholder_row is None:
            raise AccessDeniedError
        activated_invitation = InvitationToken.model_validate(
            invitation.model_dump()
            | {
                "status": "activated",
                "activated_at": now,
                "expires_at": now
                + timedelta(minutes=self._settings.stakeholder_session_ttl_minutes),
            }
        )
        validate_invitation_transition(invitation, activated_invitation)
        interview = InterviewSession(
            interview_session_id=self._id_factory("interview"),
            engagement_id=invitation.engagement_id,
            stakeholder_id=invitation.stakeholder_id,
            invitation_id=invitation.invitation_id,
            thread_id=self._id_factory("thread"),
            status="draft",
            started_at=now,
        )
        await connection.execute(
            """
            UPDATE invitation_tokens
            SET status = 'activated', activated_at = ?, expires_at = ?, interview_session_id = ?
            WHERE invitation_id = ? AND status = 'active'
            """,
            (
                self._time(now),
                self._time(activated_invitation.expires_at),
                interview.interview_session_id,
                invitation.invitation_id,
            ),
        )
        await connection.execute(
            """
            INSERT INTO interview_sessions(
                interview_session_id, engagement_id, stakeholder_id, invitation_id,
                thread_id, status, started_at, finalized_at, transcript_id,
                ingestion_version_id, failure_code, failure_message
            ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, NULL)
            """,
            (
                interview.interview_session_id,
                interview.engagement_id,
                interview.stakeholder_id,
                interview.invitation_id,
                interview.thread_id,
                interview.status,
                self._time(interview.started_at),
            ),
        )
        access_session = await self._issue_access_session(
            connection,
            principal_type="stakeholder",
            principal_id=interview.stakeholder_id,
            engagement_id=interview.engagement_id,
            stakeholder_id=interview.stakeholder_id,
            interview_session_id=interview.interview_session_id,
            thread_id=interview.thread_id,
            now=now,
            ttl_minutes=self._settings.stakeholder_session_ttl_minutes,
            maximum_expires_at=activated_invitation.expires_at,
        )
        await self._append_audit(
            connection,
            engagement_id=interview.engagement_id,
            actor="access_service",
            action="activate_invitation",
            status="succeeded",
            correlation_id=correlation_id,
            occurred_at=now,
            thread_id=interview.thread_id,
        )
        return ActivatedInterview(interview_session=interview, access_session=access_session)

    async def _resume_interview_from_invitation(
        self,
        connection: aiosqlite.Connection,
        invitation: InvitationToken,
        *,
        interview_session_id: str | None,
        correlation_id: str,
        now: datetime,
    ) -> ActivatedInterview:
        """Issue a fresh browser session for the same unfinished interview."""
        if interview_session_id is None:
            raise AccessDeniedError
        row = await self._fetchone(
            connection,
            """
            SELECT * FROM interview_sessions
            WHERE interview_session_id = ? AND engagement_id = ? AND stakeholder_id = ?
            """,
            (interview_session_id, invitation.engagement_id, invitation.stakeholder_id),
        )
        if row is None:
            raise AccessDeniedError
        interview = self._interview(row)
        if interview.status in {"finalized", "ingesting", "ready"}:
            raise DomainConflictError
        if interview.status != "draft":
            raise AccessDeniedError
        session_expiry = now + timedelta(minutes=self._settings.stakeholder_session_ttl_minutes)
        invitation_expiry = invitation.expires_at
        if invitation.status == "active":
            activated = InvitationToken.model_validate(
                invitation.model_dump()
                | {"status": "activated", "activated_at": now, "expires_at": session_expiry}
            )
            validate_invitation_transition(invitation, activated)
            await connection.execute(
                """
                UPDATE invitation_tokens
                SET status = 'activated', activated_at = ?, expires_at = ?
                WHERE invitation_id = ? AND status = 'active'
                """,
                (self._time(now), self._time(session_expiry), invitation.invitation_id),
            )
            invitation_expiry = session_expiry
        access_session = await self._issue_access_session(
            connection,
            principal_type="stakeholder",
            principal_id=interview.stakeholder_id,
            engagement_id=interview.engagement_id,
            stakeholder_id=interview.stakeholder_id,
            interview_session_id=interview.interview_session_id,
            thread_id=interview.thread_id,
            now=now,
            ttl_minutes=self._settings.stakeholder_session_ttl_minutes,
            maximum_expires_at=invitation_expiry,
        )
        await self._append_audit(
            connection,
            engagement_id=interview.engagement_id,
            actor="access_service",
            action="resume_interview_invitation",
            status="succeeded",
            correlation_id=correlation_id,
            occurred_at=now,
            thread_id=interview.thread_id,
        )
        return ActivatedInterview(interview_session=interview, access_session=access_session)

    async def _issue_access_session(
        self,
        connection: aiosqlite.Connection,
        *,
        principal_type: str,
        principal_id: str,
        engagement_id: str | None,
        stakeholder_id: str | None,
        interview_session_id: str | None,
        thread_id: str | None,
        now: datetime,
        ttl_minutes: int,
        maximum_expires_at: datetime | None = None,
    ) -> IssuedBearerToken:
        raw_token = self._token_factory()
        session_id = self._id_factory("access_session")
        expires_at = now + timedelta(minutes=ttl_minutes)
        if maximum_expires_at is not None:
            expires_at = min(expires_at, maximum_expires_at)
        await connection.execute(
            """
            INSERT INTO access_sessions(
                access_session_id, token_hash, principal_type, principal_id,
                engagement_id, stakeholder_id, interview_session_id, thread_id,
                issued_at, expires_at, revoked_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                session_id,
                token_digest(raw_token, self._settings.token_pepper),
                principal_type,
                principal_id,
                engagement_id,
                stakeholder_id,
                interview_session_id,
                thread_id,
                self._time(now),
                self._time(expires_at),
            ),
        )
        return IssuedBearerToken(
            access_session_id=session_id,
            token=raw_token,
            expires_at=expires_at,
        )

    async def _require_pm_session(
        self,
        connection: aiosqlite.Connection,
        pm_session_token: str,
        now: datetime,
    ) -> aiosqlite.Row:
        digest = token_digest(pm_session_token, self._settings.token_pepper)
        row = await self._fetchone(
            connection,
            """
            SELECT s.* FROM access_sessions AS s
            JOIN pm_access AS p ON p.pm_access_id = s.principal_id
            WHERE s.token_hash = ? AND s.principal_type = 'pm'
                AND s.revoked_at IS NULL AND s.expires_at > ? AND p.status = 'active'
            """,
            (digest, self._time(now)),
        )
        if row is None:
            raise AccessDeniedError
        return row

    async def _recheck_pm_session_selection(
        self,
        connection: aiosqlite.Connection,
        pm_session_token: str,
        engagement_id: str,
        now: datetime,
    ) -> aiosqlite.Row:
        session = await self._require_pm_session(connection, pm_session_token, now)
        if session["engagement_id"] != engagement_id:
            raise AccessDeniedError
        await self._require_active_engagement(connection, engagement_id)
        return session

    async def _require_active_engagement(
        self,
        connection: aiosqlite.Connection,
        engagement_id: str,
    ) -> None:
        row = await self._fetchone(
            connection,
            "SELECT engagement_id FROM engagements WHERE engagement_id = ? AND status = 'active'",
            (engagement_id,),
        )
        if row is None:
            raise AccessDeniedError

    def _pm_context(
        self,
        session: aiosqlite.Row,
        engagement_id: str,
        correlation_id: str,
        *,
        now: datetime,
        thread_id: str | None = None,
    ) -> AccessContext:
        context = AccessContext(
            principal_type="pm",
            principal_id=str(session["principal_id"]),
            engagement_id=engagement_id,
            stakeholder_id=None,
            interview_session_id=None,
            thread_id=thread_id,
            permissions=PM_PERMISSIONS,
            issued_at=self._parse_time(session["issued_at"]),
            expires_at=self._parse_time(session["expires_at"]),
            correlation_id=correlation_id,
        )
        context.require_active(now)
        return context

    async def _append_audit(
        self,
        connection: aiosqlite.Connection,
        *,
        engagement_id: str,
        actor: str,
        action: str,
        status: str,
        correlation_id: str,
        occurred_at: datetime,
        thread_id: str | None = None,
        failure_code: str | None = None,
    ) -> OperationalAuditEvent:
        event = OperationalAuditEvent.model_validate(
            {
                "event_id": self._id_factory("event"),
                "occurred_at": occurred_at,
                "engagement_id": engagement_id,
                "thread_id": thread_id,
                "actor": actor,
                "action": action,
                "status": status,
                "failure_code": failure_code,
                "correlation_id": correlation_id,
            }
        )
        await connection.execute(
            """
            INSERT INTO operational_audit_events(
                event_id, occurred_at, run_id, engagement_id, thread_id, actor,
                action, status, duration_ms, source_ids_json, evidence_ids_json,
                retry_count, failure_code, correlation_id
            ) VALUES (?, ?, NULL, ?, ?, ?, ?, ?, NULL, '[]', '[]', NULL, ?, ?)
            """,
            (
                event.event_id,
                self._time(event.occurred_at),
                event.engagement_id,
                event.thread_id,
                event.actor,
                event.action,
                event.status,
                event.failure_code,
                event.correlation_id,
            ),
        )
        return event

    @staticmethod
    async def _fetchone(
        connection: aiosqlite.Connection,
        query: str,
        parameters: tuple[object, ...],
    ) -> aiosqlite.Row | None:
        cursor = await connection.execute(query, parameters)
        return await cursor.fetchone()

    @staticmethod
    def _pm_access(row: aiosqlite.Row) -> PMAccess:
        return PMAccess(
            pm_access_id=row["pm_access_id"],
            token_hash=row["token_hash"],
            status=row["status"],
            created_at=AccessService._parse_time(row["created_at"]),
            revoked_at=AccessService._parse_optional_time(row["revoked_at"]),
        )

    @staticmethod
    def _engagement(row: aiosqlite.Row) -> Engagement:
        return Engagement(
            engagement_id=row["engagement_id"],
            name=row["name"],
            description=row["description"],
            status=row["status"],
            created_at=AccessService._parse_time(row["created_at"]),
            updated_at=AccessService._parse_time(row["updated_at"]),
        )

    @staticmethod
    def _stakeholder(row: aiosqlite.Row) -> Stakeholder:
        return Stakeholder(
            stakeholder_id=row["stakeholder_id"],
            engagement_id=row["engagement_id"],
            display_name=row["display_name"],
            role=row["role"],
            department=row["department"],
            status=row["status"],
            created_at=AccessService._parse_time(row["created_at"]),
            updated_at=AccessService._parse_time(row["updated_at"]),
        )

    @staticmethod
    def _interview(row: aiosqlite.Row) -> InterviewSession:
        return InterviewSession(
            interview_session_id=row["interview_session_id"],
            engagement_id=row["engagement_id"],
            stakeholder_id=row["stakeholder_id"],
            invitation_id=row["invitation_id"],
            thread_id=row["thread_id"],
            status=row["status"],
            started_at=AccessService._parse_time(row["started_at"]),
            finalized_at=AccessService._parse_optional_time(row["finalized_at"]),
            transcript_id=row["transcript_id"],
            ingestion_version_id=row["ingestion_version_id"],
            failure_code=row["failure_code"],
            failure_message=row["failure_message"],
        )

    def _recover_invitation_token(self, row: aiosqlite.Row) -> SecretStr:
        ciphertext = row["token_ciphertext"]
        if not isinstance(ciphertext, str) or not ciphertext:
            raise DomainConflictError
        try:
            return decrypt_invitation_token(ciphertext, self._settings.token_pepper)
        except ValueError as error:
            raise DomainPersistenceError from error

    @staticmethod
    def _invitation(row: aiosqlite.Row) -> InvitationToken:
        return InvitationToken(
            invitation_id=row["invitation_id"],
            engagement_id=row["engagement_id"],
            stakeholder_id=row["stakeholder_id"],
            token_hash=row["token_hash"],
            status=row["status"],
            created_at=AccessService._parse_time(row["created_at"]),
            expires_at=AccessService._parse_time(row["expires_at"]),
            activated_at=AccessService._parse_optional_time(row["activated_at"]),
            revoked_at=AccessService._parse_optional_time(row["revoked_at"]),
            created_by_pm_access_id=row["created_by_pm_access_id"],
        )

    @staticmethod
    def _audit_event(row: aiosqlite.Row) -> OperationalAuditEvent:
        return OperationalAuditEvent(
            event_id=row["event_id"],
            occurred_at=AccessService._parse_time(row["occurred_at"]),
            run_id=row["run_id"],
            engagement_id=row["engagement_id"],
            thread_id=row["thread_id"],
            actor=row["actor"],
            action=row["action"],
            status=row["status"],
            duration_ms=row["duration_ms"],
            source_ids=tuple(json.loads(row["source_ids_json"])),
            evidence_ids=tuple(json.loads(row["evidence_ids_json"])),
            retry_count=row["retry_count"],
            failure_code=row["failure_code"],
            correlation_id=row["correlation_id"],
        )

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise AccessClockError
        return value.astimezone(UTC)

    @staticmethod
    def _time(value: datetime) -> str:
        return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")

    @staticmethod
    def _parse_time(value: object) -> datetime:
        if not isinstance(value, str):
            raise DomainPersistenceError
        return datetime.fromisoformat(value).astimezone(UTC)

    @staticmethod
    def _parse_optional_time(value: object) -> datetime | None:
        return None if value is None else AccessService._parse_time(value)
