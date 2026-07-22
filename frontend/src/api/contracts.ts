export type PrincipalType = "pm" | "stakeholder";
export type IngestionState =
  | "RECEIVED"
  | "VALIDATING"
  | "EXTRACTING"
  | "ENRICHING"
  | "INDEXING"
  | "READY"
  | "FAILED"
  | "SUPERSEDED";

export interface BrowserSessionView {
  principal_type: PrincipalType;
  access_session_id: string;
  expires_at: string;
  engagement_id: string | null;
  stakeholder_id: string | null;
  interview_session_id: string | null;
  thread_id: string | null;
}

export interface ApiErrorDetail {
  code: string;
  message: string;
  correlation_id: string;
}

export interface ApiErrorResponse {
  error: ApiErrorDetail;
}

export interface OperationResponse {
  status: "ok";
}

export interface Engagement {
  engagement_id: string;
  name: string;
  description: string | null;
  status: "active" | "archived";
  created_at: string;
  updated_at: string;
}

export interface Stakeholder {
  stakeholder_id: string;
  engagement_id: string;
  display_name: string;
  role: string | null;
  department: string | null;
  status: "active" | "revoked";
  created_at: string;
  updated_at: string;
}

export interface InvitationSummary {
  invitation_id: string;
  engagement_id: string;
  stakeholder_id: string;
  status: "active" | "activated" | "expired" | "revoked";
  created_at: string;
  expires_at: string;
  activated_at: string | null;
  revoked_at: string | null;
}

export interface DocumentSource {
  document_id: string;
  engagement_id: string;
  stakeholder_id: string | null;
  role: string | null;
  department: string | null;
  doc_type: string;
  source_type: "stakeholder_document" | "engagement_document";
  original_filename: string;
  media_type: string;
  created_at: string;
}

export interface DocumentVersion {
  document_version_id: string;
  document_id: string;
  version_number: number;
  content_hash: string;
  state: IngestionState;
  is_active: boolean;
  original_artifact_id: string;
  ingestion_key: string;
  created_at: string;
  ready_at: string | null;
  superseded_at: string | null;
  failure_code: string | null;
  failure_message: string | null;
}

export interface DocumentSummary {
  source: DocumentSource;
  latest_version: DocumentVersion;
}

export interface InterviewSession {
  interview_session_id: string;
  engagement_id: string;
  stakeholder_id: string;
  invitation_id: string;
  thread_id: string;
  status: "draft" | "finalizing" | "finalized" | "ingesting" | "ready" | "failed";
  started_at: string;
  finalized_at: string | null;
  transcript_id: string | null;
  ingestion_version_id: string | null;
  failure_code: string | null;
  failure_message: string | null;
}

export interface Transcript {
  transcript_id: string;
  interview_session_id: string;
  engagement_id: string;
  stakeholder_id: string;
  role: string | null;
  department: string | null;
  status: "draft" | "finalized";
  language_observations: string[];
  finalized_at: string | null;
  content_hash: string | null;
}

export interface TranscriptIngestionVersion {
  transcript_ingestion_version_id: string;
  transcript_id: string;
  content_hash: string;
  state: "RECEIVED" | "INDEXING" | "READY" | "FAILED" | "SUPERSEDED";
  is_active: boolean;
  created_at: string;
  ready_at: string | null;
  failure_code: string | null;
  failure_message: string | null;
}

export type InsightRunStatus =
  | "queued"
  | "planning"
  | "researching"
  | "editing"
  | "validating"
  | "complete"
  | "partial"
  | "insufficient_evidence"
  | "failed";

export interface InsightRun {
  run_id: string;
  engagement_id: string;
  thread_id: string;
  status: InsightRunStatus;
  requested_question: string;
  plan_id: string | null;
  report_id: string | null;
  failure_code: string | null;
  failure_message: string | null;
  started_at: string;
  completed_at: string | null;
}

export interface SafeRunEvent {
  event_id: string;
  occurred_at: string;
  actor: string;
  action: string;
  from_status: string | null;
  to_status: string | null;
  topic_id: string | null;
  source_ids: string[];
  evidence_ids: string[];
  artifact_name: string | null;
  failure_code: string | null;
  correlation_id: string;
}

export interface SafeFailureEvent {
  status: "failed";
  failure_code: string;
  failure_message: string;
  correlation_id: string;
}

export interface InterviewStatusEvent {
  stage: "interview";
  status: "started" | "succeeded";
  message_id: string;
  correlation_id: string;
}

export interface InterviewMessageEvent {
  message_id: string;
  stakeholder_turn_index: number;
  assistant_turn_index: number;
  assistant_text: string;
  correlation_id: string;
}

export interface InterviewTokenEvent {
  message_id: string;
  sequence: number;
  delta: string;
  correlation_id: string;
}

export type InterviewStreamEvent =
  | { event: "status"; data: InterviewStatusEvent }
  | { event: "token"; data: InterviewTokenEvent }
  | { event: "message"; data: InterviewMessageEvent }
  | { event: "failure"; data: SafeFailureEvent & { stage: "interview" } };

export type InsightStreamEvent =
  { event: "progress"; data: SafeRunEvent } | { event: "failure"; data: SafeFailureEvent };
