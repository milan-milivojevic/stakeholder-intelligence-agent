import { ApiClient } from "../api/client";
import type { ApiResult } from "../api/client";
import type {
  Engagement,
  InsightRun,
  InsightStreamEvent,
  InterviewSession,
  InvitationSummary,
  OperationResponse,
  Stakeholder,
  DocumentSummary,
} from "../api/contracts";
import { decodeSseStream, parseInsightStreamEvent } from "../api/sse";
import { ContractError, parseOperationResponse } from "../api/validation";
import type {
  DocumentProcessingDetailsResponse,
  EvidenceDrillDownResponse,
  InterviewPreviewResponse,
  InsightReportResponse,
  InvitationIssuedResponse,
  InvitationLinkResponse,
  UploadResponse,
} from "./contracts";
import {
  parseDocumentListResponse,
  parseDocumentProcessingDetailsResponse,
  parseEngagementContextResponse,
  parseEngagementListResponse,
  parseEvidenceDrillDownResponse,
  parseInsightReportResponse,
  parseInsightRunListResponse,
  parseInsightStatusResponse,
  parseInterviewSessionListResponse,
  parseInterviewPreviewResponse,
  parseInvitationIssuedResponse,
  parseInvitationListResponse,
  parseInvitationSummary,
  parseStakeholderListResponse,
  parseStakeholderResponse,
  parseUploadResponse,
} from "./validation";

export interface EngagementCreateInput {
  name: string;
  description: string | null;
}

export interface StakeholderCreateInput {
  display_name: string;
  role: string | null;
  department: string | null;
}

export interface PmApi {
  listEngagements(): Promise<ApiResult<Engagement[]>>;
  createEngagement(input: EngagementCreateInput): Promise<ApiResult<Engagement>>;
  selectEngagement(engagementId: string): Promise<ApiResult<Engagement>>;
  getEngagement(engagementId: string): Promise<ApiResult<Engagement>>;
  listStakeholders(engagementId: string): Promise<ApiResult<Stakeholder[]>>;
  createStakeholder(
    engagementId: string,
    input: StakeholderCreateInput,
  ): Promise<ApiResult<Stakeholder>>;
  listInvitations(engagementId: string): Promise<ApiResult<InvitationSummary[]>>;
  issueInvitation(
    engagementId: string,
    stakeholderId: string,
  ): Promise<ApiResult<InvitationIssuedResponse>>;
  getInvitationLink(
    engagementId: string,
    invitationId: string,
  ): Promise<ApiResult<InvitationLinkResponse>>;
  revokeInvitation(
    engagementId: string,
    invitationId: string,
  ): Promise<ApiResult<InvitationSummary>>;
  listDocuments(engagementId: string): Promise<ApiResult<DocumentSummary[]>>;
  uploadDocument(engagementId: string, file: File): Promise<ApiResult<UploadResponse>>;
  deleteDocument(engagementId: string, documentId: string): Promise<ApiResult<OperationResponse>>;
  getDocumentProcessing(
    engagementId: string,
    documentId: string,
  ): Promise<ApiResult<DocumentProcessingDetailsResponse>>;
  documentArtifactPath(engagementId: string, documentId: string, artifactId: string): string;
  listInterviews(engagementId: string): Promise<ApiResult<InterviewSession[]>>;
  getInterviewPreview(
    engagementId: string,
    interviewSessionId: string,
  ): Promise<ApiResult<InterviewPreviewResponse>>;
  listInsights(engagementId: string): Promise<ApiResult<InsightRun[]>>;
  createInsight(engagementId: string, question: string): Promise<ApiResult<InsightRun>>;
  getInsightStatus(engagementId: string, runId: string): Promise<ApiResult<InsightRun>>;
  streamInsight(
    engagementId: string,
    runId: string,
    signal: AbortSignal,
  ): AsyncIterable<InsightStreamEvent>;
  getInsightReport(engagementId: string, runId: string): Promise<ApiResult<InsightReportResponse>>;
  getEvidence(
    engagementId: string,
    runId: string,
    evidenceId: string,
  ): Promise<ApiResult<EvidenceDrillDownResponse>>;
  artifactDownloadPath(
    engagementId: string,
    runId: string,
    evidenceId: string,
    artifactId: string,
  ): string;
}

function segment(value: string): string {
  if (value.length === 0) {
    throw new TypeError("A resource identifier is required.");
  }
  return encodeURIComponent(value);
}

function engagementPath(engagementId: string): string {
  return `/api/v1/pm/engagements/${segment(engagementId)}`;
}

function documentPath(engagementId: string, documentId: string): string {
  return `${engagementPath(engagementId)}/documents/${segment(documentId)}`;
}

function documentArtifactPath(
  engagementId: string,
  documentId: string,
  artifactId: string,
): string {
  return `${documentPath(engagementId, documentId)}/artifacts/${segment(artifactId)}`;
}

function jsonBody(value: unknown): RequestInit {
  return {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(value),
  };
}

function artifactPath(
  engagementId: string,
  runId: string,
  evidenceId: string,
  artifactId: string,
): string {
  return `${engagementPath(engagementId)}/insights/${segment(runId)}/evidence/${segment(
    evidenceId,
  )}/artifacts/${segment(artifactId)}`;
}

function verifyEvidenceBinding(
  response: EvidenceDrillDownResponse,
  engagementId: string,
  runId: string,
  evidenceId: string,
): EvidenceDrillDownResponse {
  if (
    response.evidence.engagement_id !== engagementId ||
    response.evidence.run_id !== runId ||
    response.evidence.evidence_id !== evidenceId
  ) {
    throw new ContractError();
  }
  for (const artifact of [response.original, ...response.related_artifacts]) {
    const expected = artifactPath(engagementId, runId, evidenceId, artifact.artifact_id);
    if (artifact.download_path !== null && artifact.download_path !== expected) {
      throw new ContractError("The server returned an unapproved artifact path.");
    }
  }
  return response;
}

function verifyDocumentProcessingBinding(
  response: DocumentProcessingDetailsResponse,
  engagementId: string,
  documentId: string,
): DocumentProcessingDetailsResponse {
  requireBinding(response.document.source.engagement_id, engagementId);
  requireBinding(response.document.source.document_id, documentId);
  requireBinding(response.document.latest_version.document_id, documentId);
  const versionId = response.document.latest_version.document_version_id;
  for (const element of response.element_previews) {
    requireBinding(element.document_version_id, versionId);
  }
  for (const artifact of response.artifacts) {
    const expected = documentArtifactPath(engagementId, documentId, artifact.artifact_id);
    if (artifact.download_path !== expected) {
      throw new ContractError("The server returned an unapproved document artifact path.");
    }
  }
  if (
    response.document.latest_version.state === "READY" &&
    !response.artifacts.some(
      (artifact) =>
        artifact.artifact_id === response.document.latest_version.original_artifact_id &&
        artifact.artifact_kind === "original",
    )
  ) {
    throw new ContractError("A ready document response omitted its original artifact.");
  }
  return response;
}

function requireBinding(actual: string, expected: string): void {
  if (actual !== expected) {
    throw new ContractError("The server returned a resource outside the requested scope.");
  }
}

export class HttpPmApi implements PmApi {
  readonly #client: ApiClient;

  constructor(client: ApiClient = new ApiClient()) {
    this.#client = client;
  }

  async listEngagements(): Promise<ApiResult<Engagement[]>> {
    const result = await this.#client.result(
      "/api/v1/pm/engagements",
      {},
      parseEngagementListResponse,
    );
    return result.ok ? { ok: true, value: result.value.engagements } : result;
  }

  async createEngagement(input: EngagementCreateInput): Promise<ApiResult<Engagement>> {
    const result = await this.#client.result(
      "/api/v1/pm/engagements",
      jsonBody(input),
      parseEngagementContextResponse,
    );
    return result.ok ? { ok: true, value: result.value.engagement } : result;
  }

  async selectEngagement(engagementId: string): Promise<ApiResult<Engagement>> {
    const result = await this.#client.result(
      `${engagementPath(engagementId)}/select`,
      { method: "POST" },
      parseEngagementContextResponse,
    );
    if (!result.ok) {
      return result;
    }
    requireBinding(result.value.engagement.engagement_id, engagementId);
    return { ok: true, value: result.value.engagement };
  }

  async getEngagement(engagementId: string): Promise<ApiResult<Engagement>> {
    const result = await this.#client.result(
      engagementPath(engagementId),
      {},
      parseEngagementContextResponse,
    );
    if (!result.ok) {
      return result;
    }
    requireBinding(result.value.engagement.engagement_id, engagementId);
    return { ok: true, value: result.value.engagement };
  }

  async listStakeholders(engagementId: string): Promise<ApiResult<Stakeholder[]>> {
    const result = await this.#client.result(
      `${engagementPath(engagementId)}/stakeholders`,
      {},
      parseStakeholderListResponse,
    );
    if (!result.ok) {
      return result;
    }
    for (const stakeholder of result.value.stakeholders) {
      requireBinding(stakeholder.engagement_id, engagementId);
    }
    return { ok: true, value: result.value.stakeholders };
  }

  async createStakeholder(
    engagementId: string,
    input: StakeholderCreateInput,
  ): Promise<ApiResult<Stakeholder>> {
    const result = await this.#client.result(
      `${engagementPath(engagementId)}/stakeholders`,
      jsonBody(input),
      parseStakeholderResponse,
    );
    if (!result.ok) {
      return result;
    }
    requireBinding(result.value.stakeholder.engagement_id, engagementId);
    return { ok: true, value: result.value.stakeholder };
  }

  async listInvitations(engagementId: string): Promise<ApiResult<InvitationSummary[]>> {
    const result = await this.#client.result(
      `${engagementPath(engagementId)}/invitations`,
      {},
      parseInvitationListResponse,
    );
    if (!result.ok) {
      return result;
    }
    for (const invitation of result.value.invitations) {
      requireBinding(invitation.engagement_id, engagementId);
    }
    return { ok: true, value: result.value.invitations };
  }

  async issueInvitation(
    engagementId: string,
    stakeholderId: string,
  ): Promise<ApiResult<InvitationIssuedResponse>> {
    const result = await this.#client.result(
      `${engagementPath(engagementId)}/stakeholders/${segment(stakeholderId)}/invitations`,
      { method: "POST" },
      parseInvitationIssuedResponse,
    );
    if (!result.ok) {
      return result;
    }
    requireBinding(result.value.invitation.engagement_id, engagementId);
    requireBinding(result.value.invitation.stakeholder_id, stakeholderId);
    return result;
  }

  async getInvitationLink(
    engagementId: string,
    invitationId: string,
  ): Promise<ApiResult<InvitationLinkResponse>> {
    const result = await this.#client.result(
      `${engagementPath(engagementId)}/invitations/${segment(invitationId)}/link`,
      {},
      parseInvitationIssuedResponse,
    );
    if (!result.ok) {
      return result;
    }
    requireBinding(result.value.invitation.engagement_id, engagementId);
    requireBinding(result.value.invitation.invitation_id, invitationId);
    return result;
  }

  async revokeInvitation(
    engagementId: string,
    invitationId: string,
  ): Promise<ApiResult<InvitationSummary>> {
    const result = await this.#client.result(
      `${engagementPath(engagementId)}/invitations/${segment(invitationId)}`,
      { method: "DELETE" },
      parseInvitationSummary,
    );
    if (!result.ok) {
      return result;
    }
    requireBinding(result.value.engagement_id, engagementId);
    requireBinding(result.value.invitation_id, invitationId);
    return result;
  }

  async listDocuments(engagementId: string): Promise<ApiResult<DocumentSummary[]>> {
    const result = await this.#client.result(
      `${engagementPath(engagementId)}/documents`,
      {},
      parseDocumentListResponse,
    );
    if (!result.ok) {
      return result;
    }
    for (const document of result.value.documents) {
      requireBinding(document.source.engagement_id, engagementId);
    }
    return { ok: true, value: result.value.documents };
  }

  async uploadDocument(engagementId: string, file: File): Promise<ApiResult<UploadResponse>> {
    const body = new FormData();
    body.set("upload", file, file.name);
    const result = await this.#client.result(
      `${engagementPath(engagementId)}/documents`,
      { method: "POST", body },
      parseUploadResponse,
    );
    if (!result.ok) {
      return result;
    }
    requireBinding(result.value.document.source.engagement_id, engagementId);
    return result;
  }

  async deleteDocument(
    engagementId: string,
    documentId: string,
  ): Promise<ApiResult<OperationResponse>> {
    return await this.#client.result(
      documentPath(engagementId, documentId),
      { method: "DELETE" },
      parseOperationResponse,
    );
  }

  async getDocumentProcessing(
    engagementId: string,
    documentId: string,
  ): Promise<ApiResult<DocumentProcessingDetailsResponse>> {
    const result = await this.#client.result(
      `${documentPath(engagementId, documentId)}/processing`,
      {},
      parseDocumentProcessingDetailsResponse,
    );
    if (!result.ok) {
      return result;
    }
    return {
      ok: true,
      value: verifyDocumentProcessingBinding(result.value, engagementId, documentId),
    };
  }

  documentArtifactPath(engagementId: string, documentId: string, artifactId: string): string {
    return documentArtifactPath(engagementId, documentId, artifactId);
  }

  async listInterviews(engagementId: string): Promise<ApiResult<InterviewSession[]>> {
    const result = await this.#client.result(
      `${engagementPath(engagementId)}/interviews`,
      {},
      parseInterviewSessionListResponse,
    );
    if (!result.ok) {
      return result;
    }
    for (const interview of result.value.interview_sessions) {
      requireBinding(interview.engagement_id, engagementId);
    }
    return { ok: true, value: result.value.interview_sessions };
  }

  async getInterviewPreview(
    engagementId: string,
    interviewSessionId: string,
  ): Promise<ApiResult<InterviewPreviewResponse>> {
    const result = await this.#client.result(
      `${engagementPath(engagementId)}/interviews/${segment(interviewSessionId)}`,
      {},
      parseInterviewPreviewResponse,
    );
    if (!result.ok) {
      return result;
    }
    requireBinding(result.value.interview_session.engagement_id, engagementId);
    requireBinding(result.value.interview_session.interview_session_id, interviewSessionId);
    return result;
  }

  async createInsight(engagementId: string, question: string): Promise<ApiResult<InsightRun>> {
    const result = await this.#client.result(
      `${engagementPath(engagementId)}/insights`,
      jsonBody({ question }),
      parseInsightStatusResponse,
    );
    if (!result.ok) {
      return result;
    }
    requireBinding(result.value.run.engagement_id, engagementId);
    return { ok: true, value: result.value.run };
  }

  async listInsights(engagementId: string): Promise<ApiResult<InsightRun[]>> {
    const result = await this.#client.result(
      `${engagementPath(engagementId)}/insights`,
      {},
      parseInsightRunListResponse,
    );
    if (!result.ok) {
      return result;
    }
    for (const run of result.value.runs) {
      requireBinding(run.engagement_id, engagementId);
    }
    return { ok: true, value: result.value.runs };
  }

  async getInsightStatus(engagementId: string, runId: string): Promise<ApiResult<InsightRun>> {
    const result = await this.#client.result(
      `${engagementPath(engagementId)}/insights/${segment(runId)}`,
      {},
      parseInsightStatusResponse,
    );
    if (!result.ok) {
      return result;
    }
    requireBinding(result.value.run.engagement_id, engagementId);
    requireBinding(result.value.run.run_id, runId);
    return { ok: true, value: result.value.run };
  }

  async *streamInsight(
    engagementId: string,
    runId: string,
    signal: AbortSignal,
  ): AsyncIterable<InsightStreamEvent> {
    const stream = await this.#client.stream(
      `${engagementPath(engagementId)}/insights/${segment(runId)}/events`,
      { signal },
    );
    for await (const event of decodeSseStream(stream)) {
      yield parseInsightStreamEvent(event);
    }
  }

  async getInsightReport(
    engagementId: string,
    runId: string,
  ): Promise<ApiResult<InsightReportResponse>> {
    const result = await this.#client.result(
      `${engagementPath(engagementId)}/insights/${segment(runId)}/report`,
      {},
      parseInsightReportResponse,
    );
    if (!result.ok) {
      return result;
    }
    requireBinding(result.value.run.engagement_id, engagementId);
    requireBinding(result.value.run.run_id, runId);
    return result;
  }

  async getEvidence(
    engagementId: string,
    runId: string,
    evidenceId: string,
  ): Promise<ApiResult<EvidenceDrillDownResponse>> {
    const result = await this.#client.result(
      `${engagementPath(engagementId)}/insights/${segment(runId)}/evidence/${segment(evidenceId)}`,
      {},
      parseEvidenceDrillDownResponse,
    );
    if (!result.ok) {
      return result;
    }
    return {
      ok: true,
      value: verifyEvidenceBinding(result.value, engagementId, runId, evidenceId),
    };
  }

  artifactDownloadPath(
    engagementId: string,
    runId: string,
    evidenceId: string,
    artifactId: string,
  ): string {
    return artifactPath(engagementId, runId, evidenceId, artifactId);
  }
}

export const pmApi: PmApi = new HttpPmApi();
