import { ApiClient } from "../api/client";
import type { ApiResult } from "../api/client";
import type {
  BrowserSessionView,
  DocumentSummary,
  InterviewStreamEvent,
  OperationResponse,
} from "../api/contracts";
import { decodeSseStream, parseInterviewStreamEvent } from "../api/sse";
import { ContractError, parseOperationResponse } from "../api/validation";
import type {
  InterviewFinishResponse,
  InterviewStatusResponse,
  ResolvedStakeholderContext,
  StakeholderScope,
  StakeholderUploadResponse,
} from "./contracts";
import {
  parseInterviewContextResponse,
  parseInterviewFinishResponse,
  parseInterviewStatusResponse,
  parseStakeholderDocumentListResponse,
  parseStakeholderUploadResponse,
} from "./validation";

const contextPath = "/api/v1/stakeholder/context";
const documentsPath = "/api/v1/stakeholder/documents";
const interviewStartPath = "/api/v1/stakeholder/interview/start";
const interviewStatusPath = "/api/v1/stakeholder/interview/status";
const interviewTurnsPath = "/api/v1/stakeholder/interview/turns";
const interviewStreamPath = `${interviewTurnsPath}/stream`;
const interviewFinishPath = "/api/v1/stakeholder/interview/finish";

export interface StakeholderApi {
  getContext(session: BrowserSessionView): Promise<ApiResult<ResolvedStakeholderContext>>;
  listDocuments(scope: StakeholderScope): Promise<ApiResult<DocumentSummary[]>>;
  uploadDocument(
    scope: StakeholderScope,
    file: File,
  ): Promise<ApiResult<StakeholderUploadResponse>>;
  deleteDocument(
    scope: StakeholderScope,
    documentId: string,
  ): Promise<ApiResult<OperationResponse>>;
  startInterview(scope: StakeholderScope): Promise<ApiResult<InterviewStatusResponse>>;
  getInterviewStatus(scope: StakeholderScope): Promise<ApiResult<InterviewStatusResponse>>;
  deleteAnswer(
    scope: StakeholderScope,
    turnIndex: number,
  ): Promise<ApiResult<InterviewStatusResponse>>;
  streamInterviewTurn(
    scope: StakeholderScope,
    originalText: string,
    messageId: string,
    signal: AbortSignal,
  ): AsyncIterable<InterviewStreamEvent>;
  finishInterview(scope: StakeholderScope): Promise<ApiResult<InterviewFinishResponse>>;
}

function requireBinding(actual: string | null, expected: string | null): void {
  if (actual !== expected) {
    throw new ContractError("The server returned data outside the fixed stakeholder scope.");
  }
}

function requireSessionId(value: string | null): string {
  if (value === null || value.length === 0) {
    throw new ContractError("The stakeholder session is missing its fixed server context.");
  }
  return value;
}

function bindContext(
  session: BrowserSessionView,
  response: ReturnType<typeof parseInterviewContextResponse>,
): ResolvedStakeholderContext {
  if (session.principal_type !== "stakeholder") {
    throw new ContractError("A stakeholder session is required.");
  }
  const engagementId = requireSessionId(session.engagement_id);
  const stakeholderId = requireSessionId(session.stakeholder_id);
  const interviewSessionId = requireSessionId(session.interview_session_id);
  const threadId = requireSessionId(session.thread_id);
  requireBinding(response.engagement.engagement_id, engagementId);
  requireBinding(response.stakeholder.engagement_id, engagementId);
  requireBinding(response.stakeholder.stakeholder_id, stakeholderId);
  requireBinding(response.interview_session.engagement_id, engagementId);
  requireBinding(response.interview_session.stakeholder_id, stakeholderId);
  requireBinding(response.interview_session.interview_session_id, interviewSessionId);
  requireBinding(response.interview_session.thread_id, threadId);
  return {
    context: response,
    scope: {
      engagementId,
      stakeholderId,
      interviewSessionId,
      threadId,
      role: response.stakeholder.role,
      department: response.stakeholder.department,
    },
  };
}

function bindDocument(document: DocumentSummary, scope: StakeholderScope): void {
  requireBinding(document.source.engagement_id, scope.engagementId);
  requireBinding(document.source.stakeholder_id, scope.stakeholderId);
  requireBinding(document.source.role, scope.role);
  requireBinding(document.source.department, scope.department);
  if (document.source.source_type !== "stakeholder_document") {
    throw new ContractError("The server returned an unapproved document source.");
  }
}

function bindInterviewStatus(
  response: InterviewStatusResponse,
  scope: StakeholderScope,
): InterviewStatusResponse {
  const session = response.interview_session;
  requireBinding(session.engagement_id, scope.engagementId);
  requireBinding(session.stakeholder_id, scope.stakeholderId);
  requireBinding(session.interview_session_id, scope.interviewSessionId);
  requireBinding(session.thread_id, scope.threadId);
  if (response.transcript !== null) {
    requireBinding(response.transcript.engagement_id, scope.engagementId);
    requireBinding(response.transcript.stakeholder_id, scope.stakeholderId);
    requireBinding(response.transcript.interview_session_id, scope.interviewSessionId);
    requireBinding(response.transcript.role, scope.role);
    requireBinding(response.transcript.department, scope.department);
    requireBinding(session.transcript_id, response.transcript.transcript_id);
  }
  if (response.ingestion_version !== null) {
    if (response.transcript === null) {
      throw new ContractError("Transcript ingestion data requires a finalized transcript.");
    }
    requireBinding(response.ingestion_version.transcript_id, response.transcript.transcript_id);
    requireBinding(
      session.ingestion_version_id,
      response.ingestion_version.transcript_ingestion_version_id,
    );
  }
  if (response.turn_count !== response.turns.length) {
    throw new ContractError("The server returned incomplete interview history.");
  }
  return response;
}

function bindFinish(
  response: InterviewFinishResponse,
  scope: StakeholderScope,
): InterviewFinishResponse {
  bindInterviewStatus(
    {
      interview_session: response.interview_session,
      transcript: response.transcript,
      ingestion_version: response.ingestion_version,
      turns: [],
      turn_count: 0,
      completion_recommended: false,
    },
    scope,
  );
  if (response.transcript.status !== "finalized") {
    throw new ContractError("Finish did not return a finalized transcript.");
  }
  return response;
}

function jsonBody(value: unknown, signal?: AbortSignal): RequestInit {
  return {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(value),
    ...(signal === undefined ? {} : { signal }),
  };
}

export class HttpStakeholderApi implements StakeholderApi {
  readonly #client: ApiClient;

  constructor(client: ApiClient = new ApiClient()) {
    this.#client = client;
  }

  async getContext(session: BrowserSessionView): Promise<ApiResult<ResolvedStakeholderContext>> {
    const result = await this.#client.result(contextPath, {}, parseInterviewContextResponse);
    return result.ok ? { ok: true, value: bindContext(session, result.value) } : result;
  }

  async listDocuments(scope: StakeholderScope): Promise<ApiResult<DocumentSummary[]>> {
    const result = await this.#client.result(
      documentsPath,
      {},
      parseStakeholderDocumentListResponse,
    );
    if (!result.ok) {
      return result;
    }
    for (const document of result.value.documents) {
      bindDocument(document, scope);
    }
    return { ok: true, value: result.value.documents };
  }

  async uploadDocument(
    scope: StakeholderScope,
    file: File,
  ): Promise<ApiResult<StakeholderUploadResponse>> {
    const body = new FormData();
    body.set("upload", file, file.name);
    const result = await this.#client.result(
      documentsPath,
      { method: "POST", body },
      parseStakeholderUploadResponse,
    );
    if (!result.ok) {
      return result;
    }
    bindDocument(result.value.document, scope);
    return result;
  }

  async deleteDocument(
    _scope: StakeholderScope,
    documentId: string,
  ): Promise<ApiResult<OperationResponse>> {
    return this.#client.result(
      `${documentsPath}/${encodeURIComponent(documentId)}`,
      { method: "DELETE" },
      parseOperationResponse,
    );
  }

  async getInterviewStatus(scope: StakeholderScope): Promise<ApiResult<InterviewStatusResponse>> {
    const result = await this.#client.result(interviewStatusPath, {}, parseInterviewStatusResponse);
    return result.ok ? { ok: true, value: bindInterviewStatus(result.value, scope) } : result;
  }

  async startInterview(scope: StakeholderScope): Promise<ApiResult<InterviewStatusResponse>> {
    const result = await this.#client.result(
      interviewStartPath,
      { method: "POST" },
      parseInterviewStatusResponse,
    );
    return result.ok ? { ok: true, value: bindInterviewStatus(result.value, scope) } : result;
  }

  async deleteAnswer(
    scope: StakeholderScope,
    turnIndex: number,
  ): Promise<ApiResult<InterviewStatusResponse>> {
    const result = await this.#client.result(
      `${interviewTurnsPath}/${String(turnIndex)}`,
      { method: "DELETE" },
      parseInterviewStatusResponse,
    );
    return result.ok ? { ok: true, value: bindInterviewStatus(result.value, scope) } : result;
  }

  async *streamInterviewTurn(
    _scope: StakeholderScope,
    originalText: string,
    messageId: string,
    signal: AbortSignal,
  ): AsyncIterable<InterviewStreamEvent> {
    const stream = await this.#client.stream(
      interviewStreamPath,
      jsonBody({ original_text: originalText, message_id: messageId }, signal),
    );
    for await (const decoded of decodeSseStream(stream)) {
      const event = parseInterviewStreamEvent(decoded);
      if (event.event !== "failure" && event.data.message_id !== messageId) {
        throw new ContractError("The interview stream returned a different message identity.");
      }
      yield event;
    }
  }

  async finishInterview(scope: StakeholderScope): Promise<ApiResult<InterviewFinishResponse>> {
    const result = await this.#client.result(
      interviewFinishPath,
      { method: "POST" },
      parseInterviewFinishResponse,
    );
    return result.ok ? { ok: true, value: bindFinish(result.value, scope) } : result;
  }
}

export const stakeholderApi: StakeholderApi = new HttpStakeholderApi();
