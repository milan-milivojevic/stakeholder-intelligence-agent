import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import axe from "axe-core";
import { describe, expect, it, vi } from "vitest";

import type { BrowserSessionView } from "../api/contracts";
import type { BrowserSessionApi } from "../auth/browser-api";
import type {
  BootstrapSource,
  BrowserBootstrap,
  BrowserRoute,
  BrowserSessionOutcome,
} from "../auth/bootstrap";
import type { StakeholderApi } from "../stakeholder/stakeholder-api";
import {
  draftStatus,
  finishResponse,
  interviewContext,
  stakeholderDocument,
  stakeholderScope,
  stakeholderUpload,
} from "../test/stakeholder-fixtures";
import { App } from "./app";

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
  engagement_id: stakeholderScope.engagementId,
  stakeholder_id: stakeholderScope.stakeholderId,
  interview_session_id: stakeholderScope.interviewSessionId,
  thread_id: stakeholderScope.threadId,
};

async function* emptyInterviewStream(): AsyncGenerator<never> {
  await Promise.resolve();
  yield* [];
}

function fakeStakeholderApi(overrides: Partial<StakeholderApi> = {}): StakeholderApi {
  return {
    getContext: () =>
      Promise.resolve({
        ok: true,
        value: { context: interviewContext, scope: stakeholderScope },
      }),
    listDocuments: () => Promise.resolve({ ok: true, value: [stakeholderDocument] }),
    uploadDocument: () => Promise.resolve({ ok: true, value: stakeholderUpload }),
    deleteDocument: () => Promise.resolve({ ok: true, value: { status: "ok" } }),
    startInterview: () => Promise.resolve({ ok: true, value: draftStatus }),
    getInterviewStatus: () => Promise.resolve({ ok: true, value: draftStatus }),
    deleteAnswer: () => Promise.resolve({ ok: true, value: draftStatus }),
    streamInterviewTurn: emptyInterviewStream,
    finishInterview: () => Promise.resolve({ ok: true, value: finishResponse }),
    ...overrides,
  };
}

function bootstrap(
  route: BrowserRoute,
  outcome: BrowserSessionOutcome,
  source: BootstrapSource = "inspection",
): BrowserBootstrap {
  return { route, source, outcome: Promise.resolve(outcome) };
}

function fakeApi(overrides: Partial<BrowserSessionApi> = {}): BrowserSessionApi {
  return {
    inspect: () => Promise.resolve({ ok: true, value: pmSession }),
    activatePm: () => Promise.resolve({ ok: true, value: pmSession }),
    activateStakeholder: () => Promise.resolve({ ok: true, value: stakeholderSession }),
    logout: () => Promise.resolve({ ok: true, value: { status: "ok" } }),
    ...overrides,
  };
}

describe("App", () => {
  it("renders an accessible PM activation surface without starter content", async () => {
    const { container } = render(
      <App bootstrap={bootstrap("pm", { kind: "denied", correlationId: null })} api={fakeApi()} />,
    );

    expect(
      screen.getByRole("heading", { level: 1, name: "Stakeholder Intelligence" }),
    ).toBeVisible();
    expect(
      screen.getByRole("heading", { level: 2, name: "Project Manager Workspace" }),
    ).toBeVisible();
    expect(await screen.findByLabelText("Access key")).toHaveAttribute("type", "password");
    expect(screen.queryByText(/vite|counter|learn react/iu)).not.toBeInTheDocument();
    expect(
      (
        await axe.run(container, {
          rules: { "color-contrast": { enabled: false } },
        })
      ).violations,
    ).toEqual([]);
  });

  it("clears the PM secret before activation and never writes browser storage", async () => {
    const user = userEvent.setup();
    const secret = "private-bootstrap-value-123456789";
    let resolveActivation: ((result: { ok: true; value: BrowserSessionView }) => void) | undefined;
    const activation = new Promise<{ ok: true; value: BrowserSessionView }>((resolve) => {
      resolveActivation = resolve;
    });
    const activatePm = vi.fn((received: string) => {
      expect(received).toBe(secret);
      return activation;
    });
    const storageWrite = vi.spyOn(Storage.prototype, "setItem");
    render(
      <App
        bootstrap={bootstrap("pm", { kind: "denied", correlationId: null })}
        api={fakeApi({ activatePm })}
      />,
    );
    const input = await screen.findByLabelText("Access key");

    await user.type(input, secret);
    await user.click(screen.getByRole("button", { name: "Open workspace" }));

    expect(input).toHaveValue("");
    expect(activatePm).toHaveBeenCalledOnce();
    expect(storageWrite).not.toHaveBeenCalled();
    await act(() => {
      resolveActivation?.({ ok: true, value: pmSession });
      return activation;
    });
    expect(await screen.findByText("Project Manager Workspace")).toBeVisible();
    const header = screen.getByRole("banner");
    expect(within(header).getByText("Session until")).toBeVisible();
    expect(within(header).getByRole("button", { name: "Sign out" })).toBeVisible();
    expect(screen.getByRole("main")).not.toContainHTML("Sign out");
    expect(
      screen.queryByRole("heading", { name: "Project manager access" }),
    ).not.toBeInTheDocument();
    expect(screen.queryByText(secret)).not.toBeInTheDocument();
    storageWrite.mockRestore();
  });

  it("shows the fixed stakeholder workspace and signs out to the clean entry state", async () => {
    const user = userEvent.setup();
    const logout = vi.fn(() =>
      Promise.resolve({ ok: true as const, value: { status: "ok" as const } }),
    );
    render(
      <App
        bootstrap={bootstrap("stakeholder", {
          kind: "authenticated",
          session: stakeholderSession,
        })}
        api={fakeApi({ logout })}
        stakeholderApi={fakeStakeholderApi()}
      />,
    );

    expect(await screen.findByText("Stakeholder workspace")).toBeVisible();
    const header = screen.getByRole("banner");
    expect(within(header).getByText("Session expires")).toBeVisible();
    expect(within(header).getByRole("button", { name: "Sign out" })).toBeVisible();
    expect(screen.getByRole("main")).not.toContainHTML("Sign out");
    await user.click(screen.getByRole("button", { name: "Sign out" }));
    expect(logout).toHaveBeenCalledWith("stakeholder");
    expect(await screen.findByText("Invitation required")).toBeVisible();
  });

  it("removes protected stakeholder content when a scoped API denies the active session", async () => {
    render(
      <App
        bootstrap={bootstrap("stakeholder", {
          kind: "authenticated",
          session: stakeholderSession,
        })}
        api={fakeApi()}
        stakeholderApi={fakeStakeholderApi({
          getContext: () =>
            Promise.resolve({
              ok: false,
              status: 403,
              detail: {
                code: "ACCESS_SESSION_REVOKED",
                message: "Session revoked.",
                correlation_id: "correlation-revoked",
              },
            }),
        })}
      />,
    );

    expect(await screen.findByText("Invitation unavailable")).toBeVisible();
    expect(screen.getByText(/correlation-revoked/u)).toBeVisible();
    expect(screen.queryByText(interviewContext.stakeholder.display_name)).not.toBeInTheDocument();
    expect(screen.queryByText("Session revoked.")).not.toBeInTheDocument();
  });

  it("keeps a session visible when logout fails instead of claiming success", async () => {
    const user = userEvent.setup();
    render(
      <App
        bootstrap={bootstrap("pm", { kind: "authenticated", session: pmSession })}
        api={fakeApi({ logout: () => Promise.reject(new Error("private failure")) })}
      />,
    );

    await user.click(await screen.findByRole("button", { name: "Sign out" }));

    expect(await screen.findByText(/Sign-out was not completed/u)).toBeVisible();
    expect(screen.getByText("Project Manager Workspace")).toBeVisible();
  });

  it("renders safe denied, unavailable, route-switch, and unknown-route states", async () => {
    const { rerender } = render(
      <App
        bootstrap={bootstrap(
          "stakeholder",
          { kind: "denied", correlationId: "correlation-safe" },
          "invitation",
        )}
        api={fakeApi()}
      />,
    );
    expect(await screen.findByText("Invitation unavailable")).toBeVisible();
    expect(screen.getByText(/correlation-safe/u)).toBeVisible();

    rerender(
      <App
        bootstrap={bootstrap("pm", { kind: "unavailable", correlationId: null })}
        api={fakeApi()}
      />,
    );
    expect(await screen.findByText("Workspace unavailable")).toBeVisible();

    rerender(
      <App
        bootstrap={bootstrap("stakeholder", { kind: "authenticated", session: pmSession })}
        api={fakeApi()}
      />,
    );
    expect(await screen.findByText("Invitation required")).toBeVisible();

    rerender(
      <App
        bootstrap={bootstrap("not-found", { kind: "denied", correlationId: null })}
        api={fakeApi()}
      />,
    );
    expect(await screen.findByText("Page not found")).toBeVisible();
  });
});
