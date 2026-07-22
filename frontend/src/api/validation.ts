import type {
  ApiErrorResponse,
  BrowserSessionView,
  OperationResponse,
  PrincipalType,
  SafeFailureEvent,
  SafeRunEvent,
} from "./contracts";

const forbiddenSecretFields = new Set([
  "access_token",
  "authorization",
  "bootstrap_token",
  "cookie",
  "invitation_token",
  "token_hash",
]);

export class ContractError extends Error {
  constructor(message = "The server response did not match the approved contract.") {
    super(message);
    this.name = "ContractError";
  }
}

export function asRecord(value: unknown): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new ContractError();
  }
  return value as Record<string, unknown>;
}

export function requiredString(record: Record<string, unknown>, key: string): string {
  const value = record[key];
  if (typeof value !== "string" || value.length === 0) {
    throw new ContractError();
  }
  return value;
}

export function nullableString(record: Record<string, unknown>, key: string): string | null {
  const value = record[key];
  if (value === null) {
    return null;
  }
  if (typeof value !== "string" || value.length === 0) {
    throw new ContractError();
  }
  return value;
}

export function requiredNumber(record: Record<string, unknown>, key: string): number {
  const value = record[key];
  if (typeof value !== "number" || !Number.isFinite(value)) {
    throw new ContractError();
  }
  return value;
}

export function requiredInteger(record: Record<string, unknown>, key: string): number {
  const value = requiredNumber(record, key);
  if (!Number.isInteger(value)) {
    throw new ContractError();
  }
  return value;
}

export function requiredBoolean(record: Record<string, unknown>, key: string): boolean {
  const value = record[key];
  if (typeof value !== "boolean") {
    throw new ContractError();
  }
  return value;
}

export function requiredArray(record: Record<string, unknown>, key: string): unknown[] {
  const value = record[key];
  if (!Array.isArray(value)) {
    throw new ContractError();
  }
  return value;
}

export function requiredStringArray(record: Record<string, unknown>, key: string): string[] {
  const value = record[key];
  if (!Array.isArray(value) || value.some((item) => typeof item !== "string")) {
    throw new ContractError();
  }
  return value as string[];
}

export function requireExactKeys(
  record: Record<string, unknown>,
  expectedKeys: readonly string[],
): void {
  const actualKeys = Object.keys(record).sort();
  const expected = [...expectedKeys].sort();
  if (
    actualKeys.length !== expected.length ||
    actualKeys.some((key, index) => key !== expected[index])
  ) {
    throw new ContractError();
  }
}

export function requireIsoTimestamp(value: string): string {
  if (Number.isNaN(Date.parse(value))) {
    throw new ContractError();
  }
  return value;
}

function requirePrincipalType(value: string): PrincipalType {
  if (value !== "pm" && value !== "stakeholder") {
    throw new ContractError();
  }
  return value;
}

function rejectSecretFields(record: Record<string, unknown>): void {
  for (const key of Object.keys(record)) {
    if (forbiddenSecretFields.has(key.toLowerCase())) {
      throw new ContractError("A browser response contained a prohibited secret field.");
    }
  }
}

export function parseBrowserSessionView(value: unknown): BrowserSessionView {
  const record = asRecord(value);
  rejectSecretFields(record);
  requireExactKeys(record, [
    "principal_type",
    "access_session_id",
    "expires_at",
    "engagement_id",
    "stakeholder_id",
    "interview_session_id",
    "thread_id",
  ]);
  const principalType = requirePrincipalType(requiredString(record, "principal_type"));
  const session: BrowserSessionView = {
    principal_type: principalType,
    access_session_id: requiredString(record, "access_session_id"),
    expires_at: requireIsoTimestamp(requiredString(record, "expires_at")),
    engagement_id: nullableString(record, "engagement_id"),
    stakeholder_id: nullableString(record, "stakeholder_id"),
    interview_session_id: nullableString(record, "interview_session_id"),
    thread_id: nullableString(record, "thread_id"),
  };

  if (
    principalType === "stakeholder" &&
    (session.engagement_id === null ||
      session.stakeholder_id === null ||
      session.interview_session_id === null ||
      session.thread_id === null)
  ) {
    throw new ContractError();
  }
  if (
    principalType === "pm" &&
    (session.stakeholder_id !== null ||
      session.interview_session_id !== null ||
      session.thread_id !== null)
  ) {
    throw new ContractError();
  }
  return session;
}

export function parseApiErrorResponse(value: unknown): ApiErrorResponse {
  const record = asRecord(value);
  requireExactKeys(record, ["error"]);
  const error = asRecord(record.error);
  requireExactKeys(error, ["code", "message", "correlation_id"]);
  return {
    error: {
      code: requiredString(error, "code"),
      message: requiredString(error, "message"),
      correlation_id: requiredString(error, "correlation_id"),
    },
  };
}

export function parseOperationResponse(value: unknown): OperationResponse {
  const record = asRecord(value);
  requireExactKeys(record, ["status"]);
  if (record.status !== "ok") {
    throw new ContractError();
  }
  return { status: "ok" };
}

export function parseSafeFailureEvent(value: unknown): SafeFailureEvent {
  const record = asRecord(value);
  if (record.status !== "failed") {
    throw new ContractError();
  }
  return {
    status: "failed",
    failure_code: requiredString(record, "failure_code"),
    failure_message: requiredString(record, "failure_message"),
    correlation_id: requiredString(record, "correlation_id"),
  };
}

export function parseSafeRunEvent(value: unknown): SafeRunEvent {
  const record = asRecord(value);
  return {
    event_id: requiredString(record, "event_id"),
    occurred_at: requireIsoTimestamp(requiredString(record, "occurred_at")),
    actor: requiredString(record, "actor"),
    action: requiredString(record, "action"),
    from_status: nullableString(record, "from_status"),
    to_status: nullableString(record, "to_status"),
    topic_id: nullableString(record, "topic_id"),
    source_ids: requiredStringArray(record, "source_ids"),
    evidence_ids: requiredStringArray(record, "evidence_ids"),
    artifact_name: nullableString(record, "artifact_name"),
    failure_code: nullableString(record, "failure_code"),
    correlation_id: requiredString(record, "correlation_id"),
  };
}
