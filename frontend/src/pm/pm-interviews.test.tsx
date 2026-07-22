import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import type { InterviewSession } from "../api/contracts";
import {
  engagement,
  fakePmApi,
  interviewPreview,
  interviewSession,
  stakeholder,
  timestamp,
} from "../test/pm-fixtures";
import { PmInterviews } from "./pm-interviews";

describe("PmInterviews", () => {
  it("shows only explicitly finalized interviews", async () => {
    const draft: InterviewSession = {
      ...interviewSession,
      interview_session_id: "interview-draft",
      status: "draft",
      finalized_at: null,
      transcript_id: null,
      ingestion_version_id: null,
    };
    render(
      <PmInterviews
        api={fakePmApi({
          listInterviews: () => Promise.resolve({ ok: true, value: [interviewSession, draft] }),
        })}
        engagement={engagement}
      />,
    );

    expect(
      await screen.findByRole("heading", { level: 4, name: "Finalized interviews" }),
    ).toHaveClass("text-xl");
    expect(
      screen.getByText("Only finalized interviews from the selected engagement are shown here."),
    ).toBeVisible();
    expect(screen.getByText(stakeholder.display_name)).toBeVisible();
    expect(screen.getByText("Ready for permitted retrieval")).toBeVisible();
    expect(screen.queryByText("Not finalized")).not.toBeInTheDocument();
    expect(screen.queryByText("Finalized interview view")).not.toBeInTheDocument();
  });

  it("renders a safe failed state and no raw transcript download", async () => {
    const failed: InterviewSession = {
      ...interviewSession,
      interview_session_id: "interview-failed",
      status: "failed",
      failure_code: "TRANSCRIPT_INGESTION_FAILED",
      failure_message: "The finalized transcript could not be indexed.",
      started_at: timestamp,
    };
    render(
      <PmInterviews
        api={fakePmApi({ listInterviews: () => Promise.resolve({ ok: true, value: [failed] }) })}
        engagement={engagement}
      />,
    );

    expect(await screen.findByText("The finalized transcript could not be indexed.")).toBeVisible();
    expect(screen.queryByRole("link", { name: /download/iu })).not.toBeInTheDocument();
  });

  it("opens a read-only preview of the complete finalized interview", async () => {
    const user = userEvent.setup();
    render(<PmInterviews api={fakePmApi()} engagement={engagement} />);

    await user.click(
      await screen.findByRole("button", {
        name: `Preview interview with ${stakeholder.display_name}`,
      }),
    );

    expect(await screen.findByRole("heading", { name: stakeholder.display_name })).toBeVisible();
    for (const turn of interviewPreview.turns) {
      expect(screen.getByText(turn.text)).toBeVisible();
    }
    expect(screen.getByText("Interview assistant")).toBeVisible();
    expect(screen.getByText(stakeholder.display_name, { selector: "li p" })).not.toHaveClass(
      "uppercase",
    );
    expect(screen.getByLabelText("Interview transcript")).toBeVisible();
    expect(screen.queryByRole("link", { name: /download/iu })).not.toBeInTheDocument();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Close preview" }));
    expect(screen.queryByLabelText("Interview transcript")).not.toBeInTheDocument();
  });

  it("renders empty and denied states honestly", async () => {
    const { rerender } = render(
      <PmInterviews
        key="empty"
        api={fakePmApi({ listInterviews: () => Promise.resolve({ ok: true, value: [] }) })}
        engagement={engagement}
      />,
    );
    expect(await screen.findByText("No finalized interviews are available.")).toBeVisible();

    rerender(
      <PmInterviews
        key="denied"
        api={fakePmApi({
          listInterviews: () =>
            Promise.resolve({
              ok: false,
              status: 403,
              detail: {
                code: "ACCESS_DENIED",
                message: "Access is not authorized.",
                correlation_id: "correlation-interviews",
              },
            }),
        })}
        engagement={engagement}
      />,
    );
    expect(await screen.findByText("Access is not authorized.")).toBeVisible();
  });
});
