"""Focused React verification through a real deterministic Agent Server."""

from __future__ import annotations

import json
import re
import time
import zipfile
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import httpx
import pytest
from playwright.sync_api import Browser, Page, expect

if TYPE_CHECKING:
    from playwright.sync_api import BrowserContext

    from tests.e2e.conftest import BrowserRuntime

pytestmark = [pytest.mark.e2e, pytest.mark.integration, pytest.mark.security]
NORMAL_TIMEOUT_MS = 60_000
FIXTURES = Path(__file__).parents[1] / "fixtures" / "ingestion"
LONG_VISION_CANARY = "LONG-VISION-CANARY-ORCHID"
CHECKPOINT_MARKERS = (b"checkpoint_message_id", b"checkpoint_id")


@dataclass(frozen=True, slots=True)
class CaseData:
    """Synthetic identifiers and secrets for one isolated browser scenario."""

    pm_token: str
    engagement_id: str
    engagement_name: str
    stakeholder_id: str
    stakeholder_name: str
    invitation_token: str
    invitation_url: str


def _context(browser: Browser) -> BrowserContext:
    return browser.new_context(
        viewport={"width": 1280, "height": 900},
        locale="en-US",
        accept_downloads=True,
    )


def _configure(page: Page) -> None:
    page.set_default_timeout(NORMAL_TIMEOUT_MS)
    page.set_default_navigation_timeout(NORMAL_TIMEOUT_MS)


def _api_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "X-Correlation-ID": "browser-preflight-setup",
    }


def _create_case(
    runtime: BrowserRuntime,
    label: str,
    *,
    role: str = "Finance Operations Lead",
    department: str = "Finance",
) -> CaseData:
    """Create isolated records through the real bearer HTTP routes."""
    with httpx.Client(base_url=runtime.agent_url, timeout=30.0) as client:
        activated = client.post(
            "/api/v1/auth/pm/activate",
            json={"bootstrap_token": runtime.pm_bootstrap_secret},
            headers={"X-Correlation-ID": f"{label}-pm-activate"},
        )
        assert activated.status_code == 200
        pm_token = str(activated.json()["access_token"])
        runtime.track_secret(pm_token)
        engagement_name = f"Post-release {label} {runtime.run_id[-8:]}"
        engagement = client.post(
            "/api/v1/pm/engagements",
            json={
                "name": engagement_name,
                "description": f"Synthetic isolated {label} browser evidence.",
            },
            headers=_api_headers(pm_token),
        )
        assert engagement.status_code == 201
        engagement_id = str(engagement.json()["engagement"]["engagement_id"])
        stakeholder_name = f"Alex {label.title()}"
        stakeholder = client.post(
            f"/api/v1/pm/engagements/{engagement_id}/stakeholders",
            json={
                "display_name": stakeholder_name,
                "role": role,
                "department": department,
            },
            headers=_api_headers(pm_token),
        )
        assert stakeholder.status_code == 201
        stakeholder_id = str(stakeholder.json()["stakeholder"]["stakeholder_id"])
        invitation = client.post(
            (f"/api/v1/pm/engagements/{engagement_id}/stakeholders/{stakeholder_id}/invitations"),
            headers=_api_headers(pm_token),
        )
        assert invitation.status_code == 201
        invitation_token = str(invitation.json()["invitation_token"])
        runtime.track_secret(invitation_token)
    return CaseData(
        pm_token=pm_token,
        engagement_id=engagement_id,
        engagement_name=engagement_name,
        stakeholder_id=stakeholder_id,
        stakeholder_name=stakeholder_name,
        invitation_token=invitation_token,
        invitation_url=f"{runtime.agent_url}/s/{invitation_token}",
    )


def _open_pm(page: Page, runtime: BrowserRuntime, case: CaseData) -> None:
    response = page.goto(f"{runtime.agent_url}/pm", wait_until="domcontentloaded")
    assert response is not None
    assert response.status == 200
    secret = page.get_by_label("Access key")
    expect(secret).to_be_visible()
    secret.fill(runtime.pm_bootstrap_secret)
    secret.press("Enter")
    expect(page.get_by_role("heading", name="Project manager workspace")).to_be_visible()
    row = page.get_by_role("listitem").filter(has_text=case.engagement_name)
    expect(row).to_have_count(1)
    row.get_by_role("button", name="Open", exact=True).click()
    active_engagement = page.get_by_role("region", name="Active engagement")
    expect(active_engagement).to_be_visible()
    expect(active_engagement.get_by_text(case.engagement_name, exact=True)).to_be_visible()


def _open_stakeholder(page: Page, case: CaseData) -> None:
    response = page.goto(case.invitation_url, wait_until="domcontentloaded")
    assert response is not None
    assert response.status == 200
    expect(page).to_have_url(case.invitation_url)
    expect(page.get_by_role("heading", name="Stakeholder workspace")).to_be_visible()
    expect(page.get_by_text(case.stakeholder_name, exact=True)).to_be_visible()


def _interview_turns(page: Page) -> Any:
    return page.get_by_role("log", name="Current interview turns").locator("article")


def _start_interview(page: Page) -> None:
    expect(page.get_by_label("Your answer")).to_have_count(0)
    page.get_by_role("button", name="Start interview").click()
    expect(page.get_by_label("Your answer")).to_be_visible()


def _write_case(runtime: BrowserRuntime, name: str, payload: dict[str, Any]) -> None:
    target = runtime.evidence_dir / f"{name}.json"
    target.write_text(
        json.dumps({"case": name, "status": "PASS", **payload}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _safe_browser_state(page: Page) -> dict[str, Any]:
    return cast(
        "dict[str, Any]",
        page.evaluate(
            """() => ({
                pathname: window.location.pathname,
                href: window.location.href,
                localStorageKeys: Object.keys(window.localStorage),
                sessionStorageKeys: Object.keys(window.sessionStorage),
                documentCookie: document.cookie,
                referrer: document.referrer,
                h1Count: document.querySelectorAll('h1').length,
                headings: Array.from(document.querySelectorAll('h1,h2,h3,h4,h5,h6'))
                    .map((heading) => ({
                        level: Number(heading.tagName.slice(1)),
                        text: heading.textContent?.trim()
                    })),
                horizontalOverflow: Math.max(
                    0,
                    document.documentElement.scrollWidth - document.documentElement.clientWidth
                )
            })"""
        ),
    )


def _assert_no_checkpoint_projection(value: object) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            assert "checkpoint" not in str(key).casefold()
            _assert_no_checkpoint_projection(child)
    elif isinstance(value, list):
        for child in value:
            _assert_no_checkpoint_projection(child)


def _status(page: Page) -> dict[str, Any]:
    return cast(
        "dict[str, Any]",
        page.evaluate(
            """async () => {
                const response = await fetch('/api/v1/stakeholder/interview/status', {
                    credentials: 'same-origin', cache: 'no-store'
                });
                return await response.json();
            }"""
        ),
    )


def _scan_trace(path: Path, forbidden: dict[str, bytes]) -> dict[str, int]:
    counts = dict.fromkeys(forbidden, 0)
    with zipfile.ZipFile(path) as archive:
        for name in archive.namelist():
            payload = archive.read(name)
            for label, value in forbidden.items():
                counts[label] += payload.count(value)
    return counts


@pytest.mark.timeout(240)
def test_shell_authentication_and_workspace_hierarchy(
    browser: Browser,
    react_preflight_runtime: BrowserRuntime,
) -> None:
    runtime = react_preflight_runtime
    case = _create_case(runtime, "shell")
    pm_context = _context(browser)
    stakeholder_context = _context(browser)
    pm_page = pm_context.new_page()
    stakeholder_page = stakeholder_context.new_page()
    _configure(pm_page)
    _configure(stakeholder_page)
    try:
        pm_page.goto(f"{runtime.agent_url}/pm", wait_until="domcontentloaded")
        expect(
            pm_page.get_by_role("heading", name="Stakeholder Intelligence", level=1)
        ).to_be_visible()
        expect(pm_page.get_by_role("heading", name="Project manager access")).to_be_visible()
        pm_page.get_by_label("Access key").fill(runtime.pm_bootstrap_secret)
        pm_page.get_by_label("Access key").press("Enter")
        expect(
            pm_page.get_by_role("heading", name="Project manager workspace", level=2)
        ).to_be_visible()
        expect(pm_page.get_by_role("heading", name="Project manager access")).to_have_count(0)
        assert runtime.pm_bootstrap_secret not in pm_page.content()
        row = pm_page.get_by_role("listitem").filter(has_text=case.engagement_name)
        row.get_by_role("button", name="Open", exact=True).click()
        active_engagement = pm_page.get_by_role("region", name="Active engagement")
        expect(active_engagement).to_be_visible()
        expect(active_engagement.get_by_text(case.engagement_name, exact=True)).to_be_visible()
        expect(pm_page.get_by_role("button", name="Change engagement")).to_be_visible()
        pm_state = _safe_browser_state(pm_page)
        assert pm_state["h1Count"] == 1
        assert pm_state["headings"][0]["level"] == 1
        assert any(
            item["level"] == 2 and item["text"] == "Project Manager Workspace"
            for item in pm_state["headings"]
        )

        _open_stakeholder(stakeholder_page, case)
        expect(
            stakeholder_page.get_by_role("heading", name="Stakeholder workspace", level=2)
        ).to_be_visible()
        expect(stakeholder_page.get_by_text("Interview invitation")).to_be_visible()
        stakeholder_state = _safe_browser_state(stakeholder_page)
        assert stakeholder_state["h1Count"] == 1
        assert stakeholder_state["pathname"] == f"/s/{case.invitation_token}"
        assert case.invitation_token not in stakeholder_page.content()
        pm_page.screenshot(path=runtime.evidence_dir / "screenshots" / "shell-pm-safe.png")
        stakeholder_page.screenshot(
            path=runtime.evidence_dir / "screenshots" / "shell-stakeholder-safe.png"
        )
    finally:
        stakeholder_context.close()
        pm_context.close()
    _write_case(
        runtime,
        "shell-authentication-and-workspace-hierarchy",
        {
            "pm_h1_count": 1,
            "stakeholder_h1_count": 1,
            "invitation_url_retained_for_local_demo": True,
        },
    )


@pytest.mark.timeout(240)
def test_real_agent_server_starts_scoped_insight_without_blocking_override(
    react_preflight_runtime: BrowserRuntime,
) -> None:
    runtime = react_preflight_runtime
    case = _create_case(runtime, "strict-insight-runtime")
    with httpx.Client(base_url=runtime.agent_url, timeout=30.0) as client:
        started = client.post(
            f"/api/v1/pm/engagements/{case.engagement_id}/insights",
            json={"question": "Can the scoped insight runtime begin its research plan?"},
            headers=_api_headers(case.pm_token),
        )
        assert started.status_code == 202, started.text
        run_id = str(started.json()["run"]["run_id"])
        deadline = time.monotonic() + 45.0
        status_payload: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            status_response = client.get(
                f"/api/v1/pm/engagements/{case.engagement_id}/insights/{run_id}",
                headers=_api_headers(case.pm_token),
            )
            assert status_response.status_code == 200, status_response.text
            status_payload = cast("dict[str, Any]", status_response.json())
            if status_payload["run"]["status"] in {
                "complete",
                "partial",
                "insufficient_evidence",
                "failed",
            }:
                break
            time.sleep(0.25)
        assert status_payload is not None
        assert status_payload["run"]["status"] == "failed"
        events = client.get(
            f"/api/v1/pm/engagements/{case.engagement_id}/insights/{run_id}/events",
            headers=_api_headers(case.pm_token),
        )
        assert events.status_code == 200, events.text
        assert '"action":"research_plan_saved"' in events.text
        assert '"action":"run_researching"' in events.text
        assert '"actor":"insight_orchestrator"' in events.text

    _write_case(
        runtime,
        "real-agent-server-scoped-insight-no-blocking-override",
        {
            "agent_server_blocking_override": False,
            "research_plan_persisted": True,
            "research_state_reached": True,
            "probe_terminal_status": status_payload["run"]["status"],
        },
    )


@pytest.mark.timeout(360)
def test_protected_preview_and_format_aware_analysis(  # noqa: PLR0915
    browser: Browser,
    react_preflight_runtime: BrowserRuntime,
) -> None:
    runtime = react_preflight_runtime
    case = _create_case(runtime, "documents")
    context = _context(browser)
    page = context.new_page()
    _configure(page)
    formats = (
        ("alpha-mixed-content.pdf", "alpha-mixed-content.pdf"),
        ("alpha-stakeholder-brief.docx", "alpha-stakeholder-brief.docx"),
        ("alpha-evidence-deck.pptx", "alpha-evidence-deck.pptx"),
        ("alpha-stakeholder-signals.xlsx", "alpha-stakeholder-signals.xlsx"),
        ("alpha-organization-chart.png", "alpha-organization-chart.png"),
    )
    try:
        _open_pm(page, runtime, case)
        page.get_by_role("tab", name="Documents").click()
        upload = page.locator("#pm-document-upload")
        for fixture_name, visible_name in formats:
            upload.set_input_files(str(FIXTURES / fixture_name))
            page.get_by_role("button", name="Upload document").click()
            expect(page.get_by_text(visible_name, exact=True)).to_be_visible()

        pdf_row = page.locator("tr").filter(has_text="alpha-mixed-content.pdf")
        with page.expect_response(re.compile(r"/documents/.+/artifacts/.+")) as pdf_info:
            pdf_row.get_by_role("button", name="Preview").click()
        pdf_response = pdf_info.value
        assert pdf_response.status == 200
        assert pdf_response.headers["content-type"].startswith("application/pdf")
        assert pdf_response.headers["cache-control"] == "no-store"
        assert pdf_response.headers["x-frame-options"] == "SAMEORIGIN"
        assert "frame-ancestors 'self'" in pdf_response.headers["content-security-policy"]
        pdf_probe = page.context.request.get(pdf_response.url)
        try:
            assert pdf_probe.status == 200
            assert pdf_probe.body().startswith(b"%PDF")
        finally:
            pdf_probe.dispose()
        pdf_preview = page.get_by_role("region", name="alpha-mixed-content.pdf")
        expect(pdf_preview.get_by_title("Preview of alpha-mixed-content.pdf")).to_be_visible()
        expect(pdf_preview.get_by_role("link", name="Download")).to_have_attribute(
            "download", "alpha-mixed-content.pdf"
        )
        pdf_preview.get_by_role("button", name="Close preview").click()

        png_row = page.locator("tr").filter(has_text="alpha-organization-chart.png")
        with page.expect_response(re.compile(r"/documents/.+/artifacts/.+")) as png_info:
            png_row.get_by_role("button", name="Preview").click()
        png_response = png_info.value
        assert png_response.status == 200
        assert png_response.headers["content-type"].startswith("image/png")
        image = page.get_by_alt_text("Original source alpha-organization-chart.png")
        expect(image).to_be_visible()
        assert image.evaluate("(element) => element.naturalWidth > 0") is True
        page.get_by_role("button", name="Close preview").click()

        pdf_row.get_by_role("button", name="View analysis").click()
        pdf_analysis = page.get_by_role("region", name="alpha-mixed-content.pdf")
        expect(pdf_analysis.get_by_text("What the system understood", exact=True)).to_be_visible()
        expect(pdf_analysis.get_by_text("Weekly readiness review", exact=True)).to_be_visible()
        expect(pdf_analysis.get_by_text(LONG_VISION_CANARY, exact=False)).to_be_visible()
        assert "window.__unsafeVisionMarkup" not in pdf_analysis.inner_html()
        expect(pdf_analysis.get_by_role("link", name="Unsafe generated link")).to_have_count(0)
        expect(pdf_analysis.locator('img[src*="invalid.example"]')).to_have_count(0)
        expect(pdf_analysis.get_by_text("retrieval chunks", exact=False)).to_have_count(0)
        expect(pdf_analysis.get_by_text("Processing details", exact=False)).to_have_count(0)
        pdf_analysis.get_by_role("button", name="Close analysis").click()

        png_row.get_by_role("button", name="View analysis").click()
        png_analysis = page.get_by_role("region", name="alpha-organization-chart.png")
        expect(png_analysis.get_by_text("Vision description", exact=True)).to_be_visible()
        expect(png_analysis.get_by_text(LONG_VISION_CANARY, exact=False)).to_be_visible()
        expect(png_analysis.get_by_text("Representative extracted content")).to_have_count(0)
        png_analysis.get_by_role("button", name="Close analysis").click()

        for filename in (
            "alpha-stakeholder-brief.docx",
            "alpha-evidence-deck.pptx",
            "alpha-stakeholder-signals.xlsx",
        ):
            row = page.locator("tr").filter(has_text=filename)
            expect(row.get_by_role("link", name="Download")).to_be_visible()
            row.get_by_role("button", name="View analysis").click()
            analysis = page.get_by_role("region", name=filename)
            expect(analysis.get_by_text("What the system understood", exact=True)).to_be_visible()
            expect(analysis.get_by_text("Weekly readiness review", exact=True)).to_be_visible()
            analysis.get_by_role("button", name="Close analysis").click()
        page.screenshot(
            path=runtime.evidence_dir / "screenshots" / "format-aware-analysis-safe.png",
            full_page=True,
        )
    finally:
        context.close()
    _write_case(
        runtime,
        "protected-preview-and-format-aware-analysis",
        {
            "agent_server_artifact_transport": True,
            "pdf_signature_valid": True,
            "png_decoded": True,
            "formats_checked": [item[0].rsplit(".", maxsplit=1)[-1] for item in formats],
            "unsafe_markdown_rendered": False,
        },
    )


@pytest.mark.timeout(300)
def test_interview_start_turns_and_restoration(
    browser: Browser,
    react_preflight_runtime: BrowserRuntime,
) -> None:
    runtime = react_preflight_runtime
    case = _create_case(runtime, "restoration")
    context = _context(browser)
    page = context.new_page()
    _configure(page)
    answer = "SHORT-ANSWER I record weekly readiness exceptions."
    try:
        _open_stakeholder(page, case)
        expect(page.get_by_label("Your answer")).to_have_count(0)
        with page.expect_response(
            re.compile(r"/api/v1/stakeholder/interview/start$")
        ) as start_info:
            page.get_by_role("button", name="Start interview").click()
        start_response = start_info.value
        assert start_response.status == 200, start_response.text()
        start_payload = cast("dict[str, Any]", start_response.json())
        (runtime.evidence_dir / "interview-start-response.json").write_text(
            json.dumps(start_payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        opening = (
            "What are the main tasks you personally perform in your day-to-day work as "
            "Finance Operations Lead?"
        )
        expect(page.get_by_text(opening, exact=True)).to_be_visible()
        expect(_interview_turns(page)).to_have_count(1)
        expect(page.get_by_label("Your answer")).to_be_visible()
        assert "checkpoint" not in page.get_by_role("main").inner_text().casefold()

        page.get_by_label("Your answer").fill(answer)
        page.get_by_role("button", name="Send answer").click()
        expect(
            page.get_by_text(
                "Who approves an exception when the weekly readiness evidence is incomplete?",
                exact=True,
            )
        ).to_be_visible()
        expect(_interview_turns(page)).to_have_count(3)

        page.reload(wait_until="domcontentloaded")
        expect(page.get_by_text("Conversation restored")).to_be_visible()
        expect(page.get_by_text(answer, exact=True)).to_be_visible()
        expect(_interview_turns(page)).to_have_count(3)
        status = _status(page)
        _assert_no_checkpoint_projection(status)
        assert [turn["turn_index"] for turn in status["turns"]] == [0, 1, 2]
        assert [turn["speaker"] for turn in status["turns"]] == [
            "assistant",
            "stakeholder",
            "assistant",
        ]
        page.screenshot(
            path=runtime.evidence_dir / "screenshots" / "conversation-restored-safe.png",
            full_page=True,
        )
    finally:
        context.close()
    _write_case(
        runtime,
        "interview-start-turns-and-restoration",
        {"opening_turn": 0, "restored_turn_count": 3, "checkpoint_identifiers_exposed": False},
    )


@pytest.mark.timeout(360)
def test_recommendation_continue_finish_and_pm_visibility(  # noqa: PLR0915
    browser: Browser,
    react_preflight_runtime: BrowserRuntime,
) -> None:
    runtime = react_preflight_runtime
    case = _create_case(runtime, "lifecycle")
    stakeholder_context = _context(browser)
    pm_context = _context(browser)
    page = stakeholder_context.new_page()
    pm_page = pm_context.new_page()
    _configure(page)
    _configure(pm_page)
    try:
        _open_stakeholder(page, case)
        _start_interview(page)
        expect(_interview_turns(page)).to_have_count(1)
        page.get_by_label("Your answer").fill(
            "SUBSTANTIVE-COVERAGE I own the weekly readiness review, record exceptions, and "
            "delay approval when Operations evidence is incomplete."
        )
        page.get_by_role("button", name="Send answer").click()
        expect(
            page.get_by_text(
                "Before we finish, is there anything important about your work on this project "
                "that we have not discussed?",
                exact=True,
            )
        ).to_be_visible()
        expect(page.get_by_role("button", name="Finish interview")).to_have_count(0)

        page.get_by_label("Your answer").fill("FINAL-CHECK-COMPLETE Nothing else is missing.")
        page.get_by_role("button", name="Send answer").click()
        expect(page.get_by_role("button", name="Finish interview")).to_be_visible()
        expect(_interview_turns(page)).to_have_count(5)
        page.reload(wait_until="domcontentloaded")
        expect(page.get_by_text("Conversation restored")).to_be_visible()
        expect(page.get_by_role("button", name="Finish interview")).to_be_visible()

        page.get_by_role("button", name="Add more information").click()
        page.get_by_label("Your answer").fill(
            "CONTINUE-AFTER-READY One additional issue affects the monthly close."
        )
        page.get_by_role("button", name="Send answer").click()
        expect(
            page.get_by_text(
                "Which team should be contacted first if that additional issue occurs?",
                exact=True,
            )
        ).to_be_visible()
        expect(_interview_turns(page)).to_have_count(7)

        finish = page.get_by_role("button", name="Finish interview")
        expect(finish).to_be_enabled()
        finish.click()
        expect(page.get_by_text("Interview finished")).to_be_visible()
        expect(
            page.get_by_text(
                "Your answers and any supporting documents are now locked and available "
                "for permitted project analysis.",
                exact=True,
            )
        ).to_be_visible()
        expect(page.get_by_label("Your answer")).to_have_count(0)
        finalized = _status(page)
        assert finalized["interview_session"]["status"] == "ready"
        assert finalized["turn_count"] == 7
        immutable_attempt = cast(
            "dict[str, Any]",
            page.evaluate(
                """async () => {
                    const response = await fetch('/api/v1/stakeholder/interview/turns/stream', {
                        method: 'POST', credentials: 'same-origin', cache: 'no-store',
                        headers: {'Content-Type': 'application/json', 'X-Stakeholder-CSRF': '1'},
                        body: JSON.stringify({
                            original_text: 'This late answer must not be stored.',
                            message_id: 'message-post-finalization-denial'
                        })
                    });
                    return {status: response.status, body: await response.text()};
                }"""
            ),
        )
        assert immutable_attempt["status"] == 200
        assert "TRANSCRIPT_IMMUTABLE" in immutable_attempt["body"]
        assert _status(page)["turn_count"] == 7

        _open_pm(pm_page, runtime, case)
        pm_page.get_by_role("tab", name="Interviews").click()
        expect(pm_page.get_by_text("Finalized interviews", exact=True)).to_be_visible()
        row = pm_page.locator("tr").filter(has_text=case.stakeholder_name)
        expect(row.get_by_text("ready", exact=True)).to_be_visible()
        expect(row.get_by_text("Ready for permitted retrieval", exact=True)).to_be_visible()
        row.get_by_role("button", name=f"Preview interview with {case.stakeholder_name}").click()
        expect(pm_page.get_by_label("Interview transcript")).to_be_visible()
        expect(
            pm_page.get_by_text(
                "CONTINUE-AFTER-READY One additional issue affects the monthly close.",
                exact=True,
            )
        ).to_be_visible()
        pm_page.get_by_role("button", name="Close preview").click()
        pm_finish_status = int(
            pm_page.evaluate(
                """async () => (await fetch('/api/v1/stakeholder/interview/finish', {
                    method: 'POST', credentials: 'same-origin', cache: 'no-store',
                    headers: {'X-Stakeholder-CSRF': '1'}
                })).status"""
            )
        )
        assert pm_finish_status == 403
        page.screenshot(
            path=runtime.evidence_dir / "screenshots" / "finalized-stakeholder-safe.png",
            full_page=True,
        )
        pm_page.screenshot(
            path=runtime.evidence_dir / "screenshots" / "finalized-pm-visible-safe.png",
            full_page=True,
        )
    finally:
        pm_context.close()
        stakeholder_context.close()
    _write_case(
        runtime,
        "recommendation-continue-finish-and-pm-visibility",
        {
            "final_open_check_preceded_recommendation": True,
            "continued_after_recommendation": True,
            "final_turn_count": 7,
            "immutable_after_finish": True,
            "pm_finish_status": 403,
        },
    )


def _upload_document(page: Page, filename: str) -> None:
    page.get_by_role("tab", name="Documents").click()
    page.locator("#pm-document-upload").set_input_files(str(FIXTURES / filename))
    page.get_by_role("button", name="Upload document").click()
    expect(page.get_by_text(filename, exact=True)).to_be_visible()


def _artifact_path(runtime: BrowserRuntime, case: CaseData) -> str:
    with httpx.Client(base_url=runtime.agent_url, timeout=30.0) as client:
        documents = client.get(
            f"/api/v1/pm/engagements/{case.engagement_id}/documents",
            headers=_api_headers(case.pm_token),
        )
        assert documents.status_code == 200
        document = documents.json()["documents"][0]
        document_id = str(document["source"]["document_id"])
        processing = client.get(
            f"/api/v1/pm/engagements/{case.engagement_id}/documents/{document_id}/processing",
            headers=_api_headers(case.pm_token),
        )
        assert processing.status_code == 200
        original = next(
            item for item in processing.json()["artifacts"] if item["artifact_kind"] == "original"
        )
        return str(original["download_path"])


@pytest.mark.timeout(360)
def test_authorization_and_cross_engagement_denials(
    browser: Browser,
    react_preflight_runtime: BrowserRuntime,
) -> None:
    runtime = react_preflight_runtime
    alpha = _create_case(runtime, "authorization-alpha")
    beta = _create_case(runtime, "authorization-beta")
    pm_context = _context(browser)
    alpha_context = _context(browser)
    replay_context = _context(browser)
    pm_page = pm_context.new_page()
    alpha_page = alpha_context.new_page()
    replay_page = replay_context.new_page()
    for page in (pm_page, alpha_page, replay_page):
        _configure(page)
    try:
        _open_pm(pm_page, runtime, beta)
        _upload_document(pm_page, "beta-process-map.jpg")
        beta_artifact_path = _artifact_path(runtime, beta)
        pm_page.get_by_role("button", name="Change engagement").click()
        alpha_row = pm_page.get_by_role("listitem").filter(has_text=alpha.engagement_name)
        alpha_row.get_by_role("button", name="Open", exact=True).click()
        cross_engagement_status = int(
            pm_page.evaluate(
                """async (path) => (await fetch(path, {
                    credentials: 'same-origin', cache: 'no-store'
                })).status""",
                beta_artifact_path,
            )
        )
        assert cross_engagement_status == 403

        _open_stakeholder(alpha_page, alpha)
        stakeholder_pm_status = int(
            alpha_page.evaluate(
                """async (path) => (await fetch(path, {
                    credentials: 'same-origin', cache: 'no-store'
                })).status""",
                beta_artifact_path,
            )
        )
        assert stakeholder_pm_status == 403
        stakeholder_finish_from_pm = int(
            pm_page.evaluate(
                """async () => (await fetch('/api/v1/stakeholder/interview/finish', {
                    method: 'POST', credentials: 'same-origin', cache: 'no-store',
                    headers: {'X-Stakeholder-CSRF': '1'}
                })).status"""
            )
        )
        assert stakeholder_finish_from_pm == 403

        replay_page.goto(alpha.invitation_url, wait_until="domcontentloaded")
        expect(replay_page).to_have_url(alpha.invitation_url)
        expect(replay_page.get_by_role("heading", name="Stakeholder workspace")).to_be_visible()
        assert alpha.invitation_token not in replay_page.content()
        assert alpha.invitation_token in replay_page.url
    finally:
        replay_context.close()
        alpha_context.close()
        pm_context.close()
    _write_case(
        runtime,
        "authorization-and-cross-engagement-denials",
        {
            "cross_engagement_artifact_status": 403,
            "stakeholder_pm_artifact_status": 403,
            "pm_stakeholder_finish_status": 403,
            "same_invitation_resume_succeeded": True,
        },
    )


@pytest.mark.timeout(300)
def test_retained_browser_evidence_safety(
    browser: Browser,
    react_preflight_runtime: BrowserRuntime,
) -> None:
    runtime = react_preflight_runtime
    case = _create_case(runtime, "evidence-safety")
    context = _context(browser)
    page = context.new_page()
    _configure(page)
    trace_path = runtime.evidence_dir / "traces" / "safe-browser-trace.zip"
    screenshot_path = runtime.evidence_dir / "screenshots" / "safe-browser-state.png"
    try:
        _open_stakeholder(page, case)
        _start_interview(page)
        context.tracing.start(screenshots=True, snapshots=True, sources=False)
        expect(page.get_by_label("Your answer")).to_be_visible()
        page.get_by_label("Your answer").fill("SHORT-ANSWER Synthetic trace safety response.")
        page.get_by_role("button", name="Send answer").click()
        expect(_interview_turns(page)).to_have_count(3)
        page.reload(wait_until="domcontentloaded")
        expect(page.get_by_text("Conversation restored")).to_be_visible()
        page.screenshot(path=screenshot_path, full_page=True)
        context.tracing.stop(path=trace_path)

        state = _safe_browser_state(page)
        assert state["pathname"] == f"/s/{case.invitation_token}"
        assert state["localStorageKeys"] == []
        assert state["sessionStorageKeys"] == []
        assert state["documentCookie"] == ""
        assert state["referrer"] == ""
        assert case.invitation_token in state["href"]
        assert case.invitation_token not in page.content()
        status = _status(page)
        _assert_no_checkpoint_projection(status)
        forbidden = {
            "pm_bootstrap_secret": runtime.pm_bootstrap_secret.encode(),
            "checkpoint_message_id": CHECKPOINT_MARKERS[0],
            "checkpoint_id": CHECKPOINT_MARKERS[1],
        }
        trace_counts = _scan_trace(trace_path, forbidden)
        assert all(count == 0 for count in trace_counts.values())
        log_payload = (runtime.evidence_dir / "logs" / "agent-server.log").read_bytes()
        assert all(value not in log_payload for value in forbidden.values())
        screenshot_payload = screenshot_path.read_bytes()
        assert case.invitation_token.encode() not in screenshot_payload
    finally:
        if context.pages:
            with suppress(Exception):  # Tracing may already be stopped after success.
                context.tracing.stop()
        context.close()
    _write_case(
        runtime,
        "retained-browser-evidence-safety",
        {
            "trace_path": str(trace_path.relative_to(runtime.evidence_dir)),
            "trace_forbidden_matches": trace_counts,
            "browser_storage_key_count": 0,
            "checkpoint_identifiers_exposed": False,
            "raw_invitation_retained": False,
        },
    )
