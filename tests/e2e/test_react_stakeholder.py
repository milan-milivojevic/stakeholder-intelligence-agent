"""Real-Chromium stakeholder parity verification for the built React application."""

from __future__ import annotations

import copy
import json
import re
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlparse

import httpx
import pytest
from playwright.sync_api import Browser, Error, Page, Request, Response, Route, expect
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

if TYPE_CHECKING:
    from playwright.sync_api import BrowserContext

    from tests.e2e.conftest import BrowserRuntime

pytestmark = [pytest.mark.e2e, pytest.mark.integration, pytest.mark.security]
NORMAL_TIMEOUT_MS = 60_000
CSRF_HEADER = "X-Stakeholder-CSRF"
TIMESTAMP = "2026-07-15T08:00:00Z"
COMPLETED_AT = "2026-07-15T08:05:00Z"
BROWSER_COOKIE = "stakeholder_ai_interview_session"


def _context(browser: Browser) -> BrowserContext:
    return browser.new_context(
        viewport={"width": 1280, "height": 900},
        locale="en-US",
        accept_downloads=False,
    )


def _configure(page: Page) -> None:
    page.set_default_timeout(NORMAL_TIMEOUT_MS)
    page.set_default_navigation_timeout(NORMAL_TIMEOUT_MS)


def _write_result(runtime: BrowserRuntime, filename: str, payload: dict[str, Any]) -> None:
    (runtime.evidence_dir / filename).write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _json(response: httpx.Response) -> dict[str, Any]:
    return cast("dict[str, Any]", response.json())


def _issue_invitation(
    runtime: BrowserRuntime,
) -> tuple[str, str, str, str, str]:
    with httpx.Client(base_url=runtime.agent_url, timeout=30.0) as client:
        activated = client.post(
            "/api/v1/auth/pm/activate",
            json={"bootstrap_token": runtime.pm_bootstrap_secret},
            headers={"X-Correlation-ID": "r4-pm-activation"},
        )
        assert activated.status_code == 200
        bearer = str(_json(activated)["access_token"])
        runtime.track_secret(bearer)
        authorization = {"Authorization": f"Bearer {bearer}"}
        engagement = client.post(
            "/api/v1/pm/engagements",
            json={
                "name": f"Stakeholder browser parity {runtime.run_id[-8:]}",
                "description": "Stakeholder browser parity verification.",
            },
            headers=authorization,
        )
        assert engagement.status_code == 201
        engagement_id = str(_json(engagement)["engagement"]["engagement_id"])
        stakeholder = client.post(
            f"/api/v1/pm/engagements/{engagement_id}/stakeholders",
            json={
                "display_name": "Alex Morgan",
                "role": "Operations lead",
                "department": "Operations",
            },
            headers=authorization,
        )
        assert stakeholder.status_code == 201
        stakeholder_id = str(_json(stakeholder)["stakeholder"]["stakeholder_id"])
        invitation = client.post(
            f"/api/v1/pm/engagements/{engagement_id}/stakeholders/{stakeholder_id}/invitations",
            headers=authorization,
        )
        assert invitation.status_code == 201
        invitation_body = _json(invitation)
        invitation_id = str(invitation_body["invitation"]["invitation_id"])
        invitation_token = str(invitation_body["invitation_token"])
        runtime.track_secret(invitation_token)
        return bearer, engagement_id, stakeholder_id, invitation_id, invitation_token


def _assert_opened_invitation_cannot_be_revoked(
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
    assert response.status_code == 409
    assert _json(response)["error"]["code"] == "DOMAIN_CONFLICT"


def _open_invitation(page: Page, runtime: BrowserRuntime, invitation_token: str) -> None:
    try:
        response = page.goto(
            f"{runtime.agent_url}/s/{invitation_token}",
            wait_until="domcontentloaded",
        )
        assert response is not None
        assert response.status == 200
    except (AssertionError, Error, PlaywrightTimeoutError):
        pytest.fail("The stakeholder invitation did not load.")


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
                horizontalOverflow: Math.max(
                    0,
                    document.documentElement.scrollWidth - document.documentElement.clientWidth
                )
            })"""
        ),
    )


def _document_payload(
    context: dict[str, Any],
    index: int,
    filename: str,
) -> dict[str, Any]:
    extension = filename.rsplit(".", maxsplit=1)[-1].casefold()
    doc_type = "jpeg" if extension in {"jpg", "jpeg"} else extension
    media_types = {
        "pdf": "application/pdf",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "png": "image/png",
        "jpeg": "image/jpeg",
    }
    document_id = f"stakeholder-document-{index}"
    stakeholder = cast("dict[str, Any]", context["stakeholder"])
    engagement = cast("dict[str, Any]", context["engagement"])
    return {
        "source": {
            "document_id": document_id,
            "engagement_id": engagement["engagement_id"],
            "stakeholder_id": stakeholder["stakeholder_id"],
            "role": stakeholder["role"],
            "department": stakeholder["department"],
            "doc_type": doc_type,
            "source_type": "stakeholder_document",
            "original_filename": filename,
            "media_type": media_types[doc_type],
            "created_at": TIMESTAMP,
        },
        "latest_version": {
            "document_version_id": f"stakeholder-document-version-{index}",
            "document_id": document_id,
            "version_number": 1,
            "content_hash": f"{index:x}".ljust(64, "a"),
            "state": "READY",
            "is_active": True,
            "original_artifact_id": f"stakeholder-artifact-{index}",
            "ingestion_key": f"stakeholder-ingestion-{index}",
            "created_at": TIMESTAMP,
            "ready_at": COMPLETED_AT,
            "superseded_at": None,
            "failure_code": None,
            "failure_message": None,
        },
    }


def _interview_status_payload(
    context: dict[str, Any],
    *,
    turns: list[dict[str, object]],
    ready: bool,
) -> dict[str, Any]:
    session = copy.deepcopy(cast("dict[str, Any]", context["interview_session"]))
    transcript_id = "transcript-r4-controlled"
    session.update(
        {
            "status": "ready" if ready else "draft",
            "finalized_at": COMPLETED_AT if ready else None,
            "transcript_id": transcript_id,
            "ingestion_version_id": "transcript-version-r4-controlled" if ready else None,
            "failure_code": None,
            "failure_message": None,
        }
    )
    stakeholder = cast("dict[str, Any]", context["stakeholder"])
    transcript = {
        "transcript_id": transcript_id,
        "interview_session_id": session["interview_session_id"],
        "engagement_id": session["engagement_id"],
        "stakeholder_id": session["stakeholder_id"],
        "role": stakeholder["role"],
        "department": stakeholder["department"],
        "status": "finalized" if ready else "draft",
        "language_observations": [],
        "finalized_at": COMPLETED_AT if ready else None,
        "content_hash": "b" * 64 if ready else None,
    }
    ingestion = (
        {
            "transcript_ingestion_version_id": "transcript-version-r4-controlled",
            "transcript_id": transcript_id,
            "content_hash": "b" * 64,
            "state": "READY",
            "is_active": True,
            "created_at": COMPLETED_AT,
            "ready_at": COMPLETED_AT,
            "failure_code": None,
            "failure_message": None,
        }
        if ready
        else None
    )
    return {
        "interview_session": session,
        "transcript": transcript,
        "ingestion_version": ingestion,
        "turns": turns,
        "turn_count": len(turns),
        "completion_recommended": any(
            turn.get("speaker") == "assistant"
            and str(turn.get("text", "")).startswith(
                "Thank you. I have enough information to complete this interview."
            )
            for turn in turns
        ),
    }


@pytest.mark.timeout(480)
def test_react_stakeholder_complete_parity_with_restart_and_permanent_finish(  # noqa: PLR0915
    browser: Browser,
    react_browser_runtime: BrowserRuntime,
) -> None:
    runtime = react_browser_runtime
    bearer, engagement_id, stakeholder_id, invitation_id, invitation_token = _issue_invitation(
        runtime
    )
    context = _context(browser)
    page = context.new_page()
    _configure(page)
    authorization_seen: list[bool] = []
    http_error_responses: list[dict[str, object]] = []
    console_errors: list[str] = []
    handler_failures: list[str] = []
    uploaded_documents: list[dict[str, Any]] = []
    upload_observations: list[dict[str, Any]] = []
    stream_observations: list[dict[str, Any]] = []
    message_attempts: dict[str, int] = {}
    lifecycle: dict[str, object] = {
        "turns": [],
        "ready": False,
        "fail_next_new_message": False,
    }

    def observe_request(request: Request) -> None:
        if request.url.startswith(f"{runtime.agent_url}/api/"):
            authorization_seen.append("authorization" in request.headers)

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

    try:
        _open_invitation(page, runtime, invitation_token)
        expect(page.get_by_role("heading", name="Stakeholder workspace")).to_be_visible()
        expect(page.get_by_text("Interview invitation")).to_be_visible()
        expect(page.get_by_text("Alex Morgan", exact=True)).to_be_visible()
        expect(page.get_by_text("Operations lead · Operations", exact=True)).to_be_visible()
        expect(page.get_by_role("button", name="Start interview")).to_be_visible()
        expect(page.get_by_label("Your answer")).to_have_count(0)

        cookies = [
            cookie
            for cookie in context.cookies(runtime.agent_url)
            if cookie["name"] == BROWSER_COOKIE
        ]
        assert len(cookies) == 1
        runtime.track_secret(str(cookies[0]["value"]))
        assert cookies[0]["httpOnly"] is True

        safe_context = cast(
            "dict[str, Any]",
            page.evaluate(
                """async () => {
                    const response = await fetch('/api/v1/stakeholder/context', {
                        credentials: 'same-origin',
                        cache: 'no-store',
                        headers: {Accept: 'application/json'}
                    });
                    return await response.json();
                }"""
            ),
        )
        assert safe_context["engagement"]["engagement_id"] == engagement_id
        assert safe_context["stakeholder"]["stakeholder_id"] == stakeholder_id

        resume_context = _context(browser)
        resume_page = resume_context.new_page()
        _configure(resume_page)
        try:
            _open_invitation(resume_page, runtime, invitation_token)
            expect(resume_page.get_by_role("heading", name="Stakeholder workspace")).to_be_visible()
            assert invitation_token not in resume_page.content()
        finally:
            resume_context.close()

        page.screenshot(
            path=runtime.evidence_dir / "screenshots" / "r4-stakeholder-context-safe.png"
        )

        def handle_stakeholder_contract(route: Route) -> None:  # noqa: PLR0911, PLR0915
            request = route.request
            path = urlparse(request.url).path
            method = request.method
            valid_csrf = request.headers.get(CSRF_HEADER.lower()) == "1"
            no_authorization = "authorization" not in request.headers

            if path == "/api/v1/stakeholder/documents":
                if method == "GET":
                    route.fulfill(
                        status=200,
                        content_type="application/json",
                        body=json.dumps({"documents": uploaded_documents}),
                    )
                    return
                body = request.post_data_buffer or b""
                filename_match = re.search(rb'filename="([^"]+)"', body)
                filename = (
                    filename_match.group(1).decode("utf-8", errors="replace")
                    if filename_match is not None
                    else "missing"
                )
                multipart = b'name="upload"' in body and filename_match is not None
                if not (valid_csrf and no_authorization and multipart):
                    handler_failures.append("The stakeholder upload violated the browser contract.")
                document = _document_payload(safe_context, len(uploaded_documents) + 1, filename)
                uploaded_documents.insert(0, document)
                upload_observations.append(
                    {
                        "filename": filename,
                        "csrf": valid_csrf,
                        "authorization_absent": no_authorization,
                        "multipart_upload_field": multipart,
                    }
                )
                route.fulfill(
                    status=201,
                    content_type="application/json",
                    body=json.dumps(
                        {
                            "document": document,
                            "element_count": 2,
                            "chunk_count": 1,
                            "attempt_id": f"attempt-{len(uploaded_documents)}",
                            "idempotent": False,
                        }
                    ),
                )
                return

            if path == "/api/v1/stakeholder/interview/start" and method == "POST":
                if not (valid_csrf and no_authorization):
                    handler_failures.append("Interview start violated the browser contract.")
                turns = cast("list[dict[str, object]]", lifecycle["turns"])
                if not turns:
                    turns.append(
                        {
                            "turn_index": 0,
                            "speaker": "assistant",
                            "text": (
                                "What are the main tasks you personally perform in your "
                                "day-to-day work as Finance Operations Lead?"
                            ),
                        }
                    )
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(
                        _interview_status_payload(
                            safe_context,
                            turns=turns,
                            ready=bool(lifecycle["ready"]),
                        )
                    ),
                )
                return

            if path == "/api/v1/stakeholder/interview/status" and method == "GET":
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(
                        _interview_status_payload(
                            safe_context,
                            turns=cast("list[dict[str, object]]", lifecycle["turns"]),
                            ready=bool(lifecycle["ready"]),
                        )
                    ),
                )
                return

            if path == "/api/v1/stakeholder/interview/turns/stream" and method == "POST":
                payload = json.loads(request.post_data or "{}")
                original_text = str(payload.get("original_text", ""))
                message_id = str(payload.get("message_id", ""))
                attempt = message_attempts.get(message_id, 0) + 1
                message_attempts[message_id] = attempt
                should_fail = bool(lifecycle["fail_next_new_message"]) and attempt == 1
                if not (valid_csrf and no_authorization and message_id.startswith("message-")):
                    handler_failures.append("The interview stream violated the browser contract.")
                stream_observations.append(
                    {
                        "message_id": message_id,
                        "attempt": attempt,
                        "original_text_length": len(original_text),
                        "csrf": valid_csrf,
                        "authorization_absent": no_authorization,
                        "outcome": "controlled_failure" if should_fail else "controlled_success",
                    }
                )
                if should_fail:
                    failure = {
                        "stage": "interview",
                        "status": "failed",
                        "failure_code": "INTERVIEW_EXECUTION_FAILED",
                        "failure_message": "The interview response could not be completed.",
                        "correlation_id": "correlation-r4-retry",
                    }
                    route.fulfill(
                        status=200,
                        headers={"Content-Type": "text/event-stream", "Cache-Control": "no-store"},
                        body=f"event: failure\ndata: {json.dumps(failure)}\n\n",
                    )
                    return
                turns = cast("list[dict[str, object]]", lifecycle["turns"])
                stakeholder_turn_index = len(turns)
                assistant_turn_index = stakeholder_turn_index + 1
                assistant_text = (
                    "Thank you. I have enough information to complete this interview. "
                    "You can finish now, or continue if you would like to add something else."
                    if assistant_turn_index >= 4
                    else "Who approves an exception to that operating process?"
                )
                turns.extend(
                    [
                        {
                            "turn_index": stakeholder_turn_index,
                            "speaker": "stakeholder",
                            "text": original_text,
                        },
                        {
                            "turn_index": assistant_turn_index,
                            "speaker": "assistant",
                            "text": assistant_text,
                        },
                    ]
                )
                lifecycle["fail_next_new_message"] = False
                status_started = {
                    "stage": "interview",
                    "status": "started",
                    "message_id": message_id,
                    "correlation_id": "correlation-r4-stream",
                }
                assistant = {
                    "message_id": message_id,
                    "stakeholder_turn_index": stakeholder_turn_index,
                    "assistant_turn_index": assistant_turn_index,
                    "assistant_text": assistant_text,
                    "correlation_id": "correlation-r4-stream",
                }
                status_succeeded = {**status_started, "status": "succeeded"}
                route.fulfill(
                    status=200,
                    headers={"Content-Type": "text/event-stream", "Cache-Control": "no-store"},
                    body=(
                        f"event: status\ndata: {json.dumps(status_started)}\n\n"
                        f"event: message\ndata: {json.dumps(assistant)}\n\n"
                        f"event: status\ndata: {json.dumps(status_succeeded)}\n\n"
                    ),
                )
                return

            if path == "/api/v1/stakeholder/interview/finish" and method == "POST":
                if not (valid_csrf and no_authorization):
                    handler_failures.append("The Finish request violated the browser contract.")
                lifecycle["ready"] = True
                status = _interview_status_payload(
                    safe_context,
                    turns=cast("list[dict[str, object]]", lifecycle["turns"]),
                    ready=True,
                )
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(
                        {
                            "interview_session": status["interview_session"],
                            "transcript": status["transcript"],
                            "ingestion_version": status["ingestion_version"],
                            "chunk_count": 2,
                            "idempotent": False,
                        }
                    ),
                )
                return

            route.continue_()

        page.route("**/api/v1/stakeholder/**", handle_stakeholder_contract)

        page.get_by_role("button", name="Start interview").click()
        expect(page.get_by_label("Your answer")).to_be_visible()
        expect(
            page.get_by_role("log", name="Current interview turns").locator("article")
        ).to_have_count(1)
        page.get_by_role("button", name="View supporting evidence").click()
        expect(page.get_by_text("No supporting evidence has been added.")).to_be_visible()

        upload_matrix = [
            ("support.pdf", "application/pdf"),
            (
                "support.docx",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ),
            (
                "support.pptx",
                "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            ),
            (
                "support.xlsx",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ),
            ("support.png", "image/png"),
            ("support.jpeg", "image/jpeg"),
        ]
        for filename, media_type in upload_matrix:
            page.locator("#stakeholder-document-upload").set_input_files(
                {"name": filename, "mimeType": media_type, "buffer": b"format test bytes"}
            )
            page.get_by_role("button", name="Upload document").click()
            expect(page.get_by_text(filename, exact=True)).to_be_visible()
        assert [item["filename"] for item in upload_observations] == [
            item[0] for item in upload_matrix
        ]

        exact_response = "  I own the weekly review and record exceptions.  "
        expect(
            page.get_by_text(
                "What are the main tasks you personally perform in your day-to-day work "
                "as Finance Operations Lead?"
            )
        ).to_be_visible()
        page.get_by_label("Your answer").fill(exact_response)
        page.get_by_role("button", name="Send answer").click()
        expect(
            page.get_by_text("Who approves an exception to that operating process?")
        ).to_be_visible()
        expect(
            page.get_by_role("log", name="Current interview turns").locator("article")
        ).to_have_count(3)
        page.screenshot(
            path=runtime.evidence_dir / "screenshots" / "r4-stakeholder-stream-safe.png"
        )

        runtime.restart_agent()
        page.reload(wait_until="domcontentloaded")
        expect(page.get_by_text("Interview invitation")).to_be_visible()
        expect(page.get_by_text("Conversation restored")).to_be_visible()
        expect(page.get_by_text(exact_response.strip(), exact=True)).to_be_visible()
        expect(
            page.get_by_role("log", name="Current interview turns").locator("article")
        ).to_have_count(3)
        page.get_by_role("button", name="View supporting evidence").click()
        expect(page.get_by_text("support.jpeg", exact=True)).to_be_visible()

        lifecycle["fail_next_new_message"] = True
        page.get_by_label("Your answer").fill("Operations approves an exception.")
        page.get_by_role("button", name="Send answer").click()
        expect(page.get_by_text("The interview response could not be completed.")).to_be_visible()
        page.get_by_role("button", name="Retry interrupted answer").click()
        expect(
            page.get_by_role("log", name="Current interview turns").locator("article")
        ).to_have_count(5)
        retry_ids = [
            str(item["message_id"])
            for item in stream_observations
            if item["outcome"] in {"controlled_failure", "controlled_success"}
        ][-2:]
        assert len(retry_ids) == 2
        assert retry_ids[0] == retry_ids[1]
        finish = page.get_by_role("button", name="Finish interview")
        expect(finish).to_be_enabled()
        expect(
            page.get_by_label(re.compile("I have finished my answers", re.IGNORECASE))
        ).to_have_count(0)
        finish.click()
        expect(page.get_by_text("Interview finished")).to_be_visible()
        expect(page.get_by_label("Your answer")).to_have_count(0)
        expect(
            page.get_by_text(
                "Your answers and any supporting documents are now locked and available "
                "for permitted project analysis.",
                exact=True,
            )
        ).to_be_visible()

        page.reload(wait_until="domcontentloaded")
        expect(page.get_by_text("Interview finished")).to_be_visible()
        expect(page.get_by_label("Your answer")).to_have_count(0)
        expect(page.get_by_role("button", name="Finish interview")).to_have_count(0)
        page.screenshot(
            path=runtime.evidence_dir / "screenshots" / "r4-stakeholder-finished-safe.png"
        )

        page.set_viewport_size({"width": 390, "height": 844})
        responsive_state = _browser_state(page)
        if responsive_state["horizontalOverflow"] != 0:
            overflow_elements = cast(
                "list[dict[str, Any]]",
                page.evaluate(
                    """() => Array.from(document.querySelectorAll('body *'))
                        .map((element) => {
                            const rect = element.getBoundingClientRect();
                            const style = window.getComputedStyle(element);
                            return {
                                tag: element.tagName.toLowerCase(),
                                className: typeof element.className === 'string'
                                    ? element.className.slice(0, 400)
                                    : '',
                                text: (element.textContent ?? '').trim().slice(0, 200),
                                left: Math.round(rect.left),
                                right: Math.round(rect.right),
                                width: Math.round(rect.width),
                                scrollWidth: element.scrollWidth,
                                clientWidth: element.clientWidth,
                                minWidth: style.minWidth,
                                overflowX: style.overflowX,
                                whiteSpace: style.whiteSpace
                            };
                        })
                        .filter((item) => item.right > window.innerWidth + 1 || item.left < -1)
                        .slice(0, 30)"""
                ),
            )
            _write_result(
                runtime,
                "r4-responsive-diagnostic.json",
                {
                    "horizontal_overflow_pixels": responsive_state["horizontalOverflow"],
                    "overflowing_elements": overflow_elements,
                },
            )
            page.screenshot(
                path=runtime.evidence_dir / "screenshots" / "r4-mobile-overflow-diagnostic.png",
                full_page=True,
            )
        assert responsive_state["horizontalOverflow"] == 0
        assert responsive_state["localStorageKeys"] == []
        assert responsive_state["sessionStorageKeys"] == []
        assert responsive_state["documentCookie"] == ""
        assert responsive_state["referrer"] == ""
        assert responsive_state["pathname"] == f"/s/{invitation_token}"

        def handle_tampered_context(route: Route) -> None:
            tampered = copy.deepcopy(safe_context)
            tampered["engagement"]["engagement_id"] = "engagement-foreign"
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(tampered),
            )

        context_pattern = "**/api/v1/stakeholder/context"
        page.route(context_pattern, handle_tampered_context)
        page.reload(wait_until="domcontentloaded")
        expect(
            page.get_by_text("The fixed stakeholder context could not be verified.")
        ).to_be_visible()
        expect(page.get_by_text("Alex Morgan", exact=True)).to_have_count(0)
        page.unroute(context_pattern, handle_tampered_context)

        _assert_opened_invitation_cannot_be_revoked(
            runtime,
            bearer=bearer,
            engagement_id=engagement_id,
            invitation_id=invitation_id,
        )
        page.reload(wait_until="domcontentloaded")
        expect(page.get_by_role("heading", name="Stakeholder workspace")).to_be_visible()
        expect(page.get_by_text("Alex Morgan", exact=True)).to_be_visible()
        assert not any(authorization_seen)
        assert not handler_failures
        assert http_error_responses == []
        assert console_errors == []
    finally:
        context.close()

    _write_result(
        runtime,
        "r4-stakeholder-parity.json",
        {
            "suite": "react-r4-stakeholder-parity",
            "status": "PASS",
            "real_agent_server_operations": [
                "PM setup and invitation issue through canonical bearer routes",
                "secure browser invitation activation and immediate URL cleanup",
                "fixed engagement, stakeholder, role, department, interview, and thread context",
                "same-link recovery into the fixed interview context",
                "browser-session continuity across a real Agent Server restart",
                "post-open invitation revocation conflict with active session continuity",
            ],
            "controlled_provider_contract_operations": [
                "six-format stakeholder upload request and response rendering",
                "safe interview SSE success and failure events",
                "same-message retry after interrupted stream",
                "checkpoint turn-count rendering before and after restart",
                "permanent Finish response, finalized transcript, and READY ingestion",
                "permanent closed state after reload",
                "foreign-scope response rejection",
            ],
            "backend_regression_dependencies": [
                "tests/integration/test_api_domain_routes.py",
                "tests/integration/test_interview_checkpoint.py",
                "tests/integration/test_interview_lifecycle.py",
                "tests/security/test_token_authorization.py",
                "tests/security/test_browser_session_transport.py",
            ],
            "upload_formats": [item[0] for item in upload_matrix],
            "upload_contract_assertions": upload_observations,
            "stream_contract_assertions": stream_observations,
            "agent_restart_count": runtime.restart_count,
            "authorization_header_requests": sum(authorization_seen),
            "browser_storage_key_count": 0,
            "http_only_cookie_hidden_from_document": True,
            "responsive_overflow_pixels": 0,
            "retained_invitation_value": False,
            "live_gemini_claim": "NOT RUN in stakeholder browser parity",
        },
    )
