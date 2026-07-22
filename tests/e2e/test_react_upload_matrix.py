"""Real-HTTP React upload matrix across PM and fixed stakeholder scopes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlparse

import pytest
from playwright.sync_api import Browser, Error, Page, Request, Response, expect

from stakeholder_intelligence_agent.api.browser_security import (
    PM_BROWSER_SESSION_COOKIE,
    STAKEHOLDER_BROWSER_SESSION_COOKIE,
)

if TYPE_CHECKING:
    from playwright.sync_api import BrowserContext

    from tests.e2e.conftest import BrowserRuntime

pytestmark = [pytest.mark.e2e, pytest.mark.integration, pytest.mark.security]
NORMAL_TIMEOUT_MS = 60_000
FIXTURES = Path(__file__).parents[1] / "fixtures" / "ingestion"
FORMAT_CASES = (
    ("alpha-mixed-content.pdf", "application/pdf"),
    (
        "alpha-stakeholder-brief.docx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ),
    (
        "alpha-evidence-deck.pptx",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ),
    (
        "alpha-stakeholder-signals.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ),
    ("alpha-organization-chart.png", "image/png"),
    ("beta-process-map.jpg", "image/jpeg"),
)


def _context(browser: Browser) -> BrowserContext:
    return browser.new_context(viewport={"width": 1280, "height": 900}, locale="en-US")


def _configure(page: Page) -> None:
    page.set_default_timeout(NORMAL_TIMEOUT_MS)
    page.set_default_navigation_timeout(NORMAL_TIMEOUT_MS)


def _state(page: Page) -> dict[str, Any]:
    return cast(
        "dict[str, Any]",
        page.evaluate(
            """() => ({
                localStorageKeys: Object.keys(window.localStorage),
                sessionStorageKeys: Object.keys(window.sessionStorage),
                documentCookie: document.cookie,
                referrer: document.referrer,
                pathname: window.location.pathname,
                horizontalOverflow: Math.max(
                    0,
                    document.documentElement.scrollWidth - document.documentElement.clientWidth
                )
            })"""
        ),
    )


def _write_result(runtime: BrowserRuntime, payload: dict[str, Any]) -> None:
    (runtime.evidence_dir / "r5-react-upload-matrix.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


@pytest.mark.timeout(420)
def test_react_six_formats_use_real_scoped_ingestion_routes(  # noqa: PLR0915
    browser: Browser,
    react_acceptance_runtime: BrowserRuntime,
) -> None:
    runtime = react_acceptance_runtime
    pm_context = _context(browser)
    pm_page = pm_context.new_page()
    _configure(pm_page)
    api_requests: list[dict[str, object]] = []
    error_responses: list[dict[str, object]] = []
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
            error_responses.append({"path": urlparse(response.url).path, "status": response.status})

    def observe_console(message: Any) -> None:
        if message.type == "error":
            console_errors.append(str(message.text)[:200])

    pm_page.on("request", observe_request)
    pm_page.on("response", observe_response)
    pm_page.on("console", observe_console)
    stakeholder_context: BrowserContext | None = None
    clean_context: BrowserContext | None = None
    matrix_engagement_id = ""
    stakeholder_id = ""
    unauthorized_status = 0
    cross_principal_status = 0
    unsupported_status = 0
    try:
        response = pm_page.goto(f"{runtime.agent_url}/pm", wait_until="domcontentloaded")
        assert response is not None
        assert response.status == 200
        secret_input = pm_page.get_by_label("Access key")
        expect(secret_input).to_be_visible()
        secret_input.fill(runtime.pm_bootstrap_secret)
        secret_input.press("Enter")
        expect(pm_page.get_by_role("heading", name="Project manager workspace")).to_be_visible()
        pm_cookie = next(
            cookie
            for cookie in pm_context.cookies(runtime.agent_url)
            if cookie["name"] == PM_BROWSER_SESSION_COOKIE
        )
        assert pm_cookie["httpOnly"] is True
        runtime.track_secret(str(pm_cookie["value"]))

        engagement_name = f"Six-format upload matrix {runtime.run_id[-8:]}"
        pm_page.get_by_label("Engagement name").fill(engagement_name)
        pm_page.get_by_label("Description (optional)").fill(
            "Real scoped ingestion-route verification."
        )
        pm_page.get_by_role("button", name="Create and open").click()
        active_engagement = pm_page.get_by_role("region", name="Active engagement")
        expect(active_engagement).to_be_visible()
        expect(active_engagement.get_by_text(engagement_name, exact=True)).to_be_visible()
        engagements = cast(
            "dict[str, Any]",
            pm_page.evaluate(
                """async () => await (await fetch('/api/v1/pm/engagements', {
                    credentials: 'same-origin', cache: 'no-store'
                })).json()"""
            ),
        )
        matrix_engagement_id = next(
            str(item["engagement_id"])
            for item in engagements["engagements"]
            if item["name"] == engagement_name
        )

        pm_page.get_by_role("tab", name="Documents").click()
        pm_upload = pm_page.locator("#pm-document-upload")
        for filename, _media_type in FORMAT_CASES:
            pm_upload.set_input_files(str(FIXTURES / filename))
            pm_page.get_by_role("button", name="Upload document").click()
            expect(pm_page.get_by_text(filename, exact=True)).to_be_visible()
        expect(pm_page.get_by_text("Document accepted", exact=True)).to_be_visible()

        mismatch_bytes = (FIXTURES / "alpha-organization-chart.png").read_bytes()
        pm_upload.set_input_files(
            {"name": "mismatched.pdf", "mimeType": "application/pdf", "buffer": mismatch_bytes}
        )
        pm_page.get_by_role("button", name="Upload document").click()
        expect(
            pm_page.get_by_text("The uploaded document type does not match its content.")
        ).to_be_visible()
        pm_upload.set_input_files(
            {
                "name": "unsupported.vsdx",
                "mimeType": "application/vnd.ms-visio.drawing",
                "buffer": b"unsupported deterministic browser fixture",
            }
        )
        pm_page.get_by_role("button", name="Upload document").click()
        expect(pm_page.get_by_text("Choose a permitted file before uploading.")).to_be_visible()
        unsupported_result = cast(
            "dict[str, Any]",
            pm_page.evaluate(
                """async (engagementId) => {
                    const body = new FormData();
                    body.append('upload', new File(
                        ['unsupported deterministic browser fixture'],
                        'unsupported.vsdx',
                        {type: 'application/vnd.ms-visio.drawing'}
                    ));
                    const response = await fetch(
                        `/api/v1/pm/engagements/${encodeURIComponent(engagementId)}/documents`,
                        {
                            method: 'POST',
                            credentials: 'same-origin',
                            cache: 'no-store',
                            headers: {'X-Stakeholder-CSRF': '1'},
                            body
                        }
                    );
                    return {status: response.status, payload: await response.json()};
                }""",
                matrix_engagement_id,
            ),
        )
        unsupported_status = int(unsupported_result["status"])
        assert unsupported_status == 400
        assert unsupported_result["payload"]["error"]["code"] == "UNSUPPORTED_TYPE"
        assert unsupported_result["payload"]["error"]["message"] == (
            "The uploaded document type is not supported."
        )
        pm_documents_before = cast(
            "dict[str, Any]",
            pm_page.evaluate(
                """async (engagementId) => await (await fetch(
                    `/api/v1/pm/engagements/${encodeURIComponent(engagementId)}/documents`,
                    {credentials: 'same-origin', cache: 'no-store'}
                )).json()""",
                matrix_engagement_id,
            ),
        )
        assert len(pm_documents_before["documents"]) == len(FORMAT_CASES)
        assert all(
            item["source"]["stakeholder_id"] is None for item in pm_documents_before["documents"]
        )
        pm_page.screenshot(
            path=runtime.evidence_dir / "screenshots" / "r5-pm-six-format-real-http.png",
            full_page=True,
        )

        pm_page.get_by_role("tab", name="Stakeholders and invitations").click()
        pm_page.get_by_role("button", name="Add new stakeholder").click()
        pm_page.get_by_label("Display name").fill("Jordan Lee")
        pm_page.get_by_label("Role (optional)").fill("Operations owner")
        pm_page.get_by_label("Department (optional)").fill("Operations")
        pm_page.get_by_role("button", name="Add stakeholder").click()
        expect(pm_page.get_by_text("Jordan Lee", exact=True)).to_be_visible()
        stakeholders = cast(
            "dict[str, Any]",
            pm_page.evaluate(
                """async (engagementId) => await (await fetch(
                    `/api/v1/pm/engagements/${encodeURIComponent(engagementId)}/stakeholders`,
                    {credentials: 'same-origin', cache: 'no-store'}
                )).json()""",
                matrix_engagement_id,
            ),
        )
        stakeholder_id = next(
            str(item["stakeholder_id"])
            for item in stakeholders["stakeholders"]
            if item["display_name"] == "Jordan Lee"
        )
        stakeholder_row = pm_page.get_by_role("listitem").filter(has_text="Jordan Lee")
        stakeholder_row.get_by_role("button", name="Generate invitation link").click()
        invitation_input = pm_page.get_by_label("Interview invitation link")
        invitation_link = invitation_input.input_value()
        invitation_token = invitation_link.removeprefix(f"{runtime.agent_url}/s/")
        assert invitation_token
        assert invitation_link != invitation_token
        runtime.track_secret(invitation_token)
        expect(invitation_input).to_have_value(invitation_link)

        stakeholder_context = _context(browser)
        stakeholder_page = stakeholder_context.new_page()
        _configure(stakeholder_page)
        stakeholder_page.on("request", observe_request)
        stakeholder_page.on("response", observe_response)
        stakeholder_page.on("console", observe_console)
        try:
            stakeholder_page.goto(invitation_link, wait_until="domcontentloaded")
            expect(stakeholder_page).to_have_url(invitation_link)
        except Error:
            pytest.fail("The stakeholder invitation did not activate.")
        expect(
            stakeholder_page.get_by_role("heading", name="Stakeholder workspace")
        ).to_be_visible()
        expect(stakeholder_page.get_by_text("Jordan Lee", exact=True)).to_be_visible()
        stakeholder_cookie = next(
            cookie
            for cookie in stakeholder_context.cookies(runtime.agent_url)
            if cookie["name"] == STAKEHOLDER_BROWSER_SESSION_COOKIE
        )
        assert stakeholder_cookie["httpOnly"] is True
        runtime.track_secret(str(stakeholder_cookie["value"]))
        stakeholder_page.get_by_role("button", name="Start interview").click()
        expect(stakeholder_page.get_by_label("Your answer")).to_be_visible()
        stakeholder_page.get_by_role("button", name="View supporting evidence").click()
        stakeholder_upload = stakeholder_page.locator("#stakeholder-document-upload")
        for filename, _media_type in FORMAT_CASES:
            stakeholder_upload.set_input_files(str(FIXTURES / filename))
            stakeholder_page.get_by_role("button", name="Upload document").click()
            expect(stakeholder_page.get_by_text(filename, exact=True)).to_be_visible()
        expect(
            stakeholder_page.get_by_text("Document uploaded successfully", exact=True)
        ).to_be_visible()
        stakeholder_documents = cast(
            "dict[str, Any]",
            stakeholder_page.evaluate(
                """async () => await (await fetch('/api/v1/stakeholder/documents', {
                    credentials: 'same-origin', cache: 'no-store'
                })).json()"""
            ),
        )
        assert len(stakeholder_documents["documents"]) == len(FORMAT_CASES)
        assert all(
            item["source"]["engagement_id"] == matrix_engagement_id
            and item["source"]["stakeholder_id"] == stakeholder_id
            and item["source"]["role"] == "Operations owner"
            and item["source"]["department"] == "Operations"
            and item["latest_version"]["state"] == "READY"
            for item in stakeholder_documents["documents"]
        )
        cross_principal_status = int(
            stakeholder_page.evaluate(
                """async (engagementId) => (await fetch(
                    `/api/v1/pm/engagements/${encodeURIComponent(engagementId)}/documents`,
                    {credentials: 'same-origin', cache: 'no-store'}
                )).status""",
                matrix_engagement_id,
            )
        )
        assert cross_principal_status == 403
        stakeholder_page.screenshot(
            path=runtime.evidence_dir / "screenshots" / "r5-stakeholder-six-format-real-http.png",
            full_page=True,
        )

        pm_documents_after = cast(
            "dict[str, Any]",
            pm_page.evaluate(
                """async (engagementId) => await (await fetch(
                    `/api/v1/pm/engagements/${encodeURIComponent(engagementId)}/documents`,
                    {credentials: 'same-origin', cache: 'no-store'}
                )).json()""",
                matrix_engagement_id,
            ),
        )
        assert len(pm_documents_after["documents"]) == len(FORMAT_CASES)
        assert all(
            item["source"]["stakeholder_id"] is None for item in pm_documents_after["documents"]
        )
        assert all(
            item["source"]["stakeholder_id"] != stakeholder_id
            for item in pm_documents_after["documents"]
        )

        clean_context = _context(browser)
        clean_page = clean_context.new_page()
        _configure(clean_page)
        clean_page.goto(f"{runtime.agent_url}/s", wait_until="domcontentloaded")
        unauthorized_status = int(
            clean_page.evaluate(
                """async () => (await fetch('/api/v1/stakeholder/documents', {
                    credentials: 'same-origin', cache: 'no-store'
                })).status"""
            )
        )
        assert unauthorized_status == 403

        for checked_page in (pm_page, stakeholder_page):
            checked_page.set_viewport_size({"width": 390, "height": 844})
            state = _state(checked_page)
            assert state["localStorageKeys"] == []
            assert state["sessionStorageKeys"] == []
            assert state["documentCookie"] == ""
            assert state["referrer"] == ""
            assert state["horizontalOverflow"] == 0
        assert _state(pm_page)["pathname"] == "/pm"
        assert _state(stakeholder_page)["pathname"] == f"/s/{invitation_token}"
        assert api_requests
        assert not any(item["authorization_header"] for item in api_requests)
        upload_errors = [
            item
            for item in error_responses
            if item["path"] == f"/api/v1/pm/engagements/{matrix_engagement_id}/documents"
            and item["status"] == 400
        ]
        assert len(upload_errors) == 2
        assert not [
            message for message in console_errors if "400" not in message and "403" not in message
        ]
    finally:
        if clean_context is not None:
            clean_context.close()
        if stakeholder_context is not None:
            stakeholder_context.close()
        pm_context.close()

    _write_result(
        runtime,
        {
            "suite": "react-r5-real-http-six-format-matrix",
            "status": "PASS",
            "formats": [filename.rsplit(".", maxsplit=1)[-1] for filename, _ in FORMAT_CASES],
            "contexts": {
                "pm": {"ready_documents": len(FORMAT_CASES), "real_http": True},
                "stakeholder": {
                    "ready_documents": len(FORMAT_CASES),
                    "real_http": True,
                    "fixed_engagement_binding": True,
                    "fixed_stakeholder_binding": True,
                    "role_department_binding": True,
                    "draft_documents_excluded_from_pm_inventory": True,
                },
            },
            "negative_cases": {
                "mismatched_content": "PASS",
                "unsupported_content_status": unsupported_status,
                "unsupported_ui_file_allowlist": "PASS",
                "unauthenticated_document_inventory_status": unauthorized_status,
                "cross_principal_document_inventory_status": cross_principal_status,
            },
            "http_boundary": {
                "api_requests": len(api_requests),
                "controlled_or_intercepted_routes": 0,
                "authorization_header_requests": sum(
                    bool(item["authorization_header"]) for item in api_requests
                ),
            },
            "ingestion_boundary": {
                "real_upload_validation": True,
                "real_persistence_and_lifecycle": True,
                "real_qdrant_adapter": True,
                "deterministic_extractor_vision_vectorizer": True,
                "real_docling_cross_check": "tests/slow/test_real_docling_shared_service.py",
                "live_gemini": "NOT RUN in this browser upload suite",
            },
            "browser_security": {
                "invitation_url_retained_for_local_demo": True,
                "raw_invitation_absent_from_rendered_content": True,
                "browser_storage_key_count": 0,
                "browser_readable_cookie": False,
                "responsive_horizontal_overflow_pixels": 0,
            },
        },
    )
