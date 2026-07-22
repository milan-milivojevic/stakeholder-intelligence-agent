"""Real-Chromium PM parity verification for the built React application."""

from __future__ import annotations

import hashlib
import json
import re
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlparse

import pytest
from playwright.sync_api import Browser, Error, Page, Request, Response, Route, expect

if TYPE_CHECKING:
    from playwright.sync_api import BrowserContext

    from tests.e2e.conftest import BrowserRuntime

pytestmark = [pytest.mark.e2e, pytest.mark.integration]
NORMAL_TIMEOUT_MS = 60_000
CSRF_HEADER = "X-Stakeholder-CSRF"
TIMESTAMP = "2026-07-15T08:00:00Z"
COMPLETED_AT = "2026-07-15T08:05:00Z"
DOWNLOAD_BYTES = b"approved deterministic source bytes\n"


def _context(browser: Browser) -> BrowserContext:
    return browser.new_context(
        viewport={"width": 1280, "height": 900},
        locale="en-US",
        accept_downloads=True,
    )


def _configure(page: Page) -> None:
    page.set_default_timeout(NORMAL_TIMEOUT_MS)
    page.set_default_navigation_timeout(NORMAL_TIMEOUT_MS)


def _write_result(runtime: BrowserRuntime, filename: str, payload: dict[str, Any]) -> None:
    (runtime.evidence_dir / filename).write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _document_payload(engagement_id: str, index: int, filename: str) -> dict[str, Any]:
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
    document_id = f"document-{index}"
    return {
        "source": {
            "document_id": document_id,
            "engagement_id": engagement_id,
            "stakeholder_id": None,
            "role": None,
            "department": None,
            "doc_type": doc_type,
            "source_type": "engagement_document",
            "original_filename": filename,
            "media_type": media_types[doc_type],
            "created_at": TIMESTAMP,
        },
        "latest_version": {
            "document_version_id": f"document-version-{index}",
            "document_id": document_id,
            "version_number": 1,
            "content_hash": f"{index:x}".ljust(64, "a"),
            "state": "READY",
            "is_active": True,
            "original_artifact_id": f"artifact-{index}",
            "ingestion_key": f"ingestion-{index}",
            "created_at": TIMESTAMP,
            "ready_at": COMPLETED_AT,
            "superseded_at": None,
            "failure_code": None,
            "failure_message": None,
        },
    }


def _run_payload(engagement_id: str, mode: str, *, queued: bool) -> dict[str, Any]:
    status = "queued" if queued else mode
    terminal_report = mode in {"complete", "partial", "insufficient_evidence"} and not queued
    failed = mode == "failed" and not queued
    return {
        "run_id": f"run-{mode}",
        "engagement_id": engagement_id,
        "thread_id": f"report-thread-{mode}",
        "status": status,
        "requested_question": f"{mode.replace('_', ' ').title()} operating-model evidence?",
        "plan_id": None if queued or failed else f"plan-{mode}",
        "report_id": f"report-{mode}" if terminal_report else None,
        "failure_code": "INSIGHT_EXECUTION_FAILED" if failed else None,
        "failure_message": "The insight run could not be completed." if failed else None,
        "started_at": TIMESTAMP,
        "completed_at": COMPLETED_AT if terminal_report or failed else None,
    }


def _report_payload(engagement_id: str, mode: str) -> dict[str, Any]:
    sufficient = mode in {"complete", "partial"}
    evidence_ids = [f"evidence-{mode}"] if sufficient else []
    findings = (
        [
            {
                "claim_id": f"claim-{mode}",
                "statement": "Operations owns the weekly service review.",
                "evidence_ids": evidence_ids,
            }
        ]
        if sufficient
        else []
    )
    citations = (
        [
            {
                "citation_id": f"citation-{mode}",
                "evidence_id": f"evidence-{mode}",
                "display_label": "Operating model page 1",
                "source_location": {
                    "kind": "pdf_page",
                    "filename": "operating-model.pdf",
                    "page": 1,
                    "bounding_box": None,
                },
                "claim_ids": [f"claim-{mode}"],
            }
        ]
        if sufficient
        else []
    )
    return {
        "report_id": f"report-{mode}",
        "engagement_id": engagement_id,
        "question": f"{mode.replace('_', ' ').title()} operating-model evidence?",
        "status": mode,
        "executive_summary": (
            "The permitted evidence supports the ownership finding."
            if sufficient
            else "Permitted evidence was insufficient for supported conclusions."
        ),
        "researched_topics": [
            {
                "topic_id": f"topic-{mode}",
                "title": "Ownership and handoffs",
                "status": "completed" if sufficient else "insufficient_evidence",
                "summary": (
                    "The topic completed with permitted evidence."
                    if sufficient
                    else "No active ready source supported a conclusion."
                ),
                "evidence_ids": evidence_ids,
            }
        ],
        "findings": findings,
        "responsibilities": [],
        "operational_risks": [],
        "buy_in_signals": [],
        "contradictions": [],
        "evidence_gaps": (
            []
            if sufficient
            else [
                {
                    "topic": "Ownership and handoffs",
                    "description": "No permitted source supported a conclusion.",
                    "impact": "Ownership cannot be asserted.",
                }
            ]
        ),
        "open_questions": [],
        "follow_up_recommendations": [],
        "evidence_ids": evidence_ids,
        "citations": citations,
        "run_metadata": {
            "run_id": f"run-{mode}",
            "started_at": TIMESTAMP,
            "completed_at": COMPLETED_AT,
            "primary_model_id": "gemini-deterministic-browser-contract",
            "fallback_model_id": "gemini-deterministic-browser-fallback",
            "topic_count": 1,
            "status_detail": "The deterministic browser contract completed.",
        },
    }


def _metrics_payload(engagement_id: str, mode: str) -> dict[str, Any]:
    sufficient = mode in {"complete", "partial"}
    return {
        "run_id": f"run-{mode}",
        "engagement_id": engagement_id,
        "thread_id": f"report-thread-{mode}",
        "started_at": TIMESTAMP,
        "completed_at": COMPLETED_AT,
        "status": mode,
        "duration_ms": 1_000,
        "topic_count": 1,
        "researcher_calls": 1,
        "max_concurrent_researchers": 1,
        "model_calls": 8,
        "model_failures": 0,
        "tool_calls": 7,
        "tool_failures": 0,
        "retrieval_calls": 1,
        "retry_count": 0,
        "timeout_count": 0,
        "rerank_candidates_total": 10 if sufficient else 0,
        "max_rerank_candidates_per_call": 10 if sufficient else 0,
        "retrieval_latency_ms": 25 if sufficient else 0,
        "reranker_latency_ms": 8 if sufficient else 0,
        "input_tokens": 120,
        "output_tokens": 80,
        "total_tokens": 200,
        "configured_topic_limit": 5,
        "configured_parallel_researcher_limit": 3,
        "configured_model_call_limit": 25,
        "configured_tool_call_limit": 40,
        "configured_retrieval_calls_per_researcher_limit": 3,
        "configured_rerank_candidate_limit": 50,
        "configured_provider_timeout_seconds": 120,
        "configured_run_timeout_seconds": 600,
        "source_ids": [f"source-{mode}"] if sufficient else [],
        "evidence_ids": [f"evidence-{mode}"] if sufficient else [],
        "tool_names": ["scoped_retrieve", "save_final_report"],
        "failure_code": None,
        "correlation_id": f"correlation-{mode}",
    }


def _event_payload(mode: str) -> dict[str, Any]:
    return {
        "event_id": f"event-{mode}",
        "occurred_at": TIMESTAMP,
        "actor": "topic-researcher",
        "action": "research_topic_completed",
        "from_status": "researching",
        "to_status": "editing",
        "topic_id": f"topic-{mode}",
        "source_ids": ["document-1"] if mode != "insufficient_evidence" else [],
        "evidence_ids": [f"evidence-{mode}"] if mode != "insufficient_evidence" else [],
        "artifact_name": f"research-{mode}.md",
        "failure_code": None,
        "correlation_id": f"correlation-{mode}",
    }


def _evidence_payload(engagement_id: str, mode: str, prefix: str) -> dict[str, Any]:
    evidence_id = f"evidence-{mode}"
    artifact_id = f"artifact-{mode}"
    download_path = f"{prefix}/insights/run-{mode}/evidence/{evidence_id}/artifacts/{artifact_id}"
    return {
        "evidence": {
            "evidence_id": evidence_id,
            "run_id": f"run-{mode}",
            "engagement_id": engagement_id,
            "topic_id": f"topic-{mode}",
            "source_id": "document-1",
            "source_version_id": "document-version-1",
            "source_type": "engagement_document",
            "stakeholder_id": None,
            "location": {
                "kind": "pdf_page",
                "filename": "operating-model.pdf",
                "page": 1,
                "bounding_box": None,
            },
            "original_excerpt": "Operations owns the weekly service review.",
            "english_interpretation": None,
            "content_hash": "b" * 64,
            "researcher_id": "researcher-browser-contract",
            "created_at": TIMESTAMP,
        },
        "original": {
            "artifact_id": artifact_id,
            "artifact_kind": "original_document",
            "media_type": "application/pdf",
            "content_hash": "c" * 64,
            "download_path": download_path,
        },
        "related_artifacts": [
            {
                "artifact_id": f"transcript-{mode}",
                "artifact_kind": "raw_transcript",
                "media_type": "application/json",
                "content_hash": "d" * 64,
                "download_path": None,
            }
        ],
    }


def _browser_state(page: Page) -> dict[str, Any]:
    return cast(
        "dict[str, Any]",
        page.evaluate(
            """() => ({
                localStorageKeys: Object.keys(window.localStorage),
                sessionStorageKeys: Object.keys(window.sessionStorage),
                referrer: document.referrer,
                pathname: window.location.pathname,
                horizontalOverflow: Math.max(
                    0,
                    document.documentElement.scrollWidth - document.documentElement.clientWidth
                )
            })"""
        ),
    )


@pytest.mark.timeout(420)
def test_react_pm_complete_parity_with_real_setup_and_controlled_provider_contracts(  # noqa: PLR0915
    browser: Browser,
    react_browser_runtime: BrowserRuntime,
) -> None:
    runtime = react_browser_runtime
    context = _context(browser)
    page = context.new_page()
    _configure(page)
    authorization_seen: list[bool] = []
    console_errors: list[str] = []
    http_error_responses: list[dict[str, object]] = []
    handler_failures: list[str] = []
    upload_observations: list[dict[str, Any]] = []
    insight_observations: list[dict[str, Any]] = []
    uploaded_documents: list[dict[str, Any]] = []

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
        response = page.goto(f"{runtime.agent_url}/pm", wait_until="domcontentloaded")
        assert response is not None
        assert response.status == 200
        page.get_by_label("Access key").fill(runtime.pm_bootstrap_secret)
        page.get_by_role("button", name="Open workspace").click()
        expect(page.get_by_role("heading", name="Project manager workspace")).to_be_visible()
        expect(page.get_by_text("No engagements yet")).to_be_visible()

        engagement_name = f"PM browser parity {runtime.run_id[-8:]}"
        page.get_by_label("Engagement name").fill(engagement_name)
        page.get_by_label("Description (optional)").fill("PM browser parity verification.")
        page.get_by_role("button", name="Create and open").click()
        active_engagement = page.get_by_role("region", name="Active engagement")
        expect(active_engagement).to_be_visible()
        expect(active_engagement.get_by_text(engagement_name, exact=True)).to_be_visible()

        safe_engagement = cast(
            "dict[str, Any]",
            page.evaluate(
                """async () => {
                    const response = await fetch('/api/v1/pm/engagements', {
                        credentials: 'same-origin',
                        cache: 'no-store',
                        headers: {Accept: 'application/json'}
                    });
                    return await response.json();
                }"""
            ),
        )
        engagement_id = str(safe_engagement["engagements"][0]["engagement_id"])

        page.get_by_role("button", name="Add new stakeholder").click()
        page.get_by_label("Display name").fill("Alex Morgan")
        page.get_by_label("Role (optional)").fill("Operations lead")
        page.get_by_label("Department (optional)").fill("Operations")
        page.get_by_role("button", name="Add stakeholder").click()
        expect(page.get_by_text("Alex Morgan", exact=True)).to_be_visible()
        page.get_by_role("button", name="Generate invitation link").click()
        invitation_input = page.get_by_label("Interview invitation link")
        expect(invitation_input).to_be_visible()
        invitation_link = invitation_input.input_value()
        expected_prefix = f"{runtime.agent_url}/s/"
        if not invitation_link.startswith(expected_prefix):
            pytest.fail("The PM invitation response did not use the approved stakeholder route.")
        invitation_token = invitation_link.removeprefix(expected_prefix)
        runtime.track_secret(invitation_token)
        expect(invitation_input).to_have_value(invitation_link)
        page.screenshot(path=runtime.evidence_dir / "screenshots" / "r3-pm-setup-safe.png")

        page.get_by_role("button", name="Revoke").click()
        expect(page.get_by_text("Revoked", exact=True)).to_be_visible()

        revoked_context = _context(browser)
        revoked_page = revoked_context.new_page()
        _configure(revoked_page)
        try:
            try:
                revoked_page.goto(invitation_link, wait_until="domcontentloaded")
                expect(revoked_page).to_have_url(invitation_link)
                expect(revoked_page.get_by_text("Invitation unavailable")).to_be_visible()
                assert invitation_token not in revoked_page.content()
            except (AssertionError, Error):
                pytest.fail("The revoked invitation did not fail safely.")
        finally:
            revoked_context.close()

        page.get_by_role("button", name="Change engagement").click()
        page.get_by_role("button", name="Open", exact=True).click()
        active_engagement = page.get_by_role("region", name="Active engagement")
        expect(active_engagement).to_be_visible()
        expect(active_engagement.get_by_text(engagement_name, exact=True)).to_be_visible()

        stakeholder_rows = cast(
            "dict[str, Any]",
            page.evaluate(
                f"""async () => {{
                    const response = await fetch(
                        '/api/v1/pm/engagements/{engagement_id}/stakeholders',
                        {{
                            credentials: 'same-origin',
                            cache: 'no-store',
                            headers: {{Accept: 'application/json'}}
                        }}
                    );
                    return await response.json();
                }}"""
            ),
        )
        stakeholder_id = str(stakeholder_rows["stakeholders"][0]["stakeholder_id"])
        api_prefix = f"/api/v1/pm/engagements/{engagement_id}"

        def handle_pm_contract(route: Route) -> None:  # noqa: PLR0911, PLR0915
            request = route.request
            path = urlparse(request.url).path
            method = request.method
            if path == f"{api_prefix}/documents":
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
                valid_csrf = request.headers.get(CSRF_HEADER.lower()) == "1"
                no_authorization = "authorization" not in request.headers
                multipart = b'name="upload"' in body and filename_match is not None
                if not (valid_csrf and no_authorization and multipart):
                    handler_failures.append("The upload request violated the browser contract.")
                document = _document_payload(engagement_id, len(uploaded_documents) + 1, filename)
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

            if path == f"{api_prefix}/interviews" and method == "GET":
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(
                        {
                            "interview_sessions": [
                                {
                                    "interview_session_id": "interview-r3-ready",
                                    "engagement_id": engagement_id,
                                    "stakeholder_id": stakeholder_id,
                                    "invitation_id": "invitation-r3-history",
                                    "thread_id": "thread-r3-ready",
                                    "status": "ready",
                                    "started_at": TIMESTAMP,
                                    "finalized_at": COMPLETED_AT,
                                    "transcript_id": "transcript-r3-ready",
                                    "ingestion_version_id": "transcript-version-r3-ready",
                                    "failure_code": None,
                                    "failure_message": None,
                                }
                            ]
                        }
                    ),
                )
                return

            if path == f"{api_prefix}/interviews/interview-r3-ready" and method == "GET":
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(
                        {
                            "interview_session": {
                                "interview_session_id": "interview-r3-ready",
                                "engagement_id": engagement_id,
                                "stakeholder_id": stakeholder_id,
                                "invitation_id": "invitation-r3-history",
                                "thread_id": "thread-r3-ready",
                                "status": "ready",
                                "started_at": TIMESTAMP,
                                "finalized_at": COMPLETED_AT,
                                "transcript_id": "transcript-r3-ready",
                                "ingestion_version_id": "transcript-version-r3-ready",
                                "failure_code": None,
                                "failure_message": None,
                            },
                            "transcript": {
                                "transcript_id": "transcript-r3-ready",
                                "interview_session_id": "interview-r3-ready",
                                "engagement_id": engagement_id,
                                "stakeholder_id": stakeholder_id,
                                "role": "Operations lead",
                                "department": "Operations",
                                "status": "finalized",
                                "language_observations": [],
                                "finalized_at": COMPLETED_AT,
                                "content_hash": "b" * 64,
                            },
                            "turns": [
                                {
                                    "turn_index": 0,
                                    "speaker": "assistant",
                                    "text": "What is your role in the weekly service review?",
                                },
                                {
                                    "turn_index": 1,
                                    "speaker": "stakeholder",
                                    "text": "I own the weekly service review.",
                                },
                            ],
                        }
                    ),
                )
                return

            if path == f"{api_prefix}/insights" and method == "POST":
                request_payload = json.loads(request.post_data or "{}")
                question = str(request_payload.get("question", "")).casefold()
                mode = next(
                    (
                        item
                        for item in (
                            "insufficient_evidence",
                            "complete",
                            "partial",
                            "failed",
                        )
                        if item.replace("_", " ") in question
                    ),
                    "complete",
                )
                valid_csrf = request.headers.get(CSRF_HEADER.lower()) == "1"
                no_authorization = "authorization" not in request.headers
                if not valid_csrf or not no_authorization:
                    handler_failures.append("The insight request violated the browser contract.")
                insight_observations.append(
                    {
                        "mode": mode,
                        "csrf": valid_csrf,
                        "authorization_absent": no_authorization,
                    }
                )
                route.fulfill(
                    status=202,
                    content_type="application/json",
                    body=json.dumps({"run": _run_payload(engagement_id, mode, queued=True)}),
                )
                return

            run_match = re.fullmatch(rf"{re.escape(api_prefix)}/insights/run-([a-z_]+)", path)
            if run_match is not None and method == "GET":
                mode = run_match.group(1)
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"run": _run_payload(engagement_id, mode, queued=False)}),
                )
                return

            events_match = re.fullmatch(
                rf"{re.escape(api_prefix)}/insights/run-([a-z_]+)/events",
                path,
            )
            if events_match is not None and method == "GET":
                mode = events_match.group(1)
                event = _event_payload(mode)
                route.fulfill(
                    status=200,
                    headers={"Content-Type": "text/event-stream", "Cache-Control": "no-store"},
                    body=f"event: progress\ndata: {json.dumps(event, separators=(',', ':'))}\n\n",
                )
                return

            report_match = re.fullmatch(
                rf"{re.escape(api_prefix)}/insights/run-([a-z_]+)/report",
                path,
            )
            if report_match is not None and method == "GET":
                mode = report_match.group(1)
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(
                        {
                            "run": _run_payload(engagement_id, mode, queued=False),
                            "report": _report_payload(engagement_id, mode),
                            "metrics": _metrics_payload(engagement_id, mode),
                        }
                    ),
                )
                return

            artifact_match = re.fullmatch(
                rf"{re.escape(api_prefix)}/insights/run-([a-z_]+)/evidence/"
                rf"evidence-([a-z_]+)/artifacts/artifact-([a-z_]+)",
                path,
            )
            if artifact_match is not None and method == "GET":
                route.fulfill(
                    status=200,
                    headers={
                        "Content-Type": "application/pdf",
                        "Content-Disposition": 'attachment; filename="operating-model.pdf"',
                        "Cache-Control": "no-store",
                    },
                    body=DOWNLOAD_BYTES,
                )
                return

            evidence_match = re.fullmatch(
                rf"{re.escape(api_prefix)}/insights/run-([a-z_]+)/evidence/evidence-([a-z_]+)",
                path,
            )
            if evidence_match is not None and method == "GET":
                mode = evidence_match.group(1)
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(_evidence_payload(engagement_id, mode, api_prefix)),
                )
                return

            route.continue_()

        context.route(f"{runtime.agent_url}/api/v1/pm/engagements/**", handle_pm_contract)

        page.get_by_role("tab", name="Documents").click()
        expect(page.get_by_text("No engagement documents are available.")).to_be_visible()
        upload_matrix = [
            ("source.pdf", "application/pdf"),
            (
                "source.docx",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ),
            (
                "source.pptx",
                "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            ),
            (
                "source.xlsx",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ),
            ("source.png", "image/png"),
            ("source.jpeg", "image/jpeg"),
        ]
        for filename, media_type in upload_matrix:
            page.get_by_label("Source document").set_input_files(
                {"name": filename, "mimeType": media_type, "buffer": b"format test bytes"}
            )
            page.get_by_role("button", name="Upload document").click()
            expect(page.get_by_text(filename, exact=True)).to_be_visible()
        assert [item["filename"] for item in upload_observations] == [
            item[0] for item in upload_matrix
        ]
        assert all(item["csrf"] for item in upload_observations)
        assert all(item["authorization_absent"] for item in upload_observations)
        assert all(item["multipart_upload_field"] for item in upload_observations)
        page.screenshot(path=runtime.evidence_dir / "screenshots" / "r3-pm-documents-safe.png")

        page.get_by_role("tab", name="Interviews").click()
        expect(page.get_by_text("Finalized interviews", exact=True)).to_be_visible()
        expect(page.get_by_text("Ready for permitted retrieval")).to_be_visible()
        expect(page.get_by_text("Alex Morgan", exact=True)).to_be_visible()
        page.get_by_role("button", name="Preview interview with Alex Morgan").click()
        expect(page.get_by_label("Interview transcript")).to_be_visible()
        expect(page.get_by_text("I own the weekly service review.", exact=True)).to_be_visible()
        page.get_by_role("button", name="Close preview").click()

        page.get_by_role("tab", name="Insight research").click()
        question = page.get_by_label("Research question")
        question.fill("Complete operating-model evidence?")
        page.get_by_role("button", name="Run insight research").click()
        expect(page.get_by_text("Analysis complete")).to_be_visible()
        expect(page.get_by_text("Evidence-grounded report")).to_be_visible()
        page.get_by_text("Technical details", exact=True).click()
        expect(page.get_by_text("Research completed")).to_be_visible()
        expect(page.get_by_text("Execution metrics")).to_be_visible()
        expect(page.get_by_text("Researcher calls")).to_be_visible()
        expect(page.get_by_text("Tool calls")).to_be_visible()
        page.screenshot(path=runtime.evidence_dir / "screenshots" / "r3-pm-report-safe.png")

        page.get_by_role("button", name="View source excerpt").click()
        drill_down = page.get_by_role("dialog", name="Operating model page 1")
        expect(drill_down).to_be_visible()
        expect(drill_down.get_by_text("Operations owns the weekly service review.")).to_be_visible()
        expect(drill_down.get_by_text("cannot be downloaded", exact=False)).to_be_visible()
        download_link = drill_down.get_by_role("link", name="Download source file")
        download_href = download_link.get_attribute("href")
        assert download_href is not None
        assert download_link.get_attribute("download") == ""
        downloaded = cast(
            "dict[str, Any]",
            page.evaluate(
                """async (path) => {
                    const response = await fetch(path, {
                        credentials: 'same-origin',
                        cache: 'no-store'
                    });
                    return {
                        status: response.status,
                        contentType: response.headers.get('content-type'),
                        bytes: Array.from(new Uint8Array(await response.arrayBuffer()))
                    };
                }""",
                download_href,
            ),
        )
        assert downloaded["status"] == 200
        assert downloaded["contentType"] == "application/pdf"
        assert bytes(downloaded["bytes"]) == DOWNLOAD_BYTES
        download_sha256 = hashlib.sha256(DOWNLOAD_BYTES).hexdigest()
        drill_down.get_by_role("button", name="Close").click()

        question.fill("Partial operating-model evidence?")
        page.get_by_role("button", name="Run insight research").click()
        expect(page.get_by_text("Analysis partially complete")).to_be_visible()

        question.fill("Insufficient evidence operating-model evidence?")
        page.get_by_role("button", name="Run insight research").click()
        expect(page.get_by_text("More evidence needed")).to_be_visible()

        question.fill("Failed operating-model evidence?")
        page.get_by_role("button", name="Run insight research").click()
        expect(page.get_by_text("Insight run failed safely")).to_be_visible()
        expect(page.get_by_text("The insight run could not be completed.")).to_be_visible()

        page.set_viewport_size({"width": 390, "height": 844})
        state = _browser_state(page)
        if state["horizontalOverflow"] != 0:
            overflow_elements = cast(
                "list[dict[str, Any]]",
                page.evaluate(
                    """() => Array.from(document.querySelectorAll('body *'))
                        .map((element) => {
                            const rect = element.getBoundingClientRect();
                            return {
                                tag: element.tagName.toLowerCase(),
                                className: typeof element.className === 'string'
                                    ? element.className.slice(0, 300)
                                    : '',
                                left: Math.round(rect.left),
                                right: Math.round(rect.right),
                                width: Math.round(rect.width),
                                scrollWidth: element.scrollWidth,
                                clientWidth: element.clientWidth
                            };
                        })
                        .filter((item) => item.right > window.innerWidth + 1 || item.left < -1)
                        .slice(0, 20)"""
                ),
            )
            _write_result(
                runtime,
                "r3-responsive-diagnostic.json",
                {
                    "horizontal_overflow_pixels": state["horizontalOverflow"],
                    "overflowing_elements": overflow_elements,
                },
            )
        assert state["horizontalOverflow"] == 0
        assert state["localStorageKeys"] == []
        assert state["sessionStorageKeys"] == []
        assert state["referrer"] == ""
        assert state["pathname"] == "/pm"
        assert not any(authorization_seen)
        assert not handler_failures
        assert http_error_responses == [{"path": "/api/v1/browser/auth/session", "status": 403}]
        assert console_errors == [
            "Failed to load resource: the server responded with a status of 403 (Forbidden)"
        ]
    finally:
        context.close()

    _write_result(
        runtime,
        "r3-pm-parity.json",
        {
            "suite": "react-r3-pm-parity",
            "status": "PASS",
            "real_agent_server_operations": [
                "browser PM activation",
                "engagement create and server selection",
                "engagement reopen",
                "stakeholder create and view",
                "invitation issue, one-time display, revoke, and revoked-link denial",
            ],
            "controlled_provider_contract_operations": [
                "six-format PM upload request and response rendering",
                "finalized interview view",
                "safe insight SSE",
                "complete, partial, insufficient-evidence, and failed report states",
                "citation and evidence drill-down",
                "approved source download",
            ],
            "backend_contract_regression_dependency": [
                "tests/integration/test_api_domain_routes.py",
                "tests/integration/test_insight_execution_observability.py",
            ],
            "upload_formats": [item[0] for item in upload_matrix],
            "upload_contract_assertions": upload_observations,
            "insight_contract_assertions": insight_observations,
            "download": {
                "bytes": len(DOWNLOAD_BYTES),
                "sha256": download_sha256,
                "retained_raw_download": False,
                "react_download_attribute": True,
                "same_origin_byte_transfer": True,
                "playwright_download_event": "NOT CLAIMED for the deterministic routed fixture",
            },
            "authorization_header_requests": sum(authorization_seen),
            "expected_unauthenticated_bootstrap_denials": 1,
            "unexpected_http_error_responses": 0,
            "unexpected_console_errors": 0,
            "browser_storage_key_count": 0,
            "responsive_overflow_pixels": 0,
            "retained_invitation_value": False,
            "live_gemini_claim": "NOT RUN in PM browser parity",
        },
    )
