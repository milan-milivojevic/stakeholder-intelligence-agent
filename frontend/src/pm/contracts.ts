import type {
  DocumentSummary,
  Engagement,
  IngestionState,
  InsightRun,
  InterviewSession,
  InvitationSummary,
  Stakeholder,
  Transcript,
} from "../api/contracts";

export interface EngagementListResponse {
  engagements: Engagement[];
}

export interface EngagementContextResponse {
  engagement: Engagement;
}

export interface StakeholderListResponse {
  stakeholders: Stakeholder[];
}

export interface StakeholderResponse {
  stakeholder: Stakeholder;
}

export interface InvitationListResponse {
  invitations: InvitationSummary[];
}

export interface InvitationIssuedResponse {
  invitation: InvitationSummary;
  invitation_token: string;
}

export type InvitationLinkResponse = InvitationIssuedResponse;

export interface DocumentListResponse {
  documents: DocumentSummary[];
}

export interface UploadResponse {
  document: DocumentSummary;
  element_count: number;
  chunk_count: number;
  attempt_id: string | null;
  idempotent: boolean;
}

export interface DocumentProcessingCount {
  name: string;
  count: number;
}

export interface DocumentLifecycleEvent {
  event_id: string;
  from_state: IngestionState | null;
  to_state: IngestionState;
  occurred_at: string;
}

export interface DocumentElementPreview {
  element_id: string;
  document_version_id: string;
  element_type: "text" | "table" | "image" | "chart" | "ocr_text" | "vision_description";
  location: SourceLocation;
  extraction_method: string;
  content_preview: string | null;
  english_interpretation: string | null;
}

export interface DocumentProcessingDetailsResponse {
  document: DocumentSummary;
  lifecycle_events: DocumentLifecycleEvent[];
  element_count: number;
  element_counts: DocumentProcessingCount[];
  chunk_count: number;
  artifact_count: number;
  artifact_counts: DocumentProcessingCount[];
  artifacts: SourceArtifactSummary[];
  element_previews: DocumentElementPreview[];
}

export interface InterviewSessionListResponse {
  interview_sessions: InterviewSession[];
}

export interface InterviewPreviewTurn {
  turn_index: number;
  speaker: "stakeholder" | "assistant";
  text: string;
}

export interface FinalizedTranscript extends Transcript {
  status: "finalized";
  finalized_at: string;
  content_hash: string;
}

export interface InterviewPreviewResponse {
  interview_session: InterviewSession;
  transcript: FinalizedTranscript;
  turns: InterviewPreviewTurn[];
}

export interface InsightStatusResponse {
  run: InsightRun;
}

export interface InsightRunListResponse {
  runs: InsightRun[];
}

export interface BoundingBox {
  x0: number;
  y0: number;
  x1: number;
  y1: number;
  coordinate_space: "points" | "pixels" | "normalized";
}

export type SourceLocation =
  | {
      kind: "pdf_page";
      filename: string;
      page: number;
      bounding_box: BoundingBox | null;
    }
  | {
      kind: "docx_rendered_page";
      filename: string;
      rendered_page: number;
      section: string | null;
      paragraph: number | null;
      bounding_box: BoundingBox | null;
    }
  | {
      kind: "pptx_slide";
      filename: string;
      slide: number;
      shape_identifier: string | null;
      bounding_box: BoundingBox | null;
    }
  | {
      kind: "xlsx_range";
      filename: string;
      sheet: string;
      cell_range: string;
      chart_identifier: string | null;
      image_identifier: string | null;
    }
  | {
      kind: "image_region";
      filename: string;
      image_index: number | null;
      region: string | null;
      bounding_box: BoundingBox | null;
    }
  | {
      kind: "transcript_turns";
      stakeholder_id: string;
      transcript_id: string;
      turn_start: number;
      turn_end: number;
    };

export interface ReportClaim {
  claim_id: string;
  statement: string;
  evidence_ids: string[];
}

export interface ResearchedTopicOutcome {
  topic_id: string;
  title: string;
  status: "completed" | "failed" | "insufficient_evidence";
  summary: string;
  evidence_ids: string[];
}

export interface ResponsibilityFinding {
  claim_id: string;
  responsibility: string;
  attribution: string;
  uncertainty: string;
  evidence_ids: string[];
}

export interface OperationalRisk {
  claim_id: string;
  risk: string;
  impact: string;
  responsibility_context: string;
  uncertainty: string;
  evidence_ids: string[];
}

export type BuyInCategory =
  | "confirmed_support"
  | "conditional_support"
  | "expressed_concern"
  | "insufficient_evidence"
  | "topic_not_discussed";

export interface BuyInSignal {
  topic: string;
  stakeholder_id: string | null;
  role: string | null;
  department: string | null;
  category: BuyInCategory;
  explanation: string;
  evidence_ids: string[];
}

export interface ContradictionSide {
  statement: string;
  stakeholder_id: string | null;
  role: string | null;
  department: string | null;
  evidence_ids: string[];
}

export interface Contradiction {
  topic: string;
  side_a: ContradictionSide;
  side_b: ContradictionSide;
  interpretation: string;
  evidence_ids: string[];
}

export interface EvidenceGap {
  topic: string;
  description: string;
  impact: string;
}

export interface FollowUpRecommendation {
  recommendation: string;
  rationale: string;
  evidence_ids: string[];
}

export interface Citation {
  citation_id: string;
  evidence_id: string;
  display_label: string;
  source_location: SourceLocation;
  claim_ids: string[];
}

export interface RunMetadata {
  run_id: string;
  started_at: string;
  completed_at: string;
  primary_model_id: string;
  fallback_model_id: string;
  topic_count: number;
  status_detail: string;
}

export interface InsightExecutionMetrics {
  run_id: string;
  engagement_id: string;
  thread_id: string;
  started_at: string;
  completed_at: string;
  status: "complete" | "partial" | "insufficient_evidence" | "failed";
  duration_ms: number;
  topic_count: number;
  researcher_calls: number;
  max_concurrent_researchers: number;
  model_calls: number;
  model_failures: number;
  tool_calls: number;
  tool_failures: number;
  retrieval_calls: number;
  retry_count: number;
  timeout_count: number;
  rerank_candidates_total: number;
  max_rerank_candidates_per_call: number;
  retrieval_latency_ms: number;
  reranker_latency_ms: number;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  configured_topic_limit: number;
  configured_parallel_researcher_limit: number;
  configured_model_call_limit: number;
  configured_tool_call_limit: number;
  configured_retrieval_calls_per_researcher_limit: number;
  configured_rerank_candidate_limit: number;
  configured_provider_timeout_seconds: number;
  configured_run_timeout_seconds: number;
  source_ids: string[];
  evidence_ids: string[];
  tool_names: string[];
  failure_code: string | null;
  correlation_id: string;
}

export interface InsightReport {
  report_id: string;
  engagement_id: string;
  question: string;
  status: "complete" | "partial" | "insufficient_evidence";
  executive_summary: string;
  researched_topics: ResearchedTopicOutcome[];
  findings: ReportClaim[];
  responsibilities: ResponsibilityFinding[];
  operational_risks: OperationalRisk[];
  buy_in_signals: BuyInSignal[];
  contradictions: Contradiction[];
  evidence_gaps: EvidenceGap[];
  open_questions: string[];
  follow_up_recommendations: FollowUpRecommendation[];
  evidence_ids: string[];
  citations: Citation[];
  run_metadata: RunMetadata;
}

export interface InsightReportResponse {
  run: InsightRun;
  report: InsightReport;
  metrics: InsightExecutionMetrics;
}

export interface EvidenceRecord {
  evidence_id: string;
  run_id: string;
  engagement_id: string;
  topic_id: string;
  source_id: string;
  source_version_id: string;
  source_type: "stakeholder_document" | "engagement_document" | "interview";
  stakeholder_id: string | null;
  location: SourceLocation;
  original_excerpt: string;
  english_interpretation: string | null;
  content_hash: string;
  researcher_id: string;
  created_at: string;
}

export interface SourceArtifactSummary {
  artifact_id: string;
  artifact_kind: string;
  media_type: string;
  content_hash: string;
  download_path: string | null;
}

export interface EvidenceDrillDownResponse {
  evidence: EvidenceRecord;
  original: SourceArtifactSummary;
  related_artifacts: SourceArtifactSummary[];
}
