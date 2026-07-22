import type {
  BrowserSessionView,
  DocumentSummary,
  InterviewSession,
  Stakeholder,
  Transcript,
  TranscriptIngestionVersion,
} from "../api/contracts";
import type {
  InterviewHistoryTurn,
  InterviewContextResponse,
  InterviewFinishResponse,
  InterviewStatusResponse,
  StakeholderScope,
  StakeholderUploadResponse,
} from "../stakeholder/contracts";
import { engagement, laterTimestamp, timestamp } from "./pm-fixtures";

export const activeStakeholder: Stakeholder = {
  stakeholder_id: "stakeholder-alpha",
  engagement_id: engagement.engagement_id,
  display_name: "Alex Morgan",
  role: "Operations lead",
  department: "Operations",
  status: "active",
  created_at: timestamp,
  updated_at: timestamp,
};

export const draftInterview: InterviewSession = {
  interview_session_id: "interview-alpha",
  engagement_id: engagement.engagement_id,
  stakeholder_id: activeStakeholder.stakeholder_id,
  invitation_id: "invitation-alpha",
  thread_id: "thread-alpha",
  status: "draft",
  started_at: timestamp,
  finalized_at: null,
  transcript_id: "transcript-alpha",
  ingestion_version_id: null,
  failure_code: null,
  failure_message: null,
};

export const readyInterview: InterviewSession = {
  ...draftInterview,
  status: "ready",
  finalized_at: laterTimestamp,
  ingestion_version_id: "transcript-version-alpha",
};

export const draftTranscript: Transcript = {
  transcript_id: "transcript-alpha",
  interview_session_id: draftInterview.interview_session_id,
  engagement_id: engagement.engagement_id,
  stakeholder_id: activeStakeholder.stakeholder_id,
  role: activeStakeholder.role,
  department: activeStakeholder.department,
  status: "draft",
  language_observations: [],
  finalized_at: null,
  content_hash: null,
};

export const finalizedTranscript: Transcript = {
  ...draftTranscript,
  status: "finalized",
  finalized_at: laterTimestamp,
  content_hash: "b".repeat(64),
};

export const readyIngestion: TranscriptIngestionVersion = {
  transcript_ingestion_version_id: "transcript-version-alpha",
  transcript_id: finalizedTranscript.transcript_id,
  content_hash: finalizedTranscript.content_hash ?? "b".repeat(64),
  state: "READY",
  is_active: true,
  created_at: laterTimestamp,
  ready_at: laterTimestamp,
  failure_code: null,
  failure_message: null,
};

export const stakeholderSession: BrowserSessionView = {
  principal_type: "stakeholder",
  access_session_id: "session-safe",
  expires_at: "2026-07-16T08:00:00Z",
  engagement_id: engagement.engagement_id,
  stakeholder_id: activeStakeholder.stakeholder_id,
  interview_session_id: draftInterview.interview_session_id,
  thread_id: draftInterview.thread_id,
};

export const interviewContext: InterviewContextResponse = {
  engagement,
  stakeholder: activeStakeholder,
  interview_session: draftInterview,
};

export const stakeholderScope: StakeholderScope = {
  engagementId: engagement.engagement_id,
  stakeholderId: activeStakeholder.stakeholder_id,
  interviewSessionId: draftInterview.interview_session_id,
  threadId: draftInterview.thread_id,
  role: activeStakeholder.role,
  department: activeStakeholder.department,
};

export const stakeholderDocument: DocumentSummary = {
  source: {
    document_id: "stakeholder-document-alpha",
    engagement_id: engagement.engagement_id,
    stakeholder_id: activeStakeholder.stakeholder_id,
    role: activeStakeholder.role,
    department: activeStakeholder.department,
    doc_type: "pdf",
    source_type: "stakeholder_document",
    original_filename: "supporting-evidence.pdf",
    media_type: "application/pdf",
    created_at: timestamp,
  },
  latest_version: {
    document_version_id: "stakeholder-document-version-alpha",
    document_id: "stakeholder-document-alpha",
    version_number: 1,
    content_hash: "a".repeat(64),
    state: "READY",
    is_active: true,
    original_artifact_id: "stakeholder-artifact-alpha",
    ingestion_key: "stakeholder-ingestion-alpha",
    created_at: timestamp,
    ready_at: laterTimestamp,
    superseded_at: null,
    failure_code: null,
    failure_message: null,
  },
};

export const stakeholderUpload: StakeholderUploadResponse = {
  document: stakeholderDocument,
  element_count: 4,
  chunk_count: 3,
  attempt_id: "attempt-alpha",
  idempotent: false,
};

export const openingQuestion =
  "What are the main tasks you personally perform in your day-to-day work as Operations lead?";

export const openingTurn: InterviewHistoryTurn = {
  turn_index: 0,
  speaker: "assistant",
  text: openingQuestion,
};

export const completedDraftTurns: InterviewHistoryTurn[] = [
  openingTurn,
  {
    turn_index: 1,
    speaker: "stakeholder",
    text: "I coordinate the weekly operations review.",
  },
  {
    turn_index: 2,
    speaker: "assistant",
    text: "Which decisions do you personally approve during the weekly operations review?",
  },
];

export const draftStatus: InterviewStatusResponse = {
  interview_session: draftInterview,
  transcript: draftTranscript,
  ingestion_version: null,
  turns: [openingTurn],
  turn_count: 1,
  completion_recommended: false,
};

export const readyStatus: InterviewStatusResponse = {
  interview_session: readyInterview,
  transcript: finalizedTranscript,
  ingestion_version: readyIngestion,
  turns: completedDraftTurns,
  turn_count: completedDraftTurns.length,
  completion_recommended: true,
};

export const finishResponse: InterviewFinishResponse = {
  interview_session: readyInterview,
  transcript: finalizedTranscript,
  ingestion_version: readyIngestion,
  chunk_count: 2,
  idempotent: false,
};
