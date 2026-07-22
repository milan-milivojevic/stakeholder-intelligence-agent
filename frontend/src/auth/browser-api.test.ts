import { describe, expect, it } from "vitest";

import type { BrowserSessionView } from "../api/contracts";
import { ApiClient } from "../api/client";
import { HttpBrowserSessionApi } from "./browser-api";

const pmSession: BrowserSessionView = {
  principal_type: "pm",
  access_session_id: "access-session-safe",
  expires_at: "2026-07-16T08:00:00Z",
  engagement_id: null,
  stakeholder_id: null,
  interview_session_id: null,
  thread_id: null,
};

describe("HttpBrowserSessionApi", () => {
  it("uses only the dedicated browser routes and closed JSON payloads", async () => {
    const requests: { path: string; init: RequestInit | undefined }[] = [];
    const fetchImplementation: typeof fetch = (input, init) => {
      const path =
        typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
      requests.push({ path, init });
      const body = path.includes("/logout?") ? { status: "ok" } : pmSession;
      return Promise.resolve(
        new Response(JSON.stringify(body), {
          headers: { "Content-Type": "application/json" },
        }),
      );
    };
    const api = new HttpBrowserSessionApi(new ApiClient(fetchImplementation));

    await expect(api.inspect("pm")).resolves.toEqual({ ok: true, value: pmSession });
    await expect(api.activatePm("p".repeat(32))).resolves.toEqual({
      ok: true,
      value: pmSession,
    });
    await expect(api.activateStakeholder("i".repeat(32))).resolves.toEqual({
      ok: true,
      value: pmSession,
    });
    await expect(api.logout("stakeholder")).resolves.toEqual({
      ok: true,
      value: { status: "ok" },
    });

    expect(requests.map(({ path }) => path)).toEqual([
      "/api/v1/browser/auth/session?principal=pm",
      "/api/v1/browser/auth/pm/activate",
      "/api/v1/browser/auth/stakeholder/activate",
      "/api/v1/browser/auth/logout?principal=stakeholder",
    ]);
    expect(requests[1]?.init?.body).toBe(JSON.stringify({ bootstrap_token: "p".repeat(32) }));
    expect(requests[2]?.init?.body).toBe(JSON.stringify({ invitation_token: "i".repeat(32) }));
    for (const request of requests) {
      expect(new Headers(request.init?.headers).get("Authorization")).toBeNull();
    }
  });
});
