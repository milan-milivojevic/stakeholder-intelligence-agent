import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { InsightStreamEvent } from "../api/contracts";
import {
  engagement,
  evidenceResponse,
  failedRun,
  fakePmApi,
  queuedRun,
  reportResponse,
  safeRunEvent,
  terminalRun,
} from "../test/pm-fixtures";
import { PmInsights } from "./pm-insights";

async function* streamEvents(
  events: InsightStreamEvent[],
  failAfterEvents = false,
): AsyncIterable<InsightStreamEvent> {
  await Promise.resolve();
  for (const event of events) {
    yield event;
  }
  if (failAfterEvents) {
    throw new Error("private provider detail");
  }
}

describe("PmInsights", () => {
  it("lists saved runs and reopens a report without creating a new Gemini run", async () => {
    const user = userEvent.setup();
    const savedRun = terminalRun("complete");
    const savedFailure = {
      ...failedRun,
      run_id: "run-failed-history",
      requested_question: "Which earlier analysis failed?",
    };
    const createInsight = vi.fn(() => Promise.resolve({ ok: true as const, value: queuedRun }));
    const listInsights = vi.fn(() =>
      Promise.resolve({ ok: true as const, value: [savedRun, savedFailure] }),
    );
    const getInsightStatus = vi.fn(() => Promise.resolve({ ok: true as const, value: savedRun }));
    const getInsightReport = vi.fn(() =>
      Promise.resolve({ ok: true as const, value: reportResponse() }),
    );

    render(
      <PmInsights
        api={fakePmApi({
          createInsight,
          listInsights,
          getInsightStatus,
          getInsightReport,
          streamInsight: () => streamEvents([{ event: "progress", data: safeRunEvent }]),
        })}
        engagement={engagement}
        view="history"
      />,
    );

    expect(await screen.findByRole("heading", { level: 4, name: "Insight history" })).toHaveClass(
      "text-xl",
    );
    expect(await screen.findByText(savedRun.requested_question)).toBeVisible();
    expect(screen.queryByText(savedFailure.requested_question)).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Open report" }));

    expect(
      await screen.findByRole("heading", { level: 4, name: savedRun.requested_question }),
    ).toHaveClass("text-xl");
    expect(
      await screen.findByRole("heading", { level: 5, name: "Evidence-grounded report" }),
    ).toHaveClass("text-lg");
    expect(screen.getByRole("button", { name: "Back to insight history" })).toHaveClass(
      "whitespace-nowrap",
    );
    expect(screen.queryByRole("list", { name: "Ready insight reports" })).not.toBeInTheDocument();
    expect(createInsight).not.toHaveBeenCalled();
    expect(getInsightStatus).not.toHaveBeenCalled();
    expect(getInsightReport).toHaveBeenCalledWith(engagement.engagement_id, savedRun.run_id);

    await user.click(screen.getByRole("button", { name: "Back to insight history" }));
    expect(await screen.findByRole("list", { name: "Ready insight reports" })).toBeVisible();
  });

  it("submits research, deduplicates safe SSE, renders a report, and drills into evidence", async () => {
    const user = userEvent.setup();
    const createInsight = vi.fn(() => Promise.resolve({ ok: true as const, value: queuedRun }));
    const getInsightStatus = vi.fn(() =>
      Promise.resolve({ ok: true as const, value: terminalRun("complete") }),
    );
    const getInsightReport = vi.fn(() =>
      Promise.resolve({ ok: true as const, value: reportResponse() }),
    );
    const getEvidence = vi.fn(() =>
      Promise.resolve({ ok: true as const, value: evidenceResponse }),
    );
    render(
      <PmInsights
        api={fakePmApi({
          createInsight,
          getInsightStatus,
          getInsightReport,
          getEvidence,
          streamInsight: () =>
            streamEvents([
              { event: "progress", data: safeRunEvent },
              { event: "progress", data: safeRunEvent },
              {
                event: "failure",
                data: {
                  status: "failed",
                  failure_code: "EVENT_STREAM_TIMEOUT",
                  failure_message: "The progress stream reached its bounded time limit.",
                  correlation_id: "correlation-stream",
                },
              },
            ]),
        })}
        engagement={engagement}
      />,
    );

    expect(
      screen.getByRole("heading", { level: 4, name: "Insight research and report" }),
    ).toHaveClass("text-xl");
    await user.click(screen.getByRole("button", { name: "Run insight research" }));
    expect(screen.getByText("Enter the research question.")).toBeVisible();
    await user.type(screen.getByLabelText("Research question"), queuedRun.requested_question);
    await user.click(screen.getByRole("button", { name: "Run insight research" }));

    expect(createInsight).toHaveBeenCalledWith(
      engagement.engagement_id,
      queuedRun.requested_question,
    );
    expect(await screen.findByRole("heading", { level: 5, name: "Analysis status" })).toHaveClass(
      "text-lg",
    );
    expect(
      await screen.findByRole("heading", { level: 5, name: "Evidence-grounded report" }),
    ).toHaveClass("text-lg");
    expect(
      screen.queryByRole("heading", { name: "Primary responsibility" }),
    ).not.toBeInTheDocument();
    expect(screen.getAllByText("Technical details")).toHaveLength(1);
    expect(screen.getByText("Research completed")).toBeInTheDocument();
    expect(screen.queryByText(/researcher: research topic completed/u)).not.toBeInTheDocument();
    expect(screen.queryByText("Live progress interrupted")).not.toBeInTheDocument();
    expect(screen.queryByText(/correlation-stream/u)).not.toBeInTheDocument();
    expect(getInsightStatus).toHaveBeenCalledWith(engagement.engagement_id, queuedRun.run_id);
    expect(getInsightReport).toHaveBeenCalled();

    const evidenceButton = screen.getAllByRole("button", { name: "View source excerpt" }).at(1);
    expect(evidenceButton).toBeDefined();
    if (evidenceButton !== undefined) {
      await user.click(evidenceButton);
    }
    expect(await screen.findByRole("dialog", { name: "Operating model page 4" })).toBeVisible();
    expect(getEvidence).toHaveBeenCalledWith(
      engagement.engagement_id,
      queuedRun.run_id,
      "evidence-beta",
    );
    expect(screen.getAllByRole("link", { name: "Download source file" })).toHaveLength(2);
    await user.click(screen.getByRole("button", { name: "Close" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("shows a persisted safe failed run without inventing a report", async () => {
    const user = userEvent.setup();
    render(
      <PmInsights
        api={fakePmApi({
          streamInsight: () => streamEvents([]),
          getInsightStatus: () => Promise.resolve({ ok: true, value: failedRun }),
        })}
        engagement={engagement}
      />,
    );

    await user.type(screen.getByLabelText("Research question"), "What failed safely?");
    await user.click(screen.getByRole("button", { name: "Run insight research" }));

    expect(await screen.findByText("Insight run failed safely")).toBeVisible();
    expect(screen.getByText(failedRun.failure_message ?? "missing")).toBeVisible();
    expect(screen.queryByText("Evidence-grounded report")).not.toBeInTheDocument();
  });

  it("reconnects after a nonterminal stream closes and then loads the persisted report", async () => {
    const user = userEvent.setup();
    let streamCalls = 0;
    const streamInsight = vi.fn(() => {
      streamCalls += 1;
      return streamEvents(streamCalls === 1 ? [] : [{ event: "progress", data: safeRunEvent }]);
    });
    const getInsightStatus = vi
      .fn()
      .mockResolvedValueOnce({ ok: true as const, value: queuedRun })
      .mockResolvedValueOnce({ ok: true as const, value: terminalRun("complete") });
    const getInsightReport = vi.fn(() =>
      Promise.resolve({ ok: true as const, value: reportResponse() }),
    );
    render(
      <PmInsights
        api={fakePmApi({ streamInsight, getInsightStatus, getInsightReport })}
        engagement={engagement}
      />,
    );

    await user.type(screen.getByLabelText("Research question"), "Can monitoring reconnect?");
    await user.click(screen.getByRole("button", { name: "Run insight research" }));

    await waitFor(() => expect(streamInsight).toHaveBeenCalledTimes(2), { timeout: 3_000 });
    expect(
      await screen.findByRole("heading", { level: 5, name: "Evidence-grounded report" }),
    ).toBeVisible();
    expect(getInsightStatus).toHaveBeenCalledTimes(2);
    expect(getInsightReport).toHaveBeenCalledTimes(1);
  });

  it("handles creation, stream, report, and evidence failures with safe messages", async () => {
    const user = userEvent.setup();
    const denied = {
      ok: false as const,
      status: 403,
      detail: {
        code: "ACCESS_DENIED",
        message: "Access is not authorized.",
        correlation_id: "correlation-insight",
      },
    };
    const { rerender } = render(
      <PmInsights
        key="creation-failure"
        api={fakePmApi({ createInsight: () => Promise.resolve(denied) })}
        engagement={engagement}
      />,
    );
    await user.type(screen.getByLabelText("Research question"), "Can this run?");
    await user.click(screen.getByRole("button", { name: "Run insight research" }));
    expect(await screen.findByText("Access is not authorized.")).toBeVisible();

    rerender(
      <PmInsights
        key="stream-failure"
        api={fakePmApi({
          streamInsight: () => streamEvents([], true),
          getInsightStatus: () => Promise.resolve({ ok: true, value: terminalRun("complete") }),
          getInsightReport: () => Promise.resolve(denied),
        })}
        engagement={engagement}
      />,
    );
    await user.type(screen.getByLabelText("Research question"), "Can the stream recover?");
    await user.click(screen.getByRole("button", { name: "Run insight research" }));
    expect(await screen.findByText("Access is not authorized.")).toBeVisible();
    expect(
      screen.queryByText("Live progress became unavailable. Reconnecting to the persisted run."),
    ).not.toBeInTheDocument();

    rerender(
      <PmInsights
        key="evidence-failure"
        api={fakePmApi({ getEvidence: () => Promise.resolve(denied) })}
        engagement={engagement}
      />,
    );
    await user.type(screen.getByLabelText("Research question"), "Can evidence be resolved?");
    await user.click(screen.getByRole("button", { name: "Run insight research" }));
    expect(await screen.findByText("Evidence-grounded report")).toBeVisible();
    const deniedEvidenceButton = screen
      .getAllByRole("button", { name: "View source excerpt" })
      .at(1);
    expect(deniedEvidenceButton).toBeDefined();
    if (deniedEvidenceButton !== undefined) {
      await user.click(deniedEvidenceButton);
    }
    expect(await screen.findByText("Access is not authorized.")).toBeVisible();
  });
});
