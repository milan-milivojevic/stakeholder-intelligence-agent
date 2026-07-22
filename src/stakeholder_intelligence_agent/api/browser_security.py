"""Same-origin browser-session transport without browser-owned authorization."""

from __future__ import annotations

from math import ceil
from typing import TYPE_CHECKING, Final, Literal

from stakeholder_intelligence_agent.errors import AccessDeniedError

if TYPE_CHECKING:
    from datetime import datetime

    from fastapi import Request, Response
    from pydantic import SecretStr

    from stakeholder_intelligence_agent.config import Settings

BrowserPrincipal = Literal["pm", "stakeholder"]
PM_BROWSER_SESSION_COOKIE: Final[str] = "stakeholder_ai_pm_session"
STAKEHOLDER_BROWSER_SESSION_COOKIE: Final[str] = "stakeholder_ai_interview_session"
LEGACY_BROWSER_SESSION_COOKIE: Final[str] = "stakeholder_browser_session"
BROWSER_SESSION_COOKIES: Final[frozenset[str]] = frozenset(
    {PM_BROWSER_SESSION_COOKIE, STAKEHOLDER_BROWSER_SESSION_COOKIE}
)
BROWSER_CSRF_HEADER: Final[str] = "X-Stakeholder-CSRF"
BROWSER_CSRF_VALUE: Final[str] = "1"
_MUTATION_METHODS: Final[frozenset[str]] = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_BROWSER_AUTH_PREFIX: Final[str] = "/api/v1/browser/auth/"
_SAFE_FETCH_SITES: Final[frozenset[str]] = frozenset({"none", "same-origin"})


def is_browser_mutation(request: Request) -> bool:
    """Identify browser activation or cookie-authenticated mutations before route execution."""
    if request.method.upper() not in _MUTATION_METHODS:
        return False
    return request.url.path.startswith(_BROWSER_AUTH_PREFIX) or bool(
        BROWSER_SESSION_COOKIES.intersection(request.cookies)
    )


def browser_session_cookie_name(principal: BrowserPrincipal) -> str:
    """Return the one cookie name authorized for the requested workspace principal."""
    return PM_BROWSER_SESSION_COOKIE if principal == "pm" else STAKEHOLDER_BROWSER_SESSION_COOKIE


def validate_browser_mutation(request: Request, settings: Settings) -> None:
    """Require the exact configured origin and the fixed non-simple CSRF header."""
    if request.headers.get("Origin") != settings.browser_origin:
        raise AccessDeniedError
    if request.headers.get(BROWSER_CSRF_HEADER) != BROWSER_CSRF_VALUE:
        raise AccessDeniedError
    fetch_site = request.headers.get("Sec-Fetch-Site")
    if fetch_site is not None and fetch_site not in _SAFE_FETCH_SITES:
        raise AccessDeniedError


def set_browser_session_cookie(  # noqa: PLR0913 -- explicit cookie scope and expiry inputs.
    response: Response,
    *,
    principal: BrowserPrincipal,
    token: SecretStr,
    expires_at: datetime,
    now: datetime,
    settings: Settings,
) -> None:
    """Set one principal-specific cookie without replacing the other workspace session."""
    max_age = max(0, ceil((expires_at - now).total_seconds()))
    response.set_cookie(
        key=browser_session_cookie_name(principal),
        value=token.get_secret_value(),
        max_age=max_age,
        expires=expires_at,
        path="/",
        secure=settings.browser_cookie_secure,
        httponly=True,
        samesite="strict",
    )
    response.delete_cookie(
        key=LEGACY_BROWSER_SESSION_COOKIE,
        path="/",
        secure=settings.browser_cookie_secure,
        httponly=True,
        samesite="strict",
    )


def clear_browser_session_cookie(
    response: Response,
    settings: Settings,
    *,
    principal: BrowserPrincipal,
) -> None:
    """Clear only the selected principal cookie using the same security attributes."""
    response.delete_cookie(
        key=browser_session_cookie_name(principal),
        path="/",
        secure=settings.browser_cookie_secure,
        httponly=True,
        samesite="strict",
    )
    response.delete_cookie(
        key=LEGACY_BROWSER_SESSION_COOKIE,
        path="/",
        secure=settings.browser_cookie_secure,
        httponly=True,
        samesite="strict",
    )
