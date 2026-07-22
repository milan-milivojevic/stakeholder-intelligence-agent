"""Resolve one persisted stakeholder session in a fresh Python process."""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime

from pydantic import BaseModel, ConfigDict, SecretStr

from stakeholder_intelligence_agent.access import AccessService
from stakeholder_intelligence_agent.config import Settings
from stakeholder_intelligence_agent.persistence import DomainDatabase


class ProbeInput(BaseModel):
    """Secret-bearing input read only from the child process standard input."""

    model_config = ConfigDict(extra="forbid")

    settings: dict[str, object]
    session_token: SecretStr
    engagement_id: str
    interview_session_id: str
    thread_id: str
    clock_at: str


async def resolve(payload: ProbeInput) -> dict[str, str]:
    """Reopen domain persistence and return only safe resolved identities."""
    settings = Settings.model_validate(payload.settings)
    service = AccessService(
        DomainDatabase(settings.domain_database),
        settings,
        clock=lambda: datetime.fromisoformat(payload.clock_at),
    )
    await service.initialize()
    context = await service.resolve_stakeholder_context(
        payload.session_token.get_secret_value(),
        correlation_id="child-process-restart",
        requested_engagement_id=payload.engagement_id,
        requested_interview_session_id=payload.interview_session_id,
        requested_thread_id=payload.thread_id,
    )
    return {
        "engagement_id": context.engagement_id,
        "stakeholder_id": context.stakeholder_id or "",
        "interview_session_id": context.interview_session_id or "",
        "thread_id": context.thread_id or "",
    }


def main() -> None:
    """Read one request from stdin and emit no credential material."""
    payload = ProbeInput.model_validate_json(sys.stdin.read())
    sys.stdout.write(json.dumps(asyncio.run(resolve(payload)), sort_keys=True))


if __name__ == "__main__":
    main()
