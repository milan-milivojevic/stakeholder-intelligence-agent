import type {
  DocumentSummary,
  Engagement,
  InterviewSession,
  Stakeholder,
  Transcript,
  TranscriptIngestionVersion,
} from "../api/contracts";

export interface InterviewContextResponse {
  engagement: Engagement;
  stakeholder: Stakeholder;
  interview_session: InterviewSession;
}

export interface StakeholderDocumentListResponse {
  documents: DocumentSummary[];
}

export interface StakeholderUploadResponse {
  document: DocumentSummary;
  element_count: number;
  chunk_count: number;
  attempt_id: string | null;
  idempotent: boolean;
}

export interface InterviewStatusResponse {
  interview_session: InterviewSession;
  transcript: Transcript | null;
  ingestion_version: TranscriptIngestionVersion | null;
  turns: InterviewHistoryTurn[];
  turn_count: number;
  completion_recommended: boolean;
}

export interface InterviewHistoryTurn {
  turn_index: number;
  speaker: "stakeholder" | "assistant";
  text: string;
}

export interface InterviewFinishResponse {
  interview_session: InterviewSession;
  transcript: Transcript;
  ingestion_version: TranscriptIngestionVersion;
  chunk_count: number;
  idempotent: boolean;
}

export interface StakeholderScope {
  engagementId: string;
  stakeholderId: string;
  interviewSessionId: string;
  threadId: string;
  role: string | null;
  department: string | null;
}

export interface ResolvedStakeholderContext {
  context: InterviewContextResponse;
  scope: StakeholderScope;
}
