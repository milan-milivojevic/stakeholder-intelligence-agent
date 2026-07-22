"""Server-resolved access and graph runtime contexts."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal, Self

from pydantic import Field, model_validator

from stakeholder_intelligence_agent.contracts.common import (
    CanonicalModel,
    NonEmptyText,
    OpaqueId,
    Permission,
    ShortText,
    UtcDatetime,
)


class AccessContext(CanonicalModel):
    """Immutable authorization context created only by trusted server code."""

    principal_type: Literal["stakeholder", "pm"]
    principal_id: OpaqueId
    engagement_id: OpaqueId
    stakeholder_id: OpaqueId | None = None
    interview_session_id: OpaqueId | None = None
    thread_id: OpaqueId | None = None
    permissions: frozenset[Permission] = Field(min_length=1)
    issued_at: UtcDatetime
    expires_at: UtcDatetime | None = None
    correlation_id: OpaqueId

    @model_validator(mode="after")
    def validate_principal_scope(self) -> Self:
        """Enforce principal-specific scope and lifetime invariants."""
        if self.principal_type == "stakeholder" and self.stakeholder_id is None:
            raise ValueError("Stakeholder access requires stakeholder_id.")
        if self.principal_type == "stakeholder" and self.principal_id != self.stakeholder_id:
            raise ValueError("Stakeholder principal_id must match stakeholder_id.")
        if self.principal_type == "pm" and self.stakeholder_id is not None:
            raise ValueError("PM access must not carry stakeholder_id.")
        if self.expires_at is not None and self.expires_at <= self.issued_at:
            raise ValueError("expires_at must be later than issued_at.")
        return self

    def require_permission(self, permission: str) -> None:
        """Reject an operation that is absent from the server allowlist."""
        if permission not in self.permissions:
            raise PermissionError("The current access context does not permit this operation.")

    def require_active(self, at: datetime) -> None:
        """Reject an expired context without disclosing protected identifiers."""
        if at.tzinfo is None or at.utcoffset() is None:
            raise ValueError("Access checks require an aware timestamp.")
        if self.expires_at is not None and at.astimezone(UTC) >= self.expires_at:
            raise PermissionError("The current access context has expired.")


class InterviewRuntimeContext(CanonicalModel):
    """Trusted runtime dependencies for the stakeholder interview graph."""

    access: AccessContext
    role: ShortText | None = None
    department: ShortText | None = None

    @model_validator(mode="after")
    def validate_interview_access(self) -> Self:
        """Require a stakeholder interview scope and persistent thread."""
        if self.access.principal_type != "stakeholder":
            raise ValueError("Interview runtime requires stakeholder access.")
        if self.access.interview_session_id is None or self.access.thread_id is None:
            raise ValueError("Interview runtime requires interview_session_id and thread_id.")
        self.access.require_permission("interview:participate")
        return self


class InsightRuntimeContext(CanonicalModel):
    """Trusted runtime dependencies for one PM insight run."""

    access: AccessContext
    run_id: OpaqueId
    question: NonEmptyText

    @model_validator(mode="after")
    def validate_insight_access(self) -> Self:
        """Require PM scope, permission, and a persistent report thread."""
        if self.access.principal_type != "pm":
            raise ValueError("Insight runtime requires PM access.")
        if self.access.thread_id is None:
            raise ValueError("Insight runtime requires thread_id.")
        self.access.require_permission("insight:run")
        return self
