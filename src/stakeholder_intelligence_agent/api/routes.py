"""Approved custom route inventory for the one LangGraph Agent Server."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, File, Header, Request, Response, UploadFile
from fastapi.responses import FileResponse, StreamingResponse

from stakeholder_intelligence_agent.api.browser_security import (
    BrowserPrincipal,
    browser_session_cookie_name,
    clear_browser_session_cookie,
    set_browser_session_cookie,
)
from stakeholder_intelligence_agent.api.read_repository import (
    DocumentProcessingRecord,
    DocumentSummaryRecord,
    FinalizedInterviewRecord,
    InterviewStatusRecord,
    InvitationSummaryRecord,
)
from stakeholder_intelligence_agent.api.runtime import ApplicationServices
from stakeholder_intelligence_agent.api.schemas import (
    BrowserSessionView,
    DocumentArtifactSummary,
    DocumentElementPreview,
    DocumentLifecycleEvent,
    DocumentListResponse,
    DocumentProcessingCount,
    DocumentProcessingDetailsResponse,
    DocumentSummary,
    EngagementContextResponse,
    EngagementCreateRequest,
    EngagementListResponse,
    EvidenceDrillDownResponse,
    InsightCreateRequest,
    InsightReportResponse,
    InsightRunListResponse,
    InsightStatusResponse,
    InterviewContextResponse,
    InterviewFinishResponse,
    InterviewHistoryTurn,
    InterviewPreviewResponse,
    InterviewSessionListResponse,
    InterviewStatusResponse,
    InterviewTurnRequest,
    InvitationIssuedResponse,
    InvitationLinkResponse,
    InvitationListResponse,
    InvitationSummary,
    OperationResponse,
    PMActivationRequest,
    SafeRunEvent,
    SessionTokenResponse,
    SourceArtifactSummary,
    StakeholderActivationRequest,
    StakeholderActivationResponse,
    StakeholderCreateRequest,
    StakeholderListResponse,
    StakeholderResponse,
    UploadResponse,
)
from stakeholder_intelligence_agent.contracts import (
    InsightRuntimeContext,
    InterviewRuntimeContext,
    InvitationToken,
)
from stakeholder_intelligence_agent.errors import (
    AccessDeniedError,
    DomainPersistenceError,
    InterviewCompletionNotReadyError,
    InterviewLifecycleError,
    ServiceNotReadyError,
    StakeholderIntelligenceError,
)
from stakeholder_intelligence_agent.interview.prompts import completion_is_recommended
from stakeholder_intelligence_agent.interview.types import (
    InterviewTokenChunk,
    TranscriptIngestionResult,
)

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from stakeholder_intelligence_agent.access import ResolvedAccessSession
    from stakeholder_intelligence_agent.access.tokens import IssuedBearerToken
    from stakeholder_intelligence_agent.contracts import AccessContext, InsightRun
    from stakeholder_intelligence_agent.ingestion.types import IngestionResult, StoredArtifact
    from stakeholder_intelligence_agent.retrieval.types import (
        SourceArtifactReference,
        SourceDrillDown,
    )

router = APIRouter(prefix="/api/v1")
RawAuthorizationHeader = Annotated[str | None, Header(alias="Authorization")]


async def _request_authorization(
    request: Request,
    authorization: RawAuthorizationHeader = None,
) -> str | None:
    """Resolve one unambiguous bearer transport while keeping authorization server-owned."""
    principal = _protected_route_principal(request.url.path)
    cookie_token = (
        None if principal is None else request.cookies.get(browser_session_cookie_name(principal))
    )
    if authorization is not None and cookie_token is not None:
        raise AccessDeniedError
    if cookie_token is None:
        return authorization
    return f"Bearer {cookie_token}"


def _protected_route_principal(path: str) -> BrowserPrincipal | None:
    if path.startswith("/api/v1/pm/"):
        return "pm"
    if path.startswith("/api/v1/stakeholder/"):
        return "stakeholder"
    return None


AuthorizationHeader = Annotated[str | None, Depends(_request_authorization)]


def _services(request: Request) -> ApplicationServices:
    services = getattr(request.app.state, "services", None)
    if not isinstance(services, ApplicationServices):
        raise ServiceNotReadyError
    return services


def _correlation_id(request: Request) -> str:
    value = getattr(request.state, "correlation_id", None)
    if not isinstance(value, str):
        raise ServiceNotReadyError
    return value


def _bearer(authorization: str | None) -> str:
    if authorization is None:
        raise AccessDeniedError
    scheme, separator, token = authorization.partition(" ")
    if separator != " " or scheme.lower() != "bearer" or not token or token.strip() != token:
        raise AccessDeniedError
    return token


async def _pm_access(  # noqa: PLR0913 -- explicit trusted scope inputs are intentional.
    services: ApplicationServices,
    token: str,
    engagement_id: str,
    request: Request,
    *,
    permission: str,
    thread_id: str | None = None,
) -> AccessContext:
    return await services.access.resolve_pm_context(
        token,
        engagement_id,
        correlation_id=_correlation_id(request),
        thread_id=thread_id,
        required_permission=permission,
    )


async def _stakeholder_access(
    services: ApplicationServices,
    token: str,
    request: Request,
    *,
    permission: str,
) -> AccessContext:
    return await services.access.resolve_stakeholder_context(
        token,
        correlation_id=_correlation_id(request),
        required_permission=permission,
    )


def _session_response(issued: IssuedBearerToken) -> SessionTokenResponse:
    return SessionTokenResponse(
        access_session_id=issued.access_session_id,
        access_token=issued.token.get_secret_value(),
        expires_at=issued.expires_at,
    )


def _browser_session_view(session: ResolvedAccessSession) -> BrowserSessionView:
    return BrowserSessionView(
        principal_type=session.principal_type,
        access_session_id=session.access_session_id,
        expires_at=session.expires_at,
        engagement_id=session.engagement_id,
        stakeholder_id=session.stakeholder_id,
        interview_session_id=session.interview_session_id,
        thread_id=session.thread_id,
    )


def _require_browser_cookie(request: Request, principal: BrowserPrincipal) -> str:
    if request.headers.get("Authorization") is not None:
        raise AccessDeniedError
    token = request.cookies.get(browser_session_cookie_name(principal))
    if token is None or not token or token.strip() != token:
        raise AccessDeniedError
    return token


def _require_browser_activation_without_bearer(request: Request) -> None:
    if request.headers.get("Authorization") is not None:
        raise AccessDeniedError


def _invitation_summary(
    record: InvitationSummaryRecord | InvitationToken,
) -> InvitationSummary:
    if isinstance(record, InvitationSummaryRecord):
        return InvitationSummary(
            invitation_id=record.invitation_id,
            engagement_id=record.engagement_id,
            stakeholder_id=record.stakeholder_id,
            status=record.status,
            created_at=record.created_at,
            expires_at=record.expires_at,
            activated_at=record.activated_at,
            revoked_at=record.revoked_at,
        )
    if isinstance(record, InvitationToken):
        return InvitationSummary(
            invitation_id=record.invitation_id,
            engagement_id=record.engagement_id,
            stakeholder_id=record.stakeholder_id,
            status=record.status,
            created_at=record.created_at,
            expires_at=record.expires_at,
            activated_at=record.activated_at,
            revoked_at=record.revoked_at,
        )
    raise AssertionError


def _document_summary(record: DocumentSummaryRecord) -> DocumentSummary:
    return DocumentSummary(source=record.source, latest_version=record.version)


def _upload_response(result: IngestionResult) -> UploadResponse:
    return UploadResponse(
        document=DocumentSummary(source=result.source, latest_version=result.version),
        element_count=len(result.elements),
        chunk_count=len(result.chunks),
        attempt_id=result.attempt_id,
        idempotent=result.idempotent,
    )


def _document_artifact_summary(
    engagement_id: str,
    document_id: str,
    artifact: StoredArtifact,
) -> DocumentArtifactSummary:
    return DocumentArtifactSummary(
        artifact_id=artifact.artifact_id,
        artifact_kind=artifact.artifact_kind,
        media_type=artifact.media_type,
        content_hash=artifact.content_hash,
        download_path=(
            f"/api/v1/pm/engagements/{engagement_id}/documents/{document_id}/"
            f"artifacts/{artifact.artifact_id}"
        ),
    )


def _document_processing_response(
    engagement_id: str,
    document_id: str,
    record: DocumentProcessingRecord,
) -> DocumentProcessingDetailsResponse:
    return DocumentProcessingDetailsResponse(
        document=_document_summary(record.summary),
        lifecycle_events=tuple(
            DocumentLifecycleEvent(
                event_id=item.event_id,
                from_state=item.from_state,
                to_state=item.to_state,
                occurred_at=item.occurred_at,
            )
            for item in record.lifecycle_events
        ),
        element_count=sum(count for _, count in record.element_counts),
        element_counts=tuple(
            DocumentProcessingCount(name=name, count=count) for name, count in record.element_counts
        ),
        chunk_count=record.chunk_count,
        artifact_count=sum(count for _, count in record.artifact_counts),
        artifact_counts=tuple(
            DocumentProcessingCount(name=name, count=count)
            for name, count in record.artifact_counts
        ),
        artifacts=tuple(
            _document_artifact_summary(engagement_id, document_id, artifact)
            for artifact in record.artifacts
        ),
        element_previews=tuple(
            DocumentElementPreview(
                element_id=item.element_id,
                document_version_id=item.document_version_id,
                element_type=item.element_type,
                location=item.location,
                extraction_method=item.extraction_method,
                content_preview=item.content_preview,
                english_interpretation=item.english_interpretation,
            )
            for item in record.element_previews
        ),
    )


def _parse_time(value: object) -> datetime:
    return datetime.fromisoformat(str(value)).astimezone(UTC)


def _json_ids(value: object) -> tuple[str, ...]:
    parsed = json.loads(str(value))
    if not isinstance(parsed, list) or any(not isinstance(item, str) for item in parsed):
        raise DomainPersistenceError
    return tuple(parsed)


def _safe_event(row: dict[str, object]) -> SafeRunEvent:
    return SafeRunEvent(
        event_id=str(row["event_id"]),
        occurred_at=_parse_time(row["occurred_at"]),
        actor=str(row["actor"]),
        action=str(row["action"]),
        from_status=None if row["from_status"] is None else str(row["from_status"]),
        to_status=None if row["to_status"] is None else str(row["to_status"]),
        topic_id=None if row["topic_id"] is None else str(row["topic_id"]),
        source_ids=_json_ids(row["source_ids_json"]),
        evidence_ids=_json_ids(row["evidence_ids_json"]),
        artifact_name=None if row["artifact_name"] is None else str(row["artifact_name"]),
        failure_code=None if row["failure_code"] is None else str(row["failure_code"]),
        correlation_id=str(row["correlation_id"]),
    )


def _sse(event: str, payload: dict[str, object]) -> str:
    serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event}\ndata: {serialized}\n\n"


async def _existing_insight_context(
    services: ApplicationServices,
    token: str,
    engagement_id: str,
    run_id: str,
    request: Request,
) -> tuple[InsightRuntimeContext, InsightRun]:
    base_access = await _pm_access(
        services,
        token,
        engagement_id,
        request,
        permission="insight:run",
    )
    run = await services.reads.insight_run(base_access, run_id, now=services.clock())
    access = await _pm_access(
        services,
        token,
        engagement_id,
        request,
        permission="insight:run",
        thread_id=run.thread_id,
    )
    return InsightRuntimeContext(
        access=access,
        run_id=run.run_id,
        question=run.requested_question,
    ), run


def _artifact_summary(
    engagement_id: str,
    run_id: str,
    evidence_id: str,
    reference: SourceArtifactReference,
) -> SourceArtifactSummary:
    downloadable = reference.artifact_kind != "raw_transcript"
    return SourceArtifactSummary(
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        media_type=reference.media_type,
        content_hash=reference.content_hash,
        download_path=(
            f"/api/v1/pm/engagements/{engagement_id}/insights/{run_id}/"
            f"evidence/{evidence_id}/"
            f"artifacts/{reference.artifact_id}"
            if downloadable
            else None
        ),
    )


async def _authorized_drill_down(
    services: ApplicationServices,
    context: InsightRuntimeContext,
    evidence_id: str,
) -> SourceDrillDown:
    result = await services.evidence.drill_down(
        context.access,
        evidence_id,
        now=services.clock(),
    )
    if result.evidence.run_id != context.run_id:
        raise AccessDeniedError
    return result


async def _execute_insight_background(
    services: ApplicationServices,
    context: InsightRuntimeContext,
) -> None:
    """Complete one persisted run while consuming every failure inside its lifecycle."""
    try:
        await services.insight.execute(context)
    except Exception:
        logger.exception(
            "Background insight execution failed",
            extra={
                "run_id": context.run_id,
                "correlation_id": context.access.correlation_id,
            },
        )
        return


@router.post("/auth/pm/activate", response_model=SessionTokenResponse)
async def activate_pm(request: Request, payload: PMActivationRequest) -> SessionTokenResponse:
    """Exchange the bootstrap secret for a bounded PM bearer session."""
    services = _services(request)
    issued = await services.access.activate_pm(payload.bootstrap_token)
    return _session_response(issued)


@router.post("/auth/stakeholder/activate", response_model=StakeholderActivationResponse)
async def activate_stakeholder(
    request: Request,
    payload: StakeholderActivationRequest,
) -> StakeholderActivationResponse:
    """Open or resume an invitation's fixed limited session mapping."""
    services = _services(request)
    activated = await services.access.activate_invitation(
        payload.invitation_token,
        correlation_id=_correlation_id(request),
    )
    session = activated.interview_session
    return StakeholderActivationResponse(
        session=_session_response(activated.access_session),
        engagement_id=session.engagement_id,
        stakeholder_id=session.stakeholder_id,
        interview_session_id=session.interview_session_id,
        thread_id=session.thread_id,
    )


@router.post("/auth/session/revoke", response_model=OperationResponse)
async def revoke_session(
    request: Request,
    authorization: AuthorizationHeader = None,
) -> OperationResponse:
    """Revoke the presented bearer without disclosing whether it existed."""
    services = _services(request)
    await services.access.revoke_session(_bearer(authorization))
    return OperationResponse()


@router.post("/browser/auth/pm/activate", response_model=BrowserSessionView)
async def activate_pm_browser(
    request: Request,
    response: Response,
    payload: PMActivationRequest,
) -> BrowserSessionView:
    """Exchange the PM bootstrap secret into a host-only HttpOnly browser cookie."""
    _require_browser_activation_without_bearer(request)
    services = _services(request)
    issued = await services.access.activate_pm(payload.bootstrap_token)
    set_browser_session_cookie(
        response,
        principal="pm",
        token=issued.token,
        expires_at=issued.expires_at,
        now=services.clock(),
        settings=services.settings,
    )
    return BrowserSessionView(
        principal_type="pm",
        access_session_id=issued.access_session_id,
        expires_at=issued.expires_at,
    )


@router.post("/browser/auth/stakeholder/activate", response_model=BrowserSessionView)
async def activate_stakeholder_browser(
    request: Request,
    response: Response,
    payload: StakeholderActivationRequest,
) -> BrowserSessionView:
    """Open or resume the invitation's fixed interview in a browser session."""
    _require_browser_activation_without_bearer(request)
    services = _services(request)
    activated = await services.access.activate_invitation(
        payload.invitation_token,
        correlation_id=_correlation_id(request),
    )
    issued = activated.access_session
    interview = activated.interview_session
    set_browser_session_cookie(
        response,
        principal="stakeholder",
        token=issued.token,
        expires_at=issued.expires_at,
        now=services.clock(),
        settings=services.settings,
    )
    return BrowserSessionView(
        principal_type="stakeholder",
        access_session_id=issued.access_session_id,
        expires_at=issued.expires_at,
        engagement_id=interview.engagement_id,
        stakeholder_id=interview.stakeholder_id,
        interview_session_id=interview.interview_session_id,
        thread_id=interview.thread_id,
    )


@router.get("/browser/auth/session", response_model=BrowserSessionView)
async def browser_session(
    request: Request,
    principal: BrowserPrincipal,
) -> BrowserSessionView:
    """Inspect the requested workspace cookie without returning its credential."""
    services = _services(request)
    session = await services.access.inspect_access_session(
        _require_browser_cookie(request, principal),
        correlation_id=_correlation_id(request),
    )
    if session.principal_type != principal:
        raise AccessDeniedError
    return _browser_session_view(session)


@router.post("/browser/auth/logout", response_model=OperationResponse)
async def logout_browser(
    request: Request,
    response: Response,
    principal: BrowserPrincipal,
) -> OperationResponse:
    """Revoke and clear only the requested workspace session."""
    services = _services(request)
    token = _require_browser_cookie(request, principal)
    session = await services.access.inspect_access_session(
        token,
        correlation_id=_correlation_id(request),
    )
    if session.principal_type != principal:
        raise AccessDeniedError
    await services.access.revoke_session(token)
    clear_browser_session_cookie(response, services.settings, principal=principal)
    return OperationResponse()


@router.get("/pm/engagements", response_model=EngagementListResponse)
async def list_engagements(
    request: Request,
    authorization: AuthorizationHeader = None,
) -> EngagementListResponse:
    services = _services(request)
    values = await services.access.list_engagements(_bearer(authorization))
    return EngagementListResponse(engagements=values)


@router.post("/pm/engagements", response_model=EngagementContextResponse, status_code=201)
async def create_engagement(
    request: Request,
    payload: EngagementCreateRequest,
    authorization: AuthorizationHeader = None,
) -> EngagementContextResponse:
    services = _services(request)
    engagement = await services.access.create_engagement(
        _bearer(authorization),
        name=payload.name,
        description=payload.description,
        correlation_id=_correlation_id(request),
    )
    return EngagementContextResponse(engagement=engagement)


@router.post(
    "/pm/engagements/{engagement_id}/select",
    response_model=EngagementContextResponse,
)
async def select_engagement(
    engagement_id: str,
    request: Request,
    authorization: AuthorizationHeader = None,
) -> EngagementContextResponse:
    services = _services(request)
    token = _bearer(authorization)
    access = await services.access.select_engagement(
        token,
        engagement_id,
        correlation_id=_correlation_id(request),
    )
    engagement = await services.reads.engagement(access, now=services.clock())
    return EngagementContextResponse(engagement=engagement)


@router.get(
    "/pm/engagements/{engagement_id}",
    response_model=EngagementContextResponse,
)
async def get_pm_engagement(
    engagement_id: str,
    request: Request,
    authorization: AuthorizationHeader = None,
) -> EngagementContextResponse:
    services = _services(request)
    access = await _pm_access(
        services,
        _bearer(authorization),
        engagement_id,
        request,
        permission="session:read",
    )
    return EngagementContextResponse(
        engagement=await services.reads.engagement(access, now=services.clock())
    )


@router.get(
    "/pm/engagements/{engagement_id}/stakeholders",
    response_model=StakeholderListResponse,
)
async def list_stakeholders(
    engagement_id: str,
    request: Request,
    authorization: AuthorizationHeader = None,
) -> StakeholderListResponse:
    services = _services(request)
    access = await _pm_access(
        services,
        _bearer(authorization),
        engagement_id,
        request,
        permission="session:read",
    )
    return StakeholderListResponse(
        stakeholders=await services.reads.stakeholders(access, now=services.clock())
    )


@router.post(
    "/pm/engagements/{engagement_id}/stakeholders",
    response_model=StakeholderResponse,
    status_code=201,
)
async def create_stakeholder(
    engagement_id: str,
    request: Request,
    payload: StakeholderCreateRequest,
    authorization: AuthorizationHeader = None,
) -> StakeholderResponse:
    services = _services(request)
    stakeholder = await services.access.create_stakeholder(
        _bearer(authorization),
        engagement_id,
        display_name=payload.display_name,
        role=payload.role,
        department=payload.department,
        correlation_id=_correlation_id(request),
    )
    return StakeholderResponse(stakeholder=stakeholder)


@router.get(
    "/pm/engagements/{engagement_id}/invitations",
    response_model=InvitationListResponse,
)
async def list_invitations(
    engagement_id: str,
    request: Request,
    authorization: AuthorizationHeader = None,
) -> InvitationListResponse:
    services = _services(request)
    access = await _pm_access(
        services,
        _bearer(authorization),
        engagement_id,
        request,
        permission="invitation:manage",
    )
    values = await services.reads.invitations(access, now=services.clock())
    return InvitationListResponse(invitations=tuple(_invitation_summary(item) for item in values))


@router.post(
    "/pm/engagements/{engagement_id}/stakeholders/{stakeholder_id}/invitations",
    response_model=InvitationIssuedResponse,
    status_code=201,
)
async def issue_invitation(
    engagement_id: str,
    stakeholder_id: str,
    request: Request,
    authorization: AuthorizationHeader = None,
) -> InvitationIssuedResponse:
    services = _services(request)
    issued = await services.access.issue_invitation(
        _bearer(authorization),
        engagement_id,
        stakeholder_id,
        correlation_id=_correlation_id(request),
    )
    return InvitationIssuedResponse(
        invitation=_invitation_summary(issued.invitation),
        invitation_token=issued.token.get_secret_value(),
    )


@router.get(
    "/pm/engagements/{engagement_id}/invitations/{invitation_id}/link",
    response_model=InvitationLinkResponse,
)
async def get_invitation_link(
    engagement_id: str,
    invitation_id: str,
    request: Request,
    authorization: AuthorizationHeader = None,
) -> InvitationLinkResponse:
    services = _services(request)
    issued = await services.access.get_invitation_link(
        _bearer(authorization),
        engagement_id,
        invitation_id,
        correlation_id=_correlation_id(request),
    )
    return InvitationLinkResponse(
        invitation=_invitation_summary(issued.invitation),
        invitation_token=issued.token.get_secret_value(),
    )


@router.delete(
    "/pm/engagements/{engagement_id}/invitations/{invitation_id}",
    response_model=InvitationSummary,
)
async def revoke_invitation(
    engagement_id: str,
    invitation_id: str,
    request: Request,
    authorization: AuthorizationHeader = None,
) -> InvitationSummary:
    services = _services(request)
    revoked = await services.access.revoke_invitation(
        _bearer(authorization),
        engagement_id,
        invitation_id,
        correlation_id=_correlation_id(request),
    )
    return _invitation_summary(revoked)


async def _read_upload(services: ApplicationServices, upload: UploadFile) -> bytes:
    try:
        return await upload.read(services.settings.max_upload_bytes + 1)
    finally:
        await upload.close()


@router.post(
    "/pm/engagements/{engagement_id}/documents",
    response_model=UploadResponse,
    status_code=201,
)
async def upload_pm_document(
    engagement_id: str,
    request: Request,
    upload: Annotated[UploadFile, File()],
    authorization: AuthorizationHeader = None,
) -> UploadResponse:
    services = _services(request)
    access = await _pm_access(
        services,
        _bearer(authorization),
        engagement_id,
        request,
        permission="document:upload",
    )
    content = await _read_upload(services, upload)
    result = await services.ingestion.ingest(
        access,
        filename=upload.filename or "upload",
        declared_media_type=upload.content_type or "application/octet-stream",
        content=content,
    )
    return _upload_response(result)


@router.get(
    "/pm/engagements/{engagement_id}/documents",
    response_model=DocumentListResponse,
)
async def list_pm_documents(
    engagement_id: str,
    request: Request,
    authorization: AuthorizationHeader = None,
) -> DocumentListResponse:
    services = _services(request)
    access = await _pm_access(
        services,
        _bearer(authorization),
        engagement_id,
        request,
        permission="source:read",
    )
    values = await services.reads.documents(access, now=services.clock())
    return DocumentListResponse(documents=tuple(_document_summary(item) for item in values))


@router.delete(
    "/pm/engagements/{engagement_id}/documents/{document_id}",
    response_model=OperationResponse,
)
async def delete_pm_document(
    engagement_id: str,
    document_id: str,
    request: Request,
    authorization: AuthorizationHeader = None,
) -> OperationResponse:
    """Withdraw one PM-owned engagement document without touching stakeholder evidence."""
    services = _services(request)
    access = await _pm_access(
        services,
        _bearer(authorization),
        engagement_id,
        request,
        permission="document:upload",
    )
    await services.ingestion.delete_pm_document(access, document_id)
    return OperationResponse()


@router.get(
    "/pm/engagements/{engagement_id}/documents/{document_id}",
    response_model=DocumentSummary,
)
async def get_pm_document(
    engagement_id: str,
    document_id: str,
    request: Request,
    authorization: AuthorizationHeader = None,
) -> DocumentSummary:
    services = _services(request)
    access = await _pm_access(
        services,
        _bearer(authorization),
        engagement_id,
        request,
        permission="source:read",
    )
    return _document_summary(
        await services.reads.document(access, document_id, now=services.clock())
    )


@router.get(
    "/pm/engagements/{engagement_id}/documents/{document_id}/processing",
    response_model=DocumentProcessingDetailsResponse,
)
async def get_pm_document_processing(
    engagement_id: str,
    document_id: str,
    request: Request,
    authorization: AuthorizationHeader = None,
) -> DocumentProcessingDetailsResponse:
    services = _services(request)
    access = await _pm_access(
        services,
        _bearer(authorization),
        engagement_id,
        request,
        permission="source:read",
    )
    record = await services.reads.document_processing(
        access,
        document_id,
        now=services.clock(),
    )
    return _document_processing_response(engagement_id, document_id, record)


@router.get(
    "/pm/engagements/{engagement_id}/documents/{document_id}/artifacts/{artifact_id}",
    response_model=None,
)
async def get_pm_document_artifact(
    engagement_id: str,
    document_id: str,
    artifact_id: str,
    request: Request,
    authorization: AuthorizationHeader = None,
) -> FileResponse:
    services = _services(request)
    access = await _pm_access(
        services,
        _bearer(authorization),
        engagement_id,
        request,
        permission="source:read",
    )
    record = await services.reads.document_processing(
        access,
        document_id,
        now=services.clock(),
    )
    artifact = next((item for item in record.artifacts if item.artifact_id == artifact_id), None)
    if artifact is None:
        raise AccessDeniedError
    path = await asyncio.to_thread(
        services.source_artifacts.resolve_virtual,
        access,
        artifact.virtual_path,
    )
    filename = (
        record.summary.source.original_filename
        if artifact.artifact_kind == "original"
        else Path(artifact.virtual_path).name
    )
    return FileResponse(
        path,
        media_type=artifact.media_type,
        filename=filename,
        content_disposition_type="inline",
        headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": "default-src 'none'; frame-ancestors 'self'",
            "X-Frame-Options": "SAMEORIGIN",
        },
    )


@router.get(
    "/pm/engagements/{engagement_id}/interviews",
    response_model=InterviewSessionListResponse,
)
async def list_pm_interviews(
    engagement_id: str,
    request: Request,
    authorization: AuthorizationHeader = None,
) -> InterviewSessionListResponse:
    services = _services(request)
    access = await _pm_access(
        services,
        _bearer(authorization),
        engagement_id,
        request,
        permission="session:read",
    )
    return InterviewSessionListResponse(
        interview_sessions=await services.reads.interview_sessions(
            access,
            now=services.clock(),
        )
    )


def _interview_history_turns(
    record: InterviewStatusRecord | FinalizedInterviewRecord,
) -> tuple[InterviewHistoryTurn, ...]:
    return tuple(
        InterviewHistoryTurn(
            turn_index=turn.turn_index,
            speaker=turn.speaker,
            text=turn.original_text,
        )
        for turn in record.turns
    )


@router.get(
    "/pm/engagements/{engagement_id}/interviews/{interview_session_id}",
    response_model=InterviewPreviewResponse,
)
async def preview_pm_interview(
    engagement_id: str,
    interview_session_id: str,
    request: Request,
    authorization: AuthorizationHeader = None,
) -> InterviewPreviewResponse:
    services = _services(request)
    access = await _pm_access(
        services,
        _bearer(authorization),
        engagement_id,
        request,
        permission="session:read",
    )
    record = await services.reads.finalized_interview(
        access,
        interview_session_id,
        now=services.clock(),
    )
    return InterviewPreviewResponse(
        interview_session=record.session,
        transcript=record.transcript,
        turns=_interview_history_turns(record),
    )


@router.get("/stakeholder/context", response_model=InterviewContextResponse)
async def stakeholder_context(
    request: Request,
    authorization: AuthorizationHeader = None,
) -> InterviewContextResponse:
    services = _services(request)
    access = await _stakeholder_access(
        services,
        _bearer(authorization),
        request,
        permission="interview:participate",
    )
    profile = await services.reads.stakeholder_profile(access, now=services.clock())
    sessions = await services.reads.interview_sessions(access, now=services.clock())
    if len(sessions) != 1:
        raise AccessDeniedError
    return InterviewContextResponse(
        engagement=await services.reads.engagement(access, now=services.clock()),
        stakeholder=profile,
        interview_session=sessions[0],
    )


@router.post("/stakeholder/documents", response_model=UploadResponse, status_code=201)
async def upload_stakeholder_document(
    request: Request,
    upload: Annotated[UploadFile, File()],
    authorization: AuthorizationHeader = None,
) -> UploadResponse:
    services = _services(request)
    access = await _stakeholder_access(
        services,
        _bearer(authorization),
        request,
        permission="document:upload",
    )
    content = await _read_upload(services, upload)
    result = await services.ingestion.ingest(
        access,
        filename=upload.filename or "upload",
        declared_media_type=upload.content_type or "application/octet-stream",
        content=content,
    )
    return _upload_response(result)


@router.get("/stakeholder/documents", response_model=DocumentListResponse)
async def list_stakeholder_documents(
    request: Request,
    authorization: AuthorizationHeader = None,
) -> DocumentListResponse:
    services = _services(request)
    access = await _stakeholder_access(
        services,
        _bearer(authorization),
        request,
        permission="source:read",
    )
    values = await services.reads.documents(access, now=services.clock())
    return DocumentListResponse(documents=tuple(_document_summary(item) for item in values))


@router.delete(
    "/stakeholder/documents/{document_id}",
    response_model=OperationResponse,
)
async def delete_stakeholder_document(
    request: Request,
    document_id: str,
    authorization: AuthorizationHeader = None,
) -> OperationResponse:
    """Withdraw one owned supporting document before interview finalization."""
    services = _services(request)
    access = await _stakeholder_access(
        services,
        _bearer(authorization),
        request,
        permission="document:upload",
    )
    await services.ingestion.delete_stakeholder_document(access, document_id)
    return OperationResponse()


@router.post("/stakeholder/interview/turns/stream")
async def stream_interview_turn(
    request: Request,
    payload: InterviewTurnRequest,
    authorization: AuthorizationHeader = None,
) -> StreamingResponse:
    """Stream safe assistant text deltas and the completed persisted turn."""
    services = _services(request)
    access = await _stakeholder_access(
        services,
        _bearer(authorization),
        request,
        permission="interview:participate",
    )
    profile = await services.reads.stakeholder_profile(access, now=services.clock())
    context = InterviewRuntimeContext(
        access=access,
        role=profile.role,
        department=profile.department,
    )
    message_id = payload.message_id or services.id_factory("message")
    correlation_id = _correlation_id(request)

    async def generate() -> AsyncIterator[str]:
        yield _sse(
            "status",
            {
                "stage": "interview",
                "status": "started",
                "message_id": message_id,
                "correlation_id": correlation_id,
            },
        )
        try:
            result = None
            async for item in services.interview.stream_turn(
                context,
                original_text=payload.original_text,
                request_message_id=message_id,
            ):
                if isinstance(item, InterviewTokenChunk):
                    yield _sse(
                        "token",
                        {
                            "message_id": message_id,
                            "sequence": item.sequence,
                            "delta": item.delta,
                            "correlation_id": correlation_id,
                        },
                    )
                else:
                    result = item
            if result is None:
                raise InterviewLifecycleError  # noqa: TRY301
            yield _sse(
                "message",
                {
                    "message_id": message_id,
                    "stakeholder_turn_index": result.stakeholder_turn.value.turn_index,
                    "assistant_turn_index": result.assistant_turn.value.turn_index,
                    "assistant_text": result.assistant_text,
                    "correlation_id": correlation_id,
                },
            )
            yield _sse(
                "status",
                {
                    "stage": "interview",
                    "status": "succeeded",
                    "message_id": message_id,
                    "correlation_id": correlation_id,
                },
            )
        except Exception as error:  # noqa: BLE001 -- the open SSE must emit a safe failure.
            safe = isinstance(error, StakeholderIntelligenceError)
            code = getattr(error, "code", "INTERVIEW_EXECUTION_FAILED")
            if not isinstance(code, str):
                code = "INTERVIEW_EXECUTION_FAILED"
            yield _sse(
                "failure",
                {
                    "stage": "interview",
                    "status": "failed",
                    "failure_code": code,
                    "failure_message": (
                        str(error) if safe else "The interview response could not be completed."
                    ),
                    "correlation_id": correlation_id,
                },
            )

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


def _interview_status_response(status: InterviewStatusRecord) -> InterviewStatusResponse:
    """Project one freshly authorized interview status for the stakeholder UI."""
    return InterviewStatusResponse(
        interview_session=status.session,
        transcript=status.transcript,
        ingestion_version=status.version,
        turns=_interview_history_turns(status),
        turn_count=status.turn_count,
        completion_recommended=completion_is_recommended(
            turn.original_text for turn in status.turns if turn.speaker == "assistant"
        ),
    )


@router.post("/stakeholder/interview/start", response_model=InterviewStatusResponse)
async def start_interview(
    request: Request,
    authorization: AuthorizationHeader = None,
) -> InterviewStatusResponse:
    """Create one idempotent, client-friendly opening question."""
    services = _services(request)
    access = await _stakeholder_access(
        services,
        _bearer(authorization),
        request,
        permission="interview:participate",
    )
    status = await services.reads.interview_status(access, now=services.clock())
    if status.session.status == "draft" and not status.turns:
        profile = await services.reads.stakeholder_profile(access, now=services.clock())
        await services.interview.start(
            InterviewRuntimeContext(
                access=access,
                role=profile.role,
                department=profile.department,
            )
        )
        status = await services.reads.interview_status(access, now=services.clock())
    return _interview_status_response(status)


@router.get("/stakeholder/interview/status", response_model=InterviewStatusResponse)
async def stakeholder_interview_status(
    request: Request,
    authorization: AuthorizationHeader = None,
) -> InterviewStatusResponse:
    services = _services(request)
    access = await _stakeholder_access(
        services,
        _bearer(authorization),
        request,
        permission="interview:participate",
    )
    status = await services.reads.interview_status(access, now=services.clock())
    return _interview_status_response(status)


@router.delete(
    "/stakeholder/interview/turns/{turn_index}",
    response_model=InterviewStatusResponse,
)
async def delete_interview_answer(
    request: Request,
    turn_index: int,
    authorization: AuthorizationHeader = None,
) -> InterviewStatusResponse:
    """Delete one stakeholder answer and every downstream draft turn."""
    services = _services(request)
    access = await _stakeholder_access(
        services,
        _bearer(authorization),
        request,
        permission="interview:participate",
    )
    profile = await services.reads.stakeholder_profile(access, now=services.clock())
    await services.interview.delete_answer(
        InterviewRuntimeContext(
            access=access,
            role=profile.role,
            department=profile.department,
        ),
        turn_index=turn_index,
    )
    status = await services.reads.interview_status(access, now=services.clock())
    return _interview_status_response(status)


@router.post("/stakeholder/interview/finish", response_model=InterviewFinishResponse)
async def finish_interview(
    request: Request,
    authorization: AuthorizationHeader = None,
) -> InterviewFinishResponse:
    services = _services(request)
    access = await _stakeholder_access(
        services,
        _bearer(authorization),
        request,
        permission="interview:finalize",
    )
    status = await services.reads.interview_status(access, now=services.clock())
    if status.session.status == "draft" and not completion_is_recommended(
        turn.original_text for turn in status.turns if turn.speaker == "assistant"
    ):
        raise InterviewCompletionNotReadyError
    profile = await services.reads.stakeholder_profile(access, now=services.clock())
    context = InterviewRuntimeContext(
        access=access,
        role=profile.role,
        department=profile.department,
    )
    result = await services.interview.finish(context)
    chunk_count = len(result.chunks) if isinstance(result, TranscriptIngestionResult) else 0
    return InterviewFinishResponse(
        interview_session=result.session,
        transcript=result.snapshot.transcript,
        ingestion_version=result.version,
        chunk_count=chunk_count,
        idempotent=result.idempotent,
    )


@router.post(
    "/pm/engagements/{engagement_id}/insights",
    response_model=InsightStatusResponse,
    status_code=202,
)
async def execute_insight(
    engagement_id: str,
    request: Request,
    payload: InsightCreateRequest,
    background_tasks: BackgroundTasks,
    authorization: AuthorizationHeader = None,
) -> InsightStatusResponse:
    services = _services(request)
    token = _bearer(authorization)
    run_id = services.id_factory("run")
    thread_id = services.id_factory("report-thread")
    access = await _pm_access(
        services,
        token,
        engagement_id,
        request,
        permission="insight:run",
        thread_id=thread_id,
    )
    context = InsightRuntimeContext(access=access, run_id=run_id, question=payload.question)
    run = await services.insight_runs.start(context, now=services.clock())
    if run.run_id == run_id:
        background_tasks.add_task(_execute_insight_background, services, context)
    return InsightStatusResponse(run=run)


@router.get(
    "/pm/engagements/{engagement_id}/insights",
    response_model=InsightRunListResponse,
)
async def insight_history(
    engagement_id: str,
    request: Request,
    authorization: AuthorizationHeader = None,
) -> InsightRunListResponse:
    services = _services(request)
    access = await _pm_access(
        services,
        _bearer(authorization),
        engagement_id,
        request,
        permission="insight:run",
    )
    runs = await services.reads.insight_runs(access, now=services.clock())
    return InsightRunListResponse(runs=runs)


@router.get(
    "/pm/engagements/{engagement_id}/insights/{run_id}",
    response_model=InsightStatusResponse,
)
async def insight_status(
    engagement_id: str,
    run_id: str,
    request: Request,
    authorization: AuthorizationHeader = None,
) -> InsightStatusResponse:
    services = _services(request)
    _, run = await _existing_insight_context(
        services,
        _bearer(authorization),
        engagement_id,
        run_id,
        request,
    )
    return InsightStatusResponse(run=run)


@router.get(
    "/pm/engagements/{engagement_id}/insights/{run_id}/report",
    response_model=InsightReportResponse,
)
async def insight_report(
    engagement_id: str,
    run_id: str,
    request: Request,
    authorization: AuthorizationHeader = None,
) -> InsightReportResponse:
    services = _services(request)
    context, _ = await _existing_insight_context(
        services,
        _bearer(authorization),
        engagement_id,
        run_id,
        request,
    )
    run, report, metrics = await services.insight.load_report(context)
    return InsightReportResponse(run=run, report=report, metrics=metrics)


@router.get(
    "/pm/engagements/{engagement_id}/insights/{run_id}/events",
    response_model=None,
)
async def insight_events(
    engagement_id: str,
    run_id: str,
    request: Request,
    authorization: AuthorizationHeader = None,
) -> StreamingResponse:
    services = _services(request)
    context, _ = await _existing_insight_context(
        services,
        _bearer(authorization),
        engagement_id,
        run_id,
        request,
    )

    async def generate() -> AsyncIterator[str]:
        emitted = 0
        deadline = monotonic() + (
            services.settings.insight_run_timeout_seconds
            + services.settings.provider_timeout_seconds
        )
        terminal = {"complete", "partial", "insufficient_evidence", "failed"}
        while monotonic() < deadline and not await request.is_disconnected():
            try:
                rows = await services.insight_runs.events(context, now=services.clock())
                for row in rows[emitted:]:
                    yield _sse("progress", _safe_event(row).model_dump(mode="json"))
                emitted = len(rows)
                run = await services.insight_runs.load(context, now=services.clock())
            except StakeholderIntelligenceError as error:
                code = getattr(error, "code", "EVENT_STREAM_FAILED")
                if not isinstance(code, str):
                    code = "EVENT_STREAM_FAILED"
                yield _sse(
                    "failure",
                    {
                        "status": "failed",
                        "failure_code": code,
                        "failure_message": str(error),
                        "correlation_id": context.access.correlation_id,
                    },
                )
                return
            if run.status in terminal:
                return
            await asyncio.sleep(0.25)
        yield _sse(
            "failure",
            {
                "status": "failed",
                "failure_code": "EVENT_STREAM_TIMEOUT",
                "failure_message": "The progress stream reached its bounded time limit.",
                "correlation_id": context.access.correlation_id,
            },
        )

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@router.get(
    "/pm/engagements/{engagement_id}/insights/{run_id}/evidence/{evidence_id}",
    response_model=EvidenceDrillDownResponse,
)
async def evidence_drill_down(
    engagement_id: str,
    run_id: str,
    evidence_id: str,
    request: Request,
    authorization: AuthorizationHeader = None,
) -> EvidenceDrillDownResponse:
    services = _services(request)
    context, _ = await _existing_insight_context(
        services,
        _bearer(authorization),
        engagement_id,
        run_id,
        request,
    )
    result = await _authorized_drill_down(services, context, evidence_id)
    return EvidenceDrillDownResponse(
        evidence=result.evidence,
        original=_artifact_summary(engagement_id, run_id, evidence_id, result.original),
        related_artifacts=tuple(
            _artifact_summary(engagement_id, run_id, evidence_id, item)
            for item in result.related_artifacts
        ),
    )


@router.get(
    "/pm/engagements/{engagement_id}/insights/{run_id}/evidence/"
    "{evidence_id}/artifacts/{artifact_id}",
    response_model=None,
)
async def evidence_artifact(  # noqa: PLR0913 -- every path ID is independently checked.
    engagement_id: str,
    run_id: str,
    evidence_id: str,
    artifact_id: str,
    request: Request,
    authorization: AuthorizationHeader = None,
) -> FileResponse:
    services = _services(request)
    context, _ = await _existing_insight_context(
        services,
        _bearer(authorization),
        engagement_id,
        run_id,
        request,
    )
    result = await _authorized_drill_down(services, context, evidence_id)
    references = (result.original, *result.related_artifacts)
    reference = next((item for item in references if item.artifact_id == artifact_id), None)
    if reference is None or reference.artifact_kind == "raw_transcript":
        raise AccessDeniedError
    path = services.source_artifacts.resolve_virtual(
        context.access,
        reference.virtual_path,
    )
    return FileResponse(
        path,
        media_type=reference.media_type,
        filename=Path(reference.virtual_path).name,
        headers={"Cache-Control": "no-store"},
    )
