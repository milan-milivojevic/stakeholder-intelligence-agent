import type {
  DocumentSummary,
  DocumentVersion,
  Engagement,
  IngestionState,
  InsightRun,
  InsightRunStatus,
  InterviewSession,
  InvitationSummary,
  Stakeholder,
} from "../api/contracts";
import {
  asRecord,
  ContractError,
  nullableString,
  requireExactKeys,
  requireIsoTimestamp,
  requiredArray,
  requiredBoolean,
  requiredInteger,
  requiredNumber,
  requiredString,
  requiredStringArray,
} from "../api/validation";
import type {
  BoundingBox,
  BuyInCategory,
  BuyInSignal,
  Citation,
  Contradiction,
  ContradictionSide,
  DocumentElementPreview,
  DocumentLifecycleEvent,
  DocumentListResponse,
  DocumentProcessingCount,
  DocumentProcessingDetailsResponse,
  EngagementContextResponse,
  EngagementListResponse,
  EvidenceDrillDownResponse,
  EvidenceGap,
  EvidenceRecord,
  FollowUpRecommendation,
  FinalizedTranscript,
  InsightExecutionMetrics,
  InsightReport,
  InsightReportResponse,
  InsightRunListResponse,
  InsightStatusResponse,
  InterviewSessionListResponse,
  InterviewPreviewResponse,
  InvitationIssuedResponse,
  InvitationListResponse,
  OperationalRisk,
  ReportClaim,
  ResearchedTopicOutcome,
  ResponsibilityFinding,
  RunMetadata,
  SourceArtifactSummary,
  SourceLocation,
  StakeholderListResponse,
  StakeholderResponse,
  UploadResponse,
} from "./contracts";

type Parser<T> = (value: unknown) => T;

function oneOf<T extends string>(value: unknown, allowed: readonly T[]): T {
  if (typeof value !== "string" || !allowed.includes(value as T)) {
    throw new ContractError();
  }
  return value as T;
}

function records<T>(record: Record<string, unknown>, key: string, parser: Parser<T>): T[] {
  return requiredArray(record, key).map((item) => parser(item));
}

function nullableInteger(record: Record<string, unknown>, key: string): number | null {
  if (record[key] === null) {
    return null;
  }
  return requiredInteger(record, key);
}

function nonNegativeInteger(record: Record<string, unknown>, key: string): number {
  const value = requiredInteger(record, key);
  if (value < 0) {
    throw new ContractError();
  }
  return value;
}

function timestamp(record: Record<string, unknown>, key: string): string {
  return requireIsoTimestamp(requiredString(record, key));
}

export function parseEngagement(value: unknown): Engagement {
  const record = asRecord(value);
  requireExactKeys(record, [
    "engagement_id",
    "name",
    "description",
    "status",
    "created_at",
    "updated_at",
  ]);
  return {
    engagement_id: requiredString(record, "engagement_id"),
    name: requiredString(record, "name"),
    description: nullableString(record, "description"),
    status: oneOf(record.status, ["active", "archived"]),
    created_at: timestamp(record, "created_at"),
    updated_at: timestamp(record, "updated_at"),
  };
}

export function parseStakeholder(value: unknown): Stakeholder {
  const record = asRecord(value);
  requireExactKeys(record, [
    "stakeholder_id",
    "engagement_id",
    "display_name",
    "role",
    "department",
    "status",
    "created_at",
    "updated_at",
  ]);
  return {
    stakeholder_id: requiredString(record, "stakeholder_id"),
    engagement_id: requiredString(record, "engagement_id"),
    display_name: requiredString(record, "display_name"),
    role: nullableString(record, "role"),
    department: nullableString(record, "department"),
    status: oneOf(record.status, ["active", "revoked"]),
    created_at: timestamp(record, "created_at"),
    updated_at: timestamp(record, "updated_at"),
  };
}

function parseInvitation(value: unknown): InvitationSummary {
  const record = asRecord(value);
  requireExactKeys(record, [
    "invitation_id",
    "engagement_id",
    "stakeholder_id",
    "status",
    "created_at",
    "expires_at",
    "activated_at",
    "revoked_at",
  ]);
  const activatedAt = nullableString(record, "activated_at");
  const revokedAt = nullableString(record, "revoked_at");
  return {
    invitation_id: requiredString(record, "invitation_id"),
    engagement_id: requiredString(record, "engagement_id"),
    stakeholder_id: requiredString(record, "stakeholder_id"),
    status: oneOf(record.status, ["active", "activated", "expired", "revoked"]),
    created_at: timestamp(record, "created_at"),
    expires_at: timestamp(record, "expires_at"),
    activated_at: activatedAt === null ? null : requireIsoTimestamp(activatedAt),
    revoked_at: revokedAt === null ? null : requireIsoTimestamp(revokedAt),
  };
}

const ingestionStates: readonly IngestionState[] = [
  "RECEIVED",
  "VALIDATING",
  "EXTRACTING",
  "ENRICHING",
  "INDEXING",
  "READY",
  "FAILED",
  "SUPERSEDED",
];

function parseDocumentVersion(value: unknown): DocumentVersion {
  const record = asRecord(value);
  requireExactKeys(record, [
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
  ]);
  const readyAt = nullableString(record, "ready_at");
  const supersededAt = nullableString(record, "superseded_at");
  return {
    document_version_id: requiredString(record, "document_version_id"),
    document_id: requiredString(record, "document_id"),
    version_number: requiredInteger(record, "version_number"),
    content_hash: requiredString(record, "content_hash"),
    state: oneOf(record.state, ingestionStates),
    is_active: requiredBoolean(record, "is_active"),
    original_artifact_id: requiredString(record, "original_artifact_id"),
    ingestion_key: requiredString(record, "ingestion_key"),
    created_at: timestamp(record, "created_at"),
    ready_at: readyAt === null ? null : requireIsoTimestamp(readyAt),
    superseded_at: supersededAt === null ? null : requireIsoTimestamp(supersededAt),
    failure_code: nullableString(record, "failure_code"),
    failure_message: nullableString(record, "failure_message"),
  };
}

export function parseDocumentSummary(value: unknown): DocumentSummary {
  const record = asRecord(value);
  requireExactKeys(record, ["source", "latest_version"]);
  const source = asRecord(record.source);
  requireExactKeys(source, [
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
  ]);
  return {
    source: {
      document_id: requiredString(source, "document_id"),
      engagement_id: requiredString(source, "engagement_id"),
      stakeholder_id: nullableString(source, "stakeholder_id"),
      role: nullableString(source, "role"),
      department: nullableString(source, "department"),
      doc_type: oneOf(source.doc_type, ["pdf", "docx", "xlsx", "pptx", "png", "jpeg"]),
      source_type: oneOf(source.source_type, ["stakeholder_document", "engagement_document"]),
      original_filename: requiredString(source, "original_filename"),
      media_type: requiredString(source, "media_type"),
      created_at: timestamp(source, "created_at"),
    },
    latest_version: parseDocumentVersion(record.latest_version),
  };
}

export function parseInterviewSession(value: unknown): InterviewSession {
  const record = asRecord(value);
  requireExactKeys(record, [
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
  ]);
  const finalizedAt = nullableString(record, "finalized_at");
  return {
    interview_session_id: requiredString(record, "interview_session_id"),
    engagement_id: requiredString(record, "engagement_id"),
    stakeholder_id: requiredString(record, "stakeholder_id"),
    invitation_id: requiredString(record, "invitation_id"),
    thread_id: requiredString(record, "thread_id"),
    status: oneOf(record.status, [
      "draft",
      "finalizing",
      "finalized",
      "ingesting",
      "ready",
      "failed",
    ]),
    started_at: timestamp(record, "started_at"),
    finalized_at: finalizedAt === null ? null : requireIsoTimestamp(finalizedAt),
    transcript_id: nullableString(record, "transcript_id"),
    ingestion_version_id: nullableString(record, "ingestion_version_id"),
    failure_code: nullableString(record, "failure_code"),
    failure_message: nullableString(record, "failure_message"),
  };
}

const insightStatuses: readonly InsightRunStatus[] = [
  "queued",
  "planning",
  "researching",
  "editing",
  "validating",
  "complete",
  "partial",
  "insufficient_evidence",
  "failed",
];

function parseInsightRun(value: unknown): InsightRun {
  const record = asRecord(value);
  requireExactKeys(record, [
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
  ]);
  const completedAt = nullableString(record, "completed_at");
  return {
    run_id: requiredString(record, "run_id"),
    engagement_id: requiredString(record, "engagement_id"),
    thread_id: requiredString(record, "thread_id"),
    status: oneOf(record.status, insightStatuses),
    requested_question: requiredString(record, "requested_question"),
    plan_id: nullableString(record, "plan_id"),
    report_id: nullableString(record, "report_id"),
    failure_code: nullableString(record, "failure_code"),
    failure_message: nullableString(record, "failure_message"),
    started_at: timestamp(record, "started_at"),
    completed_at: completedAt === null ? null : requireIsoTimestamp(completedAt),
  };
}

function parseBoundingBox(value: unknown): BoundingBox | null {
  if (value === null) {
    return null;
  }
  const record = asRecord(value);
  requireExactKeys(record, ["x0", "y0", "x1", "y1", "coordinate_space"]);
  return {
    x0: requiredNumber(record, "x0"),
    y0: requiredNumber(record, "y0"),
    x1: requiredNumber(record, "x1"),
    y1: requiredNumber(record, "y1"),
    coordinate_space: oneOf(record.coordinate_space, ["points", "pixels", "normalized"]),
  };
}

function parseSourceLocation(value: unknown): SourceLocation {
  const record = asRecord(value);
  switch (record.kind) {
    case "pdf_page":
      requireExactKeys(record, ["kind", "filename", "page", "bounding_box"]);
      return {
        kind: "pdf_page",
        filename: requiredString(record, "filename"),
        page: requiredInteger(record, "page"),
        bounding_box: parseBoundingBox(record.bounding_box),
      };
    case "docx_rendered_page":
      requireExactKeys(record, [
        "kind",
        "filename",
        "rendered_page",
        "section",
        "paragraph",
        "bounding_box",
      ]);
      return {
        kind: "docx_rendered_page",
        filename: requiredString(record, "filename"),
        rendered_page: requiredInteger(record, "rendered_page"),
        section: nullableString(record, "section"),
        paragraph: nullableInteger(record, "paragraph"),
        bounding_box: parseBoundingBox(record.bounding_box),
      };
    case "pptx_slide":
      requireExactKeys(record, ["kind", "filename", "slide", "shape_identifier", "bounding_box"]);
      return {
        kind: "pptx_slide",
        filename: requiredString(record, "filename"),
        slide: requiredInteger(record, "slide"),
        shape_identifier: nullableString(record, "shape_identifier"),
        bounding_box: parseBoundingBox(record.bounding_box),
      };
    case "xlsx_range":
      requireExactKeys(record, [
        "kind",
        "filename",
        "sheet",
        "cell_range",
        "chart_identifier",
        "image_identifier",
      ]);
      return {
        kind: "xlsx_range",
        filename: requiredString(record, "filename"),
        sheet: requiredString(record, "sheet"),
        cell_range: requiredString(record, "cell_range"),
        chart_identifier: nullableString(record, "chart_identifier"),
        image_identifier: nullableString(record, "image_identifier"),
      };
    case "image_region":
      requireExactKeys(record, ["kind", "filename", "image_index", "region", "bounding_box"]);
      return {
        kind: "image_region",
        filename: requiredString(record, "filename"),
        image_index: nullableInteger(record, "image_index"),
        region: nullableString(record, "region"),
        bounding_box: parseBoundingBox(record.bounding_box),
      };
    case "transcript_turns":
      requireExactKeys(record, [
        "kind",
        "stakeholder_id",
        "transcript_id",
        "turn_start",
        "turn_end",
      ]);
      return {
        kind: "transcript_turns",
        stakeholder_id: requiredString(record, "stakeholder_id"),
        transcript_id: requiredString(record, "transcript_id"),
        turn_start: requiredInteger(record, "turn_start"),
        turn_end: requiredInteger(record, "turn_end"),
      };
    default:
      throw new ContractError();
  }
}

function parseReportClaim(value: unknown): ReportClaim {
  const record = asRecord(value);
  requireExactKeys(record, ["claim_id", "statement", "evidence_ids"]);
  return {
    claim_id: requiredString(record, "claim_id"),
    statement: requiredString(record, "statement"),
    evidence_ids: requiredStringArray(record, "evidence_ids"),
  };
}

function parseResearchedTopic(value: unknown): ResearchedTopicOutcome {
  const record = asRecord(value);
  requireExactKeys(record, ["topic_id", "title", "status", "summary", "evidence_ids"]);
  return {
    topic_id: requiredString(record, "topic_id"),
    title: requiredString(record, "title"),
    status: oneOf(record.status, ["completed", "failed", "insufficient_evidence"]),
    summary: requiredString(record, "summary"),
    evidence_ids: requiredStringArray(record, "evidence_ids"),
  };
}

function parseResponsibility(value: unknown): ResponsibilityFinding {
  const record = asRecord(value);
  requireExactKeys(record, [
    "claim_id",
    "responsibility",
    "attribution",
    "uncertainty",
    "evidence_ids",
  ]);
  return {
    claim_id: requiredString(record, "claim_id"),
    responsibility: requiredString(record, "responsibility"),
    attribution: requiredString(record, "attribution"),
    uncertainty: requiredString(record, "uncertainty"),
    evidence_ids: requiredStringArray(record, "evidence_ids"),
  };
}

function parseOperationalRisk(value: unknown): OperationalRisk {
  const record = asRecord(value);
  requireExactKeys(record, [
    "claim_id",
    "risk",
    "impact",
    "responsibility_context",
    "uncertainty",
    "evidence_ids",
  ]);
  return {
    claim_id: requiredString(record, "claim_id"),
    risk: requiredString(record, "risk"),
    impact: requiredString(record, "impact"),
    responsibility_context: requiredString(record, "responsibility_context"),
    uncertainty: requiredString(record, "uncertainty"),
    evidence_ids: requiredStringArray(record, "evidence_ids"),
  };
}

const buyInCategories: readonly BuyInCategory[] = [
  "confirmed_support",
  "conditional_support",
  "expressed_concern",
  "insufficient_evidence",
  "topic_not_discussed",
];

function parseBuyInSignal(value: unknown): BuyInSignal {
  const record = asRecord(value);
  requireExactKeys(record, [
    "topic",
    "stakeholder_id",
    "role",
    "department",
    "category",
    "explanation",
    "evidence_ids",
  ]);
  return {
    topic: requiredString(record, "topic"),
    stakeholder_id: nullableString(record, "stakeholder_id"),
    role: nullableString(record, "role"),
    department: nullableString(record, "department"),
    category: oneOf(record.category, buyInCategories),
    explanation: requiredString(record, "explanation"),
    evidence_ids: requiredStringArray(record, "evidence_ids"),
  };
}

function parseContradictionSide(value: unknown): ContradictionSide {
  const record = asRecord(value);
  requireExactKeys(record, ["statement", "stakeholder_id", "role", "department", "evidence_ids"]);
  return {
    statement: requiredString(record, "statement"),
    stakeholder_id: nullableString(record, "stakeholder_id"),
    role: nullableString(record, "role"),
    department: nullableString(record, "department"),
    evidence_ids: requiredStringArray(record, "evidence_ids"),
  };
}

function parseContradiction(value: unknown): Contradiction {
  const record = asRecord(value);
  requireExactKeys(record, ["topic", "side_a", "side_b", "interpretation", "evidence_ids"]);
  return {
    topic: requiredString(record, "topic"),
    side_a: parseContradictionSide(record.side_a),
    side_b: parseContradictionSide(record.side_b),
    interpretation: requiredString(record, "interpretation"),
    evidence_ids: requiredStringArray(record, "evidence_ids"),
  };
}

function parseEvidenceGap(value: unknown): EvidenceGap {
  const record = asRecord(value);
  requireExactKeys(record, ["topic", "description", "impact"]);
  return {
    topic: requiredString(record, "topic"),
    description: requiredString(record, "description"),
    impact: requiredString(record, "impact"),
  };
}

function parseFollowUp(value: unknown): FollowUpRecommendation {
  const record = asRecord(value);
  requireExactKeys(record, ["recommendation", "rationale", "evidence_ids"]);
  return {
    recommendation: requiredString(record, "recommendation"),
    rationale: requiredString(record, "rationale"),
    evidence_ids: requiredStringArray(record, "evidence_ids"),
  };
}

function parseCitation(value: unknown): Citation {
  const record = asRecord(value);
  requireExactKeys(record, [
    "citation_id",
    "evidence_id",
    "display_label",
    "source_location",
    "claim_ids",
  ]);
  return {
    citation_id: requiredString(record, "citation_id"),
    evidence_id: requiredString(record, "evidence_id"),
    display_label: requiredString(record, "display_label"),
    source_location: parseSourceLocation(record.source_location),
    claim_ids: requiredStringArray(record, "claim_ids"),
  };
}

function parseRunMetadata(value: unknown): RunMetadata {
  const record = asRecord(value);
  requireExactKeys(record, [
    "run_id",
    "started_at",
    "completed_at",
    "primary_model_id",
    "fallback_model_id",
    "topic_count",
    "status_detail",
  ]);
  return {
    run_id: requiredString(record, "run_id"),
    started_at: timestamp(record, "started_at"),
    completed_at: timestamp(record, "completed_at"),
    primary_model_id: requiredString(record, "primary_model_id"),
    fallback_model_id: requiredString(record, "fallback_model_id"),
    topic_count: requiredInteger(record, "topic_count"),
    status_detail: requiredString(record, "status_detail"),
  };
}

function parseInsightExecutionMetrics(value: unknown): InsightExecutionMetrics {
  const record = asRecord(value);
  const integerKeys = [
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
  ] as const;
  requireExactKeys(record, [
    "run_id",
    "engagement_id",
    "thread_id",
    "started_at",
    "completed_at",
    "status",
    ...integerKeys,
    "source_ids",
    "evidence_ids",
    "tool_names",
    "failure_code",
    "correlation_id",
  ]);
  const integers = Object.fromEntries(
    integerKeys.map((key) => [key, nonNegativeInteger(record, key)]),
  ) as Record<(typeof integerKeys)[number], number>;
  const status = oneOf(record.status, ["complete", "partial", "insufficient_evidence", "failed"]);
  const failureCode = nullableString(record, "failure_code");
  const metrics: InsightExecutionMetrics = {
    run_id: requiredString(record, "run_id"),
    engagement_id: requiredString(record, "engagement_id"),
    thread_id: requiredString(record, "thread_id"),
    started_at: timestamp(record, "started_at"),
    completed_at: timestamp(record, "completed_at"),
    status,
    ...integers,
    source_ids: requiredStringArray(record, "source_ids"),
    evidence_ids: requiredStringArray(record, "evidence_ids"),
    tool_names: requiredStringArray(record, "tool_names"),
    failure_code: failureCode,
    correlation_id: requiredString(record, "correlation_id"),
  };
  if (
    (status === "failed") !== (failureCode !== null) ||
    metrics.topic_count > metrics.configured_topic_limit ||
    metrics.max_concurrent_researchers > metrics.configured_parallel_researcher_limit ||
    metrics.max_rerank_candidates_per_call > metrics.configured_rerank_candidate_limit ||
    metrics.model_failures > metrics.model_calls ||
    metrics.tool_failures > metrics.tool_calls ||
    metrics.retrieval_calls > metrics.tool_calls
  ) {
    throw new ContractError();
  }
  return metrics;
}

function parseInsightReport(value: unknown): InsightReport {
  const record = asRecord(value);
  requireExactKeys(record, [
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
  ]);
  const report: InsightReport = {
    report_id: requiredString(record, "report_id"),
    engagement_id: requiredString(record, "engagement_id"),
    question: requiredString(record, "question"),
    status: oneOf(record.status, ["complete", "partial", "insufficient_evidence"]),
    executive_summary: requiredString(record, "executive_summary"),
    researched_topics: records(record, "researched_topics", parseResearchedTopic),
    findings: records(record, "findings", parseReportClaim),
    responsibilities: records(record, "responsibilities", parseResponsibility),
    operational_risks: records(record, "operational_risks", parseOperationalRisk),
    buy_in_signals: records(record, "buy_in_signals", parseBuyInSignal),
    contradictions: records(record, "contradictions", parseContradiction),
    evidence_gaps: records(record, "evidence_gaps", parseEvidenceGap),
    open_questions: requiredStringArray(record, "open_questions"),
    follow_up_recommendations: records(record, "follow_up_recommendations", parseFollowUp),
    evidence_ids: requiredStringArray(record, "evidence_ids"),
    citations: records(record, "citations", parseCitation),
    run_metadata: parseRunMetadata(record.run_metadata),
  };
  if (
    new Set(report.evidence_ids).size !== report.evidence_ids.length ||
    new Set(report.citations.map((citation) => citation.evidence_id)).size !==
      report.evidence_ids.length ||
    report.citations.some((citation) => !report.evidence_ids.includes(citation.evidence_id))
  ) {
    throw new ContractError();
  }
  return report;
}

function parseEvidenceRecord(value: unknown): EvidenceRecord {
  const record = asRecord(value);
  requireExactKeys(record, [
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
  ]);
  return {
    evidence_id: requiredString(record, "evidence_id"),
    run_id: requiredString(record, "run_id"),
    engagement_id: requiredString(record, "engagement_id"),
    topic_id: requiredString(record, "topic_id"),
    source_id: requiredString(record, "source_id"),
    source_version_id: requiredString(record, "source_version_id"),
    source_type: oneOf(record.source_type, [
      "stakeholder_document",
      "engagement_document",
      "interview",
    ]),
    stakeholder_id: nullableString(record, "stakeholder_id"),
    location: parseSourceLocation(record.location),
    original_excerpt: requiredString(record, "original_excerpt"),
    english_interpretation: nullableString(record, "english_interpretation"),
    content_hash: requiredString(record, "content_hash"),
    researcher_id: requiredString(record, "researcher_id"),
    created_at: timestamp(record, "created_at"),
  };
}

function parseArtifact(value: unknown): SourceArtifactSummary {
  const record = asRecord(value);
  requireExactKeys(record, [
    "artifact_id",
    "artifact_kind",
    "media_type",
    "content_hash",
    "download_path",
  ]);
  const contentHash = requiredString(record, "content_hash");
  if (!/^[a-f0-9]{64}$/u.test(contentHash)) {
    throw new ContractError();
  }
  return {
    artifact_id: requiredString(record, "artifact_id"),
    artifact_kind: requiredString(record, "artifact_kind"),
    media_type: requiredString(record, "media_type"),
    content_hash: contentHash,
    download_path: nullableString(record, "download_path"),
  };
}

export function parseEngagementListResponse(value: unknown): EngagementListResponse {
  const record = asRecord(value);
  requireExactKeys(record, ["engagements"]);
  return { engagements: records(record, "engagements", parseEngagement) };
}

export function parseEngagementContextResponse(value: unknown): EngagementContextResponse {
  const record = asRecord(value);
  requireExactKeys(record, ["engagement"]);
  return { engagement: parseEngagement(record.engagement) };
}

export function parseStakeholderListResponse(value: unknown): StakeholderListResponse {
  const record = asRecord(value);
  requireExactKeys(record, ["stakeholders"]);
  return { stakeholders: records(record, "stakeholders", parseStakeholder) };
}

export function parseStakeholderResponse(value: unknown): StakeholderResponse {
  const record = asRecord(value);
  requireExactKeys(record, ["stakeholder"]);
  return { stakeholder: parseStakeholder(record.stakeholder) };
}

export function parseInvitationListResponse(value: unknown): InvitationListResponse {
  const record = asRecord(value);
  requireExactKeys(record, ["invitations"]);
  return { invitations: records(record, "invitations", parseInvitation) };
}

export function parseInvitationIssuedResponse(value: unknown): InvitationIssuedResponse {
  const record = asRecord(value);
  requireExactKeys(record, ["invitation", "invitation_token"]);
  const invitationToken = requiredString(record, "invitation_token");
  if (invitationToken.length < 32) {
    throw new ContractError();
  }
  return {
    invitation: parseInvitation(record.invitation),
    invitation_token: invitationToken,
  };
}

export function parseInvitationSummary(value: unknown): InvitationSummary {
  return parseInvitation(value);
}

export function parseDocumentListResponse(value: unknown): DocumentListResponse {
  const record = asRecord(value);
  requireExactKeys(record, ["documents"]);
  return { documents: records(record, "documents", parseDocumentSummary) };
}

export function parseUploadResponse(value: unknown): UploadResponse {
  const record = asRecord(value);
  requireExactKeys(record, [
    "document",
    "element_count",
    "chunk_count",
    "attempt_id",
    "idempotent",
  ]);
  return {
    document: parseDocumentSummary(record.document),
    element_count: requiredInteger(record, "element_count"),
    chunk_count: requiredInteger(record, "chunk_count"),
    attempt_id: nullableString(record, "attempt_id"),
    idempotent: requiredBoolean(record, "idempotent"),
  };
}

function parseDocumentProcessingCount(value: unknown): DocumentProcessingCount {
  const record = asRecord(value);
  requireExactKeys(record, ["name", "count"]);
  return {
    name: requiredString(record, "name"),
    count: nonNegativeInteger(record, "count"),
  };
}

function parseDocumentLifecycleEvent(value: unknown): DocumentLifecycleEvent {
  const record = asRecord(value);
  requireExactKeys(record, ["event_id", "from_state", "to_state", "occurred_at"]);
  const fromState = nullableString(record, "from_state");
  return {
    event_id: requiredString(record, "event_id"),
    from_state: fromState === null ? null : oneOf(fromState, ingestionStates),
    to_state: oneOf(record.to_state, ingestionStates),
    occurred_at: timestamp(record, "occurred_at"),
  };
}

function parseDocumentElementPreview(value: unknown): DocumentElementPreview {
  const record = asRecord(value);
  requireExactKeys(record, [
    "element_id",
    "document_version_id",
    "element_type",
    "location",
    "extraction_method",
    "content_preview",
    "english_interpretation",
  ]);
  return {
    element_id: requiredString(record, "element_id"),
    document_version_id: requiredString(record, "document_version_id"),
    element_type: oneOf(record.element_type, [
      "text",
      "table",
      "image",
      "chart",
      "ocr_text",
      "vision_description",
    ]),
    location: parseSourceLocation(record.location),
    extraction_method: requiredString(record, "extraction_method"),
    content_preview: nullableString(record, "content_preview"),
    english_interpretation: nullableString(record, "english_interpretation"),
  };
}

export function parseDocumentProcessingDetailsResponse(
  value: unknown,
): DocumentProcessingDetailsResponse {
  const record = asRecord(value);
  requireExactKeys(record, [
    "document",
    "lifecycle_events",
    "element_count",
    "element_counts",
    "chunk_count",
    "artifact_count",
    "artifact_counts",
    "artifacts",
    "element_previews",
  ]);
  return {
    document: parseDocumentSummary(record.document),
    lifecycle_events: records(record, "lifecycle_events", parseDocumentLifecycleEvent),
    element_count: nonNegativeInteger(record, "element_count"),
    element_counts: records(record, "element_counts", parseDocumentProcessingCount),
    chunk_count: nonNegativeInteger(record, "chunk_count"),
    artifact_count: nonNegativeInteger(record, "artifact_count"),
    artifact_counts: records(record, "artifact_counts", parseDocumentProcessingCount),
    artifacts: records(record, "artifacts", parseArtifact),
    element_previews: records(record, "element_previews", parseDocumentElementPreview),
  };
}

export function parseInterviewSessionListResponse(value: unknown): InterviewSessionListResponse {
  const record = asRecord(value);
  requireExactKeys(record, ["interview_sessions"]);
  return {
    interview_sessions: records(record, "interview_sessions", parseInterviewSession),
  };
}

function parseFinalizedTranscript(value: unknown): FinalizedTranscript {
  const record = asRecord(value);
  requireExactKeys(record, [
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
  ]);
  if (record.status !== "finalized") {
    throw new ContractError("Interview preview requires a finalized transcript.");
  }
  const finalizedAt = nullableString(record, "finalized_at");
  const contentHash = nullableString(record, "content_hash");
  if (finalizedAt === null || contentHash === null || !/^[a-f0-9]{64}$/u.test(contentHash)) {
    throw new ContractError("Interview preview contains incomplete finalization data.");
  }
  return {
    transcript_id: requiredString(record, "transcript_id"),
    interview_session_id: requiredString(record, "interview_session_id"),
    engagement_id: requiredString(record, "engagement_id"),
    stakeholder_id: requiredString(record, "stakeholder_id"),
    role: nullableString(record, "role"),
    department: nullableString(record, "department"),
    status: "finalized",
    language_observations: requiredStringArray(record, "language_observations"),
    finalized_at: requireIsoTimestamp(finalizedAt),
    content_hash: contentHash,
  };
}

export function parseInterviewPreviewResponse(value: unknown): InterviewPreviewResponse {
  const record = asRecord(value);
  requireExactKeys(record, ["interview_session", "transcript", "turns"]);
  const interviewSession = parseInterviewSession(record.interview_session);
  const transcript = parseFinalizedTranscript(record.transcript);
  const turns = requiredArray(record, "turns").map((value, index) => {
    const turn = asRecord(value);
    requireExactKeys(turn, ["turn_index", "speaker", "text"]);
    const turnIndex = requiredInteger(turn, "turn_index");
    if (turnIndex !== index) {
      throw new ContractError("Interview preview is not in canonical turn order.");
    }
    return {
      turn_index: turnIndex,
      speaker: oneOf(turn.speaker, ["stakeholder", "assistant"]),
      text: requiredString(turn, "text"),
    };
  });
  if (
    interviewSession.finalized_at === null ||
    interviewSession.transcript_id !== transcript.transcript_id ||
    interviewSession.interview_session_id !== transcript.interview_session_id ||
    interviewSession.engagement_id !== transcript.engagement_id ||
    interviewSession.stakeholder_id !== transcript.stakeholder_id
  ) {
    throw new ContractError("Interview preview does not match its finalized session.");
  }
  return { interview_session: interviewSession, transcript, turns };
}

export function parseInsightStatusResponse(value: unknown): InsightStatusResponse {
  const record = asRecord(value);
  requireExactKeys(record, ["run"]);
  return { run: parseInsightRun(record.run) };
}

export function parseInsightRunListResponse(value: unknown): InsightRunListResponse {
  const record = asRecord(value);
  requireExactKeys(record, ["runs"]);
  return { runs: records(record, "runs", parseInsightRun) };
}

export function parseInsightReportResponse(value: unknown): InsightReportResponse {
  const record = asRecord(value);
  requireExactKeys(record, ["run", "report", "metrics"]);
  const run = parseInsightRun(record.run);
  const report = parseInsightReport(record.report);
  const metrics = parseInsightExecutionMetrics(record.metrics);
  if (
    run.run_id !== report.run_metadata.run_id ||
    run.run_id !== metrics.run_id ||
    run.engagement_id !== report.engagement_id ||
    run.engagement_id !== metrics.engagement_id ||
    run.thread_id !== metrics.thread_id ||
    run.report_id !== report.report_id ||
    run.status !== report.status ||
    run.status !== metrics.status
  ) {
    throw new ContractError();
  }
  return { run, report, metrics };
}

export function parseEvidenceDrillDownResponse(value: unknown): EvidenceDrillDownResponse {
  const record = asRecord(value);
  requireExactKeys(record, ["evidence", "original", "related_artifacts"]);
  return {
    evidence: parseEvidenceRecord(record.evidence),
    original: parseArtifact(record.original),
    related_artifacts: records(record, "related_artifacts", parseArtifact),
  };
}
