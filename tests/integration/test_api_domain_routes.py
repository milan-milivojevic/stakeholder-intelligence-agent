"""One-backend custom-route flow with real domain, vector, and agent boundaries."""

from __future__ import annotations

from collections import defaultdict
from contextlib import asynccontextmanager
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage, ToolCall
from qdrant_client import AsyncQdrantClient

from stakeholder_intelligence_agent.access import AccessService
from stakeholder_intelligence_agent.api.app import create_app
from stakeholder_intelligence_agent.api.read_repository import DomainReadRepository
from stakeholder_intelligence_agent.api.runtime import ApplicationServices
from stakeholder_intelligence_agent.artifacts import ScopedArtifactStore
from stakeholder_intelligence_agent.contracts import InsightRuntimeContext
from stakeholder_intelligence_agent.ingestion.identity import stable_id
from stakeholder_intelligence_agent.ingestion.qdrant import QdrantVectorStager
from stakeholder_intelligence_agent.ingestion.repository import IngestionRepository
from stakeholder_intelligence_agent.ingestion.service import IngestionService
from stakeholder_intelligence_agent.ingestion.storage import IngestionArtifactStore
from stakeholder_intelligence_agent.ingestion.validation import UploadValidator
from stakeholder_intelligence_agent.insight import (
    InsightExecutionService,
    InsightGraphDependencies,
    InsightRunRepository,
    build_insight_graph,
)
from stakeholder_intelligence_agent.interview import (
    InterviewConversationService,
    TranscriptIngestionService,
    TranscriptRepository,
    build_interview_graph,
)
from stakeholder_intelligence_agent.persistence import DomainDatabase
from stakeholder_intelligence_agent.persistence.checkpointer import open_sqlite_checkpointer
from stakeholder_intelligence_agent.retrieval import (
    EvidenceRegistry,
    HybridRetrievalService,
    QdrantHybridSearcher,
    RetrievalRepository,
)
from tests.fakes import (
    DeterministicDocumentExtractor,
    DeterministicReranker,
    DeterministicVectorizer,
    DeterministicVisionEnricher,
    StaticFilterExtractor,
    ToolCallingFakeModel,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from fastapi import FastAPI
    from langchain_core.language_models import BaseChatModel

    from stakeholder_intelligence_agent.config import Settings
    from stakeholder_intelligence_agent.ingestion.types import DocumentExtractor, VisionEnricher
    from stakeholder_intelligence_agent.retrieval.types import RetrievedItem

pytestmark = pytest.mark.integration
FIXTURES = Path(__file__).parents[1] / "fixtures" / "ingestion"


class PrefixIds:
    """Return predictable unique opaque IDs independently for each prefix."""

    def __init__(self) -> None:
        self._counts: defaultdict[str, int] = defaultdict(int)

    def __call__(self, prefix: str) -> str:
        self._counts[prefix] += 1
        return f"{prefix}-{self._counts[prefix]}"

    def peek_next(self, prefix: str) -> str:
        """Return the next deterministic ID without consuming it."""

        return f"{prefix}-{self._counts[prefix] + 1}"


@dataclass(slots=True)
class RouteHarness:
    app: FastAPI
    services: ApplicationServices
    settings: Settings
    database: DomainDatabase
    access: AccessService
    retrieval: HybridRetrievalService
    transcript_repository: TranscriptRepository
    ids: PrefixIds
    insight_primary: ToolCallingFakeModel
    researcher: ToolCallingFakeModel
    editor: ToolCallingFakeModel


def _tool_call(name: str, arguments: dict[str, Any], call_id: str) -> AIMessage:
    call: ToolCall = {
        "name": name,
        "args": arguments,
        "id": call_id,
        "type": "tool_call",
    }
    return AIMessage(content="", tool_calls=[call])


def _evidence_id(run_id: str, topic_id: str, item: RetrievedItem) -> str:
    return stable_id(
        "evidence",
        run_id,
        topic_id,
        stable_id("researcher", run_id, topic_id),
        item.candidate.chunk_id,
        sha256(item.original_excerpt.encode("utf-8")).hexdigest(),
    )


def _report_payload(
    context: InsightRuntimeContext,
    selected: tuple[tuple[str, RetrievedItem], ...],
) -> dict[str, Any]:
    findings = []
    citations = []
    for index, (evidence_id, item) in enumerate(selected, start=1):
        claim_id = f"claim-{index}"
        findings.append(
            {
                "claim_id": claim_id,
                "statement": (
                    f"Authorized {item.candidate.metadata.source_type} evidence describes "
                    "the current handoff."
                ),
                "evidence_ids": [evidence_id],
            }
        )
        citations.append(
            {
                "citation_id": f"citation-{index}",
                "evidence_id": evidence_id,
                "display_label": f"Evidence {index}",
                "source_location": item.candidate.location.model_dump(mode="json"),
                "claim_ids": [claim_id],
            }
        )
    evidence_ids = [evidence_id for evidence_id, _ in selected]
    return {
        "report_id": "route-report-1",
        "engagement_id": context.access.engagement_id,
        "question": context.question,
        "status": "complete",
        "executive_summary": (
            "Current document and finalized-interview evidence both describe the handoff."
        ),
        "researched_topics": [
            {
                "topic_id": "topic-handoff",
                "title": "Operational Handoff",
                "status": "completed",
                "summary": "Both permitted source classes were reviewed.",
                "evidence_ids": evidence_ids,
            }
        ],
        "findings": findings,
        "responsibilities": [],
        "operational_risks": [],
        "buy_in_signals": [
            {
                "topic": "Operational Handoff",
                "stakeholder_id": None,
                "role": None,
                "department": None,
                "category": "topic_not_discussed",
                "explanation": "The selected excerpts do not establish a buy-in signal.",
                "evidence_ids": [],
            }
        ],
        "contradictions": [],
        "evidence_gaps": [],
        "open_questions": ["Which control owner can replace the current handoff?"],
        "follow_up_recommendations": [],
        "evidence_ids": evidence_ids,
        "citations": citations,
        "run_metadata": {
            "run_id": context.run_id,
            "started_at": "2026-07-15T00:00:00Z",
            "completed_at": "2026-07-15T00:00:02Z",
            "primary_model_id": "gemini-test-primary",
            "fallback_model_id": "gemini-test-fallback",
            "topic_count": 1,
            "status_detail": "The bounded mixed-source workflow completed.",
        },
    }


@asynccontextmanager
async def _route_harness(
    settings: Settings,
    *,
    document_extractor: DocumentExtractor | None = None,
    vision_enricher: VisionEnricher | None = None,
    interview_model: BaseChatModel | None = None,
    insight_primary_model: ToolCallingFakeModel | None = None,
) -> AsyncIterator[RouteHarness]:
    settings = settings.model_copy(update={"gemini_embedding_dimension": 128})
    database = DomainDatabase(settings.domain_database)
    ids = PrefixIds()
    access = AccessService(database, settings, id_factory=ids)
    qdrant = AsyncQdrantClient(location=":memory:")
    stager = QdrantVectorStager(settings, client=qdrant)
    source_artifacts = IngestionArtifactStore(settings.originals_root, settings.derived_root)
    ingestion = IngestionService(
        settings=settings,
        repository=IngestionRepository(
            database,
            lease_seconds=settings.ingestion_lease_seconds,
        ),
        validator=UploadValidator(settings),
        artifacts=source_artifacts,
        extractor=document_extractor or DeterministicDocumentExtractor(),
        vision=vision_enricher or DeterministicVisionEnricher(),
        vectorizer=DeterministicVectorizer(),
        vector_stager=stager,
    )
    evidence_repository = RetrievalRepository(database)
    retrieval = HybridRetrievalService(
        settings=settings,
        repository=evidence_repository,
        filter_extractor=StaticFilterExtractor(),
        vectorizer=DeterministicVectorizer(),
        search_backend=QdrantHybridSearcher(settings, client=qdrant),
        reranker=DeterministicReranker(),
    )
    transcript_repository = TranscriptRepository(
        database,
        lease_seconds=settings.ingestion_lease_seconds,
    )
    transcript_ingestion = TranscriptIngestionService(
        settings=settings,
        repository=transcript_repository,
        vectorizer=DeterministicVectorizer(),
        vector_stager=stager,
    )
    configured_interview_model = interview_model or ToolCallingFakeModel(
        responses=[
            AIMessage(content="Which current record shows where the manual handoff is delayed?"),
            AIMessage(
                content=(
                    "Thank you. I have enough information to complete this interview. "
                    "You can finish now, or continue if you would like to add something else."
                )
            ),
        ]
    )
    insight_primary = insight_primary_model or ToolCallingFakeModel(responses=[])
    researcher = ToolCallingFakeModel(responses=[])
    editor = ToolCallingFakeModel(responses=[])
    insight_runs = InsightRunRepository(database)
    agent_artifacts = ScopedArtifactStore(settings.agent_artifacts_root)
    try:
        async with open_sqlite_checkpointer(settings.checkpoint_database) as saver:
            interview = InterviewConversationService(
                repository=transcript_repository,
                graph=build_interview_graph(
                    settings,
                    primary_model=configured_interview_model,
                    fallback_model=ToolCallingFakeModel(
                        responses=[AIMessage(content="Fallback interview response.")]
                    ),
                    checkpointer=saver,
                    retrieval_service=retrieval,
                ),
                checkpointer=saver,
                ingestion=transcript_ingestion,
            )
            insight = InsightExecutionService(
                graph=build_insight_graph(
                    settings,
                    dependencies=InsightGraphDependencies(
                        primary_model=insight_primary,
                        fallback_model=ToolCallingFakeModel(
                            responses=[AIMessage(content="Fallback insight response.")]
                        ),
                        researcher_model=researcher,
                        editor_model=editor,
                        checkpointer=saver,
                        harness_provider="toolcallingfakemodel",
                        retrieval_service=retrieval,
                        evidence_repository=evidence_repository,
                        run_repository=insight_runs,
                    ),
                ),
                repository=insight_runs,
                artifacts=agent_artifacts,
                settings=settings,
            )
            services = ApplicationServices(
                settings=settings,
                database=database,
                access=access,
                reads=DomainReadRepository(database),
                ingestion=ingestion,
                transcript_repository=transcript_repository,
                interview=interview,
                insight=insight,
                insight_runs=insight_runs,
                evidence=EvidenceRegistry(evidence_repository, source_artifacts),
                source_artifacts=source_artifacts,
                agent_artifacts=agent_artifacts,
                retrieval=retrieval,
                id_factory=ids,
            )
            yield RouteHarness(
                app=create_app(services=services),
                services=services,
                settings=settings,
                database=database,
                access=access,
                retrieval=retrieval,
                transcript_repository=transcript_repository,
                ids=ids,
                insight_primary=insight_primary,
                researcher=researcher,
                editor=editor,
            )
    finally:
        await qdrant.close()


def _auth(token: str, *, correlation: str = "route-correlation") -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "X-Correlation-ID": correlation,
    }


async def test_route_inventory_contains_only_approved_domain_families() -> None:
    paths = set(create_app().openapi()["paths"])
    assert paths == {
        "/api/v1/system/health",
        "/api/v1/auth/pm/activate",
        "/api/v1/auth/stakeholder/activate",
        "/api/v1/auth/session/revoke",
        "/api/v1/browser/auth/pm/activate",
        "/api/v1/browser/auth/stakeholder/activate",
        "/api/v1/browser/auth/session",
        "/api/v1/browser/auth/logout",
        "/api/v1/pm/engagements",
        "/api/v1/pm/engagements/{engagement_id}",
        "/api/v1/pm/engagements/{engagement_id}/select",
        "/api/v1/pm/engagements/{engagement_id}/stakeholders",
        ("/api/v1/pm/engagements/{engagement_id}/stakeholders/{stakeholder_id}/invitations"),
        "/api/v1/pm/engagements/{engagement_id}/invitations",
        "/api/v1/pm/engagements/{engagement_id}/invitations/{invitation_id}",
        "/api/v1/pm/engagements/{engagement_id}/invitations/{invitation_id}/link",
        "/api/v1/pm/engagements/{engagement_id}/documents",
        "/api/v1/pm/engagements/{engagement_id}/documents/{document_id}",
        "/api/v1/pm/engagements/{engagement_id}/documents/{document_id}/processing",
        ("/api/v1/pm/engagements/{engagement_id}/documents/{document_id}/artifacts/{artifact_id}"),
        "/api/v1/pm/engagements/{engagement_id}/interviews",
        "/api/v1/pm/engagements/{engagement_id}/interviews/{interview_session_id}",
        "/api/v1/pm/engagements/{engagement_id}/insights",
        "/api/v1/pm/engagements/{engagement_id}/insights/{run_id}",
        "/api/v1/pm/engagements/{engagement_id}/insights/{run_id}/events",
        "/api/v1/pm/engagements/{engagement_id}/insights/{run_id}/report",
        ("/api/v1/pm/engagements/{engagement_id}/insights/{run_id}/evidence/{evidence_id}"),
        (
            "/api/v1/pm/engagements/{engagement_id}/insights/{run_id}/"
            "evidence/{evidence_id}/artifacts/{artifact_id}"
        ),
        "/api/v1/stakeholder/context",
        "/api/v1/stakeholder/documents",
        "/api/v1/stakeholder/documents/{document_id}",
        "/api/v1/stakeholder/interview/start",
        "/api/v1/stakeholder/interview/turns/{turn_index}",
        "/api/v1/stakeholder/interview/turns/stream",
        "/api/v1/stakeholder/interview/status",
        "/api/v1/stakeholder/interview/finish",
    }


async def test_custom_routes_complete_scoped_setup_interview_and_insight_flow(  # noqa: PLR0915
    settings: Settings,
) -> None:
    async with _route_harness(settings) as harness:  # noqa: SIM117
        async with harness.app.router.lifespan_context(harness.app):
            async with AsyncClient(
                transport=ASGITransport(app=harness.app),
                base_url="http://testserver",
            ) as client:
                invalid = await client.post(
                    "/api/v1/auth/pm/activate",
                    json={"bootstrap_token": "secret-too-short"},
                    headers={"X-Correlation-ID": "validation-correlation"},
                )
                assert invalid.status_code == 422
                assert "secret-too-short" not in invalid.text

                activated_pm = await client.post(
                    "/api/v1/auth/pm/activate",
                    json={
                        "bootstrap_token": harness.settings.pm_bootstrap_token.get_secret_value()
                    },
                    headers={"X-Correlation-ID": "pm-activation"},
                )
                assert activated_pm.status_code == 200
                assert activated_pm.headers["cache-control"] == "no-store"
                pm_token = activated_pm.json()["access_token"]
                assert (await client.get("/api/v1/pm/engagements", headers=_auth(pm_token))).json()[
                    "engagements"
                ] == []

                engagement_response = await client.post(
                    "/api/v1/pm/engagements",
                    json={"name": "Route engagement", "description": "Approved route flow."},
                    headers=_auth(pm_token),
                )
                assert engagement_response.status_code == 201
                engagement_id = engagement_response.json()["engagement"]["engagement_id"]

                stakeholder_response = await client.post(
                    f"/api/v1/pm/engagements/{engagement_id}/stakeholders",
                    json={
                        "display_name": "Alex Morgan",
                        "role": "Operations manager",
                        "department": "Operations",
                    },
                    headers=_auth(pm_token),
                )
                assert stakeholder_response.status_code == 201
                stakeholder_id = stakeholder_response.json()["stakeholder"]["stakeholder_id"]

                invitation_response = await client.post(
                    (
                        f"/api/v1/pm/engagements/{engagement_id}/stakeholders/"
                        f"{stakeholder_id}/invitations"
                    ),
                    headers=_auth(pm_token),
                )
                assert invitation_response.status_code == 201
                invitation_token = invitation_response.json()["invitation_token"]

                activated_stakeholder = await client.post(
                    "/api/v1/auth/stakeholder/activate",
                    json={"invitation_token": invitation_token},
                    headers={"X-Correlation-ID": "stakeholder-activation"},
                )
                assert activated_stakeholder.status_code == 200
                stakeholder_token = activated_stakeholder.json()["session"]["access_token"]
                stakeholder_headers = _auth(stakeholder_token)

                replay = await client.post(
                    "/api/v1/auth/stakeholder/activate",
                    json={"invitation_token": invitation_token},
                    headers={"X-Correlation-ID": "stakeholder-replay"},
                )
                assert replay.status_code == 200
                assert (
                    replay.json()["interview_session_id"]
                    == activated_stakeholder.json()["interview_session_id"]
                )
                assert replay.json()["thread_id"] == activated_stakeholder.json()["thread_id"]
                assert invitation_token not in replay.text
                invitation_id = invitation_response.json()["invitation"]["invitation_id"]
                copied_link = await client.get(
                    (f"/api/v1/pm/engagements/{engagement_id}/invitations/{invitation_id}/link"),
                    headers=_auth(pm_token),
                )
                assert copied_link.status_code == 200
                assert copied_link.json()["invitation_token"] == invitation_token

                revoked_stakeholder = await client.post(
                    f"/api/v1/pm/engagements/{engagement_id}/stakeholders",
                    json={
                        "display_name": "Revoked invite",
                        "role": "Reviewer",
                        "department": "Controls",
                    },
                    headers=_auth(pm_token),
                )
                revoked_stakeholder_id = revoked_stakeholder.json()["stakeholder"]["stakeholder_id"]
                issued_for_revocation = await client.post(
                    (
                        f"/api/v1/pm/engagements/{engagement_id}/stakeholders/"
                        f"{revoked_stakeholder_id}/invitations"
                    ),
                    headers=_auth(pm_token),
                )
                revoked_token = issued_for_revocation.json()["invitation_token"]
                invitation_id = issued_for_revocation.json()["invitation"]["invitation_id"]
                revoked = await client.delete(
                    f"/api/v1/pm/engagements/{engagement_id}/invitations/{invitation_id}",
                    headers=_auth(pm_token),
                )
                assert revoked.status_code == 200
                assert revoked.json()["status"] == "revoked"
                denied_revoked = await client.post(
                    "/api/v1/auth/stakeholder/activate",
                    json={"invitation_token": revoked_token},
                    headers={"X-Correlation-ID": "revoked-invitation"},
                )
                assert denied_revoked.status_code == 403
                assert revoked_token not in denied_revoked.text
                invitation_list = await client.get(
                    f"/api/v1/pm/engagements/{engagement_id}/invitations",
                    headers=_auth(pm_token),
                )
                assert invitation_list.status_code == 200
                assert "token_hash" not in invitation_list.text
                assert invitation_token not in invitation_list.text
                assert revoked_token not in invitation_list.text

                context_response = await client.get(
                    "/api/v1/stakeholder/context",
                    headers=stakeholder_headers,
                )
                assert context_response.status_code == 200
                assert context_response.json()["stakeholder"]["role"] == "Operations manager"

                image = (FIXTURES / "alpha-organization-chart.png").read_bytes()
                stakeholder_upload = await client.post(
                    "/api/v1/stakeholder/documents",
                    files={"upload": ("stakeholder-chart.png", image, "image/png")},
                    headers=stakeholder_headers,
                )
                assert stakeholder_upload.status_code == 201
                assert stakeholder_upload.json()["document"]["source"]["source_type"] == (
                    "stakeholder_document"
                )
                stakeholder_document_id = stakeholder_upload.json()["document"]["source"][
                    "document_id"
                ]
                deleted_document = await client.delete(
                    f"/api/v1/stakeholder/documents/{stakeholder_document_id}",
                    headers=stakeholder_headers,
                )
                assert deleted_document.status_code == 200
                assert deleted_document.json() == {"status": "ok"}
                assert (
                    await client.get(
                        "/api/v1/stakeholder/documents",
                        headers=stakeholder_headers,
                    )
                ).json() == {"documents": []}
                stakeholder_upload = await client.post(
                    "/api/v1/stakeholder/documents",
                    files={"upload": ("stakeholder-chart.png", image, "image/png")},
                    headers=stakeholder_headers,
                )
                assert stakeholder_upload.status_code == 201
                assert (
                    stakeholder_upload.json()["document"]["latest_version"]["version_number"] == 2
                )

                pm_upload = await client.post(
                    f"/api/v1/pm/engagements/{engagement_id}/documents",
                    files={"upload": ("engagement-chart.png", image, "image/png")},
                    headers=_auth(pm_token),
                )
                assert pm_upload.status_code == 201
                assert pm_upload.json()["document"]["source"]["stakeholder_id"] is None
                pm_document_id = pm_upload.json()["document"]["source"]["document_id"]
                denied_stakeholder_delete = await client.delete(
                    f"/api/v1/pm/engagements/{engagement_id}/documents/{stakeholder_document_id}",
                    headers=_auth(pm_token),
                )
                assert denied_stakeholder_delete.status_code == 403
                deleted_pm_document = await client.delete(
                    f"/api/v1/pm/engagements/{engagement_id}/documents/{pm_document_id}",
                    headers=_auth(pm_token),
                )
                assert deleted_pm_document.status_code == 200
                assert deleted_pm_document.json() == {"status": "ok"}
                pm_upload = await client.post(
                    f"/api/v1/pm/engagements/{engagement_id}/documents",
                    files={"upload": ("engagement-chart.png", image, "image/png")},
                    headers=_auth(pm_token),
                )
                assert pm_upload.status_code == 201
                assert pm_upload.json()["document"]["latest_version"]["version_number"] == 2

                pm_documents_before_finalization = await client.get(
                    f"/api/v1/pm/engagements/{engagement_id}/documents",
                    headers=_auth(pm_token),
                )
                assert pm_documents_before_finalization.status_code == 200
                assert [
                    item["source"]["source_type"]
                    for item in pm_documents_before_finalization.json()["documents"]
                ] == ["engagement_document"]
                hidden_stakeholder_document = await client.get(
                    (f"/api/v1/pm/engagements/{engagement_id}/documents/{stakeholder_document_id}"),
                    headers=_auth(pm_token),
                )
                assert hidden_stakeholder_document.status_code == 403

                started = await client.post(
                    "/api/v1/stakeholder/interview/start",
                    headers=stakeholder_headers,
                )
                assert started.status_code == 200
                assert started.json()["turn_count"] == 1
                assert started.json()["completion_recommended"] is False
                assert started.json()["turns"] == [
                    {
                        "turn_index": 0,
                        "speaker": "assistant",
                        "text": (
                            "What are the main tasks you personally perform in your day-to-day "
                            "work as Operations manager?"
                        ),
                    }
                ]
                repeated_start = await client.post(
                    "/api/v1/stakeholder/interview/start",
                    headers=stakeholder_headers,
                )
                assert repeated_start.status_code == 200
                assert repeated_start.json()["turns"] == started.json()["turns"]

                original_input = "  Vodim ručni prenos za ana@example.com.  "
                streamed = await client.post(
                    "/api/v1/stakeholder/interview/turns/stream",
                    json={"original_text": original_input, "message_id": "route-message-1"},
                    headers=stakeholder_headers,
                )
                assert streamed.status_code == 200
                assert "event: message" in streamed.text
                assert "manual handoff" in streamed.text
                assert "private reasoning" not in streamed.text.lower()

                interview_status = await client.get(
                    "/api/v1/stakeholder/interview/status",
                    headers=stakeholder_headers,
                )
                assert interview_status.status_code == 200
                assert interview_status.json()["turn_count"] == 3
                assert interview_status.json()["turns"][1]["text"] == original_input
                assert interview_status.json()["turns"][2]["speaker"] == "assistant"
                assert interview_status.json()["interview_session"]["status"] == "draft"
                assert interview_status.json()["completion_recommended"] is False

                early_finish = await client.post(
                    "/api/v1/stakeholder/interview/finish",
                    headers=stakeholder_headers,
                )
                assert early_finish.status_code == 409
                assert early_finish.json()["error"]["code"] == ("INTERVIEW_COMPLETION_NOT_READY")

                deleted_answer = await client.delete(
                    "/api/v1/stakeholder/interview/turns/1",
                    headers=stakeholder_headers,
                )
                assert deleted_answer.status_code == 200
                assert deleted_answer.json()["turns"] == started.json()["turns"]
                assert deleted_answer.json()["completion_recommended"] is False
                finish_after_deletion = await client.post(
                    "/api/v1/stakeholder/interview/finish",
                    headers=stakeholder_headers,
                )
                assert finish_after_deletion.status_code == 409
                assert finish_after_deletion.json()["error"]["code"] == (
                    "INTERVIEW_COMPLETION_NOT_READY"
                )

                completion_turn = await client.post(
                    "/api/v1/stakeholder/interview/turns/stream",
                    json={
                        "original_text": "The manual handoff is the only remaining detail.",
                        "message_id": "route-message-2",
                    },
                    headers=stakeholder_headers,
                )
                assert completion_turn.status_code == 200
                completed_status = await client.get(
                    "/api/v1/stakeholder/interview/status",
                    headers=stakeholder_headers,
                )
                assert completed_status.json()["completion_recommended"] is True

                finished = await client.post(
                    "/api/v1/stakeholder/interview/finish",
                    headers=stakeholder_headers,
                )
                assert finished.status_code == 200
                assert finished.json()["interview_session"]["status"] == "ready"
                assert finished.json()["ingestion_version"]["state"] == "READY"
                repeated_finish = await client.post(
                    "/api/v1/stakeholder/interview/finish",
                    headers=stakeholder_headers,
                )
                assert repeated_finish.status_code == 200
                assert repeated_finish.json()["idempotent"] is True
                denied_answer_delete = await client.delete(
                    "/api/v1/stakeholder/interview/turns/1",
                    headers=stakeholder_headers,
                )
                assert denied_answer_delete.status_code == 409
                assert denied_answer_delete.json()["error"]["code"] == "TRANSCRIPT_IMMUTABLE"
                denied_document_delete = await client.delete(
                    f"/api/v1/stakeholder/documents/{stakeholder_document_id}",
                    headers=stakeholder_headers,
                )
                assert denied_document_delete.status_code == 409
                assert denied_document_delete.json()["error"]["code"] == "TRANSCRIPT_IMMUTABLE"

                stakeholder_access = await harness.access.resolve_stakeholder_context(
                    stakeholder_token,
                    correlation_id="raw-transcript-check",
                    required_permission="interview:participate",
                )
                snapshot = await harness.transcript_repository.snapshot(
                    stakeholder_access,
                    now=harness.app.state.services.clock(),
                )
                assert snapshot.turns[1].value.original_text == (
                    "The manual handoff is the only remaining detail."
                )

                stakeholder_documents = await client.get(
                    "/api/v1/stakeholder/documents",
                    headers=stakeholder_headers,
                )
                assert len(stakeholder_documents.json()["documents"]) == 1
                pm_documents = await client.get(
                    f"/api/v1/pm/engagements/{engagement_id}/documents",
                    headers=_auth(pm_token),
                )
                assert len(pm_documents.json()["documents"]) == 2
                assert {
                    item["source"]["source_type"] for item in pm_documents.json()["documents"]
                } == {"engagement_document", "stakeholder_document"}
                engagement_view = await client.get(
                    f"/api/v1/pm/engagements/{engagement_id}",
                    headers=_auth(pm_token),
                )
                assert engagement_view.json()["engagement"]["engagement_id"] == engagement_id
                stakeholder_view = await client.get(
                    f"/api/v1/pm/engagements/{engagement_id}/stakeholders",
                    headers=_auth(pm_token),
                )
                assert len(stakeholder_view.json()["stakeholders"]) == 2
                document_id = pm_documents.json()["documents"][0]["source"]["document_id"]
                document_view = await client.get(
                    f"/api/v1/pm/engagements/{engagement_id}/documents/{document_id}",
                    headers=_auth(pm_token),
                )
                assert document_view.json()["source"]["document_id"] == document_id
                processing_view = await client.get(
                    (f"/api/v1/pm/engagements/{engagement_id}/documents/{document_id}/processing"),
                    headers=_auth(pm_token),
                )
                assert processing_view.status_code == 200
                processing_payload = processing_view.json()
                assert processing_payload["document"]["source"]["document_id"] == document_id
                assert processing_payload["element_count"] > 0
                assert processing_payload["chunk_count"] > 0
                assert processing_payload["artifact_count"] > 0
                assert processing_payload["lifecycle_events"][-1]["to_state"] == "READY"
                presentation_element_types = {
                    item["element_type"] for item in processing_payload["element_previews"]
                }
                assert "vision_description" in presentation_element_types
                assert "image" not in presentation_element_types
                assert "chart" not in presentation_element_types
                assert "dense_vector" not in processing_view.text
                original_artifact = next(
                    item
                    for item in processing_payload["artifacts"]
                    if item["artifact_kind"] == "original"
                )
                original_view = await client.get(
                    original_artifact["download_path"],
                    headers=_auth(pm_token),
                )
                assert original_view.status_code == 200
                assert original_view.content == image
                assert original_view.headers["content-type"] == "image/png"
                assert original_view.headers["content-disposition"].startswith("inline;")
                assert original_view.headers["x-frame-options"] == "SAMEORIGIN"
                assert "frame-ancestors 'self'" in original_view.headers["content-security-policy"]
                denied_unknown_artifact = await client.get(
                    (
                        f"/api/v1/pm/engagements/{engagement_id}/documents/{document_id}/"
                        "artifacts/artifact-foreign"
                    ),
                    headers=_auth(pm_token),
                )
                assert denied_unknown_artifact.status_code == 403
                denied_unknown_document = await client.get(
                    (
                        f"/api/v1/pm/engagements/{engagement_id}/documents/"
                        "document-foreign/processing"
                    ),
                    headers=_auth(pm_token),
                )
                assert denied_unknown_document.status_code == 403
                pm_interviews = await client.get(
                    f"/api/v1/pm/engagements/{engagement_id}/interviews",
                    headers=_auth(pm_token),
                )
                assert pm_interviews.json()["interview_sessions"][0]["status"] == "ready"
                interview_session_id = finished.json()["interview_session"]["interview_session_id"]
                pm_interview_preview = await client.get(
                    (f"/api/v1/pm/engagements/{engagement_id}/interviews/{interview_session_id}"),
                    headers=_auth(pm_token),
                )
                assert pm_interview_preview.status_code == 200
                preview_payload = pm_interview_preview.json()
                assert preview_payload["interview_session"]["status"] == "ready"
                assert preview_payload["transcript"]["status"] == "finalized"
                assert preview_payload["turns"][1] == {
                    "turn_index": 1,
                    "speaker": "stakeholder",
                    "text": "The manual handoff is the only remaining detail.",
                }
                assert "checkpoint" not in pm_interview_preview.text.lower()
                denied_interview_preview = await client.get(
                    f"/api/v1/pm/engagements/{engagement_id}/interviews/interview-foreign",
                    headers=_auth(pm_token),
                )
                assert denied_interview_preview.status_code == 403

                question = "What current evidence describes the operational handoff?"
                run_id = "run-1"
                thread_id = "report-thread-1"
                pm_access = await harness.access.resolve_pm_context(
                    pm_token,
                    engagement_id,
                    correlation_id="insight-preflight",
                    thread_id=thread_id,
                    required_permission="insight:run",
                )
                context = InsightRuntimeContext(
                    access=pm_access,
                    run_id=run_id,
                    question=question,
                )
                preflight = await harness.retrieval.retrieve(pm_access, "operational handoff")
                by_type = {item.candidate.metadata.source_type: item for item in preflight.items}
                assert "engagement_document" in by_type
                assert "interview" in by_type
                selected = tuple(
                    (
                        _evidence_id(run_id, "topic-handoff", by_type[source_type]),
                        by_type[source_type],
                    )
                    for source_type in ("engagement_document", "interview")
                )
                all_evidence_ids = [
                    _evidence_id(run_id, "topic-handoff", item) for item in preflight.items
                ]
                harness.insight_primary.responses.extend(
                    [
                        _tool_call(
                            "write_todos",
                            {
                                "todos": [
                                    {
                                        "content": "Research Operational Handoff",
                                        "status": "in_progress",
                                    },
                                    {"content": "Edit the report", "status": "pending"},
                                    {"content": "Validate the report", "status": "pending"},
                                ]
                            },
                            "route-todos",
                        ),
                        _tool_call(
                            "create_research_plan",
                            {
                                "topics": [
                                    {
                                        "topic_id": "topic-handoff",
                                        "title": "Operational Handoff",
                                        "objective": "Identify current handoff evidence.",
                                        "questions": ["Which source describes the handoff?"],
                                        "required_source_types": ["document", "interview"],
                                        "dependencies": [],
                                        "priority": 1,
                                    }
                                ],
                                "source_strategy": ["document", "interview"],
                                "completion_criteria": [
                                    "Use registered evidence from both source classes."
                                ],
                            },
                            "route-plan",
                        ),
                        _tool_call(
                            "task",
                            {
                                "description": (
                                    "topic_id=topic-handoff Research only the handoff topic."
                                ),
                                "subagent_type": "topic-researcher",
                            },
                            "route-researcher",
                        ),
                        _tool_call(
                            "write_todos",
                            {
                                "todos": [
                                    {
                                        "content": "Research Operational Handoff",
                                        "status": "completed",
                                    },
                                    {"content": "Edit the report", "status": "in_progress"},
                                    {"content": "Validate the report", "status": "pending"},
                                ]
                            },
                            "route-research-complete",
                        ),
                        _tool_call(
                            "task",
                            {
                                "description": "Load completed artifacts and create the report.",
                                "subagent_type": "report-editor",
                            },
                            "route-editor",
                        ),
                        _tool_call(
                            "write_todos",
                            {
                                "todos": [
                                    {
                                        "content": "Research Operational Handoff",
                                        "status": "completed",
                                    },
                                    {"content": "Edit the report", "status": "completed"},
                                    {"content": "Validate the report", "status": "completed"},
                                ]
                            },
                            "route-workflow-complete",
                        ),
                        AIMessage(content="The validated report artifact is available."),
                    ]
                )
                harness.researcher.responses.extend(
                    [
                        _tool_call(
                            "scoped_retrieve",
                            {"topic_id": "topic-handoff", "query": "operational handoff"},
                            "route-retrieve",
                        ),
                        _tool_call(
                            "think_tool",
                            {"reflection": "Review only the assigned topic."},
                            "route-think",
                        ),
                        _tool_call(
                            "save_research_artifacts",
                            {
                                "topic_id": "topic-handoff",
                                "findings_markdown": (
                                    "# Findings\n\nDocument and interview evidence describe "
                                    "the current handoff."
                                ),
                                "evidence_ids": all_evidence_ids,
                            },
                            "route-save-research",
                        ),
                        AIMessage(content="The assigned evidence artifacts were saved."),
                    ]
                )
                harness.editor.responses.extend(
                    [
                        _tool_call("load_research_package", {}, "route-load"),
                        _tool_call(
                            "save_final_report",
                            {"report": _report_payload(context, selected)},
                            "route-save-report",
                        ),
                        AIMessage(content="The strict report was validated and saved."),
                    ]
                )

                insight_response = await client.post(
                    f"/api/v1/pm/engagements/{engagement_id}/insights",
                    json={"question": question},
                    headers=_auth(pm_token),
                )
                assert insight_response.status_code == 202, insight_response.text
                assert insight_response.json()["run"]["run_id"] == run_id

                history_response = await client.get(
                    f"/api/v1/pm/engagements/{engagement_id}/insights",
                    headers=_auth(pm_token),
                )
                assert history_response.status_code == 200
                assert [item["run_id"] for item in history_response.json()["runs"]] == [run_id]

                status_response = await client.get(
                    f"/api/v1/pm/engagements/{engagement_id}/insights/{run_id}",
                    headers=_auth(pm_token),
                )
                assert status_response.json()["run"]["status"] == "complete"
                report_response = await client.get(
                    f"/api/v1/pm/engagements/{engagement_id}/insights/{run_id}/report",
                    headers=_auth(pm_token),
                )
                response_body = report_response.json()
                report = response_body["report"]
                metrics = response_body["metrics"]
                assert report["report_id"] == stable_id("report", run_id)
                assert report["status"] == "complete"
                assert "model_calls" not in report["run_metadata"]
                assert "tool_calls" not in report["run_metadata"]
                assert metrics["run_id"] == run_id
                assert metrics["status"] == "complete"
                assert metrics["model_calls"] > 0
                assert metrics["tool_calls"] > 0
                assert {item["evidence_id"] for item in report["citations"]} == {
                    evidence_id for evidence_id, _ in selected
                }
                events_response = await client.get(
                    f"/api/v1/pm/engagements/{engagement_id}/insights/{run_id}/events",
                    headers=_auth(pm_token),
                )
                assert events_response.headers["content-type"].startswith("text/event-stream")
                assert '"action":"report_validated"' in events_response.text
                assert "private reasoning" not in events_response.text.lower()

                document_evidence_id = next(
                    citation["evidence_id"]
                    for citation in report["citations"]
                    if citation["source_location"]["kind"] == "image_region"
                )
                drill_response = await client.get(
                    (
                        f"/api/v1/pm/engagements/{engagement_id}/insights/{run_id}/"
                        f"evidence/{document_evidence_id}"
                    ),
                    headers=_auth(pm_token),
                )
                assert drill_response.status_code == 200
                download_path = drill_response.json()["original"]["download_path"]
                assert download_path.startswith("/api/v1/")
                assert ":\\" not in download_path
                downloaded = await client.get(download_path, headers=_auth(pm_token))
                assert downloaded.status_code == 200
                assert downloaded.content == image

                second = await client.post(
                    "/api/v1/pm/engagements",
                    json={"name": "Other engagement", "description": None},
                    headers=_auth(pm_token),
                )
                second_id = second.json()["engagement"]["engagement_id"]
                second_history = await client.get(
                    f"/api/v1/pm/engagements/{second_id}/insights",
                    headers=_auth(pm_token),
                )
                assert second_history.status_code == 200
                assert second_history.json() == {"runs": []}
                denied = await client.get(
                    f"/api/v1/pm/engagements/{second_id}/insights/{run_id}",
                    headers=_auth(pm_token, correlation="same-denial"),
                )
                forged = await client.get(
                    f"/api/v1/pm/engagements/{second_id}/insights/forged-run",
                    headers=_auth(pm_token, correlation="same-denial"),
                )
                assert denied.status_code == forged.status_code == 403
                assert denied.json() == forged.json()

                await client.post(
                    f"/api/v1/pm/engagements/{engagement_id}/select",
                    headers=_auth(pm_token),
                )
                await client.post(
                    "/api/v1/auth/session/revoke",
                    headers=stakeholder_headers,
                )
                revoked_session = await client.get(
                    "/api/v1/stakeholder/context",
                    headers=stakeholder_headers,
                )
                assert revoked_session.status_code == 403
