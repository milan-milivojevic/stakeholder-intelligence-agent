import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { BrowserSessionView } from "../api/contracts";
import { engagement, fakePmApi, secondEngagement, timestamp } from "../test/pm-fixtures";
import { PmWorkspace } from "./pm-workspace";

const pmSession: BrowserSessionView = {
  principal_type: "pm",
  access_session_id: "session-safe",
  expires_at: timestamp,
  engagement_id: null,
  stakeholder_id: null,
  interview_session_id: null,
  thread_id: null,
};

describe("PmWorkspace engagement setup", () => {
  it("reopens an existing engagement through the server selection route", async () => {
    const user = userEvent.setup();
    const selectEngagement = vi.fn(() => Promise.resolve({ ok: true as const, value: engagement }));
    render(<PmWorkspace api={fakePmApi({ selectEngagement })} session={pmSession} />);

    expect(await screen.findByText("Available engagements")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Open" }));
    expect(selectEngagement).toHaveBeenCalledWith(engagement.engagement_id);
    expect(await screen.findByText("Engagement")).toBeVisible();
    expect(screen.getByRole("tab", { name: "Stakeholders and invitations" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
  });

  it("creates and immediately opens a new engagement without database editing", async () => {
    const user = userEvent.setup();
    const createEngagement = vi.fn(() =>
      Promise.resolve({ ok: true as const, value: secondEngagement }),
    );
    render(
      <PmWorkspace
        api={fakePmApi({
          listEngagements: () => Promise.resolve({ ok: true, value: [] }),
          createEngagement,
        })}
        session={pmSession}
      />,
    );

    expect(await screen.findByText("No engagements yet")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Create and open" }));
    expect(await screen.findByText("Enter an engagement name.")).toBeVisible();
    await user.type(screen.getByLabelText("Engagement name"), secondEngagement.name);
    await user.type(screen.getByLabelText("Description (optional)"), "New engagement scope");
    await user.click(screen.getByRole("button", { name: "Create and open" }));

    expect(createEngagement).toHaveBeenCalledWith({
      name: secondEngagement.name,
      description: "New engagement scope",
    });
    expect(await screen.findByText(secondEngagement.name)).toBeVisible();
  });

  it("restores a persisted selected engagement and exposes explicit feature tabs", async () => {
    const user = userEvent.setup();
    render(
      <PmWorkspace
        api={fakePmApi()}
        session={{ ...pmSession, engagement_id: engagement.engagement_id }}
      />,
    );

    expect(await screen.findByText("Engagement")).toBeVisible();
    expect(screen.getByRole("button", { name: "Change engagement" })).toHaveClass(
      "bg-brand",
      "text-brand-contrast",
    );
    await user.click(screen.getByRole("tab", { name: "Documents" }));
    expect(await screen.findByText("Engagement documents")).toBeVisible();
    await user.click(screen.getByRole("tab", { name: "Interviews" }));
    expect(await screen.findByText("Finalized interviews")).toBeVisible();
    await user.click(screen.getByRole("tab", { name: "Insight research" }));
    expect(await screen.findByText("Insight research and report")).toBeVisible();
    await user.click(screen.getByRole("tab", { name: "Insight history" }));
    expect(await screen.findByText("No ready reports yet")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Change engagement" }));
    expect(screen.getByText("Available engagements")).toBeVisible();
  });

  it("shows only the safe server error when engagement loading fails", async () => {
    render(
      <PmWorkspace
        api={fakePmApi({
          listEngagements: () =>
            Promise.resolve({
              ok: false,
              status: 503,
              detail: {
                code: "SERVICE_UNAVAILABLE",
                message: "The service is temporarily unavailable.",
                correlation_id: "correlation-safe",
              },
            }),
        })}
        session={pmSession}
      />,
    );

    expect(await screen.findByText("The service is temporarily unavailable.")).toBeVisible();
    expect(screen.getByText(/correlation-safe/u)).toBeVisible();
  });
});
