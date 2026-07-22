import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import axe from "axe-core";
import { describe, expect, it, vi } from "vitest";

import {
  completeReport,
  engagement,
  evidenceResponse,
  executionMetrics,
  fakePmApi,
  insufficientReport,
  partialReport,
  queuedRun,
  safeRunEvent,
} from "../test/pm-fixtures";
import { EvidenceDrawer, ExecutionTimeline, ReportView } from "./report-view";

describe("ReportView", () => {
  it("prioritizes a concise summary and opens grouped sources without exposing IDs", async () => {
    const user = userEvent.setup();
    const onOpenEvidence = vi.fn();
    const { container } = render(
      <ReportView
        report={completeReport()}
        metrics={executionMetrics()}
        events={[safeRunEvent]}
        onOpenEvidence={onOpenEvidence}
      />,
    );

    expect(screen.getByText("Analysis complete")).toBeVisible();
    expect(screen.getByText("What you need to know")).toBeVisible();
    expect(screen.getByText("Key insights")).toBeVisible();
    expect(screen.getByText("Recommended actions")).toBeVisible();
    expect(screen.getByText("Responsibilities")).toBeVisible();
    expect(screen.getByText("Risks and approval dependencies")).toBeVisible();
    expect(screen.getByText("Stakeholder alignment")).toBeVisible();
    expect(screen.getByText("Evidence gaps and open questions")).toBeVisible();
    expect(screen.getByText("Sources")).toBeVisible();
    expect(screen.queryByText(/evidence-alpha/u)).not.toBeInTheDocument();
    const evidenceButton = screen
      .getAllByRole("button", { name: "View supporting sources (2)" })
      .at(0);
    expect(evidenceButton).toBeDefined();
    if (evidenceButton !== undefined) {
      await user.click(evidenceButton);
    }
    expect(onOpenEvidence).toHaveBeenCalledWith(
      expect.objectContaining({
        title: "Sources supporting this recommendation",
        items: [
          expect.objectContaining({ evidenceId: "evidence-alpha" }),
          expect.objectContaining({ evidenceId: "evidence-beta" }),
        ],
      }),
    );
    expect(
      (
        await axe.run(container, {
          rules: { "color-contrast": { enabled: false } },
        })
      ).violations,
    ).toEqual([]);
  });

  it("renders partial, failed-topic, concern, and empty-list semantics", () => {
    const report = partialReport();
    const topic = report.researched_topics.at(0);
    const signal = report.buy_in_signals.at(0);
    expect(topic).toBeDefined();
    expect(signal).toBeDefined();
    if (topic === undefined || signal === undefined) {
      throw new Error("The report fixture is incomplete.");
    }
    topic.status = "failed";
    signal.category = "expressed_concern";
    report.open_questions = [];
    report.follow_up_recommendations = [];
    render(
      <ReportView
        report={report}
        metrics={executionMetrics("partial")}
        events={[safeRunEvent]}
        onOpenEvidence={vi.fn()}
      />,
    );

    expect(screen.getByText("Analysis partially complete")).toBeVisible();
    expect(screen.getByText("Technical details")).toBeVisible();
    expect(screen.getByText("Stakeholder alignment")).toBeVisible();
    expect(screen.getByText("Evidence gaps and open questions")).toBeVisible();
    expect(screen.getByText("No follow-up recommendations were reported.")).toBeVisible();
  });

  it("renders an honest insufficient-evidence report without supported-result claims", () => {
    render(
      <ReportView
        report={insufficientReport()}
        metrics={executionMetrics("insufficient_evidence")}
        events={[safeRunEvent]}
        onOpenEvidence={vi.fn()}
      />,
    );

    expect(screen.getByText("More evidence needed")).toBeVisible();
    expect(screen.getByText("No supported responsibility was identified.")).toBeVisible();
    expect(screen.getByText("No supported operational risk was identified.")).toBeVisible();
    expect(screen.getByText("No supported finding was identified.")).toBeVisible();
    expect(
      screen.getByText(
        "No cited sources are available because no supported evidence was asserted.",
      ),
    ).toBeVisible();
  });

  it("distinguishes repeated researcher actions with human-readable topic titles", () => {
    const report = completeReport();
    report.researched_topics.push({
      topic_id: "topic-beta",
      title: "Alex Morgan interview analysis",
      status: "completed",
      summary: "The finalized interview supports the responsibility findings.",
      evidence_ids: ["evidence-alpha"],
    });
    render(
      <ReportView
        report={report}
        metrics={executionMetrics()}
        events={[
          {
            ...safeRunEvent,
            event_id: "event-document-search",
            action: "scoped_retrieval_completed",
            evidence_ids: ["evidence-alpha", "evidence-beta"],
          },
          {
            ...safeRunEvent,
            event_id: "event-interview-search",
            action: "scoped_retrieval_completed",
            topic_id: "topic-beta",
            source_ids: ["interview-alpha"],
            evidence_ids: ["evidence-alpha"],
          },
        ]}
        onOpenEvidence={vi.fn()}
      />,
    );

    expect(screen.getAllByText("Technical details")).toHaveLength(1);
    expect(screen.getAllByText("Ownership and handoffs")).toHaveLength(2);
    expect(screen.getAllByText("Alex Morgan interview analysis")).toHaveLength(2);
    expect(screen.getAllByText("Source search completed")).toHaveLength(2);
    expect(screen.getByText("1 source · 2 relevant excerpts")).toBeInTheDocument();
    expect(screen.getByText("1 source · 1 relevant excerpt")).toBeInTheDocument();
    expect(screen.queryByText(/topic-researcher/u)).not.toBeInTheDocument();
  });

  it("renders bounded fallbacks for coordinator events without topic or evidence facts", () => {
    render(
      <ExecutionTimeline
        events={[
          {
            ...safeRunEvent,
            event_id: "event-unknown",
            actor: "custom_coordinator",
            action: "custom_action",
            to_status: null,
            topic_id: null,
            source_ids: [],
            evidence_ids: [],
            artifact_name: null,
          },
          {
            ...safeRunEvent,
            event_id: "event-plan",
            actor: "insight_orchestrator",
            action: "research_plan_saved",
            topic_id: "topic-unmapped",
            source_ids: [],
            evidence_ids: [],
            artifact_name: "research_plan.md",
          },
          {
            ...safeRunEvent,
            event_id: "event-findings",
            topic_id: null,
            source_ids: ["source-alpha"],
            evidence_ids: ["evidence-alpha"],
            artifact_name: "researcher/topic-alpha/findings.md",
          },
        ]}
      />,
    );

    expect(screen.getByText("custom coordinator")).toBeVisible();
    expect(screen.getByText("custom action")).toBeVisible();
    expect(screen.getByText("Research topic 1")).toBeVisible();
    expect(screen.getByText("Research plan")).toBeVisible();
    expect(screen.getByText("Research findings")).toBeVisible();
    expect(screen.getByText("1 source · 1 relevant excerpt")).toBeVisible();
  });
});

describe("EvidenceDrawer", () => {
  it("renders a focused source drawer, supports navigation, and only offers approved downloads", async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    const onSelectIndex = vi.fn();
    const value = structuredClone(evidenceResponse);
    value.evidence.english_interpretation = "Operations owns the weekly service review.";
    render(
      <EvidenceDrawer
        selection={{
          title: "Sources supporting this risk",
          items: [
            {
              evidenceId: "evidence-alpha",
              sourceLabel: "Stakeholder interview",
              locationLabel: "Interview transcript, turns 0–2",
            },
            {
              evidenceId: "evidence-beta",
              sourceLabel: "Operating model",
              locationLabel: "operating-model.pdf, page 4",
            },
          ],
        }}
        selectedIndex={1}
        value={value}
        loading={false}
        errorMessage={null}
        api={fakePmApi()}
        engagementId={engagement.engagement_id}
        runId={queuedRun.run_id}
        onSelectIndex={onSelectIndex}
        onClose={onClose}
      />,
    );

    expect(screen.getByRole("dialog", { name: "Sources supporting this risk" })).toBeVisible();
    expect(screen.getByText("Operating model")).toBeVisible();
    expect(screen.getByText("Excerpt 2 of 2")).toBeVisible();
    expect(screen.getByText("Original excerpt")).toBeVisible();
    expect(screen.getByText("English interpretation")).toBeVisible();
    const downloads = screen.getAllByRole("link", { name: "Download source file" });
    expect(downloads).toHaveLength(2);
    expect(downloads[0]).toHaveAttribute(
      "href",
      "/api/v1/pm/engagements/engagement-alpha/insights/run-alpha/evidence/evidence-beta/artifacts/artifact-original",
    );
    expect(screen.getByText(/cannot be downloaded/u)).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Previous excerpt" }));
    expect(onSelectIndex).toHaveBeenCalledWith(0);
    await user.click(screen.getByRole("button", { name: "Close" }));
    expect(onClose).toHaveBeenCalledOnce();
    expect(
      (
        await axe.run(document.body, {
          rules: { "color-contrast": { enabled: false } },
        })
      ).violations,
    ).toEqual([]);
  });
});
