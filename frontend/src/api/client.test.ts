import { describe, expect, it } from "vitest";

import { ApiClient, apiRequestFailure } from "./client";
import { asRecord } from "./validation";

describe("ApiClient", () => {
  it("uses same-origin cookies, CSRF protection, and never forwards Authorization", async () => {
    let capturedInput: string | URL | Request | undefined;
    let capturedInit: RequestInit | undefined;
    const fetchImplementation: typeof fetch = (input, init) => {
      capturedInput = input;
      capturedInit = init;
      return Promise.resolve(
        new Response('{"status":"ok"}', {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      );
    };
    const client = new ApiClient(fetchImplementation);

    await client.json(
      "/api/v1/browser/auth/logout",
      { method: "POST", headers: { Authorization: "Bearer prohibited" } },
      asRecord,
    );

    expect(capturedInput).toBe("/api/v1/browser/auth/logout");
    expect(capturedInit?.credentials).toBe("same-origin");
    expect(capturedInit?.cache).toBe("no-store");
    const headers = new Headers(capturedInit?.headers);
    expect(headers.get("Authorization")).toBeNull();
    expect(headers.get("X-Stakeholder-CSRF")).toBe("1");
  });

  it("invokes receiver-sensitive browser fetch with the global object", async () => {
    let receiverWasGlobal = false;
    const fetchImplementation: typeof fetch = function (this: unknown) {
      receiverWasGlobal = this === globalThis;
      if (!receiverWasGlobal) {
        return Promise.reject(new TypeError("Illegal invocation"));
      }
      return Promise.resolve(new Response(null, { status: 204 }));
    };
    const client = new ApiClient(fetchImplementation);

    await expect(client.request("/api/v1/browser/auth/session")).resolves.toHaveProperty(
      "status",
      204,
    );
    expect(receiverWasGlobal).toBe(true);
  });

  it("rejects external and non-API destinations before fetching", async () => {
    let calls = 0;
    const fetchImplementation: typeof fetch = () => {
      calls += 1;
      return Promise.reject(new Error("should not execute"));
    };
    const client = new ApiClient(fetchImplementation);

    await expect(client.request("https://example.test/api/v1/session")).rejects.toThrow(
      /same-origin/u,
    );
    expect(calls).toBe(0);
  });

  it("does not add CSRF headers to safe reads and preserves an explicit Accept header", async () => {
    let capturedHeaders = new Headers();
    const fetchImplementation: typeof fetch = (_input, init) => {
      capturedHeaders = new Headers(init?.headers);
      return Promise.resolve(new Response(null, { status: 204 }));
    };
    const client = new ApiClient(fetchImplementation);

    await client.request("/api/v1/browser/auth/session", {
      headers: { Accept: "text/event-stream" },
    });

    expect(capturedHeaders.get("Accept")).toBe("text/event-stream");
    expect(capturedHeaders.get("X-Stakeholder-CSRF")).toBeNull();
  });

  it("preserves the safe server error envelope", async () => {
    const fetchImplementation: typeof fetch = () =>
      Promise.resolve(
        new Response(
          JSON.stringify({
            error: {
              code: "ACCESS_DENIED",
              message: "Access is not authorized.",
              correlation_id: "correlation_1",
            },
          }),
          { status: 403, headers: { "Content-Type": "application/json" } },
        ),
      );
    const client = new ApiClient(fetchImplementation);

    await expect(client.json("/api/v1/browser/auth/session", {}, asRecord)).rejects.toMatchObject({
      status: 403,
      detail: {
        code: "ACCESS_DENIED",
        message: "Access is not authorized.",
        correlation_id: "correlation_1",
      },
    });
  });

  it("recognizes only a structurally valid cross-realm API request failure", () => {
    expect(
      apiRequestFailure({
        name: "ApiRequestError",
        status: 403,
        detail: {
          code: "ACCESS_DENIED",
          message: "Access is not authorized.",
          correlation_id: "correlation-safe",
        },
      }),
    ).toEqual({
      status: 403,
      detail: {
        code: "ACCESS_DENIED",
        message: "Access is not authorized.",
        correlation_id: "correlation-safe",
      },
    });
    expect(apiRequestFailure({ name: "DifferentError", status: 403 })).toBeNull();
    expect(apiRequestFailure({ name: "ApiRequestError", status: 999 })).toBeNull();
  });

  it("falls back to a generic safe error for a malformed error response", async () => {
    const fetchImplementation: typeof fetch = () =>
      Promise.resolve(
        new Response("not-json", { status: 500, headers: { "Content-Type": "text/plain" } }),
      );
    const client = new ApiClient(fetchImplementation);

    await expect(client.stream("/api/v1/stakeholder/interview/turns/stream", {})).rejects.toEqual(
      expect.objectContaining({ status: 500, detail: null }),
    );
    await expect(client.json("/api/v1/browser/auth/session", {}, asRecord)).rejects.toEqual(
      expect.objectContaining({ status: 500, detail: null }),
    );
  });

  it("accepts only a non-empty event-stream body", async () => {
    const goodClient = new ApiClient(() =>
      Promise.resolve(
        new Response("event: status\ndata: {}\n\n", {
          headers: { "Content-Type": "text/event-stream; charset=utf-8" },
        }),
      ),
    );
    const stream = await goodClient.stream("/api/v1/pm/engagements/alpha/insights/run/events", {});
    const reader = stream.getReader();
    expect(reader).toBeDefined();
    reader.releaseLock();

    const wrongTypeClient = new ApiClient(() =>
      Promise.resolve(new Response("{}", { headers: { "Content-Type": "application/json" } })),
    );
    await expect(
      wrongTypeClient.stream("/api/v1/pm/engagements/alpha/insights/run/events", {}),
    ).rejects.toThrow(/approved event stream/u);
  });

  it("rejects a successful JSON response with an unexpected content type", async () => {
    const client = new ApiClient(() =>
      Promise.resolve(new Response("{}", { headers: { "Content-Type": "text/plain" } })),
    );

    await expect(client.json("/api/v1/browser/auth/session", {}, asRecord)).rejects.toThrow(
      /unexpected content type/u,
    );
  });
});
