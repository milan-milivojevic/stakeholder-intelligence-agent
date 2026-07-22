import { describe, expect, it } from "vitest";

import { ApiClient } from "../api/client";
import {
  draftStatus,
  finishResponse,
  interviewContext,
  stakeholderDocument,
  stakeholderScope,
  stakeholderSession,
  stakeholderUpload,
} from "../test/stakeholder-fixtures";
import { HttpStakeholderApi } from "./stakeholder-api";

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
    calls.push({ path, init });
    if (path.endsWith("/context")) {
      return Promise.resolve(jsonResponse(interviewContext));
    }
    if (path.endsWith("/documents") && init.method === "POST") {
      return Promise.resolve(jsonResponse(stakeholderUpload, 201));
    }
    if (path.includes("/stakeholder/documents/") && init.method === "DELETE") {
      return Promise.resolve(jsonResponse({ status: "ok" }));
    }
    if (path.endsWith("/documents")) {
      return Promise.resolve(jsonResponse({ documents: [stakeholderDocument] }));
    }
    if (path.endsWith("/turns/stream")) {
      const messageId = "message-alpha";
      return Promise.resolve(
        new Response(
          [
            `event: status\ndata: ${JSON.stringify({
              stage: "interview",
              status: "started",
              message_id: messageId,
              correlation_id: "correlation-alpha",
            })}\n\n`,
            `event: message\ndata: ${JSON.stringify({
              message_id: messageId,
              stakeholder_turn_index: 4,
              assistant_turn_index: 5,
              assistant_text: "What happens next?",
              correlation_id: "correlation-alpha",
            })}\n\n`,
            `event: status\ndata: ${JSON.stringify({
              stage: "interview",
              status: "succeeded",
              message_id: messageId,
              correlation_id: "correlation-alpha",
            })}\n\n`,
          ].join(""),
          { headers: { "Content-Type": "text/event-stream" } },
        ),
      );
    }
    if (path.includes("/interview/turns/") && init.method === "DELETE") {
      return Promise.resolve(jsonResponse(draftStatus));
    }
    if (path.endsWith("/finish")) {
      return Promise.resolve(jsonResponse(finishResponse));
    }
    return Promise.resolve(jsonResponse(draftStatus));
  };
  return { api: new HttpStakeholderApi(new ApiClient(fetchImplementation)), calls };
}

describe("HttpStakeholderApi", () => {
  it("uses only fixed stakeholder routes and binds every response to server context", async () => {
    const { api, calls } = apiHarness();
    await expect(api.getContext(stakeholderSession)).resolves.toEqual({
      ok: true,
      value: { context: interviewContext, scope: stakeholderScope },
    });
    await expect(api.listDocuments(stakeholderScope)).resolves.toEqual({
      ok: true,
      value: [stakeholderDocument],
    });
    await expect(
      api.deleteDocument(stakeholderScope, stakeholderDocument.source.document_id),
    ).resolves.toEqual({ ok: true, value: { status: "ok" } });
    await expect(api.startInterview(stakeholderScope)).resolves.toEqual({
      ok: true,
      value: draftStatus,
    });
    await expect(api.getInterviewStatus(stakeholderScope)).resolves.toEqual({
      ok: true,
      value: draftStatus,
    });
    await expect(api.deleteAnswer(stakeholderScope, 1)).resolves.toEqual({
      ok: true,
      value: draftStatus,
    });
    await expect(api.finishInterview(stakeholderScope)).resolves.toEqual({
      ok: true,
      value: finishResponse,
    });

    expect(calls.map(({ path, init }) => `${init.method ?? "GET"} ${path}`)).toEqual([
      "GET /api/v1/stakeholder/context",
      "GET /api/v1/stakeholder/documents",
      "DELETE /api/v1/stakeholder/documents/stakeholder-document-alpha",
      "POST /api/v1/stakeholder/interview/start",
      "GET /api/v1/stakeholder/interview/status",
      "DELETE /api/v1/stakeholder/interview/turns/1",
      "POST /api/v1/stakeholder/interview/finish",
    ]);
    expect(new Headers(calls.at(-1)?.init.headers).get("X-Stakeholder-CSRF")).toBe("1");
    expect(calls.every(({ init }) => !new Headers(init.headers).has("Authorization"))).toBe(true);
  });

  it("uploads multipart bytes and streams allowlisted events under one message identity", async () => {
    const { api, calls } = apiHarness();
    const file = new File(["safe bytes"], "supporting.pdf", { type: "application/pdf" });
    await expect(api.uploadDocument(stakeholderScope, file)).resolves.toEqual({
      ok: true,
      value: stakeholderUpload,
    });
    const events = [];
    for await (const event of api.streamInterviewTurn(
      stakeholderScope,
      "I own the weekly review.",
      "message-alpha",
      new AbortController().signal,
    )) {
      events.push(event);
    }
    expect(events.map((event) => event.event)).toEqual(["status", "message", "status"]);

    const uploadCall = calls[0];
    const streamCall = calls[1];
    expect(uploadCall?.init.body).toBeInstanceOf(FormData);
    expect(new Headers(uploadCall?.init.headers).get("Content-Type")).toBeNull();
    expect(streamCall?.init.body).toBe(
      JSON.stringify({
        original_text: "I own the weekly review.",
        message_id: "message-alpha",
      }),
    );
    expect(new Headers(streamCall?.init.headers).get("X-Stakeholder-CSRF")).toBe("1");
  });

  it("rejects context, role, department, transcript, and message identity changes", async () => {
    const foreignContext = structuredClone(interviewContext);
    foreignContext.interview_session.thread_id = "thread-foreign";
    const contextApi = new HttpStakeholderApi(
      new ApiClient(() => Promise.resolve(jsonResponse(foreignContext))),
    );
    await expect(contextApi.getContext(stakeholderSession)).rejects.toThrow(/fixed stakeholder/u);

    const foreignDocument = structuredClone(stakeholderDocument);
    foreignDocument.source.department = "Finance";
    const documentApi = new HttpStakeholderApi(
      new ApiClient(() => Promise.resolve(jsonResponse({ documents: [foreignDocument] }))),
    );
    await expect(documentApi.listDocuments(stakeholderScope)).rejects.toThrow(/fixed stakeholder/u);

    const foreignStatus = structuredClone(draftStatus);
    if (foreignStatus.transcript !== null) {
      foreignStatus.transcript.stakeholder_id = "stakeholder-foreign";
    }
    const statusApi = new HttpStakeholderApi(
      new ApiClient(() => Promise.resolve(jsonResponse(foreignStatus))),
    );
    await expect(statusApi.getInterviewStatus(stakeholderScope)).rejects.toThrow(
      /fixed stakeholder/u,
    );

    const streamApi = new HttpStakeholderApi(
      new ApiClient(() =>
        Promise.resolve(
          new Response(
            `event: message\ndata: ${JSON.stringify({
              message_id: "message-foreign",
              stakeholder_turn_index: 0,
              assistant_turn_index: 1,
              assistant_text: "Unsafe identity change",
              correlation_id: "correlation-alpha",
            })}\n\n`,
            { headers: { "Content-Type": "text/event-stream" } },
          ),
        ),
      ),
    );
    const read = async () => {
      for await (const event of streamApi.streamInterviewTurn(
        stakeholderScope,
        "Response",
        "message-alpha",
        new AbortController().signal,
      )) {
        // Reading the generator executes the binding check.
        expect(event).toBeDefined();
      }
    };
    await expect(read()).rejects.toThrow(/message identity/u);
  });
});
