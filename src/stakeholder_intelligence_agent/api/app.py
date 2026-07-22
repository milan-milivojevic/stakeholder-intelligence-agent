"""Safe custom application lifecycle and routes for the one Agent Server process."""

import re
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Literal

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from starlette.responses import Response

from stakeholder_intelligence_agent.access.tokens import generate_opaque_id
from stakeholder_intelligence_agent.api.browser_security import (
    is_browser_mutation,
    validate_browser_mutation,
)
from stakeholder_intelligence_agent.api.routes import router
from stakeholder_intelligence_agent.api.runtime import ApplicationServices
from stakeholder_intelligence_agent.api.schemas import ApiErrorDetail, ApiErrorResponse
from stakeholder_intelligence_agent.api.spa import install_spa_routes
from stakeholder_intelligence_agent.errors import (
    AccessDeniedError,
    DomainConflictError,
    EvidenceRegistrationError,
    IngestionInProgressError,
    InterviewCompletionNotReadyError,
    ServiceNotReadyError,
    StakeholderIntelligenceError,
    TranscriptImmutableError,
    UploadSizeError,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from stakeholder_intelligence_agent.access import AccessService

_CORRELATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self'; "
    "img-src 'self' data: blob:; connect-src 'self'; font-src 'self'; "
    "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
)


def _apply_security_headers(request: Request, response: Response) -> Response:
    """Apply the same browser hardening to successes and every safe failure response."""
    if request.url.path.startswith("/assets/"):
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    else:
        response.headers.setdefault("Cache-Control", "no-store")
    response.headers.setdefault("Content-Security-Policy", _CONTENT_SECURITY_POLICY)
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    return response


class HealthResponse(BaseModel):
    """Non-sensitive process health response."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["ok"] = "ok"
    backend: Literal["langgraph_agent_server"] = "langgraph_agent_server"
    graphs: tuple[Literal["interview", "insight"], ...] = ("interview", "insight")


def create_app(
    access_service: "AccessService | None" = None,
    *,
    services: ApplicationServices | None = None,
) -> FastAPI:
    """Build the custom app and migrate domain persistence before serving."""

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> "AsyncIterator[None]":
        if services is not None:
            await services.initialize()
            application.state.access_service = services.access
            application.state.services = services
            application.state.settings = services.settings
            yield
            return
        if access_service is not None:
            await access_service.initialize()
            application.state.access_service = access_service
            application.state.services = None
            application.state.settings = None
            yield
            return
        from stakeholder_intelligence_agent.api.dependencies import (  # noqa: PLC0415
            open_application_services,
        )

        async with open_application_services() as runtime:
            application.state.access_service = runtime.access
            application.state.services = runtime
            application.state.settings = runtime.settings
            yield

    application = FastAPI(
        title="Stakeholder Intelligence Agent custom routes",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    @application.get("/api/v1/system/health")
    async def health() -> HealthResponse:
        """Report process health without claiming application readiness."""
        return HealthResponse()

    application.include_router(router)
    install_spa_routes(application)

    @application.middleware("http")
    async def correlation_middleware(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        """Attach one safe correlation ID without retaining credentials or content."""
        supplied = request.headers.get("X-Correlation-ID")
        correlation_id = (
            supplied
            if supplied is not None and _CORRELATION_ID.fullmatch(supplied)
            else generate_opaque_id("correlation")
        )
        request.state.correlation_id = correlation_id
        settings = getattr(request.app.state, "settings", None)
        if is_browser_mutation(request) and settings is not None:
            try:
                validate_browser_mutation(request, settings)
            except AccessDeniedError:
                return _apply_security_headers(
                    request,
                    error_response(
                        request,
                        status_code=403,
                        code="ACCESS_DENIED",
                        message="Access is not authorized.",
                    ),
                )
        response = await call_next(request)
        response.headers["X-Correlation-ID"] = correlation_id
        return _apply_security_headers(request, response)

    def error_response(
        request: Request,
        *,
        status_code: int,
        code: str,
        message: str,
    ) -> JSONResponse:
        correlation_id = getattr(request.state, "correlation_id", None)
        if not isinstance(correlation_id, str):
            correlation_id = generate_opaque_id("correlation")
        payload = ApiErrorResponse(
            error=ApiErrorDetail(
                code=code,
                message=message,
                correlation_id=correlation_id,
            )
        )
        return JSONResponse(
            status_code=status_code,
            content=payload.model_dump(mode="json"),
            headers={"Cache-Control": "no-store", "X-Correlation-ID": correlation_id},
        )

    @application.exception_handler(RequestValidationError)
    async def validation_error(request: Request, _: RequestValidationError) -> JSONResponse:
        """Return no submitted values, including malformed secrets, in validation errors."""
        return error_response(
            request,
            status_code=422,
            code="VALIDATION_FAILED",
            message="The request could not be validated.",
        )

    @application.exception_handler(StakeholderIntelligenceError)
    async def domain_error(
        request: Request,
        error: StakeholderIntelligenceError,
    ) -> JSONResponse:
        """Map safe domain failures without protected existence or internals."""
        if isinstance(error, AccessDeniedError):
            return error_response(
                request,
                status_code=403,
                code="ACCESS_DENIED",
                message="Access is not authorized.",
            )
        if isinstance(error, ServiceNotReadyError):
            return error_response(
                request,
                status_code=503,
                code=error.code,
                message=str(error),
            )
        if isinstance(error, EvidenceRegistrationError):
            return error_response(
                request,
                status_code=404,
                code="SOURCE_UNAVAILABLE",
                message="The requested source is not available.",
            )
        if isinstance(error, UploadSizeError):
            status_code = 413
        elif isinstance(
            error,
            (
                DomainConflictError,
                IngestionInProgressError,
                InterviewCompletionNotReadyError,
                TranscriptImmutableError,
            ),
        ):
            status_code = 409
        else:
            status_code = 400
        code = getattr(error, "code", "OPERATION_FAILED")
        if not isinstance(code, str):
            code = "OPERATION_FAILED"
        return error_response(
            request,
            status_code=status_code,
            code=code,
            message=str(error),
        )

    @application.exception_handler(Exception)
    async def unexpected_error(request: Request, _: Exception) -> JSONResponse:
        """Hide provider, database, model, path, and stack details."""
        return error_response(
            request,
            status_code=500,
            code="INTERNAL_ERROR",
            message="The operation could not be completed.",
        )

    return application


app = create_app()
