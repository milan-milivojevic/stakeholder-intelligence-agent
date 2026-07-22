"""Execute AC-01 through AC-03 against mixed, isolated, current evidence."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from langchain_core.messages import AIMessage, BaseMessage, ToolCall
from qdrant_client import AsyncQdrantClient

from stakeholder_intelligence_agent.access import AccessService
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
    from collections.abc import Callable, Sequence

    from stakeholder_intelligence_agent.config import Settings
    from stakeholder_intelligence_agent.ingestion.types import ExtractionBundle, ValidatedUpload
    from stakeholder_intelligence_agent.retrieval.types import RetrievedItem

pytestmark = [
    pytest.mark.integration,
    pytest.mark.trajectory,
    pytest.mark.timeout(120),
]

FIXTURES = Path(__file__).parents[1] / "fixtures" / "ingestion"
ALPHA_DOCUMENT_TEXT = {
    "alpha-organization-chart.png": (
        "The responsibility matrix assigns intake to Operations and final approval to Finance, "
        "but the escalation owner is blank. Manual handoffs create rework and missed deadlines."
    ),
    "alpha-influence-chart.png": (
        "The limited-pilot charter preserves customer exception visibility and requires a "
        "benefits checkpoint before expansion. It does not define an escalation owner."
    ),
}
BETA_DOCUMENT_TEXT = {
    "beta-process-map.jpg": (
        "BETA-CANARY-COBALT. Beta Legal owns release approval. This foreign evidence must never "
        "appear in an Alpha result."
    )
}
ALPHA_INTERVIEWS = (
    (
        "Olivia Reed",
        "Operations Lead",
        "Operations",
        "Operations owns intake and the manual handoff creates rework. I am concerned that the "
        "pilot lacks named training owners. The approval cycle normally takes two business days.",
    ),
    (
        "Frank Chen",
        "Finance Partner",
        "Finance",
        "Finance owns final approval and confirms support for a limited pilot. The same approval "
        "cycle normally takes seven to ten business days because submissions arrive incomplete.",
    ),
    (
        "Sam Rivera",
        "Sales Director",
        "Sales",
        "Sales conditionally supports the pilot if customer exceptions remain visible and the "
        "change does not add duplicate entry. This is compatible with the pilot charter.",
    ),
    (
        "Harper Jones",
        "Learning Manager",
        "People",
        "People can coordinate training after Operations names content owners. Approval "
        "automation was not discussed in this interview.",
    ),
)


class AcceptanceDocumentExtractor:
    """Replace only the deterministic extractor's text with scenario evidence."""

    def __init__(self, content_by_filename: dict[str, str]) -> None:
        self._content_by_filename = content_by_filename
        self._base = DeterministicDocumentExtractor()

    def extract(self, source_path: Path, upload: ValidatedUpload) -> ExtractionBundle:
        """Return format-correct elements carrying the requested synthetic semantics."""
        bundle = self._base.extract(source_path, upload)
        primary, *remaining = bundle.elements
        return replace(
            bundle,
            elements=(
                replace(primary, original_content=self._content_by_filename[upload.filename]),
                *remaining,
            ),
        )


@dataclass(slots=True)
class AcceptanceHarness:
    """Shared real persistence, Qdrant, retrieval, and evidence boundary."""

    settings: Settings
    database: DomainDatabase
    qdrant: AsyncQdrantClient
    access: AccessService
    pm_token: str
    alpha_engagement_id: str
    beta_engagement_id: str
    stakeholder_ids: dict[str, str]
    retrieval: HybridRetrievalService
    evidence_repository: RetrievalRepository

    async def context(self, *, run_id: str, thread_id: str, question: str) -> InsightRuntimeContext:
        """Resolve a fresh server-authorized Alpha PM context for one scenario."""
        access = await self.access.resolve_pm_context(
            self.pm_token,
            self.alpha_engagement_id,
            correlation_id=f"acceptance-{run_id}",
            required_permission="insight:run",
            thread_id=thread_id,
        )
        return InsightRuntimeContext(access=access, run_id=run_id, question=question)


@dataclass(frozen=True, slots=True)
class Scenario:
    """One approved question, bounded topic plan, and strict report builder."""

    acceptance_id: str
    question: str
    topics: tuple[dict[str, Any], ...]
    report_builder: Callable[
        [InsightRuntimeContext, dict[str, tuple[tuple[str, RetrievedItem], ...]], dict[str, str]],
        dict[str, Any],
    ]


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


async def _activate_stakeholder(
    access: AccessService,
    transcript_repository: TranscriptRepository,
    transcript_ingestion: TranscriptIngestionService,
    *,
    pm_token: str,
    engagement_id: str,
    display_name: str,
    role: str,
    department: str,
    statement: str,
) -> str:
    stakeholder = await access.create_stakeholder(
        pm_token,
        engagement_id,
        display_name=display_name,
        role=role,
        department=department,
        correlation_id=f"seed-{department.casefold().replace(' ', '-')}",
    )
    invitation = await access.issue_invitation(
        pm_token,
        engagement_id,
        stakeholder.stakeholder_id,
        correlation_id=f"invite-{stakeholder.stakeholder_id}",
    )
    activated = await access.activate_invitation(
        invitation.token.get_secret_value(),
        correlation_id=f"activate-{stakeholder.stakeholder_id}",
    )
    stakeholder_access = await access.resolve_stakeholder_context(
        activated.access_session.token.get_secret_value(),
        correlation_id=f"transcript-{stakeholder.stakeholder_id}",
        required_permission="source:read",
    )
    now = datetime.now(UTC)
    await transcript_repository.append_turn(
        stakeholder_access,
        speaker="stakeholder",
        original_text=statement,
        checkpoint_message_id=f"human-{stakeholder.stakeholder_id}",
        now=now,
    )
    await transcript_repository.append_turn(
        stakeholder_access,
        speaker="assistant",
        original_text="Which evidence or operating experience supports that statement?",
        checkpoint_message_id=f"assistant-{stakeholder.stakeholder_id}",
        now=now,
    )
    await transcript_repository.finalize(stakeholder_access, now=now)
    await transcript_ingestion.ingest(stakeholder_access)
    return stakeholder.stakeholder_id


async def _seed_harness(settings: Settings) -> AcceptanceHarness:
    settings = settings.model_copy(
        update={
            "gemini_embedding_dimension": 128,
            "max_retrieval_results": 20,
        }
    )
    database = DomainDatabase(settings.domain_database)
    access = AccessService(database, settings)
    await access.initialize()
    pm_session = await access.activate_pm(settings.pm_bootstrap_token.get_secret_value())
    pm_token = pm_session.token.get_secret_value()
    alpha = await access.create_engagement(
        pm_token,
        name="Alpha Canary engagement",
        description="Synthetic acceptance evidence with mixed source classes.",
        correlation_id="seed-alpha",
    )

    qdrant = AsyncQdrantClient(location=":memory:")
    stager = QdrantVectorStager(settings, client=qdrant)
    vectorizer = DeterministicVectorizer()
    document_ingestion = IngestionService(
        settings=settings,
        repository=IngestionRepository(
            database,
            lease_seconds=settings.ingestion_lease_seconds,
        ),
        validator=UploadValidator(settings),
        artifacts=IngestionArtifactStore(settings.originals_root, settings.derived_root),
        extractor=AcceptanceDocumentExtractor(ALPHA_DOCUMENT_TEXT | BETA_DOCUMENT_TEXT),
        vision=DeterministicVisionEnricher(),
        vectorizer=vectorizer,
        vector_stager=stager,
    )
    transcript_repository = TranscriptRepository(
        database,
        lease_seconds=settings.ingestion_lease_seconds,
    )
    transcript_ingestion = TranscriptIngestionService(
        settings=settings,
        repository=transcript_repository,
        vectorizer=vectorizer,
        vector_stager=stager,
    )
    await transcript_ingestion.initialize()

    alpha_pm = await access.resolve_pm_context(
        pm_token,
        alpha.engagement_id,
        correlation_id="seed-alpha-documents",
        required_permission="document:upload",
    )
    for filename in ALPHA_DOCUMENT_TEXT:
        await document_ingestion.ingest(
            alpha_pm,
            filename=filename,
            declared_media_type="image/png",
            content=(FIXTURES / filename).read_bytes(),
        )

    stakeholder_ids: dict[str, str] = {}
    for display_name, role, department, statement in ALPHA_INTERVIEWS:
        stakeholder_ids[department] = await _activate_stakeholder(
            access,
            transcript_repository,
            transcript_ingestion,
            pm_token=pm_token,
            engagement_id=alpha.engagement_id,
            display_name=display_name,
            role=role,
            department=department,
            statement=statement,
        )

    draft = await access.create_stakeholder(
        pm_token,
        alpha.engagement_id,
        display_name="Draft Interview",
        role="Unfinalized observer",
        department="Draft",
        correlation_id="seed-draft",
    )
    draft_invitation = await access.issue_invitation(
        pm_token,
        alpha.engagement_id,
        draft.stakeholder_id,
        correlation_id="seed-draft-invitation",
    )
    draft_activation = await access.activate_invitation(
        draft_invitation.token.get_secret_value(),
        correlation_id="seed-draft-activation",
    )
    draft_access = await access.resolve_stakeholder_context(
        draft_activation.access_session.token.get_secret_value(),
        correlation_id="seed-draft-turn",
    )
    await transcript_repository.append_turn(
        draft_access,
        speaker="stakeholder",
        original_text="ALPHA-DRAFT-EXCLUDED must remain unavailable to retrieval.",
        checkpoint_message_id="draft-human",
        now=datetime.now(UTC),
    )

    beta = await access.create_engagement(
        pm_token,
        name="Beta Canary engagement",
        description="Foreign canary used only to prove engagement isolation.",
        correlation_id="seed-beta",
    )
    beta_pm = await access.resolve_pm_context(
        pm_token,
        beta.engagement_id,
        correlation_id="seed-beta-document",
        required_permission="document:upload",
    )
    await document_ingestion.ingest(
        beta_pm,
        filename="beta-process-map.jpg",
        declared_media_type="image/jpeg",
        content=(FIXTURES / "beta-process-map.jpg").read_bytes(),
    )
    await _activate_stakeholder(
        access,
        transcript_repository,
        transcript_ingestion,
        pm_token=pm_token,
        engagement_id=beta.engagement_id,
        display_name="Blake Stone",
        role="Beta Legal Lead",
        department="Legal",
        statement=(
            "BETA-CANARY-COBALT. Beta release approval belongs to Legal and is foreign to Alpha."
        ),
    )

    evidence_repository = RetrievalRepository(database)
    retrieval = HybridRetrievalService(
        settings=settings,
        repository=evidence_repository,
        filter_extractor=StaticFilterExtractor(),
        vectorizer=vectorizer,
        search_backend=QdrantHybridSearcher(settings, client=qdrant),
        reranker=DeterministicReranker(),
    )
    beta_result = await retrieval.retrieve(beta_pm, "BETA-CANARY-COBALT")
    assert any("BETA-CANARY-COBALT" in item.original_excerpt for item in beta_result.items)
    await access.select_engagement(pm_token, alpha.engagement_id, correlation_id="select-alpha")
    return AcceptanceHarness(
        settings=settings,
        database=database,
        qdrant=qdrant,
        access=access,
        pm_token=pm_token,
        alpha_engagement_id=alpha.engagement_id,
        beta_engagement_id=beta.engagement_id,
        stakeholder_ids=stakeholder_ids,
        retrieval=retrieval,
        evidence_repository=evidence_repository,
    )


def _find(
    evidence: dict[str, tuple[tuple[str, RetrievedItem], ...]],
    topic_id: str,
    phrase: str,
) -> tuple[str, RetrievedItem]:
    return next(
        pair
        for pair in evidence[topic_id]
        if phrase.casefold() in pair[1].original_excerpt.casefold()
    )


def _citation(
    index: int,
    selected: tuple[str, RetrievedItem],
    claim_ids: Sequence[str],
) -> dict[str, Any]:
    evidence_id, item = selected
    return {
        "citation_id": f"citation-{index}",
        "evidence_id": evidence_id,
        "display_label": f"Evidence {index}",
        "source_location": item.candidate.location.model_dump(mode="json"),
        "claim_ids": list(claim_ids),
    }


def _report_base(
    context: InsightRuntimeContext,
    *,
    report_id: str,
    executive_summary: str,
    topics: Sequence[tuple[str, str, Sequence[str]]],
    evidence_ids: Sequence[str],
    findings: Sequence[dict[str, Any]],
    citations: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "report_id": report_id,
        "engagement_id": context.access.engagement_id,
        "question": context.question,
        "status": "complete",
        "executive_summary": executive_summary,
        "researched_topics": [
            {
                "topic_id": topic_id,
                "title": title,
                "status": "completed",
                "summary": (
                    "Current authorized document and finalized-interview evidence was reviewed."
                ),
                "evidence_ids": list(topic_evidence),
            }
            for topic_id, title, topic_evidence in topics
        ],
        "findings": list(findings),
        "responsibilities": [],
        "operational_risks": [],
        "buy_in_signals": [],
        "contradictions": [],
        "evidence_gaps": [],
        "open_questions": [],
        "follow_up_recommendations": [],
        "evidence_ids": list(evidence_ids),
        "citations": list(citations),
        "run_metadata": {
            "run_id": context.run_id,
            "started_at": "2026-07-15T00:00:00Z",
            "completed_at": "2026-07-15T00:00:03Z",
            "primary_model_id": "gemini-test-primary",
            "fallback_model_id": "gemini-test-fallback",
            "topic_count": 2,
            "status_detail": "The bounded two-topic acceptance workflow completed.",
        },
    }


def _ac01_report(
    context: InsightRuntimeContext,
    evidence: dict[str, tuple[tuple[str, RetrievedItem], ...]],
    stakeholder_ids: dict[str, str],
) -> dict[str, Any]:
    document = _find(evidence, "topic-risk", "responsibility matrix")
    operations = _find(evidence, "topic-risk", "manual handoff creates rework")
    finance = _find(evidence, "topic-ownership", "finance owns final approval")
    people = _find(evidence, "topic-ownership", "coordinate training")
    selected = (document, operations, finance, people)
    report = _report_base(
        context,
        report_id="report-ac-01",
        executive_summary=(
            "Manual handoffs and unclear escalation ownership are the largest supported risks. "
            "Operations owns intake, Finance owns final approval, and training coordination is "
            "conditional on a named content owner."
        ),
        topics=(
            ("topic-risk", "Operational Risks", (document[0], operations[0])),
            ("topic-ownership", "Responsibility and Gaps", (finance[0], people[0])),
        ),
        evidence_ids=tuple(item[0] for item in selected),
        findings=(
            {
                "claim_id": "claim-ac01-handoff",
                "statement": "Manual handoffs create rework and missed deadlines.",
                "evidence_ids": [document[0], operations[0]],
            },
            {
                "claim_id": "claim-ac01-ownership",
                "statement": "Intake and final approval have separate explicit owners.",
                "evidence_ids": [document[0], operations[0], finance[0]],
            },
            {
                "claim_id": "claim-ac01-training",
                "statement": "Training coordination depends on a named content owner.",
                "evidence_ids": [people[0]],
            },
        ),
        citations=(
            _citation(1, document, ("claim-ac01-handoff", "claim-ac01-ownership")),
            _citation(2, operations, ("claim-ac01-handoff", "claim-ac01-ownership")),
            _citation(3, finance, ("claim-ac01-ownership",)),
            _citation(4, people, ("claim-ac01-training",)),
        ),
    )
    report["responsibilities"] = [
        {
            "claim_id": "claim-ac01-operations-owner",
            "responsibility": "Own the intake process.",
            "attribution": "Operations Lead in the Operations department.",
            "uncertainty": "Explicit for intake only; escalation ownership remains undefined.",
            "evidence_ids": [document[0], operations[0]],
        },
        {
            "claim_id": "claim-ac01-finance-owner",
            "responsibility": "Own final approval.",
            "attribution": "Finance Partner in the Finance department.",
            "uncertainty": "Explicit for final approval, not for resolving incomplete submissions.",
            "evidence_ids": [document[0], finance[0]],
        },
    ]
    report["operational_risks"] = [
        {
            "claim_id": "claim-ac01-risk",
            "risk": "Manual handoffs create rework and inconsistent cycle-time expectations.",
            "impact": "Missed deadlines and disputed process performance.",
            "responsibility_context": "Operations owns intake and Finance owns final approval.",
            "uncertainty": "The escalation owner is not established by current evidence.",
            "evidence_ids": [document[0], operations[0], finance[0]],
        }
    ]
    report["evidence_gaps"] = [
        {
            "topic": "Escalation ownership",
            "description": "No authorized source names the escalation owner.",
            "impact": "The report cannot assign that responsibility without unsupported inference.",
        }
    ]
    report["open_questions"] = ["Who owns escalation when intake or approval stalls?"]
    report["buy_in_signals"] = [
        {
            "topic": "Operational ownership",
            "stakeholder_id": stakeholder_ids["People"],
            "role": "Learning Manager",
            "department": "People",
            "category": "conditional_support",
            "explanation": "Training coordination is conditional on Operations naming an owner.",
            "evidence_ids": [people[0]],
        }
    ]
    claim_map = {
        document[0]: ("claim-ac01-operations-owner", "claim-ac01-finance-owner", "claim-ac01-risk"),
        operations[0]: ("claim-ac01-operations-owner", "claim-ac01-risk"),
        finance[0]: ("claim-ac01-finance-owner", "claim-ac01-risk"),
    }
    for citation in report["citations"]:
        citation["claim_ids"].extend(claim_map.get(citation["evidence_id"], ()))
    return report


def _ac02_report(
    context: InsightRuntimeContext,
    evidence: dict[str, tuple[tuple[str, RetrievedItem], ...]],
    stakeholder_ids: dict[str, str],
) -> dict[str, Any]:
    document = _find(evidence, "topic-support", "limited-pilot charter")
    finance = _find(evidence, "topic-support", "confirms support")
    sales = _find(evidence, "topic-support", "conditionally supports")
    operations = _find(evidence, "topic-gaps", "concerned that the pilot")
    people = _find(evidence, "topic-gaps", "approval automation was not discussed")
    selected = (document, finance, sales, operations, people)
    report = _report_base(
        context,
        report_id="report-ac-02",
        executive_summary=(
            "Finance confirms support, Sales support is conditional, and Operations expresses a "
            "training-ownership concern. Approval automation was not discussed by People, while "
            "Procurement readiness lacks evidence."
        ),
        topics=(
            ("topic-support", "Topic-specific Buy-in", tuple(item[0] for item in selected[:3])),
            ("topic-gaps", "Buy-in Evidence Gaps", tuple(item[0] for item in selected[3:])),
        ),
        evidence_ids=tuple(item[0] for item in selected),
        findings=(
            {
                "claim_id": "claim-ac02-landscape",
                "statement": "Pilot support varies by role and stated condition.",
                "evidence_ids": [document[0], finance[0], sales[0], operations[0], people[0]],
            },
        ),
        citations=tuple(
            _citation(index, item, ("claim-ac02-landscape",))
            for index, item in enumerate(selected, start=1)
        ),
    )
    report["buy_in_signals"] = [
        {
            "topic": "Limited pilot",
            "stakeholder_id": stakeholder_ids["Finance"],
            "role": "Finance Partner",
            "department": "Finance",
            "category": "confirmed_support",
            "explanation": "Finance explicitly confirms support for a limited pilot.",
            "evidence_ids": [finance[0]],
        },
        {
            "topic": "Limited pilot",
            "stakeholder_id": stakeholder_ids["Sales"],
            "role": "Sales Director",
            "department": "Sales",
            "category": "conditional_support",
            "explanation": "Sales support depends on exception visibility and no duplicate entry.",
            "evidence_ids": [document[0], sales[0]],
        },
        {
            "topic": "Pilot training",
            "stakeholder_id": stakeholder_ids["Operations"],
            "role": "Operations Lead",
            "department": "Operations",
            "category": "expressed_concern",
            "explanation": "Operations explicitly raises the absence of named training owners.",
            "evidence_ids": [operations[0]],
        },
        {
            "topic": "Approval automation",
            "stakeholder_id": stakeholder_ids["People"],
            "role": "Learning Manager",
            "department": "People",
            "category": "topic_not_discussed",
            "explanation": (
                "The finalized People interview explicitly records that topic as not discussed."
            ),
            "evidence_ids": [people[0]],
        },
        {
            "topic": "Procurement readiness",
            "stakeholder_id": None,
            "role": None,
            "department": None,
            "category": "insufficient_evidence",
            "explanation": "No current authorized source establishes Procurement's position.",
            "evidence_ids": [],
        },
    ]
    report["evidence_gaps"] = [
        {
            "topic": "Procurement readiness",
            "description": "No finalized Procurement interview or approved source is available.",
            "impact": "Procurement buy-in cannot be characterized responsibly.",
        }
    ]
    report["open_questions"] = ["What is Procurement's evidence-based position on the pilot?"]
    return report


def _ac03_report(
    context: InsightRuntimeContext,
    evidence: dict[str, tuple[tuple[str, RetrievedItem], ...]],
    stakeholder_ids: dict[str, str],
) -> dict[str, Any]:
    operations = _find(evidence, "topic-conflict", "two business days")
    finance = _find(evidence, "topic-conflict", "seven to ten business days")
    document = _find(evidence, "topic-compatible", "preserves customer exception visibility")
    sales = _find(evidence, "topic-compatible", "compatible with the pilot charter")
    selected = (operations, finance, document, sales)
    report = _report_base(
        context,
        report_id="report-ac-03",
        executive_summary=(
            "Operations and Finance give materially conflicting approval-cycle durations. Sales "
            "and the pilot charter both require exception visibility, so those compatible "
            "statements are not reported as a contradiction."
        ),
        topics=(
            ("topic-conflict", "Cross-department Conflict", (operations[0], finance[0])),
            ("topic-compatible", "Compatible Statement Control", (document[0], sales[0])),
        ),
        evidence_ids=tuple(item[0] for item in selected),
        findings=(
            {
                "claim_id": "claim-ac03-cycle",
                "statement": "Operations and Finance report materially different cycle times.",
                "evidence_ids": [operations[0], finance[0]],
            },
            {
                "claim_id": "claim-ac03-compatible",
                "statement": "Sales and the pilot charter align on exception visibility.",
                "evidence_ids": [document[0], sales[0]],
            },
        ),
        citations=(
            _citation(1, operations, ("claim-ac03-cycle",)),
            _citation(2, finance, ("claim-ac03-cycle",)),
            _citation(3, document, ("claim-ac03-compatible",)),
            _citation(4, sales, ("claim-ac03-compatible",)),
        ),
    )
    report["contradictions"] = [
        {
            "topic": "Approval cycle duration",
            "side_a": {
                "statement": "The approval cycle normally takes two business days.",
                "stakeholder_id": stakeholder_ids["Operations"],
                "role": "Operations Lead",
                "department": "Operations",
                "evidence_ids": [operations[0]],
            },
            "side_b": {
                "statement": "The approval cycle normally takes seven to ten business days.",
                "stakeholder_id": stakeholder_ids["Finance"],
                "role": "Finance Partner",
                "department": "Finance",
                "evidence_ids": [finance[0]],
            },
            "interpretation": (
                "The accounts conflict on normal duration; differing submission completeness may "
                "explain the gap, but current evidence does not resolve it."
            ),
            "evidence_ids": [operations[0], finance[0]],
        }
    ]
    report["open_questions"] = [
        "Which measured start and end points should define approval-cycle duration?"
    ]
    report["buy_in_signals"] = [
        {
            "topic": "Exception visibility",
            "stakeholder_id": stakeholder_ids["Sales"],
            "role": "Sales Director",
            "department": "Sales",
            "category": "conditional_support",
            "explanation": (
                "Sales support is conditional on a control already present in the charter."
            ),
            "evidence_ids": [document[0], sales[0]],
        }
    ]
    return report


SCENARIOS = (
    Scenario(
        acceptance_id="AC-01",
        question="What are the biggest operational risks, and who is responsible?",
        topics=(
            {
                "topic_id": "topic-risk",
                "title": "Operational Risks",
                "objective": "Identify supported operational risks.",
                "questions": ["Which current evidence establishes material operating risks?"],
                "required_source_types": ["document", "interview"],
                "dependencies": [],
                "priority": 1,
            },
            {
                "topic_id": "topic-ownership",
                "title": "Responsibility and Gaps",
                "objective": "Separate explicit responsibility from unsupported inference.",
                "questions": ["Which responsibilities and ownership gaps are supported?"],
                "required_source_types": ["document", "interview"],
                "dependencies": [],
                "priority": 1,
            },
        ),
        report_builder=_ac01_report,
    ),
    Scenario(
        acceptance_id="AC-02",
        question="Which roles have buy-in, and where are the gaps?",
        topics=(
            {
                "topic_id": "topic-support",
                "title": "Topic-specific Buy-in",
                "objective": "Classify only qualitative evidence-grounded support or concern.",
                "questions": ["Which roles express support, conditions, or concern by topic?"],
                "required_source_types": ["document", "interview"],
                "dependencies": [],
                "priority": 1,
            },
            {
                "topic_id": "topic-gaps",
                "title": "Buy-in Evidence Gaps",
                "objective": "Distinguish missing evidence from a topic not discussed.",
                "questions": ["Where is the evidence absent or silent?"],
                "required_source_types": ["document", "interview"],
                "dependencies": [],
                "priority": 1,
            },
        ),
        report_builder=_ac02_report,
    ),
    Scenario(
        acceptance_id="AC-03",
        question="Where do statements from different departments conflict?",
        topics=(
            {
                "topic_id": "topic-conflict",
                "title": "Cross-department Conflict",
                "objective": "Identify only contradictions with two independently supported sides.",
                "questions": ["Which departmental statements materially conflict?"],
                "required_source_types": ["document", "interview"],
                "dependencies": [],
                "priority": 1,
            },
            {
                "topic_id": "topic-compatible",
                "title": "Compatible Statement Control",
                "objective": "Reject different but compatible statements as contradictions.",
                "questions": ["Which compared statements remain compatible?"],
                "required_source_types": ["document", "interview"],
                "dependencies": [],
                "priority": 1,
            },
        ),
        report_builder=_ac03_report,
    ),
)


async def _run_scenario(
    harness: AcceptanceHarness,
    scenario: Scenario,
) -> dict[str, Any]:
    suffix = scenario.acceptance_id.casefold().replace("-", "")
    context = await harness.context(
        run_id=f"run-{suffix}",
        thread_id=f"thread-{suffix}",
        question=scenario.question,
    )
    preflight = await harness.retrieval.retrieve(context.access, scenario.question)
    preflight_text = "\n".join(item.original_excerpt for item in preflight.items)
    assert "BETA-CANARY-COBALT" not in preflight_text
    assert "ALPHA-DRAFT-EXCLUDED" not in preflight_text
    assert {item.candidate.metadata.engagement_id for item in preflight.items} == {
        harness.alpha_engagement_id
    }
    assert {item.candidate.metadata.source_type for item in preflight.items} >= {
        "engagement_document",
        "interview",
    }
    assert (
        len(
            {
                item.candidate.metadata.stakeholder_id
                for item in preflight.items
                if item.candidate.metadata.source_type == "interview"
            }
        )
        >= 2
    )

    evidence = {
        str(topic["topic_id"]): tuple(
            (_evidence_id(context.run_id, str(topic["topic_id"]), item), item)
            for item in preflight.items
        )
        for topic in scenario.topics
    }
    report_payload = scenario.report_builder(context, evidence, harness.stakeholder_ids)
    primary_responses: list[BaseMessage] = [
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
    ]
    completed_titles: set[str] = set()
    for topic in scenario.topics:
        completed_titles.add(str(topic["title"]))
        all_research_complete = len(completed_titles) == len(scenario.topics)
        primary_responses.extend(
            [
                _tool_call(
                    "task",
                    {
                        "description": (
                            f"topic_id={topic['topic_id']} Research only {topic['title']} and save "
                            "the required artifacts."
                        ),
                        "subagent_type": "topic-researcher",
                    },
                    f"{suffix}-{topic['topic_id']}",
                ),
                _tool_call(
                    "write_todos",
                    {
                        "todos": [
                            *[
                                {
                                    "content": f"Research {candidate['title']}",
                                    "status": (
                                        "completed"
                                        if str(candidate["title"]) in completed_titles
                                        else "in_progress"
                                    ),
                                }
                                for candidate in scenario.topics
                            ],
                            {
                                "content": "Edit the structured report",
                                "status": ("in_progress" if all_research_complete else "pending"),
                            },
                            {"content": "Validate the report", "status": "pending"},
                        ]
                    },
                    f"{suffix}-{topic['topic_id']}-todo-complete",
                ),
            ]
        )
    primary_responses.extend(
        [
            _tool_call(
                "task",
                {
                    "description": "Load every completed topic artifact and create the report.",
                    "subagent_type": "report-editor",
                },
                f"{suffix}-editor",
            ),
            _tool_call(
                "write_todos",
                {
                    "todos": [
                        *[
                            {"content": f"Research {topic['title']}", "status": "completed"}
                            for topic in scenario.topics
                        ],
                        {"content": "Edit the structured report", "status": "completed"},
                        {"content": "Validate the report", "status": "completed"},
                    ]
                },
                f"{suffix}-todos-complete",
            ),
            AIMessage(content="The validated report artifact is available."),
        ]
    )
    researcher_responses: list[BaseMessage] = []
    for topic in scenario.topics:
        topic_id = str(topic["topic_id"])
        researcher_responses.extend(
            [
                _tool_call(
                    "scoped_retrieve",
                    {"topic_id": topic_id, "query": scenario.question},
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

    primary = ToolCallingFakeModel(responses=primary_responses)
    researcher = ToolCallingFakeModel(responses=researcher_responses)
    editor = ToolCallingFakeModel(
        responses=[
            _tool_call("load_research_package", {}, f"{suffix}-load"),
            _tool_call(
                "save_final_report",
                {"report": report_payload},
                f"{suffix}-save-report",
            ),
            AIMessage(content="The strict report was validated and saved."),
        ]
    )
    run_repository = InsightRunRepository(harness.database)
    async with open_sqlite_checkpointer(harness.settings.checkpoint_database) as saver:
        graph = build_insight_graph(
            harness.settings,
            dependencies=InsightGraphDependencies(
                primary_model=primary,
                fallback_model=ToolCallingFakeModel(
                    responses=[AIMessage(content="Fallback response.")]
                ),
                researcher_model=researcher,
                editor_model=editor,
                checkpointer=saver,
                harness_provider="toolcallingfakemodel",
                retrieval_service=harness.retrieval,
                evidence_repository=harness.evidence_repository,
                run_repository=run_repository,
            ),
        )
        execution = InsightExecutionService(
            graph=graph,
            repository=run_repository,
            artifacts=ScopedArtifactStore(harness.settings.agent_artifacts_root),
            settings=harness.settings,
        )
        result = await execution.execute(context)

    assert result.run.status == "complete"
    assert result.report.status == "complete"
    assert result.metrics.status == "complete"
    assert result.metrics.topic_count == len(scenario.topics)
    assert result.metrics.researcher_calls == len(scenario.topics)
    assert (
        1
        <= result.metrics.max_concurrent_researchers
        <= (harness.settings.max_parallel_researchers)
    )
    assert result.metrics.model_calls == (
        primary.call_count + researcher.call_count + editor.call_count
    )
    assert result.metrics.retrieval_calls == len(scenario.topics)
    assert result.metrics.max_rerank_candidates_per_call <= (harness.settings.max_rerank_candidates)
    assert set(result.report.evidence_ids) <= set(result.metrics.evidence_ids)
    assert result.report.question == scenario.question
    assert primary.call_count == (2 * len(scenario.topics)) + 5
    assert researcher.call_count == 8
    assert editor.call_count == 3
    actions = [str(event["action"]) for event in result.events]
    assert actions.index("research_plan_saved") < actions.index("scoped_retrieval_completed")
    assert actions.index("research_artifacts_saved") < actions.index("research_package_loaded")
    assert actions.index("research_package_loaded") < actions.index("report_validated")
    assert not any(
        "reasoning" in key or "prompt" in key for event in result.events for key in event
    )

    source_types: set[str] = set()
    interview_stakeholders: set[str] = set()
    for citation in result.report.citations:
        record = await harness.evidence_repository.load_evidence(
            context.access,
            citation.evidence_id,
            now=datetime.now(UTC),
        )
        assert record.location == citation.source_location
        assert record.engagement_id == harness.alpha_engagement_id
        assert "BETA-CANARY-COBALT" not in record.original_excerpt
        source_types.add(record.source_type)
        if record.source_type == "interview" and record.stakeholder_id is not None:
            interview_stakeholders.add(record.stakeholder_id)
    assert "engagement_document" in source_types
    assert len(interview_stakeholders) >= 2

    serialized = json.dumps(result.report.model_dump(mode="json"), sort_keys=True)
    assert "BETA-CANARY-COBALT" not in serialized
    assert "ALPHA-DRAFT-EXCLUDED" not in serialized
    if scenario.acceptance_id == "AC-01":
        assert result.report.operational_risks
        assert result.report.responsibilities
        assert any("escalation owner" in gap.description for gap in result.report.evidence_gaps)
    elif scenario.acceptance_id == "AC-02":
        assert {signal.category for signal in result.report.buy_in_signals} == {
            "confirmed_support",
            "conditional_support",
            "expressed_concern",
            "insufficient_evidence",
            "topic_not_discussed",
        }
        prohibited = {"score", "rank", "grade", "leaderboard"}
        assert not any(word in serialized.casefold() for word in prohibited)
    else:
        assert len(result.report.contradictions) == 1
        contradiction = result.report.contradictions[0]
        assert contradiction.side_a.department == "Operations"
        assert contradiction.side_b.department == "Finance"
        assert set(contradiction.side_a.evidence_ids).isdisjoint(contradiction.side_b.evidence_ids)
        assert "exception visibility" not in contradiction.topic.casefold()

    scope_root = (
        harness.settings.agent_artifacts_root / harness.alpha_engagement_id / f"thread-{suffix}"
    )
    assert (scope_root / "research_plan.md").is_file()
    assert all(
        (scope_root / "research" / str(topic["topic_id"]) / "sources.json").is_file()
        for topic in scenario.topics
    )
    return {
        "acceptance_id": scenario.acceptance_id,
        "question": scenario.question,
        "status": result.report.status,
        "report": result.report.model_dump(mode="json"),
        "source_types": sorted(source_types),
        "finalized_interview_count": len(interview_stakeholders),
        "event_actions": actions,
        "server_metrics": result.metrics.model_dump(mode="json"),
        "beta_canary_absent": True,
        "draft_canary_absent": True,
    }


async def test_ac_01_through_ac_03_use_real_deep_agent_and_isolated_mixed_evidence(
    settings: Settings,
) -> None:
    harness = await _seed_harness(settings)
    try:
        results = [await _run_scenario(harness, scenario) for scenario in SCENARIOS]
    finally:
        await harness.qdrant.close()

    assert [result["acceptance_id"] for result in results] == ["AC-01", "AC-02", "AC-03"]
    assert all(result["status"] == "complete" for result in results)
