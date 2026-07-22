import { describe, expect, it } from "vitest";

import type { BrowserSessionView } from "../api/contracts";
import { browserSessionReducer, decideRoute, initialBrowserSessionState } from "./session-state";

const pmSession: BrowserSessionView = {
  principal_type: "pm",
  access_session_id: "session-safe",
  expires_at: "2026-07-16T08:00:00Z",
  engagement_id: null,
  stakeholder_id: null,
  interview_session_id: null,
  thread_id: null,
};
const stakeholderSession: BrowserSessionView = {
  ...pmSession,
  principal_type: "stakeholder",
  engagement_id: "engagement-safe",
  stakeholder_id: "stakeholder-safe",
  interview_session_id: "interview-safe",
  thread_id: "thread-safe",
};

describe("browser session reducer", () => {
  it("separates missing clean sessions from denied invitation activation", () => {
    const missing = browserSessionReducer(initialBrowserSessionState, {
      type: "bootstrap-settled",
      source: "inspection",
      outcome: { kind: "denied", correlationId: "hidden-for-clean-inspection" },
    });
    const denied = browserSessionReducer(initialBrowserSessionState, {
      type: "bootstrap-settled",
      source: "invitation",
      outcome: { kind: "denied", correlationId: "correlation-safe" },
    });

    expect(missing).toEqual({
      phase: "activation-required",
      reason: "missing",
      correlationId: null,
    });
    expect(denied).toEqual({ phase: "denied", correlationId: "correlation-safe" });
  });

  it("handles PM activation, logout retry, logout success, and reset", () => {
    const loading = browserSessionReducer(
      { phase: "activation-required", reason: "missing", correlationId: null },
      { type: "activation-started" },
    );
    const active = browserSessionReducer(loading, {
      type: "activation-settled",
      outcome: { kind: "authenticated", session: pmSession },
    });
    const loggingOut = browserSessionReducer(active, { type: "logout-started" });
    const failedLogout = browserSessionReducer(loggingOut, {
      type: "logout-failed",
      failure: { kind: "unavailable", correlationId: null },
    });
    const loggingOutAgain = browserSessionReducer(failedLogout, { type: "logout-started" });
    const signedOut = browserSessionReducer(loggingOutAgain, { type: "logout-succeeded" });
    const reset = browserSessionReducer(
      { phase: "unavailable", correlationId: null },
      { type: "reset-activation" },
    );

    expect(active).toEqual({ phase: "active", session: pmSession, logoutFailure: null });
    expect(failedLogout).toMatchObject({ phase: "active", logoutFailure: { kind: "unavailable" } });
    expect(signedOut).toMatchObject({ phase: "activation-required", reason: "missing" });
    expect(reset).toMatchObject({ phase: "activation-required", reason: "missing" });
  });

  it("maps authentication failures and all route-guard decisions explicitly", () => {
    const deniedActivation = browserSessionReducer(initialBrowserSessionState, {
      type: "activation-settled",
      outcome: { kind: "denied", correlationId: "correlation-safe" },
    });
    const unavailable = browserSessionReducer(initialBrowserSessionState, {
      type: "activation-settled",
      outcome: { kind: "unavailable", correlationId: null },
    });
    const pmActive = { phase: "active", session: pmSession, logoutFailure: null } as const;
    const stakeholderActive = {
      phase: "active",
      session: stakeholderSession,
      logoutFailure: null,
    } as const;

    expect(deniedActivation).toMatchObject({ phase: "activation-required", reason: "denied" });
    expect(decideRoute("pm", initialBrowserSessionState)).toBe("loading");
    expect(decideRoute("pm", deniedActivation)).toBe("activation-required");
    expect(decideRoute("stakeholder", { phase: "denied", correlationId: null })).toBe("denied");
    expect(decideRoute("pm", unavailable)).toBe("unavailable");
    expect(decideRoute("pm", pmActive)).toBe("active");
    expect(decideRoute("stakeholder", stakeholderActive)).toBe("active");
    expect(decideRoute("stakeholder", pmActive)).toBe("activation-required");
    expect(decideRoute("not-found", pmActive)).toBe("not-found");
    expect(decideRoute("pm", { phase: "logging-out", session: pmSession })).toBe("loading");
  });

  it("removes an active workspace when a scoped API reports denial", () => {
    const denied = browserSessionReducer(
      { phase: "active", session: stakeholderSession, logoutFailure: null },
      { type: "session-denied", correlationId: "correlation-denied" },
    );

    expect(denied).toEqual({ phase: "denied", correlationId: "correlation-denied" });
    expect(decideRoute("stakeholder", denied)).toBe("denied");
  });
});
