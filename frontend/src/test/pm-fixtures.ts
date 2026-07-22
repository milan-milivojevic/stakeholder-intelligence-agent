import type { ApiResult } from "../api/client";
import type {
  DocumentSummary,
  Engagement,
  InsightRun,
  InsightStreamEvent,
  InterviewSession,
  InvitationSummary,
  SafeRunEvent,
  Stakeholder,
} from "../api/contracts";
import type {
  DocumentProcessingDetailsResponse,
  EvidenceDrillDownResponse,
  InsightExecutionMetrics,
  InsightReport,
  InsightReportResponse,
  InterviewPreviewResponse,
  UploadResponse,
} from "../pm/contracts";
import type { PmApi } from "../pm/pm-api";

export const timestamp = "2026-07-15T08:00:00Z";
export const laterTimestamp = "2026-07-15T08:05:00Z";

export const engagement: Engagement = {
  engagement_id: "engagement-alpha",
  name: "Service transformation",
  description: "Evidence-grounded operating model research.",
  status: "active",
  created_at: timestamp,
  updated_at: timestamp,
};

export const secondEngagement: Engagement = {
  ...engagement,
  engagement_id: "engagement-beta",
  name: "Customer experience",
};

export const stakeholder: Stakeholder = {
  stakeholder_id: "stakeholder-alpha",
  engagement_id: engagement.engagement_id,
  display_name: "Alex Morgan",
  role: "Operations lead",
  department: "Operations",
  status: "active",
  created_at: timestamp,
  updated_at: timestamp,
};

export const invitation: InvitationSummary = {
  invitation_id: "invitation-alpha",
  engagement_id: engagement.engagement_id,
  stakeholder_id: stakeholder.stakeholder_id,
  status: "active",
  created_at: timestamp,
  expires_at: "2026-07-16T08:00:00Z",
  activated_at: null,
  revoked_at: null,
};

export const revokedInvitation: InvitationSummary = {
  ...invitation,
  status: "revoked",
  revoked_at: laterTimestamp,
};

export const documentSummary: DocumentSummary = {
  source: {
    document_id: "document-alpha",
    engagement_id: engagement.engagement_id,
    stakeholder_id: null,
    role: null,
    department: null,
    doc_type: "pdf",
    source_type: "engagement_document",
    original_filename: "operating-model.pdf",
    media_type: "application/pdf",
    created_at: timestamp,
  },
  latest_version: {
    document_version_id: "document-version-alpha",
    document_id: "document-alpha",
    version_number: 1,
    content_hash: "a".repeat(64),
    state: "READY",
    is_active: true,
    original_artifact_id: "artifact-original",
    ingestion_key: "ingestion-alpha",
    created_at: timestamp,
    ready_at: laterTimestamp,
    superseded_at: null,
    failure_code: null,
    failure_message: null,
  },
};

export const uploadResponse: UploadResponse = {
  document: documentSummary,
  element_count: 4,
  chunk_count: 3,
  attempt_id: "attempt-alpha",
  idempotent: false,
};

export const documentProcessingDetails: DocumentProcessingDetailsResponse = {
  document: documentSummary,
  lifecycle_events: [
    {
      event_id: "event-received",
      from_state: null,
      to_state: "RECEIVED",
      occurred_at: timestamp,
    },
    {
      event_id: "event-ready",
      from_state: "INDEXING",
      to_state: "READY",
      occurred_at: laterTimestamp,
    },
  ],
  element_count: 4,
  element_counts: [
    { name: "text", count: 2 },
    { name: "table", count: 1 },
    { name: "vision_description", count: 1 },
  ],
  chunk_count: 3,
  artifact_count: 2,
  artifact_counts: [
    { name: "original", count: 1 },
    { name: "page_render", count: 1 },
  ],
  artifacts: [
    {
      artifact_id: "artifact-original",
      artifact_kind: "original",
      media_type: "application/pdf",
      content_hash: "a".repeat(64),
      download_path:
        "/api/v1/pm/engagements/engagement-alpha/documents/document-alpha/artifacts/artifact-original",
    },
    {
      artifact_id: "artifact-page",
      artifact_kind: "page_render",
      media_type: "image/png",
      content_hash: "c".repeat(64),
      download_path:
        "/api/v1/pm/engagements/engagement-alpha/documents/document-alpha/artifacts/artifact-page",
    },
  ],
  element_previews: [
    {
      element_id: "element-text",
      document_version_id: documentSummary.latest_version.document_version_id,
      element_type: "text",
      location: {
        kind: "pdf_page",
        filename: documentSummary.source.original_filename,
        page: 1,
        bounding_box: null,
      },
      extraction_method: "docling",
      content_preview: "Operations owns the weekly service review.",
      english_interpretation: null,
    },
    {
      element_id: "element-table",
      document_version_id: documentSummary.latest_version.document_version_id,
      element_type: "table",
      location: {
        kind: "pdf_page",
        filename: documentSummary.source.original_filename,
        page: 1,
        bounding_box: null,
      },
      extraction_method: "docling",
      content_preview:
        "| Role | Responsibility |\n| --- | --- |\n| Operations | Weekly service review |",
      english_interpretation: null,
    },
    {
      element_id: "element-vision",
      document_version_id: documentSummary.latest_version.document_version_id,
      element_type: "vision_description",
      location: {
        kind: "pdf_page",
        filename: documentSummary.source.original_filename,
        page: 2,
        bounding_box: null,
      },
      extraction_method: "gemini_vision",
      content_preview:
        "**Visual type:** Grouped bar chart\n\n- Product has high influence.\n- Operations has the highest change impact.",
      english_interpretation: null,
    },
  ],
};

export const interviewSession: InterviewSession = {
  interview_session_id: "interview-alpha",
  engagement_id: engagement.engagement_id,
  stakeholder_id: stakeholder.stakeholder_id,
  invitation_id: invitation.invitation_id,
  thread_id: "thread-alpha",
  status: "ready",
  started_at: timestamp,
  finalized_at: laterTimestamp,
  transcript_id: "transcript-alpha",
  ingestion_version_id: "transcript-version-alpha",
  failure_code: null,
  failure_message: null,
};

export const interviewPreview: InterviewPreviewResponse = {
  interview_session: interviewSession,
  transcript: {
    transcript_id: interviewSession.transcript_id ?? "transcript-alpha",
    interview_session_id: interviewSession.interview_session_id,
    engagement_id: engagement.engagement_id,
    stakeholder_id: stakeholder.stakeholder_id,
    role: stakeholder.role,
    department: stakeholder.department,
    status: "finalized",
    language_observations: [],
    finalized_at: laterTimestamp,
    content_hash: "b".repeat(64),
  },
  turns: [
    {
      turn_index: 0,
      speaker: "assistant",
      text: "What is your role in the weekly operations review?",
    },
    {
      turn_index: 1,
      speaker: "stakeholder",
      text: "I coordinate the review and confirm the follow-up owners.",
    },
  ],
};

export const queuedRun: InsightRun = {
  run_id: "run-alpha",
  engagement_id: engagement.engagement_id,
  thread_id: "report-thread-alpha",
  status: "queued",
  requested_question: "Where are the operating-model risks?",
  plan_id: null,
  report_id: null,
  failure_code: null,
  failure_message: null,
  started_at: timestamp,
  completed_at: null,
};

export const safeRunEvent: SafeRunEvent = {
  event_id: "event-alpha",
  occurred_at: timestamp,
  actor: "researcher",
  action: "research_topic_completed",
  from_status: "researching",
  to_status: "editing",
  topic_id: "topic-alpha",
  source_ids: ["document-alpha"],
  evidence_ids: ["evidence-alpha"],
  artifact_name: "research-topic-alpha.md",
  failure_code: null,
  correlation_id: "correlation-alpha",
};

function baseReport(): InsightReport {
  return {
    report_id: "report-alpha",
    engagement_id: engagement.engagement_id,
    question: queuedRun.requested_question,
    status: "complete",
    executive_summary: "Ownership is supported, while one handoff creates an operational risk.",
    researched_topics: [
      {
        topic_id: "topic-alpha",
        title: "Ownership and handoffs",
        status: "completed",
        summary: "The permitted evidence supports the ownership finding.",
        evidence_ids: ["evidence-alpha", "evidence-beta"],
      },
    ],
    findings: [
      {
        claim_id: "claim-finding",
        statement: "Operations owns the weekly service review.",
        evidence_ids: ["evidence-alpha"],
      },
    ],
    responsibilities: [
      {
        claim_id: "claim-responsibility",
        responsibility: "Run the weekly service review.",
        attribution: "Operations lead",
        uncertainty: "Escalation ownership was not explicit.",
        evidence_ids: ["evidence-alpha"],
      },
    ],
    operational_risks: [
      {
        claim_id: "claim-risk",
        risk: "Handoff decisions can be delayed.",
        impact: "Service incidents may remain unresolved.",
        responsibility_context: "Operations and Product share the handoff.",
        uncertainty: "The escalation deadline was not documented.",
        evidence_ids: ["evidence-beta"],
      },
    ],
    buy_in_signals: [
      {
        topic: "Weekly service review",
        stakeholder_id: stakeholder.stakeholder_id,
        role: stakeholder.role,
        department: stakeholder.department,
        category: "conditional_support",
        explanation: "Support depends on a documented escalation owner.",
        evidence_ids: ["evidence-alpha"],
      },
    ],
    contradictions: [
      {
        topic: "Escalation ownership",
        side_a: {
          statement: "Operations expects Product to own escalation.",
          stakeholder_id: stakeholder.stakeholder_id,
          role: stakeholder.role,
          department: stakeholder.department,
          evidence_ids: ["evidence-alpha"],
        },
        side_b: {
          statement: "The process document assigns escalation to Operations.",
          stakeholder_id: null,
          role: null,
          department: null,
          evidence_ids: ["evidence-beta"],
        },
        interpretation: "The evidence supports a genuine ownership disagreement.",
        evidence_ids: ["evidence-alpha", "evidence-beta"],
      },
    ],
    evidence_gaps: [
      {
        topic: "Escalation deadline",
        description: "No permitted source specifies a deadline.",
        impact: "Delay exposure cannot be bounded.",
      },
    ],
    open_questions: ["Who approves an escalation exception?"],
    follow_up_recommendations: [
      {
        recommendation: "Document the escalation owner and deadline.",
        rationale: "This resolves the supported handoff risk.",
        evidence_ids: ["evidence-alpha", "evidence-beta"],
      },
    ],
    evidence_ids: ["evidence-alpha", "evidence-beta"],
    citations: [
      {
        citation_id: "citation-alpha",
        evidence_id: "evidence-alpha",
        display_label: "Interview evidence",
        source_location: {
          kind: "transcript_turns",
          stakeholder_id: stakeholder.stakeholder_id,
          transcript_id: "transcript-alpha",
          turn_start: 0,
          turn_end: 2,
        },
        claim_ids: ["claim-finding", "claim-responsibility"],
      },
      {
        citation_id: "citation-beta",
        evidence_id: "evidence-beta",
        display_label: "Operating model page 4",
        source_location: {
          kind: "pdf_page",
          filename: "operating-model.pdf",
          page: 4,
          bounding_box: null,
        },
        claim_ids: ["claim-risk"],
      },
    ],
    run_metadata: {
      run_id: queuedRun.run_id,
      started_at: timestamp,
      completed_at: laterTimestamp,
      primary_model_id: "gemini-primary-test",
      fallback_model_id: "gemini-fallback-test",
      topic_count: 1,
      status_detail: "The validated report completed with cited permitted evidence.",
    },
  };
}

export function completeReport(): InsightReport {
  return baseReport();
}

export function partialReport(): InsightReport {
  return { ...baseReport(), status: "partial" };
}

export function insufficientReport(): InsightReport {
  const report = baseReport();
  return {
    ...report,
    status: "insufficient_evidence",
    executive_summary: "Permitted evidence was insufficient for supported conclusions.",
    researched_topics: [
      {
        topic_id: "topic-alpha",
        title: "Ownership and handoffs",
        status: "insufficient_evidence",
        summary: "No active ready source supported a conclusion.",
        evidence_ids: [],
      },
    ],
    findings: [],
    responsibilities: [],
    operational_risks: [],
    buy_in_signals: [
      {
        topic: "Weekly service review",
        stakeholder_id: null,
        role: null,
        department: null,
        category: "insufficient_evidence",
        explanation: "No permitted evidence supports a buy-in conclusion.",
        evidence_ids: [],
      },
    ],
    contradictions: [],
    evidence_ids: [],
    citations: [],
    follow_up_recommendations: [
      {
        recommendation: "Collect a finalized interview and an approved process source.",
        rationale: "The missing sources prevent supported conclusions.",
        evidence_ids: [],
      },
    ],
  };
}

export function terminalRun(status: "complete" | "partial" | "insufficient_evidence"): InsightRun {
  return {
    ...queuedRun,
    status,
    plan_id: "plan-alpha",
    report_id: "report-alpha",
    completed_at: laterTimestamp,
  };
}

export const failedRun: InsightRun = {
  ...queuedRun,
  status: "failed",
  completed_at: laterTimestamp,
  failure_code: "INSIGHT_EXECUTION_FAILED",
  failure_message: "The insight run could not be completed.",
};

export function executionMetrics(
  status: "complete" | "partial" | "insufficient_evidence" = "complete",
): InsightExecutionMetrics {
  const hasEvidence = status !== "insufficient_evidence";
  return {
    run_id: queuedRun.run_id,
    engagement_id: engagement.engagement_id,
    thread_id: queuedRun.thread_id,
    started_at: timestamp,
    completed_at: laterTimestamp,
    status,
    duration_ms: 300_000,
    topic_count: 1,
    researcher_calls: 1,
    max_concurrent_researchers: 1,
    model_calls: 8,
    model_failures: 0,
    tool_calls: 7,
    tool_failures: 0,
    retrieval_calls: 1,
    retry_count: 0,
    timeout_count: 0,
    rerank_candidates_total: hasEvidence ? 10 : 0,
    max_rerank_candidates_per_call: hasEvidence ? 10 : 0,
    retrieval_latency_ms: hasEvidence ? 42 : 0,
    reranker_latency_ms: hasEvidence ? 12 : 0,
    input_tokens: 120,
    output_tokens: 80,
    total_tokens: 200,
    configured_topic_limit: 5,
    configured_parallel_researcher_limit: 3,
    configured_model_call_limit: 25,
    configured_tool_call_limit: 40,
    configured_retrieval_calls_per_researcher_limit: 3,
    configured_rerank_candidate_limit: 50,
    configured_provider_timeout_seconds: 120,
    configured_run_timeout_seconds: 600,
    source_ids: hasEvidence ? [documentSummary.source.document_id] : [],
    evidence_ids: hasEvidence ? ["evidence-alpha", "evidence-beta"] : [],
    tool_names: ["scoped_retrieve", "save_final_report"],
    failure_code: null,
    correlation_id: "correlation-alpha",
  };
}

export function reportResponse(
  status: "complete" | "partial" | "insufficient_evidence" = "complete",
): InsightReportResponse {
  const report =
    status === "complete"
      ? completeReport()
      : status === "partial"
        ? partialReport()
        : insufficientReport();
  return { run: terminalRun(status), report, metrics: executionMetrics(status) };
}

export const evidenceResponse: EvidenceDrillDownResponse = {
  evidence: {
    evidence_id: "evidence-beta",
    run_id: queuedRun.run_id,
    engagement_id: engagement.engagement_id,
    topic_id: "topic-alpha",
    source_id: documentSummary.source.document_id,
    source_version_id: documentSummary.latest_version.document_version_id,
    source_type: "engagement_document",
    stakeholder_id: null,
    location: {
      kind: "pdf_page",
      filename: documentSummary.source.original_filename,
      page: 4,
      bounding_box: null,
    },
    original_excerpt: "Operations owns the weekly service review.",
    english_interpretation: null,
    content_hash: "b".repeat(64),
    researcher_id: "researcher-alpha",
    created_at: timestamp,
  },
  original: {
    artifact_id: "artifact-original",
    artifact_kind: "original_document",
    media_type: "application/pdf",
    content_hash: "a".repeat(64),
    download_path:
      "/api/v1/pm/engagements/engagement-alpha/insights/run-alpha/evidence/evidence-beta/artifacts/artifact-original",
  },
  related_artifacts: [
    {
      artifact_id: "artifact-derived",
      artifact_kind: "rendered_page",
      media_type: "image/png",
      content_hash: "c".repeat(64),
      download_path:
        "/api/v1/pm/engagements/engagement-alpha/insights/run-alpha/evidence/evidence-beta/artifacts/artifact-derived",
    },
    {
      artifact_id: "artifact-transcript",
      artifact_kind: "raw_transcript",
      media_type: "application/json",
      content_hash: "d".repeat(64),
      download_path: null,
    },
  ],
};

function success<T>(value: T): ApiResult<T> {
  return { ok: true, value };
}

async function* insightEvents(): AsyncIterable<InsightStreamEvent> {
  await Promise.resolve();
  yield { event: "progress", data: safeRunEvent };
}

export function fakePmApi(overrides: Partial<PmApi> = {}): PmApi {
  return {
    listEngagements: () => Promise.resolve(success([engagement])),
    createEngagement: () => Promise.resolve(success(engagement)),
    selectEngagement: () => Promise.resolve(success(engagement)),
    getEngagement: () => Promise.resolve(success(engagement)),
    listStakeholders: () => Promise.resolve(success([stakeholder])),
    createStakeholder: () => Promise.resolve(success(stakeholder)),
    listInvitations: () => Promise.resolve(success([invitation])),
    issueInvitation: () =>
      Promise.resolve(success({ invitation, invitation_token: "A".repeat(48) })),
    getInvitationLink: () =>
      Promise.resolve(success({ invitation, invitation_token: "A".repeat(48) })),
    revokeInvitation: () => Promise.resolve(success(revokedInvitation)),
    listDocuments: () => Promise.resolve(success([documentSummary])),
    uploadDocument: () => Promise.resolve(success(uploadResponse)),
    deleteDocument: () => Promise.resolve(success({ status: "ok" })),
    getDocumentProcessing: () => Promise.resolve(success(documentProcessingDetails)),
    documentArtifactPath: (engagementId, documentId, artifactId) =>
      `/api/v1/pm/engagements/${engagementId}/documents/${documentId}/artifacts/${artifactId}`,
    listInterviews: () => Promise.resolve(success([interviewSession])),
    getInterviewPreview: () => Promise.resolve(success(interviewPreview)),
    listInsights: () => Promise.resolve(success([])),
    createInsight: () => Promise.resolve(success(queuedRun)),
    getInsightStatus: () => Promise.resolve(success(terminalRun("complete"))),
    streamInsight: () => insightEvents(),
    getInsightReport: () => Promise.resolve(success(reportResponse())),
    getEvidence: () => Promise.resolve(success(evidenceResponse)),
    artifactDownloadPath: (engagementId, runId, evidenceId, artifactId) =>
      `/api/v1/pm/engagements/${engagementId}/insights/${runId}/evidence/${evidenceId}/artifacts/${artifactId}`,
    ...overrides,
  };
}
