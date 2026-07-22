import { describe, expect, it } from "vitest";

import { ApiClient } from "../api/client";
import {
  documentProcessingDetails,
  documentSummary,
  engagement,
  evidenceResponse,
  interviewPreview,
  interviewSession,
  invitation,
  reportResponse,
  revokedInvitation,
  safeRunEvent,
  stakeholder,
  terminalRun,
  uploadResponse,
} from "../test/pm-fixtures";
import { HttpPmApi } from "./pm-api";

interface CapturedCall {
  path: string;
  init: RequestInit;
}

function jsonResponse(value: unknown, status = 200): Response {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function apiHarness() {
  const calls: CapturedCall[] = [];
  const fetchImplementation: typeof fetch = (input, init = {}) => {
    const path = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    const method = init.method ?? "GET";
    calls.push({ path, init });

    if (path.endsWith("/events")) {
      return Promise.resolve(
        new Response(`event: progress\ndata: ${JSON.stringify(safeRunEvent)}\n\n`, {
          headers: { "Content-Type": "text/event-stream" },
        }),
      );
    }
    if (path.endsWith("/report")) {
      return Promise.resolve(jsonResponse(reportResponse()));
    }
    if (path.includes("/evidence/evidence-beta")) {
      return Promise.resolve(jsonResponse(evidenceResponse));
    }
    if (path.endsWith("/insights/run-alpha")) {
      return Promise.resolve(jsonResponse({ run: terminalRun("complete") }));
    }
    if (path.endsWith("/insights") && method === "GET") {
      return Promise.resolve(jsonResponse({ runs: [terminalRun("complete")] }));
    }
    if (path.endsWith("/insights") && method === "POST") {
      return Promise.resolve(jsonResponse({ run: terminalRun("complete") }, 202));
    }
    if (path.endsWith("/interviews/interview-alpha")) {
      return Promise.resolve(jsonResponse(interviewPreview));
    }
    if (path.endsWith("/interviews")) {
      return Promise.resolve(jsonResponse({ interview_sessions: [interviewSession] }));
    }
    if (path.endsWith("/documents/document-alpha/processing")) {
      return Promise.resolve(jsonResponse(documentProcessingDetails));
    }
    if (path.endsWith("/documents/document-alpha") && method === "DELETE") {
      return Promise.resolve(jsonResponse({ status: "ok" }));
    }
    if (path.endsWith("/documents") && method === "POST") {
      return Promise.resolve(jsonResponse(uploadResponse, 201));
    }
    if (path.endsWith("/documents")) {
      return Promise.resolve(jsonResponse({ documents: [documentSummary] }));
    }
    if (path.endsWith("/invitations/invitation-alpha/link")) {
      return Promise.resolve(jsonResponse({ invitation, invitation_token: "A".repeat(48) }));
    }
    if (path.endsWith("/invitations/invitation-alpha") && method === "DELETE") {
      return Promise.resolve(jsonResponse(revokedInvitation));
    }
    if (path.endsWith("/stakeholders/stakeholder-alpha/invitations")) {
      return Promise.resolve(jsonResponse({ invitation, invitation_token: "A".repeat(48) }, 201));
    }
    if (path.endsWith("/invitations")) {
      return Promise.resolve(jsonResponse({ invitations: [invitation] }));
    }
    if (path.endsWith("/stakeholders") && method === "POST") {
      return Promise.resolve(jsonResponse({ stakeholder }, 201));
    }
    if (path.endsWith("/stakeholders")) {
      return Promise.resolve(jsonResponse({ stakeholders: [stakeholder] }));
    }
    if (path.endsWith("/select") || (path !== "/api/v1/pm/engagements" && method === "GET")) {
      return Promise.resolve(jsonResponse({ engagement }));
    }
    if (path === "/api/v1/pm/engagements" && method === "POST") {
      return Promise.resolve(jsonResponse({ engagement }, 201));
    }
    return Promise.resolve(jsonResponse({ engagements: [engagement] }));
  };
  return { api: new HttpPmApi(new ApiClient(fetchImplementation)), calls };
}

describe("HttpPmApi", () => {
  it("uses the canonical PM routes for setup, invitation, document, and interview operations", async () => {
    const { api, calls } = apiHarness();

    await expect(api.listEngagements()).resolves.toEqual({ ok: true, value: [engagement] });
    await expect(
      api.createEngagement({ name: engagement.name, description: engagement.description }),
    ).resolves.toEqual({ ok: true, value: engagement });
    await expect(api.selectEngagement(engagement.engagement_id)).resolves.toEqual({
      ok: true,
      value: engagement,
    });
    await expect(api.getEngagement(engagement.engagement_id)).resolves.toEqual({
      ok: true,
      value: engagement,
    });
    await expect(api.listStakeholders(engagement.engagement_id)).resolves.toEqual({
      ok: true,
      value: [stakeholder],
    });
    await expect(
      api.createStakeholder(engagement.engagement_id, {
        display_name: stakeholder.display_name,
        role: stakeholder.role,
        department: stakeholder.department,
      }),
    ).resolves.toEqual({ ok: true, value: stakeholder });
    await expect(api.listInvitations(engagement.engagement_id)).resolves.toEqual({
      ok: true,
      value: [invitation],
    });
    await expect(
      api.issueInvitation(engagement.engagement_id, stakeholder.stakeholder_id),
    ).resolves.toEqual({
      ok: true,
      value: { invitation, invitation_token: "A".repeat(48) },
    });
    await expect(
      api.getInvitationLink(engagement.engagement_id, invitation.invitation_id),
    ).resolves.toEqual({
      ok: true,
      value: { invitation, invitation_token: "A".repeat(48) },
    });
    await expect(
      api.revokeInvitation(engagement.engagement_id, invitation.invitation_id),
    ).resolves.toEqual({ ok: true, value: revokedInvitation });
    await expect(api.listDocuments(engagement.engagement_id)).resolves.toEqual({
      ok: true,
      value: [documentSummary],
    });
    await expect(
      api.deleteDocument(engagement.engagement_id, documentSummary.source.document_id),
    ).resolves.toEqual({ ok: true, value: { status: "ok" } });
    await expect(
      api.getDocumentProcessing(engagement.engagement_id, documentSummary.source.document_id),
    ).resolves.toEqual({
      ok: true,
      value: documentProcessingDetails,
    });
    await expect(api.listInterviews(engagement.engagement_id)).resolves.toEqual({
      ok: true,
      value: [interviewSession],
    });
    await expect(
      api.getInterviewPreview(engagement.engagement_id, interviewSession.interview_session_id),
    ).resolves.toEqual({ ok: true, value: interviewPreview });

    expect(calls.map(({ path, init }) => `${init.method ?? "GET"} ${path}`)).toEqual([
      "GET /api/v1/pm/engagements",
      "POST /api/v1/pm/engagements",
      "POST /api/v1/pm/engagements/engagement-alpha/select",
      "GET /api/v1/pm/engagements/engagement-alpha",
      "GET /api/v1/pm/engagements/engagement-alpha/stakeholders",
      "POST /api/v1/pm/engagements/engagement-alpha/stakeholders",
      "GET /api/v1/pm/engagements/engagement-alpha/invitations",
      "POST /api/v1/pm/engagements/engagement-alpha/stakeholders/stakeholder-alpha/invitations",
      "GET /api/v1/pm/engagements/engagement-alpha/invitations/invitation-alpha/link",
      "DELETE /api/v1/pm/engagements/engagement-alpha/invitations/invitation-alpha",
      "GET /api/v1/pm/engagements/engagement-alpha/documents",
      "DELETE /api/v1/pm/engagements/engagement-alpha/documents/document-alpha",
      "GET /api/v1/pm/engagements/engagement-alpha/documents/document-alpha/processing",
      "GET /api/v1/pm/engagements/engagement-alpha/interviews",
      "GET /api/v1/pm/engagements/engagement-alpha/interviews/interview-alpha",
    ]);
    for (const call of calls.filter(({ init }) => init.method !== "GET")) {
      expect(new Headers(call.init.headers).get("X-Stakeholder-CSRF")).toBe("1");
    }
  });

  it("uploads multipart bytes without overriding the browser content type", async () => {
    const { api, calls } = apiHarness();
    const file = new File(["safe test bytes"], "source.pdf", { type: "application/pdf" });

    await expect(api.uploadDocument(engagement.engagement_id, file)).resolves.toEqual({
      ok: true,
      value: uploadResponse,
    });

    const call = calls.at(0);
    expect(call).toBeDefined();
    if (call === undefined) {
      throw new Error("The upload request was not captured.");
    }
    expect(call.path).toBe("/api/v1/pm/engagements/engagement-alpha/documents");
    expect(call.init.body).toBeInstanceOf(FormData);
    expect((call.init.body as FormData).get("upload")).toBeInstanceOf(File);
    expect(new Headers(call.init.headers).get("Content-Type")).toBeNull();
  });

  it("submits insights, parses allowlisted SSE, and resolves bound reports and evidence", async () => {
    const { api } = apiHarness();
    const controller = new AbortController();

    await expect(
      api.createInsight(engagement.engagement_id, "Where are the operating-model risks?"),
    ).resolves.toEqual({ ok: true, value: terminalRun("complete") });
    await expect(api.listInsights(engagement.engagement_id)).resolves.toEqual({
      ok: true,
      value: [terminalRun("complete")],
    });
    await expect(api.getInsightStatus(engagement.engagement_id, "run-alpha")).resolves.toEqual({
      ok: true,
      value: terminalRun("complete"),
    });
    const events = [];
    for await (const event of api.streamInsight(
      engagement.engagement_id,
      "run-alpha",
      controller.signal,
    )) {
      events.push(event);
    }
    expect(events).toEqual([{ event: "progress", data: safeRunEvent }]);
    await expect(api.getInsightReport(engagement.engagement_id, "run-alpha")).resolves.toEqual({
      ok: true,
      value: reportResponse(),
    });
    await expect(
      api.getEvidence(engagement.engagement_id, "run-alpha", "evidence-beta"),
    ).resolves.toEqual({ ok: true, value: evidenceResponse });
    expect(
      api.artifactDownloadPath(
        engagement.engagement_id,
        "run-alpha",
        "evidence-beta",
        "artifact-original",
      ),
    ).toBe(
      "/api/v1/pm/engagements/engagement-alpha/insights/run-alpha/evidence/evidence-beta/artifacts/artifact-original",
    );
  });

  it("encodes path segments and rejects evidence or server download-path identity changes", async () => {
    const { api, calls } = apiHarness();
    await expect(api.getEngagement("engagement/foreign")).rejects.toThrow(/requested scope/u);
    expect(calls[0]?.path).toContain("engagement%2Fforeign");
    expect(() => api.artifactDownloadPath("", "run", "evidence", "artifact")).toThrow(
      /identifier/u,
    );

    const identityChanged = structuredClone(evidenceResponse);
    identityChanged.evidence.engagement_id = "engagement-foreign";
    const identityApi = new HttpPmApi(
      new ApiClient(() => Promise.resolve(jsonResponse(identityChanged))),
    );
    await expect(
      identityApi.getEvidence(engagement.engagement_id, "run-alpha", "evidence-beta"),
    ).rejects.toThrow(/contract/u);

    const interviewIdentityChanged = structuredClone(interviewPreview);
    interviewIdentityChanged.interview_session.engagement_id = "engagement-foreign";
    interviewIdentityChanged.transcript.engagement_id = "engagement-foreign";
    const interviewIdentityApi = new HttpPmApi(
      new ApiClient(() => Promise.resolve(jsonResponse(interviewIdentityChanged))),
    );
    await expect(
      interviewIdentityApi.getInterviewPreview(
        engagement.engagement_id,
        interviewSession.interview_session_id,
      ),
    ).rejects.toThrow(/requested scope/u);

    const pathChanged = structuredClone(evidenceResponse);
    pathChanged.original.download_path = "/api/v1/pm/engagements/foreign/private";
    const pathApi = new HttpPmApi(new ApiClient(() => Promise.resolve(jsonResponse(pathChanged))));
    await expect(
      pathApi.getEvidence(engagement.engagement_id, "run-alpha", "evidence-beta"),
    ).rejects.toThrow(/artifact path/u);

    const documentPathChanged = structuredClone(documentProcessingDetails);
    const firstArtifact = documentPathChanged.artifacts.at(0);
    if (firstArtifact === undefined) {
      throw new Error("The document processing fixture requires one artifact.");
    }
    firstArtifact.download_path = "/api/v1/pm/engagements/foreign/documents/document-alpha/private";
    const documentPathApi = new HttpPmApi(
      new ApiClient(() => Promise.resolve(jsonResponse(documentPathChanged))),
    );
    await expect(
      documentPathApi.getDocumentProcessing(
        engagement.engagement_id,
        documentSummary.source.document_id,
      ),
    ).rejects.toThrow(/document artifact path/u);
    expect(
      api.documentArtifactPath(
        engagement.engagement_id,
        documentSummary.source.document_id,
        "artifact-original",
      ),
    ).toBe(
      "/api/v1/pm/engagements/engagement-alpha/documents/document-alpha/artifacts/artifact-original",
    );
  });

  it("preserves safe API failures across every result-returning PM operation", async () => {
    const failure = {
      error: {
        code: "SERVICE_UNAVAILABLE",
        message: "The service is temporarily unavailable.",
        correlation_id: "correlation-safe-failure",
      },
    };
    const api = new HttpPmApi(new ApiClient(() => Promise.resolve(jsonResponse(failure, 503))));
    const file = new File(["safe test bytes"], "source.pdf", { type: "application/pdf" });
    const operations = [
      api.listEngagements(),
      api.createEngagement({ name: "Alpha", description: null }),
      api.selectEngagement("engagement-alpha"),
      api.getEngagement("engagement-alpha"),
      api.listStakeholders("engagement-alpha"),
      api.createStakeholder("engagement-alpha", {
        display_name: "Alex Morgan",
        role: null,
        department: null,
      }),
      api.listInvitations("engagement-alpha"),
      api.issueInvitation("engagement-alpha", "stakeholder-alpha"),
      api.getInvitationLink("engagement-alpha", "invitation-alpha"),
      api.revokeInvitation("engagement-alpha", "invitation-alpha"),
      api.listDocuments("engagement-alpha"),
      api.uploadDocument("engagement-alpha", file),
      api.getDocumentProcessing("engagement-alpha", "document-alpha"),
      api.listInterviews("engagement-alpha"),
      api.getInterviewPreview("engagement-alpha", "interview-alpha"),
      api.createInsight("engagement-alpha", "Where are the risks?"),
      api.listInsights("engagement-alpha"),
      api.getInsightStatus("engagement-alpha", "run-alpha"),
      api.getInsightReport("engagement-alpha", "run-alpha"),
      api.getEvidence("engagement-alpha", "run-alpha", "evidence-alpha"),
    ];

    for (const operation of operations) {
      await expect(operation).resolves.toEqual({
        ok: false,
        status: 503,
        detail: failure.error,
      });
    }
  });
});
