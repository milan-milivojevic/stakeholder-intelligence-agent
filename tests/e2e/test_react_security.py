"""Isolated real-Chromium verification for the React browser-session boundary."""

from __future__ import annotations

import json
import secrets
from typing import TYPE_CHECKING, Any, cast

import httpx
import pytest
from playwright.sync_api import (
    Browser,
    Error,
    Page,
    Request,
    expect,
)
from playwright.sync_api import (
    TimeoutError as PlaywrightTimeoutError,
)

if TYPE_CHECKING:
    from playwright.sync_api import BrowserContext, Cookie

    from tests.e2e.conftest import BrowserRuntime

pytestmark = [pytest.mark.e2e, pytest.mark.integration, pytest.mark.security]
PM_BROWSER_COOKIE = "stakeholder_ai_pm_session"
STAKEHOLDER_BROWSER_COOKIE = "stakeholder_ai_interview_session"
CSRF_HEADER = "X-Stakeholder-CSRF"
NORMAL_TIMEOUT_MS = 60_000


def _context(browser: Browser) -> BrowserContext:
    return browser.new_context(
        viewport={"width": 1280, "height": 900},
        locale="en-US",
        accept_downloads=False,
    )


def _configure(page: Page) -> None:
    page.set_default_timeout(NORMAL_TIMEOUT_MS)
    page.set_default_navigation_timeout(NORMAL_TIMEOUT_MS)


def _json(response: httpx.Response) -> dict[str, Any]:
    return cast("dict[str, Any]", response.json())


def _issue_invitation(runtime: BrowserRuntime, label: str) -> tuple[str, str, str, str]:
    with httpx.Client(base_url=runtime.agent_url, timeout=30.0) as client:
        activated = client.post(
            "/api/v1/auth/pm/activate",
            json={"bootstrap_token": runtime.pm_bootstrap_secret},
            headers={"X-Correlation-ID": f"{label}-pm-activation"},
        )
        assert activated.status_code == 200
        bearer = str(_json(activated)["access_token"])
        runtime.track_secret(bearer)
        authorization = {"Authorization": f"Bearer {bearer}"}
        engagement = client.post(
            "/api/v1/pm/engagements",
            json={"name": f"{label} engagement", "description": "Browser security verification."},
            headers=authorization,
        )
        assert engagement.status_code == 201
        engagement_id = str(_json(engagement)["engagement"]["engagement_id"])
        stakeholder = client.post(
            f"/api/v1/pm/engagements/{engagement_id}/stakeholders",
            json={
                "display_name": f"{label} stakeholder",
                "role": "Operations lead",
                "department": "Operations",
            },
            headers=authorization,
        )
        assert stakeholder.status_code == 201
        stakeholder_id = str(_json(stakeholder)["stakeholder"]["stakeholder_id"])
        invitation = client.post(
            (f"/api/v1/pm/engagements/{engagement_id}/stakeholders/{stakeholder_id}/invitations"),
            headers=authorization,
        )
        assert invitation.status_code == 201
        invitation_body = _json(invitation)
        invitation_id = str(invitation_body["invitation"]["invitation_id"])
        invitation_token = str(invitation_body["invitation_token"])
        runtime.track_secret(invitation_token)
        return bearer, engagement_id, invitation_id, invitation_token


def _revoke_invitation(
    runtime: BrowserRuntime,
    *,
    bearer: str,
    engagement_id: str,
    invitation_id: str,
) -> None:
    response = httpx.delete(
        f"{runtime.agent_url}/api/v1/pm/engagements/{engagement_id}/invitations/{invitation_id}",
        headers={"Authorization": f"Bearer {bearer}"},
        timeout=30.0,
    )
    assert response.status_code == 200


def _session_cookie(
    context: BrowserContext,
    runtime: BrowserRuntime,
    *,
    principal: str,
) -> tuple[Cookie, str]:
    cookie_name = PM_BROWSER_COOKIE if principal == "pm" else STAKEHOLDER_BROWSER_COOKIE
    cookies = [
        cookie for cookie in context.cookies(runtime.agent_url) if cookie["name"] == cookie_name
    ]
    assert len(cookies) == 1
    cookie = cookies[0]
    value = str(cookie["value"])
    runtime.track_secret(value)
    assert cookie["httpOnly"] is True
    assert cookie["secure"] is False
    assert cookie["sameSite"] == "Strict"
    assert cookie["path"] == "/"
    assert cookie["domain"] == "127.0.0.1"
    return cookie, value


def _safe_browser_state(page: Page) -> dict[str, Any]:
    return cast(
        "dict[str, Any]",
        page.evaluate(
            """() => ({
                localStorageKeys: Object.keys(window.localStorage),
                sessionStorageKeys: Object.keys(window.sessionStorage),
                referrer: document.referrer,
                pathname: window.location.pathname,
                historyState: JSON.stringify(window.history.state),
                language: document.documentElement.lang,
                title: document.title,
                h1Count: document.querySelectorAll('h1').length,
                horizontalOverflow: Math.max(
                    0,
                    document.documentElement.scrollWidth - document.documentElement.clientWidth
                )
            })"""
        ),
    )


def _write_result(runtime: BrowserRuntime, filename: str, payload: dict[str, Any]) -> None:
    (runtime.evidence_dir / filename).write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _open_invitation(page: Page, runtime: BrowserRuntime, invitation_token: str) -> None:
    try:
        response = page.goto(
            f"{runtime.agent_url}/s/{invitation_token}",
            wait_until="domcontentloaded",
        )
        if response is None or response.status != 200:
            pytest.fail("The stakeholder invitation entry did not return the SPA.")
    except (Error, PlaywrightTimeoutError):
        pytest.fail("The stakeholder invitation did not load.")


@pytest.mark.timeout(180)
def test_react_agent_server_browser_contract_preflight(
    react_browser_runtime: BrowserRuntime,
) -> None:
    runtime = react_browser_runtime
    with httpx.Client(base_url=runtime.agent_url, timeout=30.0) as client:
        spa = client.get("/pm")
        if spa.status_code != 200:
            pytest.fail("The real Agent Server did not serve the built React entry point.")
        assert spa.headers["referrer-policy"] == "no-referrer"
        assert 'name="referrer" content="no-referrer"' in spa.text

        clean_session = client.get(
            "/api/v1/browser/auth/session?principal=pm",
            headers={"X-Correlation-ID": "r2-real-server-clean-session"},
        )
        assert clean_session.status_code == 403
        assert clean_session.headers["content-type"].startswith("application/json")
        assert _json(clean_session) == {
            "error": {
                "code": "ACCESS_DENIED",
                "message": "Access is not authorized.",
                "correlation_id": "r2-real-server-clean-session",
            }
        }

        activated = client.post(
            "/api/v1/browser/auth/pm/activate",
            json={"bootstrap_token": runtime.pm_bootstrap_secret},
            headers={
                "Origin": runtime.agent_url,
                CSRF_HEADER: "1",
                "Sec-Fetch-Site": "same-origin",
                "X-Correlation-ID": "r2-real-server-activation",
            },
        )
        assert activated.status_code == 200
        body = _json(activated)
        assert body["principal_type"] == "pm"
        assert "access_token" not in body
        session_cookie = client.cookies.get(PM_BROWSER_COOKIE)
        assert isinstance(session_cookie, str)
        runtime.track_secret(session_cookie)
        assert session_cookie not in activated.text

    _write_result(
        runtime,
        "r2-agent-server-preflight.json",
        {
            "suite": "react-r2-agent-server-browser-contract",
            "status": "PASS",
            "spa_status": 200,
            "clean_session_status": 403,
            "activation_status": 200,
            "json_contract": True,
            "no_referrer_headers": True,
        },
    )


@pytest.mark.timeout(300)
def test_react_pm_cookie_reload_logout_csrf_and_exact_origin(  # noqa: PLR0915
    browser: Browser,
    react_browser_runtime: BrowserRuntime,
) -> None:
    runtime = react_browser_runtime
    context = _context(browser)
    page = context.new_page()
    _configure(page)
    authorization_seen: list[bool] = []
    safe_responses: list[dict[str, object]] = []
    safe_request_failures: list[dict[str, str | None]] = []
    safe_console_errors: list[str] = []
    safe_page_error_types: list[str] = []

    def observe_api_request(request: Request) -> None:
        if request.url.startswith(f"{runtime.agent_url}/api/"):
            authorization_seen.append("authorization" in request.headers)

    page.on("request", observe_api_request)
    page.on(
        "response",
        lambda item: (
            safe_responses.append(
                {
                    "path": "/api/v1/browser/auth/session",
                    "status": item.status,
                    "content_type": item.headers.get("content-type", ""),
                }
            )
            if item.url == f"{runtime.agent_url}/api/v1/browser/auth/session?principal=pm"
            else None
        ),
    )
    page.on(
        "requestfailed",
        lambda item: (
            safe_request_failures.append(
                {
                    "path": "/api/v1/browser/auth/session",
                    "failure": item.failure,
                }
            )
            if item.url == f"{runtime.agent_url}/api/v1/browser/auth/session?principal=pm"
            else None
        ),
    )
    page.on(
        "console",
        lambda message: (
            safe_console_errors.append(message.text[:300]) if message.type == "error" else None
        ),
    )
    page.on("pageerror", lambda error: safe_page_error_types.append(type(error).__name__))
    try:
        response = page.goto(f"{runtime.agent_url}/pm", wait_until="domcontentloaded")
        if response is None or response.status != 200:
            pytest.fail("The project manager entry did not return the SPA.")
        headers = response.all_headers()
        assert headers["cache-control"] == "no-store"
        assert headers["referrer-policy"] == "no-referrer"
        assert "default-src 'self'" in headers["content-security-policy"]
        try:
            expect(page.get_by_label("Access key")).to_be_visible()
        except AssertionError:
            direct_probe = cast(
                "dict[str, object]",
                page.evaluate(
                    """async () => {
                        try {
                            const response = await fetch(
                                '/api/v1/browser/auth/session?principal=pm',
                                {
                                    credentials: 'same-origin',
                                    cache: 'no-store',
                                    redirect: 'error',
                                    headers: {Accept: 'application/json'}
                                }
                            );
                            return {
                                status: response.status,
                                contentType: response.headers.get('content-type') || '',
                                responseType: response.type
                            };
                        } catch (error) {
                            return {fetchErrorType: error?.constructor?.name || 'unknown'};
                        }
                    }"""
                ),
            )
            _write_result(
                runtime,
                "r2-pm-browser-diagnostic.json",
                {
                    "initial_session_responses": safe_responses,
                    "request_failures": safe_request_failures,
                    "console_errors": safe_console_errors,
                    "page_error_types": safe_page_error_types,
                    "direct_fetch_probe": direct_probe,
                },
            )
            pytest.fail("The browser did not classify the clean PM session as activation-required.")

        page.keyboard.press("Tab")
        assert page.evaluate("() => document.activeElement?.tagName") == "INPUT"
        secret_input = page.get_by_label("Access key")
        secret_input.fill(runtime.pm_bootstrap_secret)
        secret_input.press("Enter")
        expect(page.get_by_role("heading", name="Project manager workspace")).to_be_visible()
        _, session_cookie = _session_cookie(context, runtime, principal="pm")
        if runtime.pm_bootstrap_secret in page.content():
            pytest.fail("The PM bootstrap value remained in rendered content.")

        browser_state = _safe_browser_state(page)
        assert browser_state["localStorageKeys"] == []
        assert browser_state["sessionStorageKeys"] == []
        assert browser_state["referrer"] == ""
        assert browser_state["pathname"] == "/pm"
        assert browser_state["language"] == "en"
        assert browser_state["title"] == "Stakeholder Intelligence"
        assert browser_state["h1Count"] == 1

        missing_csrf = page.evaluate(
            """async () => (await fetch('/api/v1/pm/engagements', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({name: 'Rejected missing CSRF'})
            })).status"""
        )
        wrong_csrf = page.evaluate(
            """async () => (await fetch('/api/v1/pm/engagements', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-Stakeholder-CSRF': 'wrong'
                },
                body: JSON.stringify({name: 'Rejected wrong CSRF'})
            })).status"""
        )
        valid_csrf = page.evaluate(
            """async () => (await fetch('/api/v1/pm/engagements', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-Stakeholder-CSRF': '1'
                },
                body: JSON.stringify({name: 'Accepted browser CSRF'})
            })).status"""
        )
        assert (missing_csrf, wrong_csrf, valid_csrf) == (403, 403, 201)

        page.reload(wait_until="domcontentloaded")
        expect(page.get_by_role("heading", name="Project manager workspace")).to_be_visible()
        page.set_viewport_size({"width": 390, "height": 844})
        assert _safe_browser_state(page)["horizontalOverflow"] == 0
        page.screenshot(path=runtime.evidence_dir / "screenshots" / "r2-pm-active.png")

        page.get_by_role("button", name="Sign out").click()
        expect(page.get_by_label("Access key")).to_be_visible()
        assert not [
            cookie
            for cookie in context.cookies(runtime.agent_url)
            if cookie["name"] == PM_BROWSER_COOKIE
        ]

        context.add_cookies(
            [
                {
                    "name": PM_BROWSER_COOKIE,
                    "value": session_cookie,
                    "url": runtime.agent_url,
                    "httpOnly": True,
                    "secure": False,
                    "sameSite": "Strict",
                }
            ]
        )
        page.reload(wait_until="domcontentloaded")
        expect(page.get_by_label("Access key")).to_be_visible()
        assert not any(authorization_seen)
    finally:
        context.close()

    wrong_origin_context = _context(browser)
    wrong_origin_page = wrong_origin_context.new_page()
    _configure(wrong_origin_page)
    try:
        wrong_origin_page.goto("http://localhost:2024/pm", wait_until="domcontentloaded")
        wrong_origin_page.get_by_label("Access key").fill(runtime.pm_bootstrap_secret)
        wrong_origin_page.get_by_role("button", name="Open workspace").click()
        expect(wrong_origin_page.get_by_text("That access key was not accepted.")).to_be_visible()
        assert not wrong_origin_context.cookies("http://localhost:2024")
    finally:
        wrong_origin_context.close()

    _write_result(
        runtime,
        "r2-pm-security.json",
        {
            "suite": "react-r2-pm-security",
            "status": "PASS",
            "cookie": {
                "host_only_origin": "127.0.0.1",
                "http_only": True,
                "same_site": "Strict",
                "secure": False,
                "loopback_http_exception": True,
            },
            "reload_continuity": True,
            "logout_revocation": True,
            "replayed_cookie_rejected": True,
            "csrf_statuses": {"missing": 403, "wrong": 403, "valid": 201},
            "wrong_origin_rejected": True,
            "authorization_header_requests": sum(authorization_seen),
            "storage_key_count": 0,
            "referrer_empty": True,
            "responsive_overflow_pixels": 0,
        },
    )


@pytest.mark.timeout(300)
def test_react_stakeholder_history_resume_forgery_revocation_and_logout(  # noqa: PLR0915
    browser: Browser,
    react_browser_runtime: BrowserRuntime,
) -> None:
    runtime = react_browser_runtime
    bearer, engagement_id, invitation_id, invitation_token = _issue_invitation(
        runtime, f"Security primary {runtime.run_id[-8:]}"
    )
    context = _context(browser)
    page = context.new_page()
    _configure(page)
    try:
        initial_history_length = page.evaluate("() => window.history.length")
        _open_invitation(page, runtime, invitation_token)
        invitation_url = f"{runtime.agent_url}/s/{invitation_token}"
        expect(page).to_have_url(invitation_url)
        expect(page.get_by_role("heading", name="Stakeholder workspace")).to_be_visible()
        _session_cookie(context, runtime, principal="stakeholder")
        state = _safe_browser_state(page)
        assert state["pathname"] == f"/s/{invitation_token}"
        assert state["historyState"] == "null"
        assert state["referrer"] == ""
        assert state["localStorageKeys"] == []
        assert state["sessionStorageKeys"] == []
        assert page.evaluate("() => window.history.length") == initial_history_length + 1
        if invitation_token in page.content():
            pytest.fail("The invitation value remained in rendered content.")

        page.go_back(wait_until="domcontentloaded")
        expect(page).to_have_url("about:blank")
        page.go_forward(wait_until="domcontentloaded")
        expect(page).to_have_url(invitation_url)
        expect(page.get_by_role("heading", name="Stakeholder workspace")).to_be_visible()
        page.reload(wait_until="domcontentloaded")
        expect(page.get_by_role("heading", name="Stakeholder workspace")).to_be_visible()
        page.screenshot(path=runtime.evidence_dir / "screenshots" / "r2-stakeholder-active.png")

        page.get_by_role("button", name="Sign out").click()
        expect(page.get_by_text("Invitation required")).to_be_visible()
    finally:
        context.close()

    resume_context = _context(browser)
    resume_page = resume_context.new_page()
    _configure(resume_page)
    try:
        _open_invitation(resume_page, runtime, invitation_token)
        expect(resume_page).to_have_url(invitation_url)
        expect(resume_page.get_by_role("heading", name="Stakeholder workspace")).to_be_visible()
        _session_cookie(resume_context, runtime, principal="stakeholder")
        if invitation_token in resume_page.content():
            pytest.fail("A resumed invitation value remained in rendered content.")
    finally:
        resume_context.close()

    forged_token = secrets.token_urlsafe(32)
    runtime.track_secret(forged_token)
    forged_context = _context(browser)
    forged_page = forged_context.new_page()
    _configure(forged_page)
    try:
        _open_invitation(forged_page, runtime, forged_token)
        expect(forged_page).to_have_url(f"{runtime.agent_url}/s/{forged_token}")
        expect(forged_page.get_by_text("Invitation unavailable")).to_be_visible()
        if forged_token in forged_page.content():
            pytest.fail("A forged invitation value remained in rendered content.")
    finally:
        forged_context.close()

    revoked_bearer, revoked_engagement, revoked_invitation, revoked_token = _issue_invitation(
        runtime, f"Security revoked {runtime.run_id[-8:]}"
    )
    revoked_context = _context(browser)
    revoked_page = revoked_context.new_page()
    _configure(revoked_page)
    try:
        _revoke_invitation(
            runtime,
            bearer=revoked_bearer,
            engagement_id=revoked_engagement,
            invitation_id=revoked_invitation,
        )
        _open_invitation(revoked_page, runtime, revoked_token)
        expect(revoked_page.get_by_text("Invitation unavailable")).to_be_visible()
    finally:
        revoked_context.close()

    _write_result(
        runtime,
        "r2-stakeholder-security.json",
        {
            "suite": "react-r2-stakeholder-security",
            "status": "PASS",
            "url_cleaned_before_workspace": True,
            "history_entry_replaced": True,
            "reload_continuity": True,
            "logout_revocation": True,
            "same_invitation_resume_succeeded": True,
            "forged_invitation_rejected": True,
            "pre_activation_invitation_revocation_enforced": True,
            "storage_key_count": 0,
            "referrer_empty": True,
            "retained_raw_token_fields": 0,
            "primary_invitation_id_retained": False,
            "primary_engagement_id_retained": False,
            "primary_setup_values_used_only_for_server_control": bool(
                bearer and engagement_id and invitation_id
            ),
        },
    )
