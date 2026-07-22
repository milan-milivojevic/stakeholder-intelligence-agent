import type { Transcript, TranscriptIngestionVersion } from "../api/contracts";
import {
  asRecord,
  ContractError,
  nullableString,
  requireExactKeys,
  requireIsoTimestamp,
  requiredArray,
  requiredBoolean,
  requiredInteger,
  requiredString,
  requiredStringArray,
} from "../api/validation";
import {
  parseDocumentSummary,
  parseEngagement,
  parseInterviewSession,
  parseStakeholder,
} from "../pm/validation";
import type {
  InterviewContextResponse,
  InterviewFinishResponse,
  InterviewStatusResponse,
  StakeholderDocumentListResponse,
  StakeholderUploadResponse,
} from "./contracts";

function timestamp(record: Record<string, unknown>, key: string): string {
  return requireIsoTimestamp(requiredString(record, key));
}

function nullableTimestamp(record: Record<string, unknown>, key: string): string | null {
  const value = nullableString(record, key);
  return value === null ? null : requireIsoTimestamp(value);
}

function nonnegativeInteger(record: Record<string, unknown>, key: string): number {
  const value = requiredInteger(record, key);
  if (value < 0) {
    throw new ContractError();
  }
  return value;
}

function parseTranscript(value: unknown): Transcript {
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
  if (record.status !== "draft" && record.status !== "finalized") {
    throw new ContractError();
  }
  const contentHash = nullableString(record, "content_hash");
  if (contentHash !== null && !/^[a-f0-9]{64}$/u.test(contentHash)) {
    throw new ContractError();
  }
  return {
    transcript_id: requiredString(record, "transcript_id"),
    interview_session_id: requiredString(record, "interview_session_id"),
    engagement_id: requiredString(record, "engagement_id"),
    stakeholder_id: requiredString(record, "stakeholder_id"),
    role: nullableString(record, "role"),
    department: nullableString(record, "department"),
    status: record.status,
    language_observations: requiredStringArray(record, "language_observations"),
    finalized_at: nullableTimestamp(record, "finalized_at"),
    content_hash: contentHash,
  };
}

const transcriptIngestionStates = [
  "RECEIVED",
  "INDEXING",
  "READY",
  "FAILED",
  "SUPERSEDED",
] as const;

function parseTranscriptIngestionVersion(value: unknown): TranscriptIngestionVersion {
  const record = asRecord(value);
  requireExactKeys(record, [
    "transcript_ingestion_version_id",
    "transcript_id",
    "content_hash",
    "state",
    "is_active",
    "created_at",
    "ready_at",
    "failure_code",
    "failure_message",
  ]);
  if (
    typeof record.state !== "string" ||
    !transcriptIngestionStates.includes(record.state as (typeof transcriptIngestionStates)[number])
  ) {
    throw new ContractError();
  }
  const contentHash = requiredString(record, "content_hash");
  if (!/^[a-f0-9]{64}$/u.test(contentHash)) {
    throw new ContractError();
  }
  return {
    transcript_ingestion_version_id: requiredString(record, "transcript_ingestion_version_id"),
    transcript_id: requiredString(record, "transcript_id"),
    content_hash: contentHash,
    state: record.state as TranscriptIngestionVersion["state"],
    is_active: requiredBoolean(record, "is_active"),
    created_at: timestamp(record, "created_at"),
    ready_at: nullableTimestamp(record, "ready_at"),
    failure_code: nullableString(record, "failure_code"),
    failure_message: nullableString(record, "failure_message"),
  };
}

export function parseInterviewContextResponse(value: unknown): InterviewContextResponse {
  const record = asRecord(value);
  requireExactKeys(record, ["engagement", "stakeholder", "interview_session"]);
  return {
    engagement: parseEngagement(record.engagement),
    stakeholder: parseStakeholder(record.stakeholder),
    interview_session: parseInterviewSession(record.interview_session),
  };
}

export function parseStakeholderDocumentListResponse(
  value: unknown,
): StakeholderDocumentListResponse {
  const record = asRecord(value);
  requireExactKeys(record, ["documents"]);
  return {
    documents: requiredArray(record, "documents").map((item) => parseDocumentSummary(item)),
  };
}

export function parseStakeholderUploadResponse(value: unknown): StakeholderUploadResponse {
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
    element_count: nonnegativeInteger(record, "element_count"),
    chunk_count: nonnegativeInteger(record, "chunk_count"),
    attempt_id: nullableString(record, "attempt_id"),
    idempotent: requiredBoolean(record, "idempotent"),
  };
}

export function parseInterviewStatusResponse(value: unknown): InterviewStatusResponse {
  const record = asRecord(value);
  requireExactKeys(record, [
    "interview_session",
    "transcript",
    "ingestion_version",
    "turns",
    "turn_count",
    "completion_recommended",
  ]);
  const turns = requiredArray(record, "turns").map((item, index) => {
    const turn = asRecord(item);
    requireExactKeys(turn, ["turn_index", "speaker", "text"]);
    const speaker = requiredString(turn, "speaker");
    if (speaker !== "stakeholder" && speaker !== "assistant") {
      throw new ContractError("Interview history contains an invalid speaker.");
    }
    const typedSpeaker: "stakeholder" | "assistant" = speaker;
    const turnIndex = nonnegativeInteger(turn, "turn_index");
    if (turnIndex !== index) {
      throw new ContractError("Interview history is not in canonical order.");
    }
    return {
      turn_index: turnIndex,
      speaker: typedSpeaker,
      text: requiredString(turn, "text"),
    };
  });
  const turnCount = nonnegativeInteger(record, "turn_count");
  if (turnCount !== turns.length) {
    throw new ContractError("Interview history does not match its recorded turn count.");
  }
  return {
    interview_session: parseInterviewSession(record.interview_session),
    transcript: record.transcript === null ? null : parseTranscript(record.transcript),
    ingestion_version:
      record.ingestion_version === null
        ? null
        : parseTranscriptIngestionVersion(record.ingestion_version),
    turns,
    turn_count: turnCount,
    completion_recommended: requiredBoolean(record, "completion_recommended"),
  };
}

export function parseInterviewFinishResponse(value: unknown): InterviewFinishResponse {
  const record = asRecord(value);
  requireExactKeys(record, [
    "interview_session",
    "transcript",
    "ingestion_version",
    "chunk_count",
    "idempotent",
  ]);
  return {
    interview_session: parseInterviewSession(record.interview_session),
    transcript: parseTranscript(record.transcript),
    ingestion_version: parseTranscriptIngestionVersion(record.ingestion_version),
    chunk_count: nonnegativeInteger(record, "chunk_count"),
    idempotent: requiredBoolean(record, "idempotent"),
  };
}
