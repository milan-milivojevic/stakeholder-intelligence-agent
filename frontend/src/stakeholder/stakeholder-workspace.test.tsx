import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import axe from "axe-core";
import { describe, expect, it, vi } from "vitest";

import {
  draftStatus,
  finishResponse,
  interviewContext,
  stakeholderDocument,
  stakeholderScope,
  stakeholderSession,
  stakeholderUpload,
} from "../test/stakeholder-fixtures";
import type { StakeholderApi } from "./stakeholder-api";
import { StakeholderWorkspace } from "./stakeholder-workspace";

async function* emptyStream(): AsyncGenerator<never> {
  await Promise.resolve();
  yield* [];
}

function fakeApi(overrides: Partial<StakeholderApi> = {}): StakeholderApi {
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
    streamInterviewTurn: emptyStream,
    finishInterview: () => Promise.resolve({ ok: true, value: finishResponse }),
    ...overrides,
  };
}

describe("StakeholderWorkspace", () => {
  it("renders a concise interview entry and reveals evidence only when requested", async () => {
    const user = userEvent.setup();
    const { container } = render(
      <StakeholderWorkspace
        api={fakeApi()}
        session={stakeholderSession}
        onUnauthorized={vi.fn()}
      />,
    );

    expect(await screen.findByText("Interview invitation")).toBeVisible();
    expect(container).toHaveTextContent(
      `You have been invited to a guided interview for engagement ${interviewContext.engagement.name}.`,
    );
    expect(screen.getByText(interviewContext.engagement.name)).toBeVisible();
    expect(screen.getByText(interviewContext.stakeholder.display_name)).toBeVisible();
    expect(
      screen.getByText(
        `${interviewContext.stakeholder.role ?? ""} · ${interviewContext.stakeholder.department ?? ""}`,
      ),
    ).toBeVisible();
    expect(screen.queryByText("Role:")).not.toBeInTheDocument();
    expect(screen.queryByText("Department:")).not.toBeInTheDocument();
    const invitationContext = screen.getByText(interviewContext.engagement.name).parentElement;
    expect(invitationContext).toHaveClass("break-words", "min-w-0");
    expect(invitationContext).not.toHaveClass("whitespace-nowrap", "overflow-x-auto");
    expect(screen.queryByText("Secure context verified")).not.toBeInTheDocument();
    expect(
      screen.queryByText(stakeholderDocument.source.original_filename),
    ).not.toBeInTheDocument();
    expect(screen.getByText("Conversation restored")).toBeVisible();
    expect(
      screen.getByText(
        "What are the main tasks you personally perform in your day-to-day work as Operations lead?",
      ),
    ).toBeVisible();
    await user.click(screen.getByRole("button", { name: "View supporting evidence" }));
    expect(await screen.findByText(stakeholderDocument.source.original_filename)).toBeVisible();
    expect(
      (
        await axe.run(container, {
          rules: { "color-contrast": { enabled: false } },
        })
      ).violations,
    ).toEqual([]);
  });

  it("clears protected rendering and delegates an unauthorized context to the session gate", async () => {
    const onUnauthorized = vi.fn();
    render(
      <StakeholderWorkspace
        api={fakeApi({
          getContext: () =>
            Promise.resolve({
              ok: false,
              status: 403,
              detail: {
                code: "ACCESS_DENIED",
                message: "Denied.",
                correlation_id: "correlation-denied",
              },
            }),
        })}
        session={stakeholderSession}
        onUnauthorized={onUnauthorized}
      />,
    );

    await waitFor(() => expect(onUnauthorized).toHaveBeenCalledWith("correlation-denied"));
    expect(screen.queryByText(interviewContext.stakeholder.display_name)).not.toBeInTheDocument();
    expect(screen.queryByText("Denied.")).not.toBeInTheDocument();
  });
});
