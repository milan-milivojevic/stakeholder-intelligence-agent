import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { Stakeholder } from "../api/contracts";
import {
  engagement,
  fakePmApi,
  interviewSession,
  invitation,
  stakeholder,
} from "../test/pm-fixtures";
import { PmStakeholders } from "./pm-stakeholders";

describe("PmStakeholders", () => {
  it("creates a stakeholder and supports generate, later copy, and pre-start revoke", async () => {
    const user = userEvent.setup();
    const created: Stakeholder = {
      ...stakeholder,
      stakeholder_id: "stakeholder-beta",
      display_name: "Jordan Lee",
      role: "Product lead",
      department: "Product",
    };
    const createStakeholder = vi.fn(() => Promise.resolve({ ok: true as const, value: created }));
    const generatedInvitation = {
      ...invitation,
      invitation_id: "invitation-beta",
      stakeholder_id: created.stakeholder_id,
      status: "active" as const,
      expires_at: new Date(Date.now() + 60 * 60 * 1000).toISOString(),
      activated_at: null,
      revoked_at: null,
    };
    const issueInvitation = vi.fn(() =>
      Promise.resolve({
        ok: true as const,
        value: { invitation: generatedInvitation, invitation_token: "A".repeat(48) },
      }),
    );
    const getInvitationLink = vi.fn(() =>
      Promise.resolve({
        ok: true as const,
        value: { invitation: generatedInvitation, invitation_token: "A".repeat(48) },
      }),
    );
    const revokeInvitation = vi.fn(() =>
      Promise.resolve({
        ok: true as const,
        value: {
          ...generatedInvitation,
          status: "revoked" as const,
          revoked_at: new Date().toISOString(),
        },
      }),
    );
    const clipboardWrite = vi.fn(() => Promise.resolve());
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText: clipboardWrite },
    });
    const storageWrite = vi.spyOn(Storage.prototype, "setItem");
    render(
      <PmStakeholders
        api={fakePmApi({
          createStakeholder,
          issueInvitation,
          getInvitationLink,
          revokeInvitation,
        })}
        engagement={engagement}
      />,
    );

    expect(
      await screen.findByRole("heading", {
        level: 4,
        name: "Stakeholders and interview invitations",
      }),
    ).toHaveClass("text-xl");
    expect(screen.getByRole("heading", { level: 5, name: "Engagement stakeholders" })).toHaveClass(
      "text-lg",
    );
    expect(
      screen.getByRole("heading", { level: 5, name: "Interview invitation lifecycle" }),
    ).toHaveClass("text-lg");
    const completedLifecycleRow = screen.getByRole("row", {
      name: /Alex Morgan Completed Completed .* Interview completed/u,
    });
    const completedLifecycleCells = within(completedLifecycleRow).getAllByRole("cell");
    expect(completedLifecycleCells[1]).toHaveTextContent("Completed");
    expect(completedLifecycleCells[2]).toHaveTextContent("Completed");
    expect(completedLifecycleCells[4]).toHaveTextContent("Interview completed");
    expect(screen.getByRole("button", { name: "Interview completed" })).toBeDisabled();
    expect(screen.queryByLabelText("Display name")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Add new stakeholder" }));
    expect(screen.getByRole("heading", { level: 6, name: "Add stakeholder" })).toHaveClass(
      "text-base",
    );
    await user.type(screen.getByLabelText("Display name"), created.display_name);
    await user.type(screen.getByLabelText("Role (optional)"), created.role ?? "");
    await user.type(screen.getByLabelText("Department (optional)"), created.department ?? "");
    await user.click(screen.getByRole("button", { name: "Add stakeholder" }));
    expect(createStakeholder).toHaveBeenCalledWith(engagement.engagement_id, {
      display_name: created.display_name,
      role: created.role,
      department: created.department,
    });
    expect(await screen.findByText(created.display_name)).toBeVisible();
    expect(screen.getByText("No invitation")).toBeVisible();

    await user.click(screen.getByRole("button", { name: "Generate invitation link" }));
    expect(await screen.findByText("Invitation ready")).toBeVisible();
    expect(await screen.findByText("Ready to open")).toBeVisible();
    const invitationInput = await screen.findByLabelText("Interview invitation link");
    const invitationLink = `${window.location.origin}/s/${"A".repeat(48)}`;
    expect(invitationInput).toHaveValue(invitationLink);
    await user.click(screen.getByRole("button", { name: "Copy invitation link" }));
    expect(clipboardWrite).toHaveBeenCalledWith(invitationLink);
    expect(screen.getByText("Copied to the clipboard.")).toBeVisible();
    expect(storageWrite).not.toHaveBeenCalled();
    expect(screen.queryByRole("columnheader", { name: "Invitation link" })).not.toBeInTheDocument();

    const copyButtons = screen.getAllByRole("button", { name: "Copy invitation" });
    const copyButton = copyButtons.at(0);
    if (copyButton === undefined) {
      throw new Error("Expected a copy invitation action.");
    }
    await user.click(copyButton);
    expect(getInvitationLink).not.toHaveBeenCalled();
    expect(clipboardWrite).toHaveBeenLastCalledWith(invitationLink);

    await user.click(screen.getByRole("button", { name: "Revoke" }));
    expect(revokeInvitation).toHaveBeenCalledWith(
      engagement.engagement_id,
      generatedInvitation.invitation_id,
    );
    expect(await screen.findByText("Revoked")).toBeVisible();
    storageWrite.mockRestore();
  });

  it("shows an opened invitation as an active resumable session without revoke", async () => {
    const user = userEvent.setup();
    const activeStakeholder = { ...stakeholder, status: "active" as const };
    const activeInvitation = {
      ...invitation,
      status: "activated" as const,
      activated_at: new Date().toISOString(),
      expires_at: new Date(Date.now() + 60 * 60 * 1000).toISOString(),
    };
    const draftInterview = {
      ...interviewSession,
      status: "draft" as const,
      finalized_at: null,
      transcript_id: null,
      ingestion_version_id: null,
    };
    const getInvitationLink = vi.fn(() =>
      Promise.resolve({
        ok: true as const,
        value: { invitation: activeInvitation, invitation_token: "C".repeat(48) },
      }),
    );
    const clipboardWrite = vi.fn(() => Promise.resolve());
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText: clipboardWrite },
    });
    render(
      <PmStakeholders
        api={fakePmApi({
          listStakeholders: () => Promise.resolve({ ok: true, value: [activeStakeholder] }),
          listInvitations: () => Promise.resolve({ ok: true, value: [activeInvitation] }),
          listInterviews: () => Promise.resolve({ ok: true, value: [draftInterview] }),
          getInvitationLink,
        })}
        engagement={engagement}
      />,
    );

    expect(await screen.findByText("Session active")).toBeVisible();
    expect(screen.getByText("Interview in progress")).toBeVisible();
    expect(screen.getByText("In progress")).toBeVisible();
    const copyButtons = screen.getAllByRole("button", { name: "Copy invitation" });
    expect(copyButtons.length).toBeGreaterThan(0);
    expect(screen.queryByRole("button", { name: "Revoke" })).not.toBeInTheDocument();
    expect(screen.queryByRole("columnheader", { name: "Invitation link" })).not.toBeInTheDocument();
    const copyButton = copyButtons.at(0);
    if (copyButton === undefined) {
      throw new Error("Expected a copy invitation action.");
    }
    await user.click(copyButton);
    expect(getInvitationLink).toHaveBeenCalledWith(
      engagement.engagement_id,
      activeInvitation.invitation_id,
    );
    expect(clipboardWrite).toHaveBeenCalledWith(`${window.location.origin}/s/${"C".repeat(48)}`);
  });

  it("shows an unfinished expired session and enables a replacement link", async () => {
    const activeStakeholder = { ...stakeholder, status: "active" as const };
    const expiredSessionInvitation = {
      ...invitation,
      status: "expired" as const,
      activated_at: new Date(Date.now() - 9 * 60 * 60 * 1000).toISOString(),
      expires_at: new Date(Date.now() - 60 * 60 * 1000).toISOString(),
    };
    const draftInterview = {
      ...interviewSession,
      status: "draft" as const,
      finalized_at: null,
      transcript_id: null,
      ingestion_version_id: null,
    };
    const issueInvitation = vi.fn(() =>
      Promise.resolve({
        ok: true as const,
        value: {
          invitation: {
            ...invitation,
            invitation_id: "invitation-replacement",
            created_at: new Date().toISOString(),
            expires_at: new Date(Date.now() + 60 * 60 * 1000).toISOString(),
          },
          invitation_token: "B".repeat(48),
        },
      }),
    );
    const user = userEvent.setup();
    render(
      <PmStakeholders
        api={fakePmApi({
          listStakeholders: () => Promise.resolve({ ok: true, value: [activeStakeholder] }),
          listInvitations: () => Promise.resolve({ ok: true, value: [expiredSessionInvitation] }),
          listInterviews: () => Promise.resolve({ ok: true, value: [draftInterview] }),
          issueInvitation,
        })}
        engagement={engagement}
      />,
    );

    expect((await screen.findAllByText("Session expired")).length).toBeGreaterThan(0);
    const generateButtons = screen.getAllByRole("button", { name: "Generate invitation link" });
    const generateButton = generateButtons.at(0);
    if (generateButton === undefined) {
      throw new Error("Expected a replacement invitation action.");
    }
    await user.click(generateButton);
    expect(issueInvitation).toHaveBeenCalledWith(
      engagement.engagement_id,
      stakeholder.stakeholder_id,
    );
    expect(await screen.findByText("Ready to open")).toBeVisible();
  });

  it("validates required stakeholder input and renders empty records", async () => {
    const user = userEvent.setup();
    render(
      <PmStakeholders
        api={fakePmApi({
          listStakeholders: () => Promise.resolve({ ok: true, value: [] }),
          listInvitations: () => Promise.resolve({ ok: true, value: [] }),
        })}
        engagement={engagement}
      />,
    );

    expect(await screen.findByText("No stakeholders have been added.")).toBeVisible();
    expect(screen.getByText("No invitations have been issued.")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Add new stakeholder" }));
    expect(screen.getByRole("button", { name: "Close form" })).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Add stakeholder" }));
    expect(screen.getByText("Enter the stakeholder's display name.")).toBeVisible();
  });

  it("surfaces a safe load failure without rendering protected records", async () => {
    render(
      <PmStakeholders
        api={fakePmApi({
          listInvitations: () =>
            Promise.resolve({
              ok: false,
              status: 403,
              detail: {
                code: "ACCESS_DENIED",
                message: "Access is not authorized.",
                correlation_id: "correlation-denied",
              },
            }),
        })}
        engagement={engagement}
      />,
    );

    expect(await screen.findByText("Access is not authorized.")).toBeVisible();
    expect(screen.queryByText(stakeholder.display_name)).not.toBeInTheDocument();
  });
});
