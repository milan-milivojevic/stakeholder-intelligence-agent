import { describe, expect, it, vi } from "vitest";

import type { BrowserSessionView } from "../api/contracts";
import type { BrowserSessionApi } from "./browser-api";
import { prepareBrowserBootstrap, settleSession } from "./bootstrap";

const pmSession: BrowserSessionView = {
  principal_type: "pm",
  access_session_id: "session-safe",
  expires_at: "2026-07-16T08:00:00Z",
  engagement_id: null,
  stakeholder_id: null,
  interview_session_id: null,
  thread_id: null,
};

function fakeApi(overrides: Partial<BrowserSessionApi> = {}): BrowserSessionApi {
  return {
    inspect: () => Promise.resolve({ ok: true, value: pmSession }),
    activatePm: () => Promise.resolve({ ok: true, value: pmSession }),
    activateStakeholder: () => Promise.resolve({ ok: true, value: pmSession }),
    logout: () => Promise.resolve({ ok: true, value: { status: "ok" } }),
    ...overrides,
  };
}

describe("secure browser bootstrap", () => {
  it("keeps the full invitation URL while activating the stakeholder workspace", async () => {
    const order: string[] = [];
    const invitationToken = "invitation_token-safe-1234567890_abcd";
    const api = fakeApi({
      activateStakeholder: (received) => {
        order.push(`activate:${received}`);
        return Promise.resolve({
          ok: true,
          value: { ...pmSession, principal_type: "stakeholder" },
        });
      },
    });
    const bootstrap = prepareBrowserBootstrap({ pathname: `/s/${invitationToken}` }, api);

    expect(order).toEqual([`activate:${invitationToken}`]);
    expect(bootstrap.route).toBe("stakeholder");
    expect(bootstrap.source).toBe("invitation");
    await expect(bootstrap.outcome).resolves.toMatchObject({ kind: "authenticated" });
  });

  it("inspects clean PM and stakeholder routes", async () => {
    const inspect = vi.fn(() => Promise.resolve({ ok: true as const, value: pmSession }));
    const api = fakeApi({ inspect });

    const pm = prepareBrowserBootstrap({ pathname: "/pm" }, api);
    const stakeholder = prepareBrowserBootstrap({ pathname: "/s" }, api);

    expect(pm.route).toBe("pm");
    expect(stakeholder.route).toBe("stakeholder");
    expect(inspect).toHaveBeenNthCalledWith(1, "pm");
    expect(inspect).toHaveBeenNthCalledWith(2, "stakeholder");
    await expect(pm.outcome).resolves.toMatchObject({ kind: "authenticated" });
  });

  it("rejects malformed invitation paths without sending them to the API", async () => {
    const activateStakeholder = vi.fn(() =>
      Promise.resolve({ ok: true as const, value: pmSession }),
    );
    const malformed = prepareBrowserBootstrap(
      { pathname: "/s/too-short" },
      fakeApi({ activateStakeholder }),
    );
    const unknown = prepareBrowserBootstrap(
      { pathname: "/settings" },
      fakeApi({ activateStakeholder }),
    );

    expect(activateStakeholder).not.toHaveBeenCalled();
    await expect(malformed.outcome).resolves.toEqual({ kind: "denied", correlationId: null });
    expect(unknown.route).toBe("not-found");
  });

  it("converts thrown failures into safe, non-rejecting outcomes", async () => {
    const denied = settleSession(
      Promise.resolve({
        ok: false,
        status: 403,
        detail: {
          code: "ACCESS_DENIED",
          message: "Access is not authorized.",
          correlation_id: "correlation-safe",
        },
      }),
    );
    const unavailable = settleSession(Promise.reject(new Error("private network detail")));
    const serverFailure = settleSession(Promise.resolve({ ok: false, status: 503, detail: null }));

    await expect(denied).resolves.toEqual({
      kind: "denied",
      correlationId: "correlation-safe",
    });
    await expect(unavailable).resolves.toEqual({ kind: "unavailable", correlationId: null });
    await expect(serverFailure).resolves.toEqual({ kind: "unavailable", correlationId: null });
  });
});
