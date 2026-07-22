"""Guard canonical field names against silent implementation drift."""

from pydantic import BaseModel

from stakeholder_intelligence_agent.contracts import (
    AccessContext,
    Citation,
    DocumentSource,
    DocumentVersion,
    DocxRenderedPageLocation,
    Engagement,
    EvidenceRecord,
    ImageRegionLocation,
    InsightExecutionEvent,
    InsightExecutionMetrics,
    InsightReport,
    InsightRun,
    InterviewSession,
    InvitationToken,
    OperationalAuditEvent,
    PdfPageLocation,
    PMAccess,
    PptxSlideLocation,
    ResearchPlan,
    ResearchTopic,
    RetrievalCandidate,
    RetrievalFilter,
    SafeProgressEvent,
    SearchChunk,
    SourceElement,
    Stakeholder,
    Transcript,
    TranscriptIngestionVersion,
    TranscriptTurn,
    TranscriptTurnsLocation,
    XlsxRangeLocation,
)
from stakeholder_intelligence_agent.contracts.insight import BuyInSignal, Contradiction

CANONICAL_FIELDS: dict[type[BaseModel], frozenset[str]] = {
    AccessContext: frozenset(
        {
            "principal_type",
            "principal_id",
            "engagement_id",
            "stakeholder_id",
            "interview_session_id",
            "thread_id",
            "permissions",
            "issued_at",
            "expires_at",
            "correlation_id",
        }
    ),
    Engagement: frozenset(
        {"engagement_id", "name", "description", "status", "created_at", "updated_at"}
    ),
    PMAccess: frozenset({"pm_access_id", "token_hash", "status", "created_at", "revoked_at"}),
    Stakeholder: frozenset(
        {
            "stakeholder_id",
            "engagement_id",
            "display_name",
            "role",
            "department",
            "status",
            "created_at",
            "updated_at",
        }
    ),
    InvitationToken: frozenset(
        {
            "invitation_id",
            "engagement_id",
            "stakeholder_id",
            "token_hash",
            "status",
            "created_at",
            "expires_at",
            "activated_at",
            "revoked_at",
            "created_by_pm_access_id",
        }
    ),
    InterviewSession: frozenset(
        {
            "interview_session_id",
            "engagement_id",
            "stakeholder_id",
            "invitation_id",
            "thread_id",
            "status",
            "started_at",
            "finalized_at",
            "transcript_id",
            "ingestion_version_id",
            "failure_code",
            "failure_message",
        }
    ),
    Transcript: frozenset(
        {
            "transcript_id",
            "interview_session_id",
            "engagement_id",
            "stakeholder_id",
            "role",
            "department",
            "status",
            "language_observations",
            "finalized_at",
            "content_hash",
        }
    ),
    TranscriptTurn: frozenset(
        {"turn_index", "speaker", "original_text", "created_at", "checkpoint_message_id"}
    ),
    TranscriptIngestionVersion: frozenset(
        {
            "transcript_ingestion_version_id",
            "transcript_id",
            "content_hash",
            "state",
            "is_active",
            "created_at",
            "ready_at",
            "failure_code",
            "failure_message",
        }
    ),
    DocumentSource: frozenset(
        {
            "document_id",
            "engagement_id",
            "stakeholder_id",
            "role",
            "department",
            "doc_type",
            "source_type",
            "original_filename",
            "media_type",
            "created_at",
        }
    ),
    PdfPageLocation: frozenset({"kind", "filename", "page", "bounding_box"}),
    DocxRenderedPageLocation: frozenset(
        {
            "kind",
            "filename",
            "rendered_page",
            "section",
            "paragraph",
            "bounding_box",
        }
    ),
    PptxSlideLocation: frozenset({"kind", "filename", "slide", "shape_identifier", "bounding_box"}),
    XlsxRangeLocation: frozenset(
        {
            "kind",
            "filename",
            "sheet",
            "cell_range",
            "chart_identifier",
            "image_identifier",
        }
    ),
    ImageRegionLocation: frozenset({"kind", "filename", "image_index", "region", "bounding_box"}),
    TranscriptTurnsLocation: frozenset(
        {"kind", "stakeholder_id", "transcript_id", "turn_start", "turn_end"}
    ),
    DocumentVersion: frozenset(
        {
            "document_version_id",
            "document_id",
            "version_number",
            "content_hash",
            "state",
            "is_active",
            "original_artifact_id",
            "ingestion_key",
            "created_at",
            "ready_at",
            "superseded_at",
            "failure_code",
            "failure_message",
        }
    ),
    SourceElement: frozenset(
        {
            "element_id",
            "document_version_id",
            "element_type",
            "original_content",
            "english_interpretation",
            "location",
            "parent_element_id",
            "artifact_id",
            "content_hash",
            "extraction_method",
        }
    ),
    SearchChunk: frozenset(
        {
            "chunk_id",
            "engagement_id",
            "source_id",
            "source_version_id",
            "element_ids",
            "text_for_retrieval",
            "location",
            "stakeholder_id",
            "role",
            "department",
            "doc_type",
            "source_type",
            "dense_vector",
            "sparse_vector",
            "is_active_ready",
        }
    ),
    RetrievalFilter: frozenset(
        {
            "engagement_id",
            "active_ready_only",
            "stakeholder_id",
            "role",
            "department",
            "doc_type",
            "source_type",
        }
    ),
    RetrievalCandidate: frozenset(
        {
            "chunk_id",
            "hybrid_rank",
            "rrf_score",
            "reranker_score",
            "final_rank",
            "source_preview",
            "location",
            "metadata",
        }
    ),
    ResearchPlan: frozenset(
        {
            "plan_id",
            "run_id",
            "engagement_id",
            "question",
            "topics",
            "source_strategy",
            "completion_criteria",
            "created_at",
        }
    ),
    ResearchTopic: frozenset(
        {
            "topic_id",
            "title",
            "objective",
            "questions",
            "required_source_types",
            "dependencies",
            "priority",
        }
    ),
    EvidenceRecord: frozenset(
        {
            "evidence_id",
            "run_id",
            "engagement_id",
            "topic_id",
            "source_id",
            "source_version_id",
            "source_type",
            "stakeholder_id",
            "location",
            "original_excerpt",
            "english_interpretation",
            "content_hash",
            "researcher_id",
            "created_at",
        }
    ),
    Citation: frozenset(
        {"citation_id", "evidence_id", "display_label", "source_location", "claim_ids"}
    ),
    InsightReport: frozenset(
        {
            "report_id",
            "engagement_id",
            "question",
            "status",
            "executive_summary",
            "researched_topics",
            "findings",
            "responsibilities",
            "operational_risks",
            "buy_in_signals",
            "contradictions",
            "evidence_gaps",
            "open_questions",
            "follow_up_recommendations",
            "evidence_ids",
            "citations",
            "run_metadata",
        }
    ),
    BuyInSignal: frozenset(
        {
            "topic",
            "stakeholder_id",
            "role",
            "department",
            "category",
            "explanation",
            "evidence_ids",
        }
    ),
    Contradiction: frozenset({"topic", "side_a", "side_b", "interpretation", "evidence_ids"}),
    InsightRun: frozenset(
        {
            "run_id",
            "engagement_id",
            "thread_id",
            "status",
            "requested_question",
            "plan_id",
            "report_id",
            "failure_code",
            "failure_message",
            "started_at",
            "completed_at",
        }
    ),
    InsightExecutionEvent: frozenset(
        {
            "event_id",
            "occurred_at",
            "run_id",
            "engagement_id",
            "thread_id",
            "actor",
            "operation_type",
            "tool_name",
            "status",
            "duration_ms",
            "source_ids",
            "evidence_ids",
            "retry_count",
            "failure_code",
            "correlation_id",
        }
    ),
    InsightExecutionMetrics: frozenset(
        {
            "run_id",
            "engagement_id",
            "thread_id",
            "started_at",
            "completed_at",
            "status",
            "duration_ms",
            "topic_count",
            "researcher_calls",
            "max_concurrent_researchers",
            "model_calls",
            "model_failures",
            "tool_calls",
            "tool_failures",
            "retrieval_calls",
            "retry_count",
            "timeout_count",
            "rerank_candidates_total",
            "max_rerank_candidates_per_call",
            "retrieval_latency_ms",
            "reranker_latency_ms",
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "configured_topic_limit",
            "configured_parallel_researcher_limit",
            "configured_model_call_limit",
            "configured_tool_call_limit",
            "configured_retrieval_calls_per_researcher_limit",
            "configured_rerank_candidate_limit",
            "configured_provider_timeout_seconds",
            "configured_run_timeout_seconds",
            "source_ids",
            "evidence_ids",
            "tool_names",
            "failure_code",
            "correlation_id",
        }
    ),
    OperationalAuditEvent: frozenset(
        {
            "event_id",
            "occurred_at",
            "run_id",
            "engagement_id",
            "thread_id",
            "actor",
            "action",
            "status",
            "duration_ms",
            "source_ids",
            "evidence_ids",
            "retry_count",
            "failure_code",
            "correlation_id",
        }
    ),
    SafeProgressEvent: frozenset(
        {
            "event_id",
            "occurred_at",
            "engagement_id",
            "run_id",
            "thread_id",
            "stage",
            "status",
            "todo_id",
            "todo_status",
            "subagent",
            "tool_name",
            "artifact_name",
            "source_ids",
            "evidence_ids",
            "duration_ms",
            "retry_count",
            "failure_code",
            "failure_message",
            "correlation_id",
        }
    ),
}


def test_canonical_field_names_are_exact() -> None:
    for contract, expected_fields in CANONICAL_FIELDS.items():
        assert frozenset(contract.model_fields) == expected_fields, contract.__name__
