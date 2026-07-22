"""Deterministic custom-route services hosted by the real LangGraph development server."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import TYPE_CHECKING, Any, override

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from scripts.run_ui_test_backend import _settings

from stakeholder_intelligence_agent.api import dependencies
from stakeholder_intelligence_agent.api.app import create_app
from stakeholder_intelligence_agent.ingestion.types import ElementDraft, ExtractionBundle
from tests.fakes import DeterministicDocumentExtractor, ToolCallingFakeModel
from tests.integration.test_api_domain_routes import _route_harness, _tool_call

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from langchain_core.callbacks import CallbackManagerForLLMRun
    from langchain_core.messages import BaseMessage

    from stakeholder_intelligence_agent.api.runtime import ApplicationServices
    from stakeholder_intelligence_agent.contracts.source import SourceLocation
    from stakeholder_intelligence_agent.ingestion.types import ValidatedUpload


E2E_PORT = int(os.environ.get("STAKEHOLDER_E2E_PORT", "2024"))
LONG_VISION_CANARY = "LONG-VISION-CANARY-ORCHID"


class BrowserTestDocumentExtractor(DeterministicDocumentExtractor):
    """Add representative structured content to the deterministic format extractor."""

    @override
    def extract(self, source_path: Path, upload: ValidatedUpload) -> ExtractionBundle:
        bundle = super().extract(source_path, upload)
        if upload.document_type not in {"pdf", "docx", "pptx", "xlsx"}:
            return bundle
        primary = bundle.elements[0]
        table = ElementDraft(
            key="browser-test-structured-table",
            element_type="table",
            original_content=(
                "| Responsibility | Owner |\n"
                "| --- | --- |\n"
                "| Weekly readiness review | Finance Operations |"
            ),
            location=primary.location,
            extraction_method="docling_test_double_v1",
        )
        return replace(bundle, elements=(primary, table, *bundle.elements[1:]))


class BrowserTestVisionEnricher:
    """Return long, structured, hostile-markup-aware synthetic vision output."""

    async def describe(
        self,
        *,
        content: bytes,
        media_type: str,
        filename: str,
        location: SourceLocation,
    ) -> str:
        del content, media_type
        detail = " ".join(["The visible reporting line remains source evidence."] * 22)
        return (
            "## Structure and relationships\n\n"
            "- The Steering Committee oversees Finance Operations.\n"
            "- The weekly readiness review has a named owner.\n\n"
            f"**{LONG_VISION_CANARY}** {filename} at {location.kind}. {detail}\n\n"
            "<script>window.__unsafeVisionMarkup = true</script>\n\n"
            "[Unsafe generated link](javascript:alert('blocked'))\n\n"
            "![Unsafe generated image](https://invalid.example/generated.png)"
        )


class ContentAwareInterviewModel(ToolCallingFakeModel):
    """Branch on answer content so readiness is not coupled to a fixed turn count."""

    @override
    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        del stop, run_manager, kwargs
        self.call_count += 1
        self.seen_message_text.append(tuple(message.text for message in messages))
        latest_answer = next(
            (message.text for message in reversed(messages) if isinstance(message, HumanMessage)),
            "",
        )
        normalized = latest_answer.casefold()
        if "final-check-complete" in normalized:
            response = (
                "Thank you. I have enough information to complete this interview. "
                "You can finish now, or continue if you would like to add something else."
            )
        elif "substantive-coverage" in normalized or "comprehensive-coverage" in normalized:
            response = (
                "Before we finish, is there anything important about your work on this project "
                "that we have not discussed?"
            )
        elif "continue-after-ready" in normalized:
            response = "Which team should be contacted first if that additional issue occurs?"
        else:
            response = "Who approves an exception when the weekly readiness evidence is incomplete?"
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=response))])


@asynccontextmanager
async def open_e2e_services() -> AsyncIterator[ApplicationServices]:
    """Yield real domain services with deterministic provider boundaries for browser tests."""
    settings = _settings(E2E_PORT)
    model = ContentAwareInterviewModel(responses=[])
    insight_model = ToolCallingFakeModel(
        responses=[
            _tool_call(
                "write_todos",
                {
                    "todos": [
                        {"content": "Research runtime compatibility", "status": "in_progress"},
                        {"content": "Edit the report", "status": "pending"},
                    ]
                },
                "browser-runtime-todos",
            ),
            _tool_call(
                "create_research_plan",
                {
                    "topics": [
                        {
                            "topic_id": "topic-runtime",
                            "title": "Runtime compatibility",
                            "objective": "Verify that scoped research can begin.",
                            "questions": ["Can the scoped backend enter research?"],
                            "required_source_types": ["document"],
                            "dependencies": [],
                            "priority": 1,
                        }
                    ],
                    "source_strategy": ["document"],
                    "completion_criteria": ["Persist the scoped research plan."],
                },
                "browser-runtime-plan",
            ),
            _tool_call(
                "task",
                {
                    "description": "topic_id=topic-runtime Research only runtime compatibility.",
                    "subagent_type": "topic-researcher",
                },
                "browser-runtime-researcher",
            ),
        ]
    )
    async with _route_harness(
        settings,
        document_extractor=BrowserTestDocumentExtractor(),
        vision_enricher=BrowserTestVisionEnricher(),
        interview_model=model,
        insight_primary_model=insight_model,
    ) as harness:
        await harness.services.initialize()
        yield harness.services


dependencies.open_application_services = open_e2e_services
app = create_app()
