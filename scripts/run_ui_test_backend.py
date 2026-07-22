"""Run the real domain stack with deterministic model doubles for browser acceptance."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import uvicorn
from langchain_core.messages import AIMessage, BaseMessage
from pydantic import SecretStr
from tests.acceptance.test_business_scenarios import (
    ALPHA_DOCUMENT_TEXT,
    ALPHA_INTERVIEWS,
    BETA_DOCUMENT_TEXT,
    FIXTURES,
    SCENARIOS,
    AcceptanceDocumentExtractor,
    Scenario,
    _report_base,
)
from tests.acceptance.test_business_scenarios import (
    _evidence_id as _acceptance_evidence_id,
)
from tests.fakes import DeterministicDocumentExtractor
from tests.integration.test_api_domain_routes import RouteHarness, _route_harness, _tool_call

from stakeholder_intelligence_agent.api.browser_security import PM_BROWSER_SESSION_COOKIE
from stakeholder_intelligence_agent.config import Settings
from stakeholder_intelligence_agent.contracts import (
    InsightRuntimeContext,
    InterviewRuntimeContext,
)
from stakeholder_intelligence_agent.interview.prompts import COMPLETION_RECOMMENDATION

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from fastapi import Request
    from starlette.responses import Response

    from stakeholder_intelligence_agent.ingestion.types import ExtractionBundle, ValidatedUpload
    from stakeholder_intelligence_agent.retrieval.types import RetrievedItem

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MIN_SECRET_LENGTH = 32
INSUFFICIENT_QUESTION = "What evidence supports Procurement readiness?"


class MissingTestBootstrapError(RuntimeError):
    """The synthetic browser backend was started without its test bootstrap value."""

    def __init__(self) -> None:
        super().__init__("UI_TEST_PM_BOOTSTRAP_TOKEN must contain at least 32 characters.")


@dataclass(frozen=True, slots=True)
class AcceptanceSeed:
    """IDs needed to bind deterministic report construction to seeded real evidence."""

    alpha_engagement_id: str
    beta_engagement_id: str
    stakeholder_ids: dict[str, str]


class BrowserDocumentExtractor:
    """Preserve acceptance semantics and support the complete six-format browser matrix."""

    def __init__(self) -> None:
        self._semantic = AcceptanceDocumentExtractor(ALPHA_DOCUMENT_TEXT | BETA_DOCUMENT_TEXT)
        self._fallback = DeterministicDocumentExtractor()
        self._semantic_filenames = frozenset(ALPHA_DOCUMENT_TEXT | BETA_DOCUMENT_TEXT)

    def extract(self, source_path: Path, upload: ValidatedUpload) -> ExtractionBundle:
        """Use scenario text for named fixtures and deterministic format facts otherwise."""
        if upload.filename in self._semantic_filenames:
            return self._semantic.extract(source_path, upload)
        return self._fallback.extract(source_path, upload)


def _settings(port: int) -> Settings:
    bootstrap = os.environ.get("UI_TEST_PM_BOOTSTRAP_TOKEN", "")
    if len(bootstrap) < MIN_SECRET_LENGTH:
        raise MissingTestBootstrapError
    configured_root = os.environ.get("UI_TEST_DATA_ROOT", "").strip()
    data_root = (
        Path(configured_root).resolve()
        if configured_root
        else PROJECT_ROOT / ".cache" / "ui-test-backend" / uuid4().hex / "data"
    )
    return Settings(
        environment="test",
        google_api_key=SecretStr(uuid4().hex),
        gemini_primary_chat_model="gemini-test-primary",
        gemini_fallback_chat_model="gemini-test-fallback",
        gemini_vision_model="gemini-test-vision",
        gemini_embedding_model="gemini-test-embedding",
        pm_bootstrap_token=SecretStr(bootstrap),
        token_pepper=SecretStr(uuid4().hex),
        browser_origin=f"http://127.0.0.1:{port}",
        data_root=data_root,
        domain_database=data_root / "domain.sqlite3",
        checkpoint_database=data_root / "checkpoints.sqlite3",
        originals_root=data_root / "originals",
        derived_root=data_root / "derived",
        agent_artifacts_root=data_root / "agent-artifacts",
        audit_root=data_root / "audit",
        gemini_embedding_dimension=128,
        max_retrieval_results=20,
    )


async def _seed_interview(  # noqa: PLR0913
    harness: RouteHarness,
    *,
    pm_token: str,
    engagement_id: str,
    display_name: str,
    role: str,
    department: str,
    statement: str,
) -> str:
    stakeholder = await harness.access.create_stakeholder(
        pm_token,
        engagement_id,
        display_name=display_name,
        role=role,
        department=department,
        correlation_id=f"seed-{department.casefold().replace(' ', '-')}",
    )
    invitation = await harness.access.issue_invitation(
        pm_token,
        engagement_id,
        stakeholder.stakeholder_id,
        correlation_id=f"invite-{stakeholder.stakeholder_id}",
    )
    activated = await harness.access.activate_invitation(
        invitation.token.get_secret_value(),
        correlation_id=f"activate-{stakeholder.stakeholder_id}",
    )
    access = await harness.access.resolve_stakeholder_context(
        activated.access_session.token.get_secret_value(),
        correlation_id=f"transcript-{stakeholder.stakeholder_id}",
    )
    now = datetime.now(UTC)
    await harness.transcript_repository.append_turn(
        access,
        speaker="stakeholder",
        original_text=statement,
        checkpoint_message_id=f"human-{stakeholder.stakeholder_id}",
        now=now,
    )
    await harness.transcript_repository.append_turn(
        access,
        speaker="assistant",
        original_text=COMPLETION_RECOMMENDATION,
        checkpoint_message_id=f"assistant-{stakeholder.stakeholder_id}",
        now=now,
    )
    await harness.services.interview.finish(
        InterviewRuntimeContext(access=access, role=role, department=department)
    )
    return stakeholder.stakeholder_id


async def _seed_acceptance(harness: RouteHarness) -> AcceptanceSeed:
    bootstrap = harness.settings.pm_bootstrap_token.get_secret_value()
    session = await harness.access.activate_pm(bootstrap)
    pm_token = session.token.get_secret_value()
    alpha = await harness.access.create_engagement(
        pm_token,
        name="Alpha Canary engagement",
        description="Isolated mixed-source React acceptance evidence.",
        correlation_id="seed-alpha",
    )
    alpha_pm = await harness.access.resolve_pm_context(
        pm_token,
        alpha.engagement_id,
        correlation_id="seed-alpha-documents",
        required_permission="document:upload",
    )
    for filename in ALPHA_DOCUMENT_TEXT:
        await harness.services.ingestion.ingest(
            alpha_pm,
            filename=filename,
            declared_media_type="image/png",
            content=(FIXTURES / filename).read_bytes(),
        )

    stakeholder_ids: dict[str, str] = {}
    for display_name, role, department, statement in ALPHA_INTERVIEWS:
        stakeholder_ids[department] = await _seed_interview(
            harness,
            pm_token=pm_token,
            engagement_id=alpha.engagement_id,
            display_name=display_name,
            role=role,
            department=department,
            statement=statement,
        )

    draft = await harness.access.create_stakeholder(
        pm_token,
        alpha.engagement_id,
        display_name="Draft Interview",
        role="Unfinalized observer",
        department="Draft",
        correlation_id="seed-draft",
    )
    draft_invitation = await harness.access.issue_invitation(
        pm_token,
        alpha.engagement_id,
        draft.stakeholder_id,
        correlation_id="seed-draft-invitation",
    )
    draft_activation = await harness.access.activate_invitation(
        draft_invitation.token.get_secret_value(),
        correlation_id="seed-draft-activation",
    )
    draft_access = await harness.access.resolve_stakeholder_context(
        draft_activation.access_session.token.get_secret_value(),
        correlation_id="seed-draft-turn",
    )
    await harness.transcript_repository.append_turn(
        draft_access,
        speaker="stakeholder",
        original_text="ALPHA-DRAFT-EXCLUDED must remain unavailable to retrieval.",
        checkpoint_message_id="draft-human",
        now=datetime.now(UTC),
    )

    beta = await harness.access.create_engagement(
        pm_token,
        name="Beta Canary engagement",
        description="Foreign evidence used to prove engagement isolation.",
        correlation_id="seed-beta",
    )
    beta_pm = await harness.access.resolve_pm_context(
        pm_token,
        beta.engagement_id,
        correlation_id="seed-beta-document",
        required_permission="document:upload",
    )
    beta_filename = next(iter(BETA_DOCUMENT_TEXT))
    await harness.services.ingestion.ingest(
        beta_pm,
        filename=beta_filename,
        declared_media_type="image/jpeg",
        content=(FIXTURES / beta_filename).read_bytes(),
    )
    await _seed_interview(
        harness,
        pm_token=pm_token,
        engagement_id=beta.engagement_id,
        display_name="Blake Stone",
        role="Beta Legal Lead",
        department="Legal",
        statement=(
            "BETA-CANARY-COBALT. Beta release approval belongs to Legal and is foreign to Alpha."
        ),
    )
    return AcceptanceSeed(
        alpha_engagement_id=alpha.engagement_id,
        beta_engagement_id=beta.engagement_id,
        stakeholder_ids=stakeholder_ids,
    )


def _insufficient_report(
    context: InsightRuntimeContext,
    _evidence: dict[str, tuple[tuple[str, RetrievedItem], ...]],
    _stakeholder_ids: dict[str, str],
) -> dict[str, Any]:
    report = _report_base(
        context,
        report_id="report-insufficient",
        executive_summary=(
            "Current permitted evidence does not establish Procurement readiness or ownership."
        ),
        topics=(("topic-procurement", "Procurement readiness", ()),),
        evidence_ids=(),
        findings=(),
        citations=(),
    )
    report["status"] = "insufficient_evidence"
    report["researched_topics"][0]["status"] = "insufficient_evidence"
    report["researched_topics"][0]["summary"] = (
        "No current permitted source supports a Procurement conclusion."
    )
    report["evidence_gaps"] = [
        {
            "topic": "Procurement readiness",
            "description": "No finalized Procurement interview or approved source is available.",
            "impact": "Procurement readiness and ownership cannot be asserted.",
        }
    ]
    report["open_questions"] = [
        "Which current source establishes Procurement readiness and ownership?"
    ]
    report["run_metadata"].update(
        {
            "topic_count": 1,
            "status_detail": "The bounded research completed with insufficient evidence.",
        }
    )
    return report


INSUFFICIENT_SCENARIO = Scenario(
    acceptance_id="INSUFFICIENT-EVIDENCE",
    question=INSUFFICIENT_QUESTION,
    topics=(
        {
            "topic_id": "topic-procurement",
            "title": "Procurement readiness",
            "objective": "Determine whether current permitted evidence supports a conclusion.",
            "questions": ["Which source establishes Procurement readiness and ownership?"],
            "required_source_types": ["document", "interview"],
            "dependencies": [],
            "priority": 1,
        },
    ),
    report_builder=_insufficient_report,
)
ALL_SCENARIOS = (*SCENARIOS, INSUFFICIENT_SCENARIO)


async def _prime_insight(
    harness: RouteHarness,
    seed: AcceptanceSeed,
    *,
    bearer: str,
    engagement_id: str,
    question: str,
) -> None:
    scenario = next((item for item in ALL_SCENARIOS if item.question == question), None)
    if scenario is None or engagement_id != seed.alpha_engagement_id:
        return
    run_id = harness.ids.peek_next("run")
    thread_id = harness.ids.peek_next("report-thread")
    access = await harness.access.resolve_pm_context(
        bearer,
        engagement_id,
        correlation_id=f"{run_id}-ui-acceptance",
        thread_id=thread_id,
        required_permission="insight:run",
    )
    context = InsightRuntimeContext(access=access, run_id=run_id, question=question)
    preflight = await harness.retrieval.retrieve(access, question)
    evidence = {
        str(topic["topic_id"]): tuple(
            (
                _acceptance_evidence_id(run_id, str(topic["topic_id"]), item),
                item,
            )
            for item in preflight.items
        )
        for topic in scenario.topics
    }
    report = scenario.report_builder(context, evidence, seed.stakeholder_ids)
    suffix = scenario.acceptance_id.casefold().replace("-", "_")
    harness.insight_primary.responses = [
        _tool_call(
            "write_todos",
            {
                "todos": [
                    *[
                        {"content": f"Research {topic['title']}", "status": "in_progress"}
                        for topic in scenario.topics
                    ],
                    {"content": "Edit the structured report", "status": "pending"},
                    {"content": "Validate the report", "status": "pending"},
                ]
            },
            f"{suffix}-todos",
        ),
        _tool_call(
            "create_research_plan",
            {
                "topics": list(scenario.topics),
                "source_strategy": ["document", "interview"],
                "completion_criteria": [
                    "Use only registered current-engagement evidence.",
                    "Report gaps instead of unsupported conclusions.",
                ],
            },
            f"{suffix}-plan",
        ),
        *[
            _tool_call(
                "task",
                {
                    "description": (
                        f"topic_id={topic['topic_id']} Research only {topic['title']} and "
                        "save the required artifacts."
                    ),
                    "subagent_type": "topic-researcher",
                },
                f"{suffix}-{topic['topic_id']}",
            )
            for topic in scenario.topics
        ],
        _tool_call(
            "task",
            {
                "description": "Load every completed topic artifact and create the report.",
                "subagent_type": "report-editor",
            },
            f"{suffix}-editor",
        ),
        AIMessage(content="The validated report artifact is available."),
    ]
    harness.insight_primary.i = 0
    researcher_responses: list[BaseMessage] = []
    for topic in scenario.topics:
        topic_id = str(topic["topic_id"])
        researcher_responses.extend(
            [
                _tool_call(
                    "scoped_retrieve",
                    {"topic_id": topic_id, "query": question},
                    f"{suffix}-{topic_id}-retrieve",
                ),
                _tool_call(
                    "think_tool",
                    {"reflection": f"Review only the bounded {topic_id} evidence."},
                    f"{suffix}-{topic_id}-think",
                ),
                _tool_call(
                    "save_research_artifacts",
                    {
                        "topic_id": topic_id,
                        "findings_markdown": (
                            f"# Findings\n\nRegistered evidence for {topic['title']} was reviewed."
                        ),
                        "evidence_ids": [item[0] for item in evidence[topic_id]],
                    },
                    f"{suffix}-{topic_id}-save",
                ),
                AIMessage(content=f"Artifacts for {topic_id} were saved."),
            ]
        )
    harness.researcher.responses = researcher_responses
    harness.researcher.i = 0
    harness.editor.responses = [
        _tool_call("load_research_package", {}, f"{suffix}-load"),
        _tool_call(
            "save_final_report",
            {"report": report},
            f"{suffix}-save-report",
        ),
        AIMessage(content="The strict report was validated and saved."),
    ]
    harness.editor.i = 0


def _install_insight_primer(harness: RouteHarness, seed: AcceptanceSeed) -> None:
    insight_prefix = "/api/v1/pm/engagements/"
    insight_suffix = "/insights"

    async def prime_middleware(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        path = request.url.path
        if (
            request.method == "POST"
            and path.startswith(insight_prefix)
            and path.endswith(insight_suffix)
        ):
            payload = json.loads(await request.body())
            authorization = request.headers.get("authorization", "")
            bearer = authorization.removeprefix("Bearer ") or request.cookies.get(
                PM_BROWSER_SESSION_COOKIE, ""
            )
            engagement_id = (
                path.removeprefix(insight_prefix).removesuffix(insight_suffix).strip("/")
            )
            question = payload.get("question") if isinstance(payload, dict) else None
            if bearer and engagement_id and isinstance(question, str):
                await _prime_insight(
                    harness,
                    seed,
                    bearer=bearer,
                    engagement_id=engagement_id,
                    question=question,
                )
        return await call_next(request)

    harness.app.middleware("http")(prime_middleware)


async def _serve(port: int) -> None:
    settings = _settings(port)
    extractor = BrowserDocumentExtractor()
    async with _route_harness(settings, document_extractor=extractor) as harness:
        await harness.services.initialize()
        seed = await _seed_acceptance(harness)
        _install_insight_primer(harness, seed)
        config = uvicorn.Config(
            harness.app,
            host="127.0.0.1",
            port=port,
            log_level="warning",
            access_log=False,
        )
        await uvicorn.Server(config).serve()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=2024)
    arguments = parser.parse_args()
    asyncio.run(_serve(arguments.port))


if __name__ == "__main__":
    main()
