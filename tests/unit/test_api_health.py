"""Minimal custom-route contract test."""

from httpx import ASGITransport, AsyncClient

from stakeholder_intelligence_agent.access import AccessService
from stakeholder_intelligence_agent.api import app
from stakeholder_intelligence_agent.api.app import create_app
from stakeholder_intelligence_agent.config import Settings
from stakeholder_intelligence_agent.persistence import DomainDatabase


async def test_health_is_safe_and_does_not_claim_readiness() -> None:
    openapi = app.openapi()
    assert "/api/v1/system/health" in openapi["paths"]
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.get("/api/v1/system/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "backend": "langgraph_agent_server",
        "graphs": ["interview", "insight"],
    }


async def test_app_lifespan_migrates_domain_database(settings: Settings) -> None:
    service = AccessService(DomainDatabase(settings.domain_database), settings)
    application = create_app(service)

    async with application.router.lifespan_context(application):
        async with AsyncClient(
            transport=ASGITransport(app=application),
            base_url="http://testserver",
        ) as client:
            assert (await client.get("/api/v1/system/health")).status_code == 200
        assert application.state.access_service is service

    assert settings.domain_database.exists()


async def test_domain_routes_fail_closed_without_complete_service_graph(settings: Settings) -> None:
    service = AccessService(DomainDatabase(settings.domain_database), settings)
    application = create_app(service)

    async with application.router.lifespan_context(application):  # noqa: SIM117
        async with AsyncClient(
            transport=ASGITransport(app=application),
            base_url="http://testserver",
        ) as client:
            response = await client.get("/api/v1/pm/engagements")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "SERVICE_NOT_READY"
