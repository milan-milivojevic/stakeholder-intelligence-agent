"""Browser-session security matrix at the real custom-route boundary."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response

from stakeholder_intelligence_agent.api.browser_security import (
    BROWSER_CSRF_HEADER,
    BROWSER_CSRF_VALUE,
    PM_BROWSER_SESSION_COOKIE,
    STAKEHOLDER_BROWSER_SESSION_COOKIE,
)
from stakeholder_intelligence_agent.api.spa import install_spa_routes
from tests.integration.test_api_domain_routes import _route_harness

if TYPE_CHECKING:
    from pathlib import Path

    from stakeholder_intelligence_agent.config import Settings
    from tests.integration.test_api_domain_routes import RouteHarness

pytestmark = [pytest.mark.integration, pytest.mark.security]
HTTP_ORIGIN = "http://127.0.0.1:2024"
HTTPS_ORIGIN = "https://localhost:2024"
SAFE_SESSION_FIELDS = {
    "principal_type",
    "access_session_id",
    "expires_at",
    "engagement_id",
    "stakeholder_id",
    "interview_session_id",
    "thread_id",
}


def _browser_headers(
    *,
    origin: str = HTTP_ORIGIN,
    correlation_id: str = "browser-security",
    fetch_site: str = "same-origin",
) -> dict[str, str]:
    return {
        "Origin": origin,
        BROWSER_CSRF_HEADER: BROWSER_CSRF_VALUE,
        "Sec-Fetch-Site": fetch_site,
        "X-Correlation-ID": correlation_id,
    }


def _assert_access_denied(response: Response, correlation_id: str) -> None:
    assert response.status_code == 403
    assert response.json() == {
        "error": {
            "code": "ACCESS_DENIED",
            "message": "Access is not authorized.",
            "correlation_id": correlation_id,
        }
    }
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"


async def _activate_pm(client: AsyncClient, harness: RouteHarness) -> tuple[dict[str, object], str]:
    bootstrap_secret = harness.settings.pm_bootstrap_token.get_secret_value()
    response = await client.post(
        "/api/v1/browser/auth/pm/activate",
        json={"bootstrap_token": bootstrap_secret},
        headers=_browser_headers(origin=harness.settings.browser_origin),
    )
    assert response.status_code == 200
    body = response.json()
    assert set(body) == SAFE_SESSION_FIELDS
    assert body["principal_type"] == "pm"
    session_cookie = client.cookies.get(PM_BROWSER_SESSION_COOKIE)
    assert isinstance(session_cookie, str)
    assert session_cookie not in response.text
    assert bootstrap_secret not in response.text
    assert "access_token" not in response.text
    assert "token_hash" not in response.text
    return body, session_cookie


async def _create_stakeholder_invitation(
    client: AsyncClient,
    *,
    name: str,
) -> tuple[str, str, str, str]:
    engagement = await client.post(
        "/api/v1/pm/engagements",
        json={"name": f"{name} engagement", "description": "Browser security matrix."},
        headers=_browser_headers(),
    )
    assert engagement.status_code == 201
    engagement_id = engagement.json()["engagement"]["engagement_id"]
    stakeholder = await client.post(
        f"/api/v1/pm/engagements/{engagement_id}/stakeholders",
        json={
            "display_name": name,
            "role": "Operations lead",
            "department": "Operations",
        },
        headers=_browser_headers(),
    )
    assert stakeholder.status_code == 201
    stakeholder_id = stakeholder.json()["stakeholder"]["stakeholder_id"]
    invitation = await client.post(
        (f"/api/v1/pm/engagements/{engagement_id}/stakeholders/{stakeholder_id}/invitations"),
        headers=_browser_headers(),
    )
    assert invitation.status_code == 201
    invitation_body = invitation.json()
    return (
        engagement_id,
        stakeholder_id,
        invitation_body["invitation"]["invitation_id"],
        invitation_body["invitation_token"],
    )


async def test_pm_browser_cookie_origin_csrf_reload_logout_and_tampering(
    settings: Settings,
) -> None:
    settings = settings.model_copy(update={"browser_origin": HTTP_ORIGIN})
    async with _route_harness(settings) as harness:  # noqa: SIM117
        async with harness.app.router.lifespan_context(harness.app):
            async with AsyncClient(
                transport=ASGITransport(app=harness.app),
                base_url=HTTP_ORIGIN,
            ) as client:
                secret = harness.settings.pm_bootstrap_token.get_secret_value()
                payload = {"bootstrap_token": secret}
                denied_responses = (
                    await client.post(
                        "/api/v1/browser/auth/pm/activate",
                        json=payload,
                        headers={"X-Correlation-ID": "missing-browser-guards"},
                    ),
                    await client.post(
                        "/api/v1/browser/auth/pm/activate",
                        json=payload,
                        headers={
                            **_browser_headers(correlation_id="wrong-origin"),
                            "Origin": "http://localhost:2024",
                        },
                    ),
                    await client.post(
                        "/api/v1/browser/auth/pm/activate",
                        json=payload,
                        headers={
                            "Origin": HTTP_ORIGIN,
                            "X-Correlation-ID": "missing-csrf",
                        },
                    ),
                    await client.post(
                        "/api/v1/browser/auth/pm/activate",
                        json=payload,
                        headers=_browser_headers(
                            correlation_id="cross-site",
                            fetch_site="cross-site",
                        ),
                    ),
                )
                for response, correlation_id in zip(
                    denied_responses,
                    ("missing-browser-guards", "wrong-origin", "missing-csrf", "cross-site"),
                    strict=True,
                ):
                    _assert_access_denied(response, correlation_id)
                    assert secret not in response.text

                session_body, original_cookie = await _activate_pm(client, harness)
                set_cookie = client.cookies.jar
                assert len(set_cookie) == 1
                header = (
                    await client.get("/api/v1/browser/auth/session?principal=pm")
                ).request.headers["cookie"]
                assert original_cookie in header

                activation = await client.post(
                    "/api/v1/browser/auth/logout?principal=pm",
                    headers={
                        **_browser_headers(correlation_id="ambiguous-activation"),
                        "Authorization": "Bearer " + ("a" * 32),
                    },
                )
                _assert_access_denied(activation, "ambiguous-activation")

                inspected = await client.get("/api/v1/browser/auth/session?principal=pm")
                assert inspected.status_code == 200
                assert inspected.json() == session_body
                assert original_cookie not in inspected.text

                engagements = await client.get("/api/v1/pm/engagements")
                assert engagements.status_code == 200
                assert engagements.json() == {"engagements": []}

                missing_mutation_guards = await client.post(
                    "/api/v1/pm/engagements",
                    json={"name": "Must not be created"},
                    headers={"X-Correlation-ID": "cookie-csrf"},
                )
                _assert_access_denied(missing_mutation_guards, "cookie-csrf")

                created = await client.post(
                    "/api/v1/pm/engagements",
                    json={"name": "Browser authorized engagement"},
                    headers=_browser_headers(),
                )
                assert created.status_code == 201

                ambiguous = await client.get(
                    "/api/v1/pm/engagements",
                    headers={
                        "Authorization": "Bearer " + ("b" * 32),
                        "X-Correlation-ID": "ambiguous-transport",
                    },
                )
                _assert_access_denied(ambiguous, "ambiguous-transport")

                logout = await client.post(
                    "/api/v1/browser/auth/logout?principal=pm",
                    headers=_browser_headers(correlation_id="browser-logout"),
                )
                assert logout.status_code == 200
                assert logout.json() == {"status": "ok"}
                cleared_cookie = logout.headers["set-cookie"].lower()
                assert "max-age=0" in cleared_cookie
                assert "httponly" in cleared_cookie
                assert "samesite=strict" in cleared_cookie
                assert "domain=" not in cleared_cookie
                assert client.cookies.get(PM_BROWSER_SESSION_COOKIE) is None

                after_logout = await client.get(
                    "/api/v1/browser/auth/session?principal=pm",
                    headers={"X-Correlation-ID": "after-logout"},
                )
                _assert_access_denied(after_logout, "after-logout")

                replay = await client.get(
                    "/api/v1/browser/auth/session?principal=pm",
                    headers={
                        "Cookie": f"{PM_BROWSER_SESSION_COOKIE}={original_cookie}",
                        "X-Correlation-ID": "invalid-cookie",
                    },
                )
                forged = await client.get(
                    "/api/v1/browser/auth/session?principal=pm",
                    headers={
                        "Cookie": f"{PM_BROWSER_SESSION_COOKIE}={'f' * 48}",
                        "X-Correlation-ID": "invalid-cookie",
                    },
                )
                _assert_access_denied(replay, "invalid-cookie")
                _assert_access_denied(forged, "invalid-cookie")
                assert replay.json() == forged.json()
                assert original_cookie not in replay.text


@pytest.mark.parametrize(
    ("origin", "secure_expected"),
    [(HTTP_ORIGIN, False), (HTTPS_ORIGIN, True)],
)
async def test_cookie_flags_follow_http_exception_and_https_requirement(
    settings: Settings,
    origin: str,
    secure_expected: bool,
) -> None:
    scoped_settings = settings.model_copy(update={"browser_origin": origin})
    async with _route_harness(scoped_settings) as harness:  # noqa: SIM117
        async with harness.app.router.lifespan_context(harness.app):
            async with AsyncClient(
                transport=ASGITransport(app=harness.app),
                base_url=origin,
            ) as client:
                response = await client.post(
                    "/api/v1/browser/auth/pm/activate",
                    json={
                        "bootstrap_token": harness.settings.pm_bootstrap_token.get_secret_value()
                    },
                    headers=_browser_headers(origin=origin),
                )
                assert response.status_code == 200
                cookie = response.headers["set-cookie"].lower()
                assert "httponly" in cookie
                assert "samesite=strict" in cookie
                assert "path=/" in cookie
                assert "max-age=" in cookie
                assert "expires=" in cookie
                assert "domain=" not in cookie
                assert ("; secure" in cookie) is secure_expected


async def test_stakeholder_link_resumes_fixed_context_and_cannot_be_revoked_after_open(
    settings: Settings,
) -> None:
    settings = settings.model_copy(update={"browser_origin": HTTP_ORIGIN})
    async with _route_harness(settings) as harness:  # noqa: SIM117
        async with harness.app.router.lifespan_context(harness.app):
            transport = ASGITransport(app=harness.app)
            async with AsyncClient(transport=transport, base_url=HTTP_ORIGIN) as pm_client:
                await _activate_pm(pm_client, harness)
                (
                    engagement_id,
                    stakeholder_id,
                    invitation_id,
                    invitation_token,
                ) = await _create_stakeholder_invitation(pm_client, name="Alex Browser")

                async with AsyncClient(
                    transport=ASGITransport(app=harness.app),
                    base_url=HTTP_ORIGIN,
                ) as stakeholder_client:
                    activated = await stakeholder_client.post(
                        "/api/v1/browser/auth/stakeholder/activate",
                        json={"invitation_token": invitation_token},
                        headers=_browser_headers(correlation_id="stakeholder-activate"),
                    )
                    assert activated.status_code == 200
                    body = activated.json()
                    assert set(body) == SAFE_SESSION_FIELDS
                    assert body["principal_type"] == "stakeholder"
                    assert body["engagement_id"] == engagement_id
                    assert body["stakeholder_id"] == stakeholder_id
                    stakeholder_cookie = stakeholder_client.cookies.get(
                        STAKEHOLDER_BROWSER_SESSION_COOKIE
                    )
                    assert isinstance(stakeholder_cookie, str)
                    assert stakeholder_cookie not in activated.text
                    assert invitation_token not in activated.text

                    context = await stakeholder_client.get("/api/v1/stakeholder/context")
                    assert context.status_code == 200
                    assert context.json()["stakeholder"]["stakeholder_id"] == stakeholder_id

                    denied_pm_surface = await stakeholder_client.get(
                        "/api/v1/pm/engagements",
                        headers={"X-Correlation-ID": "stakeholder-as-pm"},
                    )
                    _assert_access_denied(denied_pm_surface, "stakeholder-as-pm")

                    denied_stakeholder_surface = await pm_client.get(
                        "/api/v1/stakeholder/context",
                        headers={"X-Correlation-ID": "pm-as-stakeholder"},
                    )
                    _assert_access_denied(denied_stakeholder_surface, "pm-as-stakeholder")

                    async with AsyncClient(
                        transport=ASGITransport(app=harness.app),
                        base_url=HTTP_ORIGIN,
                    ) as attacker:
                        replay = await attacker.post(
                            "/api/v1/browser/auth/stakeholder/activate",
                            json={"invitation_token": invitation_token},
                            headers=_browser_headers(correlation_id="invalid-invitation"),
                        )
                        forged = await attacker.post(
                            "/api/v1/browser/auth/stakeholder/activate",
                            json={"invitation_token": "z" * 48},
                            headers=_browser_headers(correlation_id="invalid-invitation"),
                        )
                    assert replay.status_code == 200
                    assert replay.json()["interview_session_id"] == body["interview_session_id"]
                    assert replay.json()["thread_id"] == body["thread_id"]
                    _assert_access_denied(forged, "invalid-invitation")
                    assert invitation_token not in replay.text

                    revoked = await pm_client.delete(
                        f"/api/v1/pm/engagements/{engagement_id}/invitations/{invitation_id}",
                        headers=_browser_headers(correlation_id="revoke-invitation"),
                    )
                    assert revoked.status_code == 409
                    assert revoked.json()["error"]["code"] == "DOMAIN_CONFLICT"

                    active_session = await stakeholder_client.get(
                        "/api/v1/browser/auth/session?principal=stakeholder",
                        headers={"X-Correlation-ID": "active-session"},
                    )
                    assert active_session.status_code == 200
                    assert (
                        active_session.json()["interview_session_id"]
                        == body["interview_session_id"]
                    )
                    assert stakeholder_cookie not in active_session.text


async def test_same_browser_keeps_pm_and_stakeholder_sessions_concurrent(
    settings: Settings,
) -> None:
    settings = settings.model_copy(update={"browser_origin": HTTP_ORIGIN})
    async with _route_harness(settings) as harness:  # noqa: SIM117
        async with harness.app.router.lifespan_context(harness.app):
            async with AsyncClient(
                transport=ASGITransport(app=harness.app),
                base_url=HTTP_ORIGIN,
            ) as client:
                _, pm_cookie = await _activate_pm(client, harness)
                (
                    engagement_id,
                    stakeholder_id,
                    _,
                    invitation_token,
                ) = await _create_stakeholder_invitation(client, name="Same browser")

                stakeholder_activation = await client.post(
                    "/api/v1/browser/auth/stakeholder/activate",
                    json={"invitation_token": invitation_token},
                    headers=_browser_headers(correlation_id="same-browser-stakeholder"),
                )
                assert stakeholder_activation.status_code == 200
                assert stakeholder_activation.json()["principal_type"] == "stakeholder"
                stakeholder_cookie = client.cookies.get(STAKEHOLDER_BROWSER_SESSION_COOKIE)
                assert isinstance(stakeholder_cookie, str)
                assert stakeholder_cookie != pm_cookie
                assert client.cookies.get(PM_BROWSER_SESSION_COOKIE) == pm_cookie
                context = await client.get("/api/v1/stakeholder/context")
                assert context.status_code == 200
                assert context.json()["engagement"]["engagement_id"] == engagement_id
                assert context.json()["stakeholder"]["stakeholder_id"] == stakeholder_id
                assert (await client.get("/api/v1/pm/engagements")).status_code == 200
                pm_inspection = await client.get("/api/v1/browser/auth/session?principal=pm")
                stakeholder_inspection = await client.get(
                    "/api/v1/browser/auth/session?principal=stakeholder"
                )
                assert pm_inspection.json()["principal_type"] == "pm"
                assert stakeholder_inspection.json()["principal_type"] == "stakeholder"

                stakeholder_logout = await client.post(
                    "/api/v1/browser/auth/logout?principal=stakeholder",
                    headers=_browser_headers(correlation_id="same-browser-stakeholder-logout"),
                )
                assert stakeholder_logout.status_code == 200
                assert client.cookies.get(STAKEHOLDER_BROWSER_SESSION_COOKIE) is None
                assert client.cookies.get(PM_BROWSER_SESSION_COOKIE) == pm_cookie
                assert (await client.get("/api/v1/pm/engagements")).status_code == 200

                resumed = await client.post(
                    "/api/v1/browser/auth/stakeholder/activate",
                    json={"invitation_token": invitation_token},
                    headers=_browser_headers(correlation_id="same-browser-stakeholder-resume"),
                )
                assert resumed.status_code == 200
                resumed_cookie = client.cookies.get(STAKEHOLDER_BROWSER_SESSION_COOKIE)
                assert isinstance(resumed_cookie, str)
                pm_logout = await client.post(
                    "/api/v1/browser/auth/logout?principal=pm",
                    headers=_browser_headers(correlation_id="same-browser-pm-logout"),
                )
                assert pm_logout.status_code == 200
                assert client.cookies.get(PM_BROWSER_SESSION_COOKIE) is None
                assert client.cookies.get(STAKEHOLDER_BROWSER_SESSION_COOKIE) == resumed_cookie
                assert (await client.get("/api/v1/stakeholder/context")).status_code == 200


async def test_expired_session_and_invitation_are_rejected(settings: Settings) -> None:
    settings = settings.model_copy(update={"browser_origin": HTTP_ORIGIN})
    async with _route_harness(settings) as harness:  # noqa: SIM117
        async with harness.app.router.lifespan_context(harness.app):
            async with AsyncClient(
                transport=ASGITransport(app=harness.app),
                base_url=HTTP_ORIGIN,
            ) as client:
                pm_body, pm_cookie = await _activate_pm(client, harness)
                async with harness.database.transaction() as connection:
                    await connection.execute(
                        "UPDATE access_sessions SET expires_at = ? WHERE access_session_id = ?",
                        ("2000-01-01T00:00:00+00:00", pm_body["access_session_id"]),
                    )
                expired_session = await client.get(
                    "/api/v1/browser/auth/session?principal=pm",
                    headers={"X-Correlation-ID": "expired-session"},
                )
                _assert_access_denied(expired_session, "expired-session")
                assert pm_cookie not in expired_session.text

            async with AsyncClient(
                transport=ASGITransport(app=harness.app),
                base_url=HTTP_ORIGIN,
            ) as fresh_pm:
                await _activate_pm(fresh_pm, harness)
                _, _, invitation_id, invitation_token = await _create_stakeholder_invitation(
                    fresh_pm,
                    name="Expired invitation",
                )
                async with harness.database.transaction() as connection:
                    await connection.execute(
                        """
                        UPDATE invitation_tokens SET created_at = ?, expires_at = ?
                        WHERE invitation_id = ?
                        """,
                        (
                            "1999-01-01T00:00:00+00:00",
                            "2000-01-01T00:00:00+00:00",
                            invitation_id,
                        ),
                    )

            async with AsyncClient(
                transport=ASGITransport(app=harness.app),
                base_url=HTTP_ORIGIN,
            ) as stakeholder:
                expired_invitation = await stakeholder.post(
                    "/api/v1/browser/auth/stakeholder/activate",
                    json={"invitation_token": invitation_token},
                    headers=_browser_headers(correlation_id="expired-invitation"),
                )
                _assert_access_denied(expired_invitation, "expired-invitation")
                assert invitation_token not in expired_invitation.text


async def test_spa_host_serves_only_approved_routes_without_reflecting_token(
    tmp_path: Path,
) -> None:
    dist = tmp_path / "dist"
    assets = dist / "assets"
    assets.mkdir(parents=True)
    (dist / "index.html").write_text(
        '<!doctype html><meta name="referrer" content="no-referrer"><div id="root"></div>',
        encoding="utf-8",
    )
    (assets / "application.js").write_text("export {};", encoding="utf-8")
    application = FastAPI()
    install_spa_routes(application, dist_root=dist)

    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url=HTTP_ORIGIN,
    ) as client:
        for route in ("/pm", "/s", "/s/not-a-retained-secret"):
            response = await client.get(route)
            assert response.status_code == 200
            assert "not-a-retained-secret" not in response.text
            assert 'content="no-referrer"' in response.text
        asset = await client.get("/assets/application.js")
        assert asset.status_code == 200
        assert asset.text == "export {};"
        assert (await client.get("/settings")).status_code == 404
        assert (await client.get("/api/v1/browser/auth/session?principal=pm")).status_code == 404
