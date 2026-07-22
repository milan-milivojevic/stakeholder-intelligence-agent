"""Real-HTTP React acceptance for the required business scenarios."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlparse

import pytest
from playwright.sync_api import Browser, Page, Request, Response, expect
from scripts.run_ui_test_backend import INSUFFICIENT_QUESTION

from tests.acceptance.test_business_scenarios import SCENARIOS

if TYPE_CHECKING:
    from playwright.sync_api import BrowserContext

    from tests.e2e.conftest import BrowserRuntime

pytestmark = [pytest.mark.e2e, pytest.mark.integration, pytest.mark.trajectory]
NORMAL_TIMEOUT_MS = 60_000


def _context(browser: Browser) -> BrowserContext:
    return browser.new_context(
        viewport={"width": 1280, "height": 900},
        locale="en-US",
        accept_downloads=True,
    )


def _configure(page: Page) -> None:
    page.set_default_timeout(NORMAL_TIMEOUT_MS)
    page.set_default_navigation_timeout(NORMAL_TIMEOUT_MS)


def _open_disclosure(page: Page, title: str) -> None:
    summary_title = page.get_by_text(title, exact=True)
    details = summary_title.locator("xpath=ancestor::details")
    if details.get_attribute("open") is None:
        summary_title.click()


def _browser_state(page: Page) -> dict[str, Any]:
    return cast(
        "dict[str, Any]",
        page.evaluate(
            """() => ({
                localStorageKeys: Object.keys(window.localStorage),
                sessionStorageKeys: Object.keys(window.sessionStorage),
                documentCookie: document.cookie,
                referrer: document.referrer,
                pathname: window.location.pathname,
                language: document.documentElement.lang,
                h1Count: document.querySelectorAll('h1').length,
                horizontalOverflow: Math.max(
                    0,
                    document.documentElement.scrollWidth - document.documentElement.clientWidth
                )
            })"""
        ),
    )


def _write_result(runtime: BrowserRuntime, payload: dict[str, Any]) -> None:
    (runtime.evidence_dir / "r5-react-acceptance.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


@pytest.mark.timeout(420)
def test_react_pm_acceptance_scenarios_use_real_backend(  # noqa: PLR0915
    browser: Browser,
    react_acceptance_runtime: BrowserRuntime,
) -> None:
    runtime = react_acceptance_runtime
    context = _context(browser)
    page = context.new_page()
    _configure(page)
    api_requests: list[dict[str, object]] = []
    http_error_responses: list[dict[str, object]] = []
    console_errors: list[str] = []

    def observe_request(request: Request) -> None:
        if request.url.startswith(f"{runtime.agent_url}/api/"):
            api_requests.append(
                {
                    "path": urlparse(request.url).path,
                    "method": request.method,
                    "authorization_header": "authorization" in request.headers,
                }
            )

    def observe_response(response: Response) -> None:
        if response.status >= 400:
            http_error_responses.append(
                {"path": urlparse(response.url).path, "status": response.status}
            )

    page.on("request", observe_request)
    page.on("response", observe_response)
    page.on(
        "console",
        lambda message: (
            console_errors.append(message.text[:200]) if message.type == "error" else None
        ),
    )

    scenario_results: list[dict[str, str]] = []
    try:
        response = page.goto(f"{runtime.agent_url}/pm", wait_until="domcontentloaded")
        assert response is not None
        assert response.status == 200
        secret_input = page.get_by_label("Access key")
        expect(secret_input).to_be_visible()
        page.keyboard.press("Tab")
        assert page.evaluate("() => document.activeElement?.tagName") == "INPUT"
        secret_input.fill(runtime.pm_bootstrap_secret)
        secret_input.press("Enter")
        expect(page.get_by_role("heading", name="Project manager workspace")).to_be_visible()
        assert runtime.pm_bootstrap_secret not in page.content()

        expect(page.get_by_text("Alpha Canary engagement", exact=True)).to_be_visible()
        expect(page.get_by_text("Beta Canary engagement", exact=True)).to_be_visible()
        alpha_row = page.get_by_role("listitem").filter(has_text="Alpha Canary engagement")
        expect(alpha_row).to_have_count(1)
        alpha_row.get_by_role("button", name="Open", exact=True).click()
        active_engagement = page.get_by_role("region", name="Active engagement")
        expect(active_engagement).to_be_visible()
        expect(active_engagement.get_by_text("Alpha Canary engagement", exact=True)).to_be_visible()
        page.get_by_role("tab", name="Insight research").click()
        question = page.get_by_label("Research question")

        question.fill(SCENARIOS[0].question)
        page.get_by_role("button", name="Run insight research").click()
        expect(
            page.get_by_text(
                "Manual handoffs and unclear escalation ownership are the largest supported risks.",
                exact=False,
            )
        ).to_be_visible()
        expect(page.get_by_text("Analysis complete")).to_be_visible()
        _open_disclosure(page, "Risks and approval dependencies")
        expect(
            page.get_by_text("Manual handoffs create rework and missed deadlines.").first
        ).to_be_visible()
        expect(page.get_by_text("Responsibilities", exact=True)).to_be_visible()
        scenario_results.append({"acceptance_id": "AC-01", "status": "complete"})
        page.screenshot(
            path=runtime.evidence_dir / "screenshots" / "r5-ac-01-real-backend.png",
            full_page=True,
        )

        evidence_button = page.get_by_role(
            "button", name=re.compile(r"^View (supporting source|source excerpt)")
        ).first
        evidence_button.click()
        drill_down = page.get_by_role("dialog")
        expect(drill_down.get_by_text("Source supporting this analysis")).to_be_visible()
        drill_down_text = drill_down.inner_text()
        assert "BETA-CANARY-COBALT" not in drill_down_text
        assert "ALPHA-DRAFT-EXCLUDED" not in drill_down_text
        drill_down.get_by_role("button", name="Close").click()

        question.fill(SCENARIOS[1].question)
        page.get_by_role("button", name="Run insight research").click()
        expect(
            page.get_by_text(
                "Finance confirms support, Sales support is conditional",
                exact=False,
            )
        ).to_be_visible()
        _open_disclosure(page, "Stakeholder alignment")
        for category in (
            "confirmed support",
            "conditional support",
            "expressed concern",
            "topic not discussed",
            "insufficient evidence",
        ):
            expect(page.get_by_text(category, exact=True)).to_be_visible()
        expect(page.get_by_text("Procurement readiness", exact=True).first).to_be_visible()
        scenario_results.append({"acceptance_id": "AC-02", "status": "complete"})
        page.screenshot(
            path=runtime.evidence_dir / "screenshots" / "r5-ac-02-real-backend.png",
            full_page=True,
        )

        question.fill(SCENARIOS[2].question)
        page.get_by_role("button", name="Run insight research").click()
        _open_disclosure(page, "Stakeholder alignment")
        expect(page.get_by_text("Approval cycle duration", exact=True)).to_be_visible()
        expect(
            page.get_by_text("The approval cycle normally takes two business days.")
        ).to_be_visible()
        expect(
            page.get_by_text("The approval cycle normally takes seven to ten business days.")
        ).to_be_visible()
        expect(
            page.get_by_text(
                "Sales and the pilot charter both require exception visibility",
                exact=False,
            )
        ).to_be_visible()
        scenario_results.append({"acceptance_id": "AC-03", "status": "complete"})
        page.screenshot(
            path=runtime.evidence_dir / "screenshots" / "r5-ac-03-real-backend.png",
            full_page=True,
        )

        question.fill(INSUFFICIENT_QUESTION)
        page.get_by_role("button", name="Run insight research").click()
        expect(page.get_by_text("More evidence needed")).to_be_visible()
        expect(
            page.get_by_text(
                "Current permitted evidence does not establish Procurement readiness or ownership."
            )
        ).to_be_visible()
        expect(
            page.get_by_role("region", name="Key insights").get_by_text(
                "No finalized Procurement interview or approved source is available.",
                exact=False,
            )
        ).to_be_visible()
        expect(page.get_by_text("No supported finding was identified.")).to_be_visible()
        scenario_results.append(
            {"acceptance_id": "INSUFFICIENT-EVIDENCE", "status": "insufficient_evidence"}
        )
        page.screenshot(
            path=runtime.evidence_dir / "screenshots" / "r5-insufficient-real-backend.png",
            full_page=True,
        )

        report_text = page.get_by_role("article").inner_text()
        assert "BETA-CANARY-COBALT" not in report_text
        assert "ALPHA-DRAFT-EXCLUDED" not in report_text
        page.set_viewport_size({"width": 390, "height": 844})
        state = _browser_state(page)
        assert state["horizontalOverflow"] == 0
        assert state["localStorageKeys"] == []
        assert state["sessionStorageKeys"] == []
        assert state["documentCookie"] == ""
        assert state["referrer"] == ""
        assert state["pathname"] == "/pm"
        assert state["language"] == "en"
        assert state["h1Count"] == 1
        page.screenshot(
            path=runtime.evidence_dir / "screenshots" / "r5-acceptance-mobile.png",
            full_page=True,
        )
        assert api_requests
        assert not any(item["authorization_header"] for item in api_requests)
        assert http_error_responses
        assert all(
            item == {"path": "/api/v1/browser/auth/session", "status": 403}
            for item in http_error_responses
        )
        assert not [message for message in console_errors if "403" not in message]
    finally:
        context.close()

    _write_result(
        runtime,
        {
            "suite": "react-r5-real-backend-acceptance",
            "status": "PASS",
            "scenarios": scenario_results,
            "http_boundary": {
                "real_network_api_requests": len(api_requests),
                "controlled_or_intercepted_routes": 0,
                "authorization_header_requests": sum(
                    bool(item["authorization_header"]) for item in api_requests
                ),
                "expected_clean_session_denials": len(http_error_responses),
                "unexpected_http_errors": 0,
            },
            "backend": {
                "real_fastapi_routes": True,
                "real_domain_persistence": True,
                "real_scoped_retrieval": True,
                "real_deep_agent_graph": True,
                "deterministic_model_doubles": True,
                "live_gemini": "NOT RUN in this browser acceptance suite",
            },
            "security": {
                "foreign_beta_canary_absent": True,
                "unfinalized_draft_canary_absent": True,
                "browser_storage_key_count": 0,
                "browser_readable_cookie": False,
            },
            "accessibility_and_responsive": {
                "keyboard_pm_activation": True,
                "semantic_role_queries": True,
                "single_h1": True,
                "document_language": "en",
                "mobile_horizontal_overflow_pixels": 0,
            },
        },
    )
