"""Full Deep Agent run using real scoped document and finalized-interview evidence."""

from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from langchain_core.messages import AIMessage, ToolCall
from pydantic import TypeAdapter
from qdrant_client import AsyncQdrantClient

from stakeholder_intelligence_agent.access import AccessService
from stakeholder_intelligence_agent.artifacts import ScopedArtifactStore
from stakeholder_intelligence_agent.contracts import InsightRuntimeContext
from stakeholder_intelligence_agent.contracts.source import SourceLocation
from stakeholder_intelligence_agent.errors import DomainConflictError, EvidencePolicyError
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
from stakeholder_intelligence_agent.insight.tools import validate_report_evidence
from stakeholder_intelligence_agent.interview import (
    TranscriptIngestionService,
    TranscriptRepository,
)
from stakeholder_intelligence_agent.persistence import DomainDatabase
from stakeholder_intelligence_agent.persistence.checkpointer import open_sqlite_checkpointer
from stakeholder_intelligence_agent.retrieval import (
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
    from stakeholder_intelligence_agent.config import Settings
    from stakeholder_intelligence_agent.contracts import AccessContext
    from stakeholder_intelligence_agent.retrieval.types import RetrievedItem

pytestmark = [pytest.mark.integration, pytest.mark.trajectory]
FIXTURES = Path(__file__).parents[1] / "fixtures" / "ingestion"


def _tool_call(name: str, arguments: dict[str, Any], call_id: str) -> AIMessage:
    call: ToolCall = {
        "name": name,
        "args": arguments,
        "id": call_id,
        "type": "tool_call",
    }
    return AIMessage(content="", tool_calls=[call])


def _evidence_id(run_id: str, topic_id: str, item: RetrievedItem) -> str:
    content_hash = sha256(item.original_excerpt.encode("utf-8")).hexdigest()
    researcher_id = stable_id("researcher", run_id, topic_id)
    return stable_id(
        "evidence",
        run_id,
        topic_id,
        researcher_id,
        item.candidate.chunk_id,
        content_hash,
    )


async def _access_contexts(
    settings: Settings,
    database: DomainDatabase,
) -> tuple[AccessContext, AccessContext]:
    access = AccessService(database, settings)
    await access.initialize()
    pm_session = await access.activate_pm(settings.pm_bootstrap_token.get_secret_value())
    pm_token = pm_session.token.get_secret_value()
    engagement = await access.create_engagement(
        pm_token,
        name="Deep Agent evidence",
        description="Synthetic mixed-source insight trajectory.",
        correlation_id="insight-engagement",
    )
    stakeholder = await access.create_stakeholder(
        pm_token,
        engagement.engagement_id,
        display_name="Alex Morgan",
        role="Operations manager",
        department="Operations",
        correlation_id="insight-stakeholder",
    )
    invitation = await access.issue_invitation(
        pm_token,
        engagement.engagement_id,
        stakeholder.stakeholder_id,
        correlation_id="insight-invitation",
    )
    activated = await access.activate_invitation(
        invitation.token.get_secret_value(),
        correlation_id="insight-activation",
    )
    stakeholder_access = await access.resolve_stakeholder_context(
        activated.access_session.token.get_secret_value(),
        correlation_id="insight-stakeholder-access",
        required_permission="source:read",
    )
    pm_access = await access.resolve_pm_context(
        pm_token,
        engagement.engagement_id,
        correlation_id="insight-pm-access",
        required_permission="insight:run",
        thread_id="insight-evidence-thread",
    )
    return pm_access, stakeholder_access


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
                    "a manual operational handoff."
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
    evidence_ids = [item[0] for item in selected]
    return {
        "report_id": "report-evidence-a",
        "engagement_id": context.access.engagement_id,
        "question": context.question,
        "status": "complete",
        "executive_summary": (
            "Current document and finalized-interview evidence both identify a manual handoff."
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
        "open_questions": ["Which control owner can replace the manual handoff?"],
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


async def test_deep_agent_registers_mixed_evidence_and_persists_complete_report(  # noqa: PLR0915
    settings: Settings,
) -> None:
    settings = settings.model_copy(update={"gemini_embedding_dimension": 128})
    database = DomainDatabase(settings.domain_database)
    pm_access, stakeholder_access = await _access_contexts(settings, database)
    qdrant = AsyncQdrantClient(location=":memory:")
    stager = QdrantVectorStager(settings, client=qdrant)
    document_ingestion = IngestionService(
        settings=settings,
        repository=IngestionRepository(
            database,
            lease_seconds=settings.ingestion_lease_seconds,
        ),
        validator=UploadValidator(settings),
        artifacts=IngestionArtifactStore(settings.originals_root, settings.derived_root),
        extractor=DeterministicDocumentExtractor(),
        vision=DeterministicVisionEnricher(),
        vectorizer=DeterministicVectorizer(),
        vector_stager=stager,
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
    evidence_repository = RetrievalRepository(database)
    retrieval = HybridRetrievalService(
        settings=settings,
        repository=evidence_repository,
        filter_extractor=StaticFilterExtractor(),
        vectorizer=DeterministicVectorizer(),
        search_backend=QdrantHybridSearcher(settings, client=qdrant),
        reranker=DeterministicReranker(),
    )
    try:
        await document_ingestion.initialize()
        await document_ingestion.ingest(
            pm_access,
            filename="handoff-map.png",
            declared_media_type="image/png",
            content=(FIXTURES / "alpha-organization-chart.png").read_bytes(),
        )
        now = datetime.now(UTC)
        external_non_english_text = (
            "Vodim ru\u010dni prenos od prijema do odobrenja; ka\u0161njenje se ponavlja."
        )
        await transcript_repository.append_turn(
            stakeholder_access,
            speaker="stakeholder",
            original_text=external_non_english_text,
            checkpoint_message_id="insight-human-a",
            now=now,
        )
        await transcript_repository.append_turn(
            stakeholder_access,
            speaker="assistant",
            original_text="Which record shows the delay?",
            checkpoint_message_id="insight-assistant-a",
            now=now,
        )
        await transcript_repository.finalize(stakeholder_access, now=now)
        await transcript_ingestion.initialize()
        await transcript_ingestion.ingest(stakeholder_access)

        question = "What current evidence describes the operational handoff?"
        context = InsightRuntimeContext(
            access=pm_access,
            run_id="run-evidence-a",
            question=question,
        )
        preflight = await retrieval.retrieve(pm_access, "manual operational handoff")
        source_types = {item.candidate.metadata.source_type for item in preflight.items}
        assert "interview" in source_types
        assert source_types & {"engagement_document", "stakeholder_document"}
        selected_items = []
        for wanted in ("engagement_document", "interview"):
            item = next(
                item for item in preflight.items if item.candidate.metadata.source_type == wanted
            )
            selected_items.append((_evidence_id(context.run_id, "topic-handoff", item), item))
        selected = tuple(selected_items)
        all_evidence_ids = [
            _evidence_id(context.run_id, "topic-handoff", item) for item in preflight.items
        ]

        primary = ToolCallingFakeModel(
            responses=[
                _tool_call(
                    "write_todos",
                    {
                        "todos": [
                            {"content": "Research Operational Handoff", "status": "in_progress"},
                            {"content": "Edit the structured report", "status": "pending"},
                            {"content": "Validate the report", "status": "pending"},
                        ]
                    },
                    "mixed-todos",
                ),
                _tool_call(
                    "create_research_plan",
                    {
                        "topics": [
                            {
                                "topic_id": "topic-handoff",
                                "title": "Operational Handoff",
                                "objective": "Identify current evidence about the handoff.",
                                "questions": ["What source describes the handoff?"],
                                "required_source_types": ["document", "interview"],
                                "dependencies": [],
                                "priority": 1,
                            }
                        ],
                        "source_strategy": ["document", "interview"],
                        "completion_criteria": ["Use registered evidence from both classes."],
                    },
                    "mixed-plan",
                ),
                _tool_call(
                    "task",
                    {
                        "description": "topic_id=topic-handoff Research only the handoff topic.",
                        "subagent_type": "topic-researcher",
                    },
                    "mixed-researcher",
                ),
                _tool_call(
                    "write_todos",
                    {
                        "todos": [
                            {
                                "content": "Research Operational Handoff",
                                "status": "completed",
                            },
                            {
                                "content": "Edit the structured report",
                                "status": "in_progress",
                            },
                            {"content": "Validate the report", "status": "pending"},
                        ]
                    },
                    "mixed-research-complete",
                ),
                _tool_call(
                    "task",
                    {
                        "description": "Load completed artifacts and create the report.",
                        "subagent_type": "report-editor",
                    },
                    "mixed-editor",
                ),
                _tool_call(
                    "write_todos",
                    {
                        "todos": [
                            {
                                "content": "Research Operational Handoff",
                                "status": "completed",
                            },
                            {
                                "content": "Edit the structured report",
                                "status": "completed",
                            },
                            {"content": "Validate the report", "status": "completed"},
                        ]
                    },
                    "mixed-workflow-complete",
                ),
                AIMessage(content="The validated report artifact is available."),
            ]
        )
        researcher = ToolCallingFakeModel(
            responses=[
                _tool_call(
                    "scoped_retrieve",
                    {"topic_id": "topic-handoff", "query": "manual operational handoff"},
                    "mixed-retrieve",
                ),
                _tool_call(
                    "think_tool",
                    {"reflection": "Review only the assigned topic and registered evidence."},
                    "mixed-think",
                ),
                _tool_call(
                    "scoped_retrieve",
                    {
                        "topic_id": "topic-handoff",
                        "query": "documented operational handoff ownership",
                    },
                    "mixed-retrieve-confirmation",
                ),
                _tool_call(
                    "think_tool",
                    {"reflection": "Confirm whether the scoped evidence set has stabilized."},
                    "mixed-think-confirmation",
                ),
                _tool_call(
                    "save_research_artifacts",
                    {
                        "topic_id": "topic-handoff",
                        "findings_markdown": (
                            "# Findings\n\nDocument and interview evidence both "
                            "describe the handoff."
                        ),
                        "evidence_ids": all_evidence_ids,
                    },
                    "mixed-save",
                ),
                AIMessage(content="The assigned evidence artifacts were saved."),
            ]
        )
        editor_payload = _report_payload(context, selected)
        for citation in editor_payload["citations"]:
            citation["source_location"] = "model-authored location text must be ignored"
        selected_ids = {evidence_id for evidence_id, _ in selected}
        claim_linked_interview_id = next(
            evidence_id
            for evidence_id, item in selected
            if item.candidate.metadata.source_type == "interview"
        )
        non_claim_evidence_id = next(
            evidence_id for evidence_id in all_evidence_ids if evidence_id not in selected_ids
        )
        editor_payload["buy_in_signals"] = [
            {
                "topic": "Operational Handoff",
                "stakeholder_id": stakeholder_access.stakeholder_id,
                "role": "Operations Lead",
                "department": "Operations",
                "category": "confirmed_support",
                "explanation": "The interview supports replacing the manual handoff.",
                "evidence_ids": [claim_linked_interview_id, non_claim_evidence_id],
            }
        ]
        editor = ToolCallingFakeModel(
            responses=[
                _tool_call("load_research_package", {}, "mixed-load"),
                _tool_call(
                    "save_final_report",
                    {"report": editor_payload},
                    "mixed-report",
                ),
                AIMessage(content="The strict report was validated and saved."),
            ]
        )
        run_repository = InsightRunRepository(database)
        async with open_sqlite_checkpointer(settings.checkpoint_database) as saver:
            graph = build_insight_graph(
                settings,
                dependencies=InsightGraphDependencies(
                    primary_model=primary,
                    fallback_model=ToolCallingFakeModel(
                        responses=[AIMessage(content="Fallback response.")]
                    ),
                    researcher_model=researcher,
                    editor_model=editor,
                    checkpointer=saver,
                    harness_provider="toolcallingfakemodel",
                    retrieval_service=retrieval,
                    evidence_repository=evidence_repository,
                    run_repository=run_repository,
                ),
            )
            execution = InsightExecutionService(
                graph=graph,
                repository=run_repository,
                artifacts=ScopedArtifactStore(settings.agent_artifacts_root),
                settings=settings,
            )
            result = await execution.execute(context)
            repeated = await execution.execute(context)

        assert result.run.status == "complete"
        assert result.report.status == "complete"
        assert result.metrics.status == "complete"
        assert repeated.metrics == result.metrics
        assert result.metrics.model_calls == (
            primary.call_count + researcher.call_count + editor.call_count
        )
        assert result.metrics.model_failures == 0
        assert result.metrics.tool_calls > 0
        assert result.metrics.tool_failures == 0
        assert result.metrics.retrieval_calls >= 1
        assert result.metrics.topic_count == result.report.run_metadata.topic_count
        assert result.metrics.researcher_calls == 1
        assert result.metrics.max_concurrent_researchers == 1
        assert result.metrics.max_rerank_candidates_per_call <= settings.max_rerank_candidates
        assert set(result.report.evidence_ids) <= set(result.metrics.evidence_ids)
        measured_events = await run_repository.execution_events(
            context,
            now=datetime.now(UTC),
        )
        assert sum(event.operation_type == "model" for event in measured_events) == (
            result.metrics.model_calls
        )
        assert sum(event.operation_type == "tool" for event in measured_events) == (
            result.metrics.tool_calls
        )
        assert {event.actor for event in measured_events} >= {
            "insight-orchestrator",
            "topic-researcher",
            "report-editor",
        }
        assert all(event.duration_ms >= 0 for event in measured_events)
        assert {citation.evidence_id for citation in result.report.citations} == {
            evidence_id for evidence_id, _ in selected
        }
        assert result.report.buy_in_signals[0].evidence_ids == (claim_linked_interview_id,)
        assert repeated.idempotent is True
        assert primary.call_count == 7
        assert researcher.call_count == 6
        assert editor.call_count == 3
        interview_citation = next(
            citation
            for citation in result.report.citations
            if next(
                item for evidence_id, item in selected if evidence_id == citation.evidence_id
            ).candidate.metadata.source_type
            == "interview"
        )
        interview_record = await evidence_repository.load_evidence(
            pm_access,
            interview_citation.evidence_id,
            now=datetime.now(UTC),
        )
        assert external_non_english_text in interview_record.original_excerpt
        assert all(ord(character) < 128 for character in result.report.model_dump_json())
        actions = [str(event["action"]) for event in result.events]
        assert "langgraph_update_streamed" in actions
        assert actions.index("run_planning") < actions.index("run_researching")
        assert actions.index("run_researching") < actions.index("research_artifacts_saved")
        assert actions.index("research_artifacts_saved") < actions.index("run_editing")
        assert actions.index("run_editing") < actions.index("run_validating")
        assert actions.index("run_validating") < actions.index("report_validated")

        document_citation_index, document_citation = next(
            (index, citation)
            for index, citation in enumerate(result.report.citations)
            if citation.source_location.kind == "image_region"
        )
        location_payload = document_citation.source_location.model_dump(mode="json")
        location_payload["region"] = "tampered-region"
        tampered_location: SourceLocation = TypeAdapter(SourceLocation).validate_python(
            location_payload
        )
        tampered_citation = document_citation.model_copy(
            update={"source_location": tampered_location}
        )
        tampered_citations = list(result.report.citations)
        tampered_citations[document_citation_index] = tampered_citation
        tampered_report = result.report.model_copy(
            update={
                "citations": tuple(tampered_citations),
            }
        )
        with pytest.raises(EvidencePolicyError):
            await validate_report_evidence(
                tampered_report,
                context,
                set(all_evidence_ids),
                evidence_repository,
            )
    finally:
        await qdrant.close()


async def test_direct_model_answer_cannot_bypass_report_lifecycle(
    settings: Settings,
) -> None:
    database = DomainDatabase(settings.domain_database)
    pm_access, _ = await _access_contexts(settings, database)
    context = InsightRuntimeContext(
        access=pm_access,
        run_id="run-direct-answer",
        question="Give me a direct answer without research.",
    )
    primary = ToolCallingFakeModel(responses=[AIMessage(content="Unsupported direct answer.")])
    run_repository = InsightRunRepository(database)
    graph = build_insight_graph(
        settings,
        dependencies=InsightGraphDependencies(
            primary_model=primary,
            fallback_model=ToolCallingFakeModel(
                responses=[AIMessage(content="Fallback response.")]
            ),
            researcher_model=primary,
            editor_model=primary,
            harness_provider="toolcallingfakemodel",
            run_repository=run_repository,
        ),
    )
    execution = InsightExecutionService(
        graph=graph,
        repository=run_repository,
        artifacts=ScopedArtifactStore(settings.agent_artifacts_root),
        settings=settings,
    )

    with pytest.raises(DomainConflictError):
        await execution.execute(context)

    failed = await run_repository.load(context, now=datetime.now(UTC))
    assert failed.status == "failed"
    assert failed.report_id is None
    assert failed.failure_code == "REPORT_NOT_PRODUCED"
    assert not ScopedArtifactStore(settings.agent_artifacts_root).exists(
        pm_access,
        "/report/insight_report.json",
    )
    assert primary.call_count == 1
